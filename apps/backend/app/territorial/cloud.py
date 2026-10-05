"""Administration of the deployed AIDP workflow, using the existing operator bootstrap."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import time
import threading
from datetime import datetime
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import HTTPException

from ..autonomous import AutonomousGovernanceClient
from .core import SYNTHETIC_MODES, canonical_mode, PLATFORMS, utc_text, default_source, simulation_state, source_migration, aidp_credential_name, validate_review, review_location
from . import capture, landing, sensor_capture, sensors, sensor_reset
from .database import read_document, read_documents, mutate_document, upsert_posts, query_posts
from .source_rules import check_revision, source_view, validate_rules
from .scheduling import needs_schedule, set_schedule, submit_run, keep_streams_running


class CloudRuntime:
    def __init__(self, settings, aidp_factory):
        self.settings, self.aidp_factory = settings, aidp_factory
        self.database = AutonomousGovernanceClient(settings.autonomous_runtime_file)
        self.capture_lock = threading.RLock()
        self._snapshot_lock = threading.Lock()
        self._snapshot_cache = (None, None)

    def _connect(self):
        return self.database._connect(self.database._runtime())

    async def _io(self, function, *args):
        try:
            return await asyncio.to_thread(function, *args)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "The Territorial Control AIDP/Autonomous runtime is not ready; check deployment") from exc

    def _doc(self, name):
        with self._connect() as connection:
            return read_document(connection, name)

    def _documents(self, names):
        # Reuse the authenticated connection for this response; never cache live controls.
        with self._connect() as connection:
            return read_documents(connection, names)

    def _change(self, name, change):
        with self._connect() as connection:
            return mutate_document(connection, name, change)

    def _sources(self):
        documents = self._documents(("configuration", *("status_" + name for name in PLATFORMS),
            "simulation", "status_pipeline", "status_synthetic", "checkpoint_reset"))
        configuration = documents["configuration"]
        def migrate(document):
            sources = dict(document.get("sources", {}))
            for platform in PLATFORMS:
                if platform in sources:
                    changes = source_migration(platform, sources[platform])
                    if "query" in changes:
                        changes["query_version"] = str(uuid4())
                    sources[platform] = {**sources[platform], **changes}
            return {**document, "sources": sources}
        if any(source_migration(platform, source) for platform, source in configuration.get("sources", {}).items() if platform in PLATFORMS):
            configuration = self._change("configuration", migrate)
        sources = []
        for platform in PLATFORMS:
            source = {**default_source(platform), **configuration.get("sources", {}).get(platform, {})}
            status = documents["status_" + platform]
            source.update({key: value for key, value in status.items() if key in
                {"status", "last_run_at", "next_due", "last_error", "requested_action", "request_id", "last_received_count", "capture_slot"}})
            if not source["enabled"]:
                source.update(status="disabled", next_due=None)
            elif source["mode"] == "real" and status.get("configuration_revision") != configuration.get("revision", 0) and not status.get("requested_action"):
                source.update(status="ready", last_error=None, next_due=None)
            sources.append(source_view(source))
        return {"sources": sources, "simulation": simulation_state(documents["simulation"], time.time()), "runtime": "aidp",
                "social_schedule": capture.schedule(configuration.get("social_schedule")),
                "pipeline": documents["status_pipeline"], "capture_summary": documents["status_synthetic"],
                "synthetic_reset": self._social_reset_status(documents["checkpoint_reset"])}

    async def sources(self):
        return await self._io(self._sources)

    async def save_capture_schedule(self, kind, values):
        def save():
            with self.capture_lock:
                def change(doc):
                    return {**doc, kind + "_schedule": capture.update_schedule(doc.get(kind + "_schedule"), values)}
                try:
                    return self._change("configuration", change)[kind + "_schedule"]
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
        return await self._io(save)

    def _capture_status(self, kind):
        key = "sensor" if kind == "sensors" else "social"
        now, configuration = time.time(), self._doc("configuration")
        shared = capture.schedule(configuration.get(key + "_schedule"))
        runtime = self._doc("runtime")
        client = self.aidp_factory()
        try:
            response = client.object_storage.get_object(self.settings.objectstorage_namespace,
                runtime.get("bucket") or self.settings.bucket_name, "04_gold/prisma/current.json")
            published = json.loads(response.data.content)
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                raise
            published = {}
        if key == "sensor":
            checkpoint = self._doc("checkpoint_sensors")
            state = sensor_capture.cloud_configuration(self)
            due = state.get("next_due")
            pending = bool(checkpoint.get("pending") or any(row.get("pending") for row in checkpoint.get("by_type", {}).values()))
            captured_at = state.get("last_run_at")
        else:
            checkpoint = self._doc("checkpoint_synthetic")
            candidates, captured_at = [], self._doc("status_synthetic").get("last_data_at")
            for name in PLATFORMS:
                source = {**default_source(name), **configuration.get("sources", {}).get(name, {})}
                if not source["enabled"] or not source.get("capture_running"):
                    continue
                status = self._doc("status_" + name)
                cursor = checkpoint.get("sources", {}).get(name, {})
                previous = status.get("capture_slot") if source["mode"] == "real" else cursor.get("capture_slot")
                slot = capture.schedule_at(shared, now, previous)
                if source["mode"] == "real":
                    checkpoint[name] = self._doc("checkpoint_" + name)
                    retry = checkpoint[name].get("retry_at", 0) or 0
                    if status.get("status") == "rate_limited" and status.get("next_due"):
                        retry = max(retry, datetime.fromisoformat(status["next_due"].replace("Z", "+00:00")).timestamp())
                    if retry > now:
                        slot = max(slot or 0, retry)
                if slot is not None:
                    candidates.append(utc_text(slot))
                elif status.get("next_due"):
                    candidates.append(status["next_due"])
            due = min(candidates, default=None)
            pipeline = self._doc("status_pipeline")
            pending = bool(checkpoint.get("pending") or pipeline.get("pending_count") or pipeline.get("last_error"))
        pending = pending or bool(captured_at and captured_at > (published.get("published_at") or ""))
        return {"server_now": utc_text(now), "schedule": shared, key + "_schedule": shared, "next_capture_at": due,
            "capture_revision": hashlib.sha256(json.dumps(checkpoint, sort_keys=True).encode()).hexdigest(),
            "publication_version": published.get("version"), "publication_revision": published.get(key + "_revision"),
            "processing_pending": bool(pending)}

    async def capture_status(self, kind):
        return await self._io(self._capture_status, kind)

    def _credential(self, platform, token, reference=None):
        client = self.aidp_factory()
        reference = reference or default_source(platform)["secret_ref"]
        name = aidp_credential_name(platform, reference)
        existing = [item for item in client._list("/credentials", params={"displayName": name}, phase="control")
                    if (item.get("displayName") or item.get("name")) == name]
        if len(existing) > 1:
            raise HTTPException(409, "Duplicate source credentials exist")
        payload = {"displayName": name, "type": "SECRET_TOKEN", "credentialDescription": "God's Eye View API source credential",
                   "credentialDetails": {"credentialType": "SECRET_TOKEN", "secretTokenPair": [{"secretKey": "bearer_token", "secretValue": token}]}}
        if existing:
            key = str(existing[0].get("key") or existing[0].get("id"))
            client._request("PUT", "/credentials/" + quote(key, safe=""), payload=payload, phase="control")
        else:
            client._request("POST", "/credentials", payload=payload, phase="control")
        return reference

    def _update(self, platform, payload):
        if platform not in PLATFORMS:
            raise HTTPException(404, "Unknown platform")
        fields = {name: value for name, value in payload.items() if name in {"enabled", "mode", "query", "interval_minutes", "synthetic_batch_max", "correlation_window_minutes", "report_thresholds"}}
        old = {**default_source(platform), **self._doc("configuration").get("sources", {}).get(platform, {})}
        check_revision(old, payload.get("expected_revision"))
        fields = {**source_migration(platform, old), **fields}
        old["mode"] = canonical_mode(old["mode"])
        if "mode" in fields:
            fields["mode"] = canonical_mode(fields["mode"])
        candidate = {**old, **fields}
        if not candidate["enabled"] or candidate["mode"] != old["mode"]:
            fields.update(capture_running=False, capture_paused=False)
        try:
            capture.validate_source(candidate)
            validate_rules(candidate)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if payload.get("secret_ref", old["secret_ref"]) not in {old["secret_ref"], default_source(platform)["secret_ref"]}:
            raise HTTPException(422, "AIDP manages this source's credential reference")
        token = payload.get("bearer_token")
        if token:
            if len(token) > 8192 or any(character.isspace() for character in token):
                raise HTTPException(422, "Invalid credential value")
            fields.update(secret_ref=self._credential(platform, token, candidate["secret_ref"]), credential_configured=True)
        def change(document):
            sources = dict(document.get("sources", {}))
            old = {**default_source(platform), **sources.get(platform, {})}
            check_revision(old, payload.get("expected_revision"))
            if fields.get("query", old["query"]) != old["query"] or fields.get("mode", old["mode"]) != old["mode"]:
                fields["query_version"] = str(uuid4())
            sources[platform] = {**old, **fields, "config_version": old.get("config_version", 1) + 1}
            return {**document, "sources": sources}
        document = self._change("configuration", change)
        if fields.get("capture_running") is False:
            self._change("status_" + platform, lambda doc: {**doc, "requested_action": None,
                "next_due": None, "last_error": None, "status": "disabled" if not candidate["enabled"] else "ready"})
        return source_view(document["sources"][platform])

    async def update_source(self, platform, payload):
        def save():
            with self.capture_lock:
                self._guard_reset()
                previous = self._doc("configuration").get("sources", {}).get(platform, {})
                result = self._update(platform, payload)
                stopped = previous.get("enabled", True) and previous.get("capture_running") and not result.get("capture_running")
                # ponytail: a failed drain retains CSVs; the next explicit Run retries ingestion, not an idle Save.
                self._wake(str(uuid4()), only_if_active=not stopped)
                return result
        return await self._io(save)

    def _wake(self, request_id, only_if_active=False):
        runtime = self._doc("runtime")
        client = self.aidp_factory()
        if runtime.get("streaming_mode") == "persistent":
            keep_streams_running(client._request, runtime, request_id)
            return
        pipeline = self._doc("status_pipeline")
        active = needs_schedule(self._doc("configuration"), self._doc("simulation")) or (pipeline.get("pending_count", 0) > 0 and not pipeline.get("needs_attention"))
        persistent = set_schedule(client._request, runtime, active)
        # Explicit Run/Test and publication controls still enqueue a finite run while the schedule is paused.
        if active or not only_if_active:
            submit_run(client._request, runtime, request_id, persistent=persistent)

    def _request_source(self, platform, action):
        with self.capture_lock:
            return self._request_source_locked(platform, action)

    def _request_source_locked(self, platform, action):
        self._guard_reset()
        if platform not in PLATFORMS:
            raise HTTPException(404, "Unknown platform")
        source = next(item for item in self._sources()["sources"] if item["platform"] == platform)
        if source["mode"] == "real" and (platform != "x" or not source["credential_configured"]):
            raise HTTPException(409, "Real capture requires a validated connector and credential")
        if action == "run":
            source = self._start_capture(source)
            self._change("checkpoint_enrichment", lambda doc: {**doc, "attempts": 0, "retry_at": 0,
                "last_error": None, "circuit_open": False})
            if source["mode"] in SYNTHETIC_MODES and source["status"] == "completed" and not source.get("capture_running"):
                self._wake(str(uuid4()))
                return {"status": "completed", "message": "Synthetic capture completed; pending processing retried", "source": source}
        if source["mode"] in SYNTHETIC_MODES:
            for query in capture.query_lines(source["query"]):
                capture.search_terms(query)
            if action == "run":
                self._produce(force=True, platform=platform)
                self._wake(str(uuid4()))
            source = next(item for item in self._sources()["sources"] if item["platform"] == platform)
            if action == "run" and source["status"] == "completed":
                return {"status": "completed", "message": "Synthetic capture completed", "source": source}
            return {"status": "simulation", "message": "Synthetic query validated on the VM producer", "source": source}
        request_id = str(uuid4())
        self._change("status_" + platform, lambda doc: {**doc, "status": "queued", "requested_action": action,
            "request_id": request_id, "next_due": utc_text(time.time()), "last_error": None})
        self._wake(request_id)
        return {"status": "queued", "message": "Request queued as a finite AIDP run", "source": {**source, "status": "queued"}}

    def _start_capture(self, source):
        with self.capture_lock:
            if source["enabled"] and source.get("capture_running"):
                return source
            configured = self._doc("configuration").get("sources", {})
            institutional = not any(item.get("capture_running") and item.get("enabled") and item.get("mode") in SYNTHETIC_MODES for item in configured.values())
            controls = self._doc("checkpoint_controls")
            saved = controls.get(source["platform"], {})
            cursors = self._doc("checkpoint_synthetic").get("sources", {})
            completed = source["mode"] in SYNTHETIC_MODES and capture.is_complete(saved, cursors.get(source["platform"], {}))
            pending = completed and capture.institutional_pending(saved, controls.get("institutional", {}), cursors)
            if completed and not pending:
                if source["status"] != "completed":
                    self._complete_capture(source["platform"], saved)
                return {**source, "capture_running": False, "capture_paused": True, "status": "completed", "next_due": None, "last_error": None}
            control = saved if source.get("capture_paused") or pending else None
            anchor = capture.schedule_at(self._doc("configuration").get("social_schedule"), time.time())
            control = control or {"run_id": str(uuid4()), "anchor_at": anchor if anchor is not None else time.time(), "seed": 0}
            self._change("checkpoint_controls", lambda doc: {**doc, source["platform"]: control,
                **({"institutional": control} if institutional else {})})
            self._change("configuration", lambda doc: {**doc, "sources": {**doc.get("sources", {}),
                source["platform"]: {**doc.get("sources", {}).get(source["platform"], source), "enabled": True, "capture_running": True, "capture_paused": False}}})
            return {**source, "enabled": True, "capture_running": True, "capture_paused": False}

    def _complete_capture(self, platform, control):
        # Persist the final drain before stopping capture; a failed native trigger is retried by tick.
        self._change("status_synthetic", lambda doc: {**doc, "final_job_pending": True,
            "final_request_id": control["run_id"] + "-" + platform + "-complete"})
        self._change("status_" + platform, lambda doc: {**doc, "status": "completed", "next_due": None,
            "last_error": None, "requested_action": None})
        self._change("configuration", lambda doc: {**doc, "sources": {**doc.get("sources", {}),
            platform: {**default_source(platform), **doc.get("sources", {}).get(platform, {}),
                "capture_running": False, "capture_paused": True}}})

    async def test_source(self, platform):
        return await self._io(self._request_source, platform, "test")

    async def run_source(self, platform):
        return await self._io(self._request_source, platform, "run")

    async def pause_source(self, platform):
        def pause():
            with self.capture_lock:
                self._guard_reset()
                self._change("configuration", lambda doc: {**doc, "sources": {**doc.get("sources", {}),
                    platform: {**default_source(platform), **doc.get("sources", {}).get(platform, {}),
                               "capture_running": False, "capture_paused": True}}})
                self._change("status_" + platform, lambda doc: {**doc, "status": "paused", "requested_action": None,
                    "last_error": None, "next_due": None})
                self._wake(str(uuid4()))
                source = next(item for item in self._sources()["sources"] if item["platform"] == platform)
                return {"status": "paused", "message": "Capture paused; publications and checkpoints retained", "source": source}
        return await self._io(pause)

    async def posts(self, platform, limit, before_seq=None, max_seq=None):
        def read():
            with self._connect() as connection:
                return query_posts(connection, platform, limit, before_seq, max_seq)
        return await self._io(read)

    async def ordered_posts(self, platform, limit, position=None, maximum=None, sort="published_at", order="desc"):
        from .database import query_ordered_posts
        def read():
            with self._connect() as connection:
                return query_ordered_posts(connection, platform, limit, position, maximum, sort, order)
        return await self._io(read)

    def _guard_reset(self):
        if self._social_reset_status().get("status") in {"pending", "error"}:
            raise HTTPException(409, "Finish or retry the Synthetic reset before changing sources or reviews")

    async def synthetic_reset_status(self):
        return await self._io(self._social_reset_status)

    def _social_reset_status(self, state=None):
        state = self._doc("checkpoint_reset") if state is None else state
        return {} if state.get("sensor_type") else state

    def _prepare_reset(self):
        def stop(document):
            sources = dict(document.get("sources", {}))
            for platform in PLATFORMS:
                source = {**default_source(platform), **sources.get(platform, {})}
                if source["mode"] in SYNTHETIC_MODES:
                    sources[platform] = {**source, "mode": "Synthetic", "capture_running": False, "capture_paused": False,
                                         "config_version": source.get("config_version", 1) + 1}
            return {**document, "sources": sources}
        configuration = self._change("configuration", stop)
        for platform, source in configuration["sources"].items():
            if source["mode"] in SYNTHETIC_MODES:
                self._change("status_" + platform, lambda doc: {**doc, "status": "paused", "requested_action": None,
                    "last_error": None, "next_due": None, "last_run_at": None, "last_received_count": None})
        self._change("simulation", lambda doc: {**doc, "status": "paused", "elapsed_seconds": 0,
            "started_at": None, "anchor_at": time.time(), "run_id": str(uuid4()), "capture_complete": True,
            "final_job_pending": False})

    def _reset_command(self, operation_id):
        current = self._doc("checkpoint_reset")
        from .synthetic_reset import check_scope, operation_history
        check_scope(current, operation_id)
        if operation_id in current.get("completed_ids", []):
            return {"operation_id": operation_id, "status": "completed", "stage": "completed"}
        if current.get("operation_id") == operation_id and current.get("status") == "completed":
            return current
        if current.get("status") in {"pending", "error"} and current.get("operation_id") != operation_id:
            raise HTTPException(409, "A Synthetic reset is already pending; retry that operation")
        if self._doc("runtime").get("synthetic_reset_version") != 2:
            raise HTTPException(501, "Update the AIDP workflow before resetting Synthetic data")
        if current.get("operation_id") != operation_id:
            current = self._change("checkpoint_reset", lambda doc: {**operation_history(doc), "operation_id": operation_id,
                "status": "pending", "stage": "preparing", "ready": False, "counts": {}})
        return current

    def _reset_synthetic(self, operation_id):
        operation_id = str(UUID(operation_id))
        # ponytail: one VM producer owns this lock; multiple replicas require a durable writer lease before scaling.
        with self.capture_lock:
            current = self._reset_command(operation_id)
            if current["status"] == "completed":
                return current
            try:
                if not current.get("ready"):
                    self._prepare_reset()
                    self._change("checkpoint_reset", lambda doc: {**doc, "ready": True, "status": "pending", "stage": "waiting_for_aidp", "error": None})
                elif current.get("status") == "error":
                    self._change("checkpoint_reset", lambda doc: {**doc, "status": "pending", "error": None}
                        if doc.get("status") == "error" else doc)
                self._wake(operation_id)
            except Exception:
                # A timed-out submission may already be running; retain its identity and never claim deletion succeeded.
                self._change("checkpoint_reset", lambda doc: {**doc, "status": "error",
                    "error": "Synthetic reset could not be scheduled. Retry this operation."}
                    if doc.get("operation_id") == operation_id and doc.get("status") not in {"completed", "cancelled"} else doc)
            return self._doc("checkpoint_reset")

    async def reset_synthetic(self, operation_id):
        return await self._io(self._reset_synthetic, operation_id)

    def _simulation_state(self):
        return simulation_state(self._doc("simulation"), time.time())

    def _simulation(self, action):
        with self.capture_lock:
            self._guard_reset()
            return self._simulation_locked(action)

    def _simulation_locked(self, action):
        if action not in {"start", "pause", "resume", "reset", "replay"}:
            raise HTTPException(422, "Invalid simulation action")
        state = self._simulation_state()
        if action in {"start", "reset", "replay"}:
            state = {"elapsed_seconds": 0, "run_id": str(uuid4()), "anchor_at": time.time(), "capture_complete": False,
                     "final_job_pending": False}
        elif state.get("anchor_at") is None:
            state["anchor_at"] = time.time() - state["elapsed_seconds"]
        state.update(status="paused" if action in {"pause", "reset"} else "running", started_at=time.time())
        self._change("simulation", lambda current: {**current, **state})
        self._produce(force=True)
        self._wake(str(uuid4()))
        return self._simulation_state()

    def _project_posts(self, events, now, key):
        with self._connect() as connection:
            upsert_posts(connection, events, "captured", ingested_at=utc_text(now), batch_key=key)

    def _produce(self, force=False, platform=None):
        with self.capture_lock:
            if self._social_reset_status().get("status") in {"pending", "error"}:
                return False
            now, control = time.time(), self._doc("simulation")
            state = simulation_state(control, now)
            config = self._doc("configuration")
            sources = [{**default_source(name), **config.get("sources", {}).get(name, {})} for name in PLATFORMS]
            runtime, client = self._doc("runtime"), self.aidp_factory()
            finished = []
            for source, source_control, continuous in capture.inputs(sources, self._doc("checkpoint_controls"), state):
                name = source["platform"]
                if platform and name in PLATFORMS and name != platform:
                    continue
                checkpoint = self._doc("checkpoint_synthetic")
                saved = checkpoint.get("sources", {}).get(name, {})
                pending = checkpoint.get("pending", {}).get(name)
                if not pending and continuous and capture.is_complete(source_control, saved):
                    if name in PLATFORMS:
                        finished.append((name, source_control))
                    continue
                result = pending or (capture.continuous_batch if continuous else capture.batch)(source, source_control, saved, now, force,
                    schedule=config.get("social_schedule"))
                if result is None:
                    continue
                events, cursor = result
                try:
                    if not pending:
                        self._change("checkpoint_synthetic", lambda doc: {**doc, "pending": {**doc.get("pending", {}), name: result}})
                    if name in PLATFORMS:
                        self._change("status_" + name, lambda doc: {**doc, "status": "capturing", "last_error": None})
                    key = landing.write_objects(client.object_storage, runtime, events, cursor["batch_key"])
                    self._project_posts(events, now, key)
                    self._change("checkpoint_synthetic", lambda doc: {**doc, "sources": {**doc.get("sources", {}), name: cursor},
                        "pending": {key: value for key, value in doc.get("pending", {}).items() if key != name}})
                    self._change("status_synthetic", lambda doc: {**doc, "status": "ready", "last_error": None,
                        "last_run_at": utc_text(now), "landing_count": doc.get("landing_count", 0) + bool(key),
                        "last_data_at": utc_text(now) if events else doc.get("last_data_at"),
                        "last_landing_key": key or doc.get("last_landing_key")})
                    if name in PLATFORMS:
                        self._change("status_" + name, lambda doc: {**doc, "status": "simulation", "last_error": None,
                            "last_run_at": utc_text(now), "next_due": utc_text(cursor["next_due"]) if cursor["next_due"] is not None else None,
                            "last_received_count": len(events), "configuration_revision": config.get("revision", 0)})
                        if continuous and capture.is_complete(source_control, cursor):
                            finished.append((name, source_control))
                except Exception as exc:
                    self._change("status_synthetic", lambda doc: {**doc, "status": "error", "last_error": type(exc).__name__})
                    if name in PLATFORMS:
                        self._change("status_" + name, lambda doc: {**doc, "status": "error", "last_error": type(exc).__name__})
                    raise
            for name, source_control in finished:
                if capture.institutional_pending(source_control, self._doc("checkpoint_controls").get("institutional", {}),
                        self._doc("checkpoint_synthetic").get("sources", {})):
                    continue
                self._complete_capture(name, source_control)
            cursors = self._doc("checkpoint_synthetic").get("sources", {})
            completed = (state["elapsed_seconds"] >= 600 and not state["capture_complete"]
                and all(capture.is_complete(state, cursors.get(source["platform"], {})) for source in capture.sources(sources)))
            if completed:
                self._change("simulation", lambda doc: {**doc, "capture_complete": True, "final_job_pending": True}
                             if doc.get("run_id") == state["run_id"] else doc)
            return completed or control.get("final_job_pending", False)

    async def tick(self):
        def keep_alive():
            runtime = self._doc("runtime")
            if runtime.get("streaming_mode") == "persistent":
                keep_streams_running(self.aidp_factory()._request, runtime, "keepalive-" + str(int(time.time() // 60)))
        def sensor_tick():
            with self.capture_lock:
                return sensor_capture.cloud_tick(self)
        results = await asyncio.gather(self._io(keep_alive), self._io(sensor_tick), self._io(self._produce), return_exceptions=True)
        if results[2] is True:
            run_id = (await self._io(self._doc, "simulation")).get("run_id")
            await self._io(self._wake, str(run_id) + "-final")
            await self._io(self._change, "simulation", lambda doc: {**doc, "final_job_pending": False}
                           if doc.get("run_id") == run_id else doc)
        pending = await self._io(self._doc, "status_synthetic")
        if pending.get("final_job_pending"):
            request_id = pending["final_request_id"]
            await self._io(self._wake, request_id)
            await self._io(self._change, "status_synthetic", lambda doc: {**doc, "final_job_pending": False}
                if doc.get("final_request_id") == request_id else doc)
        for result in results:
            if isinstance(result, Exception):
                raise result

    async def sensors(self):
        documents = await self._io(self._documents, ("configuration", "status_sensors", "checkpoint_sensors", "checkpoint_reset"))
        config = sensor_capture.cloud_configuration(self, documents)
        config = sensor_reset.annotated(config, documents["checkpoint_reset"])
        return {"config": config, "configs": config["configs"], "reset": config["reset"], "sensor_schedule": config["sensor_schedule"], "runtime": "aidp"}

    async def update_sensors(self, values, sensor_type=None):
        values = dict(values)
        if values.pop("mode", "Synthetic") != "Synthetic":
            raise HTTPException(422, "Only Synthetic sensor capture is available")
        def save():
            with self.capture_lock:
                try:
                    sensor_reset.guard(self._doc("checkpoint_reset"), sensor_type)
                    return sensor_capture.cloud_save(self, values, sensor_type)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
        return {"config": await self._io(save), "runtime": "aidp"}

    async def control_sensors(self, running, sensor_type=None):
        def control():
            with self.capture_lock:
                sensor_reset.guard(self._doc("checkpoint_reset"), sensor_type)
                if running and not self._doc("runtime").get("sensor_job_key"):
                    raise HTTPException(409, "Install the independent Sensors streaming workflow before starting capture")
                try:
                    return sensor_capture.cloud_control(self, running, sensor_type)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
        return {"config": await self._io(control), "message": "Simulated sensor capture started" if running else "Sensor capture paused"}

    async def sensor_reset_status(self, sensor_type):
        return sensor_reset.state_for(await self._io(self._doc, "checkpoint_reset"), sensor_type)

    async def reset_sensors(self, sensor_type, operation_id):
        def reset():
            with self.capture_lock:
                return sensor_reset.cloud(self, sensor_type, operation_id)
        return await self._io(reset)

    async def sensor_location(self, sensor_id, values):
        def save():
            with self.capture_lock:
                self._guard_reset()
                sensor = next((row for row in self._snapshot().get("sensors", []) if row["sensor_id"] == sensor_id), None)
                if sensor is None:
                    raise HTTPException(404, "Unknown sensor")
                def change(doc):
                    try:
                        locations = sensors.update_location(sensor, doc.get("sensor_locations", {}), values, time.time())
                    except ValueError as exc:
                        raise HTTPException(422, str(exc)) from exc
                    return {**doc, "sensor_locations": locations}
                saved = self._change("reviews", change)["sensor_locations"][sensor_id]
                result = {"sensor_id": sensor_id, **saved, "location_saved": True, "location_pending_publication": True}
                try:
                    self._wake(str(uuid4()))
                except Exception:
                    result["publication_error"] = "Sensor location saved, but publication could not be started. Try again."
                return result
        return await self._io(save)

    async def simulation(self, action):
        return await self._io(self._simulation, action)

    def _snapshot(self):
        client = self.aidp_factory()
        bucket = self._doc("runtime").get("bucket") or self.settings.bucket_name
        def fetch(key):
            response = client.object_storage.get_object(self.settings.objectstorage_namespace, bucket, key)
            return json.loads(response.data.content)
        pointer = fetch("04_gold/prisma/current.json")
        key = str(pointer.get("snapshot_key", ""))
        if not key.startswith("04_gold/prisma/snapshots/") or ".." in key:
            raise HTTPException(503, "Invalid Territorial Control publication")
        identity = (self.settings.objectstorage_namespace, bucket, key, pointer.get("version"))
        # ponytail: retain only one immutable publication; mutable controls and the pointer are always read afresh.
        with self._snapshot_lock:
            if self._snapshot_cache[0] != identity:
                snapshot = fetch(key)
                if snapshot.get("version") != pointer.get("version"):
                    raise HTTPException(503, "Incomplete Territorial Control publication")
                self._snapshot_cache = (identity, snapshot)
            return {**deepcopy(self._snapshot_cache[1]), "runtime": "aidp"}

    async def snapshot(self):
        return await self._io(self._snapshot)

    def _review(self, incident_id, status, note, expected_evidence_ids=None, lat=None, lon=None):
        with self.capture_lock:
            self._guard_reset()
            return self._review_locked(incident_id, status, note, expected_evidence_ids, lat, lon)

    def _review_locked(self, incident_id, status, note, expected_evidence_ids=None, lat=None, lon=None):
        snapshot = self._snapshot()
        incident = next((item for item in snapshot.get("incidents", []) if item["id"] == incident_id), None)
        if incident is None:
            raise HTTPException(404, "Unknown incident")
        if expected_evidence_ids is not None and set(expected_evidence_ids) != set(incident["evidence_ids"]):
            raise HTTPException(409, "The event evidence changed. Review the current evidence before saving again.")
        def change(doc):
            previous = doc.get("items", {}).get(incident_id, {})
            try:
                position = review_location(incident, previous, lat, lon)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return {**doc, "items": {**doc.get("items", {}), incident_id: {**previous, **position,
                "status": status, "note": note, "updated_at": utc_text(time.time()), "evidence_ids": incident["evidence_ids"]}}}
        saved = self._change("reviews", change)["items"][incident_id]
        result = {**incident, **review_location(incident, saved), "review_status": status, "review_note": note,
                  "reviewed_evidence_ids": incident["evidence_ids"], "review_saved": True, "review_pending_publication": True}
        try:
            self._wake(str(uuid4()))
        except Exception:
            result["publication_error"] = "Review saved, but publication could not be started. Try again."
        return result

    async def review(self, incident_id, status, note, expected_evidence_ids=None, lat=None, lon=None):
        validate_review(status, note, lat, lon)
        if expected_evidence_ids is None and lat is None:
            return await self._io(self._review, incident_id, status, note)
        return await self._io(self._review, incident_id, status, note, expected_evidence_ids, lat, lon)

    def _chat(self, payload, cookie, key):
        from .agent_gateway import invoke
        client = self.aidp_factory()
        bucket = self._doc("runtime").get("bucket") or self.settings.bucket_name
        response = client.object_storage.get_object(self.settings.objectstorage_namespace,
            bucket, ".control/prisma/agent.json")
        metadata = json.loads(response.data.content)
        if metadata.get("state") != "ACTIVE":
            raise HTTPException(503, "The Territorial Control agent is not active")
        return invoke(client, metadata["endpoint"], payload, cookie, key, self._snapshot())

    async def chat(self, payload, cookie, key):
        return await self._io(self._chat, payload, cookie, key)
