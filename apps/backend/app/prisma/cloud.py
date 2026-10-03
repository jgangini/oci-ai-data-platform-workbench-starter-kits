"""Administration of the deployed AIDP workflow, using the existing operator bootstrap."""
from __future__ import annotations

import asyncio
import json
import time
import threading
from urllib.parse import quote
from uuid import uuid4

from fastapi import HTTPException

from ..autonomous import AutonomousGovernanceClient
from .core import PLATFORMS, utc_text, default_source, simulation_state, source_migration, aidp_credential_name
from . import capture, landing
from .database import read_document, mutate_document
from .scheduling import needs_schedule, set_schedule, submit_run


class CloudRuntime:
    def __init__(self, settings, aidp_factory):
        self.settings, self.aidp_factory = settings, aidp_factory
        self.database = AutonomousGovernanceClient(settings.autonomous_runtime_file)
        self.capture_lock = threading.RLock()

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

    def _change(self, name, change):
        with self._connect() as connection:
            return mutate_document(connection, name, change)

    def _sources(self):
        configuration = self._doc("configuration")
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
            status = self._doc("status_" + platform)
            source.update({key: value for key, value in status.items() if key in
                {"status", "last_run_at", "next_due", "last_error", "requested_action", "request_id", "last_received_count"}})
            if not source["enabled"]:
                source.update(status="disabled", next_due=None)
            elif source["mode"] == "real" and status.get("configuration_revision") != configuration.get("revision", 0) and not status.get("requested_action"):
                source.update(status="ready", last_error=None, next_due=None)
            sources.append(source)
        return {"sources": sources, "simulation": self._simulation_state(), "runtime": "aidp",
                "pipeline": self._doc("status_pipeline"), "capture_summary": self._doc("status_synthetic")}

    async def sources(self):
        return await self._io(self._sources)

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
        fields = {name: value for name, value in payload.items() if name in {"enabled", "mode", "query", "interval_minutes"}}
        old = {**default_source(platform), **self._doc("configuration").get("sources", {}).get(platform, {})}
        fields = {**source_migration(platform, old), **fields}
        candidate = {**old, **fields}
        if not candidate["enabled"] or candidate["mode"] != old["mode"]:
            fields["capture_running"] = False
        try:
            capture.validate_source(candidate)
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
            if fields.get("query", old["query"]) != old["query"] or fields.get("mode", old["mode"]) != old["mode"]:
                fields["query_version"] = str(uuid4())
            sources[platform] = {**old, **fields}
            return {**document, "sources": sources}
        document = self._change("configuration", change)
        if fields.get("capture_running") is False:
            self._change("status_" + platform, lambda doc: {**doc, "requested_action": None,
                "next_due": None, "last_error": None, "status": "disabled" if not candidate["enabled"] else "ready"})
        return document["sources"][platform]

    async def update_source(self, platform, payload):
        result = await self._io(self._update, platform, payload)
        await self._io(self._wake, str(uuid4()))
        return result

    def _wake(self, request_id):
        runtime = self._doc("runtime")
        client = self.aidp_factory()
        # Persisted requests survive a busy job; queue this finite run before changing the recurring schedule.
        submit_run(client._request, runtime, request_id)
        set_schedule(client._request, runtime, needs_schedule(self._doc("configuration"), self._doc("simulation")))

    def _request_source(self, platform, action):
        if platform not in PLATFORMS:
            raise HTTPException(404, "Unknown platform")
        source = next(item for item in self._sources()["sources"] if item["platform"] == platform)
        if not source["enabled"]:
            raise HTTPException(409, "Enable the source before running it")
        if source["mode"] == "real" and (platform != "x" or not source["credential_configured"]):
            raise HTTPException(409, "Real capture requires a validated connector and credential")
        if action == "run":
            source = self._start_capture(source)
        if source["mode"] == "simulation":
            for query in capture.query_lines(source["query"]):
                capture.search_terms(query)
            if action == "run":
                self._produce(force=True, platform=platform)
                self._wake(str(uuid4()))
            source = next(item for item in self._sources()["sources"] if item["platform"] == platform)
            return {"status": "simulation", "message": "Synthetic query validated on the VM producer", "source": source}
        request_id = str(uuid4())
        self._change("status_" + platform, lambda doc: {**doc, "status": "queued", "requested_action": action,
            "request_id": request_id, "next_due": utc_text(time.time()), "last_error": None})
        self._wake(request_id)
        return {"status": "queued", "message": "Request queued as a finite AIDP run", "source": {**source, "status": "queued"}}

    def _start_capture(self, source):
        with self.capture_lock:
            if source.get("capture_running"):
                return source
            configured = self._doc("configuration").get("sources", {})
            institutional = not any(item.get("capture_running") and item.get("enabled") and item.get("mode") == "simulation" for item in configured.values())
            control = {"run_id": str(uuid4()), "anchor_at": time.time()}
            self._change("checkpoint_controls", lambda doc: {**doc, source["platform"]: control,
                **({"institutional": control} if institutional else {})})
            self._change("configuration", lambda doc: {**doc, "sources": {**doc.get("sources", {}),
                source["platform"]: {**source, "capture_running": True}}})
            return {**source, "capture_running": True}

    async def test_source(self, platform):
        return await self._io(self._request_source, platform, "test")

    async def run_source(self, platform):
        return await self._io(self._request_source, platform, "run")

    def _simulation_state(self):
        return simulation_state(self._doc("simulation"), time.time())

    def _simulation(self, action):
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

    def _produce(self, force=False, platform=None):
        with self.capture_lock:
            now, control = time.time(), self._doc("simulation")
            state = simulation_state(control, now)
            config = self._doc("configuration")
            sources = [{**default_source(name), **config.get("sources", {}).get(name, {})} for name in PLATFORMS]
            runtime, client = self._doc("runtime"), self.aidp_factory()
            for source, source_control, continuous in capture.inputs(sources, self._doc("checkpoint_controls"), state):
                name = source["platform"]
                if (platform and name in PLATFORMS and name != platform) or (not continuous and state["capture_complete"] and not force):
                    continue
                saved = self._doc("checkpoint_synthetic").get("sources", {}).get(name, {})
                result = (capture.continuous_batch if continuous else capture.batch)(source, source_control, saved, now, force)
                if result is None:
                    continue
                events, cursor = result
                try:
                    key = landing.write_objects(client.object_storage, runtime, events, cursor["batch_key"])
                    self._change("checkpoint_synthetic", lambda doc: {**doc, "sources": {**doc.get("sources", {}), name: cursor}})
                    self._change("status_synthetic", lambda doc: {**doc, "status": "ready", "last_error": None,
                        "last_run_at": utc_text(now), "landing_count": doc.get("landing_count", 0) + bool(key),
                        "last_landing_key": key or doc.get("last_landing_key")})
                    if name in PLATFORMS:
                        self._change("status_" + name, lambda doc: {**doc, "status": "simulation", "last_error": None,
                            "last_run_at": utc_text(now), "next_due": utc_text(cursor["next_due"]),
                            "last_received_count": len(events), "configuration_revision": config.get("revision", 0)})
                except Exception as exc:
                    self._change("status_synthetic", lambda doc: {**doc, "status": "error", "last_error": type(exc).__name__})
                    raise
            completed = state["elapsed_seconds"] >= 600 and not state["capture_complete"]
            if completed:
                self._change("simulation", lambda doc: {**doc, "capture_complete": True, "final_job_pending": True}
                             if doc.get("run_id") == state["run_id"] else doc)
            return completed or control.get("final_job_pending", False)

    async def tick(self):
        if await self._io(self._produce):
            run_id = (await self._io(self._doc, "simulation")).get("run_id")
            await self._io(self._wake, str(run_id) + "-final")
            await self._io(self._change, "simulation", lambda doc: {**doc, "final_job_pending": False}
                           if doc.get("run_id") == run_id else doc)

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
        snapshot = fetch(key)
        if snapshot.get("version") != pointer.get("version"):
            raise HTTPException(503, "Incomplete Territorial Control publication")
        return {**snapshot, "runtime": "aidp"}

    async def snapshot(self):
        return await self._io(self._snapshot)

    def _review(self, incident_id, status, note):
        snapshot = self._snapshot()
        incident = next((item for item in snapshot.get("incidents", []) if item["id"] == incident_id), None)
        if incident is None:
            raise HTTPException(404, "Unknown incident")
        self._change("reviews", lambda doc: {**doc, "items": {**doc.get("items", {}),
            incident_id: {"status": status, "note": note, "updated_at": utc_text(time.time())}}})
        self._wake(str(uuid4()))
        return {**incident, "review_status": status, "review_pending_publication": True}

    async def review(self, incident_id, status, note):
        return await self._io(self._review, incident_id, status, note)

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
