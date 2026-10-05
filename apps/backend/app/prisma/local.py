"""Local-only runtime. Cloud execution must use the separate AIDP adapter."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from .core import SYNTHETIC_MODES, canonical_mode, default_source, source_migration, utc_text
from .store import PrismaStore
from .x import XFailure, poll_queries
from . import landing, capture
from .capture import validate_source
from .source_rules import check_revision, source_view, validate_rules
from . import sensor_capture, sensor_reset


class LocalCredentials:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)

    def _path(self, reference: str) -> Path:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", reference):
            raise ValueError("Invalid credential reference")
        return self.directory / reference

    def exists(self, reference: str) -> bool:
        return self._path(reference).is_file()

    def put(self, reference: str, token: str) -> None:
        if not token or len(token) > 8192 or any(character.isspace() for character in token):
            raise ValueError("Invalid credential value")
        destination = self._path(reference)
        with NamedTemporaryFile(mode="w", dir=self.directory, delete=False, encoding="utf-8") as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            output.write(token)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, reference: str) -> str:
        try:
            return self._path(reference).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise XFailure("credential_required") from exc


class LocalPrismaRuntime:
    def __init__(self, directory: Path, *, clock=time.time, client_factory=httpx.Client):
        self.store = PrismaStore(directory / "prisma.sqlite3", clock)
        self.credentials = LocalCredentials(directory / "prisma-secrets")
        for source in self.store.sources():
            configured = self.credentials.exists(source["secret_ref"])
            changes = source_migration(source["platform"], {**source, "credential_configured": configured})
            if configured != source["credential_configured"]:
                changes["credential_configured"] = configured
            if changes:
                self.store.update_source(source["platform"], changes)
        self.clock, self.client_factory = clock, client_factory
        self.lock = asyncio.Lock()

    async def sources(self) -> dict:
        async with self.lock:
            snapshot = self.store.snapshot()
            return {"sources": [source_view(source) for source in self.store.sources()], "simulation": self.store.simulation_state(), "runtime": "local_fixture",
                    "social_schedule": self.store.capture_schedule("social"),
                    "synthetic_reset": self.store.synthetic_reset_status(), "capture_summary": self.store.capture_summary(),
                    "pipeline": {"status": "local_fixture", "version": snapshot["version"], "last_run_at": snapshot.get("published_at")}}

    async def save_capture_schedule(self, kind, values):
        async with self.lock:
            return self.store.save_capture_schedule(kind, values)

    async def capture_status(self, kind):
        async with self.lock:
            key = "sensor" if kind == "sensors" else "social"
            shared = self.store.capture_schedule(key)
            now = self.clock()
            with self.store.connection() as db:
                published = self.store._get(db, "snapshot_publication", {})
                if key == "sensor":
                    checkpoint = self.store._get(db, "checkpoint_sensors", {})
                    state = sensor_capture.local_configuration(self.store)
                    due = state.get("next_due")
                    pending = bool(checkpoint.get("pending") or any(row.get("pending") for row in checkpoint.get("by_type", {}).values()))
                else:
                    checkpoint = {name: self.store._get(db, "synthetic:" + name, {}) for name in capture.PLATFORMS}
                    candidates = []
                    for source in self.store.sources():
                        if not source["enabled"] or not source.get("capture_running"):
                            continue
                        cursor = checkpoint[source["platform"]]
                        previous = source.get("capture_slot") if source["mode"] == "real" else cursor.get("capture_slot")
                        slot = capture.schedule_at(shared, now, previous)
                        if source["mode"] == "real":
                            checkpoint[source["platform"]] = self.store.checkpoint(source["platform"])
                            retry = checkpoint[source["platform"]].get("retry_at", 0) or 0
                            if source.get("status") == "rate_limited" and source.get("next_due"):
                                retry = max(retry, datetime.fromisoformat(source["next_due"].replace("Z", "+00:00")).timestamp())
                            if retry > now:
                                slot = max(slot or 0, retry)
                        if slot is not None:
                            candidates.append(utc_text(slot))
                        elif source.get("next_due"):
                            candidates.append(source["next_due"])
                    due = min(candidates, default=None)
                    pending = any(self.store._get(db, "synthetic:pending:" + name, None) for name in capture.PLATFORMS)
                publication = self.store._get(db, "publication", {})
                pending = pending or (publication.get("published_at") or "") > (published.get("published_at") or "")
            return {"server_now": utc_text(now), "schedule": shared, key + "_schedule": shared, "next_capture_at": due,
                "capture_revision": hashlib.sha256(json.dumps(checkpoint, sort_keys=True).encode()).hexdigest(),
                "publication_version": published.get("version"), "publication_revision": published.get(key + "_revision"),
                "processing_pending": bool(pending)}

    async def update_source(self, platform: str, payload: dict) -> dict:
        async with self.lock:
            values = dict(payload)
            if "mode" in values:
                values["mode"] = canonical_mode(values["mode"])
            token = values.pop("bearer_token", None)
            expected = values.pop("expected_revision", None)
            current = self.store.source(platform)
            check_revision(current, expected)
            candidate = {**current, **values}
            if current["mode"] in SYNTHETIC_MODES or candidate["mode"] in SYNTHETIC_MODES:
                self.store.ensure_synthetic_capture()
            validate_source(candidate)
            validate_rules(candidate)
            if token:
                if candidate["secret_ref"] in {f"prisma-{platform}", f"PrismaSource_{platform}"}:
                    candidate["secret_ref"] = values["secret_ref"] = default_source(platform)["secret_ref"]
                self.credentials.put(candidate["secret_ref"], token)
            values["credential_configured"] = self.credentials.exists(candidate["secret_ref"])
            values.update(status="ready" if candidate["mode"] == "real" else "simulation", last_error=None,
                          config_version=current.get("config_version", 1) + 1)
            if not candidate["enabled"] or candidate["mode"] != current["mode"]:
                values.update(capture_running=False, capture_paused=False, next_due=None)
            if not candidate["enabled"]:
                values["status"] = "disabled"
            return source_view(self.store.update_source(platform, values))

    async def _capture(self, platform: str, test: bool, scheduled=False) -> dict:
        async with self.lock:
            source = self.store.source(platform)
            if source["mode"] in SYNTHETIC_MODES:
                self.store.ensure_synthetic_capture()
            if scheduled:
                due = datetime.fromisoformat(source["next_due"].replace("Z", "+00:00")).timestamp() if source["next_due"] else 0
                planned = capture.schedule_at(self.store.capture_schedule("social"), self.clock(), source.get("capture_slot"))
                due = planned if planned is not None else due
                running = source["enabled"] and source.get("capture_running")
                if not running or due > self.clock():
                    return {"status": "scheduled" if running else "paused", "source": source_view(source)}
            if not test and not scheduled:
                if source["mode"] == "real" and not self.credentials.exists(source["secret_ref"]):
                    source = self.store.record_source(platform, {"status": "credential_required", "last_error": "credential_required",
                        "last_run_at": utc_text(self.clock()), "next_due": None})
                    return {"status": "credential_required", "message": "Real capture requires a credential", "source": source_view(source)}
                source = self.store.start_capture(platform)
            if source["status"] == "rate_limited" and source["next_due"]:
                retry = datetime.fromisoformat(source["next_due"].replace("Z", "+00:00")).timestamp()
                if retry > self.clock():
                    return {"status": "rate_limited", "message": "Waiting for the X quota window", "source": source_view(source)}
            if source["mode"] in SYNTHETIC_MODES:
                if not test:
                    self.store.advance_simulation(force=True, platform=platform)
                source = self.store.source(platform)
                completed = not test and source["status"] == "completed"
                return {"status": "completed" if completed else "simulation",
                    "message": "Synthetic query validated" if test else "Synthetic capture completed" if completed else "Synthetic capture started",
                    "source": source_view(source)}
            if not test:
                slot = capture.schedule_at(self.store.capture_schedule("social"), self.clock(), source.get("capture_slot"))
                if slot is not None and slot > self.clock():
                    source = self.store.record_source(platform, {"next_due": utc_text(slot), "status": "scheduled"})
                    return {"status": "scheduled", "source": source_view(source)}
            result = await asyncio.to_thread(self._poll, source, test)
            return {"status": result["status"], "message": "Connection test completed" if test else "Capture processed", "source": source_view(result)}

    def _poll(self, source: dict, test: bool) -> dict:
        now, platform = self.clock(), source["platform"]
        try:
            token = self.credentials.get(source["secret_ref"])
            saved = {} if test else self.store.checkpoint(platform)
            def on_page(events, checkpoint):
                directory = self.store.path.parent / "prisma-landing"
                name = landing.write_file(directory, events, {"platform": platform, "checkpoint": checkpoint})
                self.store.persist_page(platform, landing.records((directory / name).read_bytes()), checkpoint)
            def on_checkpoint(checkpoint):
                self.store.persist_page(platform, [], checkpoint)
            with self.client_factory() as client:
                result = poll_queries(client, token, source, saved, now, on_page, on_checkpoint, test=test)
            slot = capture.schedule_at(self.store.capture_schedule("social"), now, source.get("capture_slot"))
            if not test and slot is not None:
                result.update(capture_slot=slot, next_due=utc_text(capture.schedule_at(self.store.capture_schedule("social"), now, slot)))
            return self.store.record_source(platform, {**result, "last_run_at": utc_text(now)})
        except XFailure as exc:
            return self.store.record_source(platform, {"status": exc.code, "last_error": exc.code,
                "last_run_at": utc_text(now), "next_due": utc_text(exc.retry_at) if exc.retry_at else None})
        except httpx.RequestError:
            return self.store.record_source(platform, {"status": "network_error", "last_error": "network_error",
                "last_run_at": utc_text(now), "next_due": utc_text(now + 60)})

    async def test_source(self, platform: str) -> dict:
        return await self._capture(platform, True)

    async def run_source(self, platform: str) -> dict:
        return await self._capture(platform, False)

    async def pause_source(self, platform: str) -> dict:
        async with self.lock:
            source = self.store.record_source(platform, {"capture_running": False, "capture_paused": True,
                "status": "paused", "next_due": None, "last_error": None})
            return {"status": "paused", "message": "Capture paused; publications and checkpoints retained", "source": source_view(source)}

    async def posts(self, platform, limit, before_seq=None, max_seq=None):
        return self.store.posts(platform, limit, before_seq, max_seq)

    async def simulation(self, action: str) -> dict:
        async with self.lock:
            return self.store.control_simulation(action)

    async def reset_synthetic(self, operation_id: str) -> dict:
        async with self.lock:
            return self.store.reset_synthetic(operation_id)

    async def synthetic_reset_status(self) -> dict:
        return self.store.synthetic_reset_status()

    async def snapshot(self) -> dict:
        async with self.lock:
            return self.store.snapshot()

    async def review(self, incident_id: str, status: str, note: str, expected_evidence_ids=None, lat=None, lon=None) -> dict:
        async with self.lock:
            return self.store.review(incident_id, status, note, expected_evidence_ids, lat, lon)

    async def tick(self) -> None:
        async with self.lock:
            self.store.advance_simulation()
            sensor_capture.local_tick(self.store)
            self.store.snapshot()
            sources = self.store.sources()
        for source in sources:
            if not source["enabled"] or not source.get("capture_running", False) or source["mode"] != "real":
                continue
            if source["last_error"] and not source["next_due"]:
                continue
            due = datetime.fromisoformat(source["next_due"].replace("Z", "+00:00")).timestamp() if source["next_due"] else 0
            planned = capture.schedule_at(self.store.capture_schedule("social"), self.clock(), source.get("capture_slot"))
            due = planned if planned is not None else due
            if due <= self.clock():
                await self._capture(source["platform"], False, scheduled=True)

    async def sensors(self):
        config = sensor_capture.local_configuration(self.store)
        with self.store.connection() as db:
            config = sensor_reset.annotated(config, self.store._get(db, "synthetic_reset", {}))
        return {"config": config, "configs": config["configs"], "sensor_schedule": config["sensor_schedule"], "runtime": "local_fixture"}

    async def update_sensors(self, values, sensor_type=None):
        values = dict(values)
        if values.pop("mode", "Synthetic") != "Synthetic":
            raise ValueError("Only Synthetic sensor capture is available")
        async with self.lock:
            with self.store.connection() as db:
                sensor_reset.guard(self.store._get(db, "synthetic_reset", {}), sensor_type)
            return {"config": sensor_capture.local_save(self.store, values, sensor_type), "runtime": "local_fixture"}

    async def control_sensors(self, running, sensor_type=None):
        async with self.lock:
            with self.store.connection() as db:
                sensor_reset.guard(self.store._get(db, "synthetic_reset", {}), sensor_type)
            config = sensor_capture.local_control(self.store, running, sensor_type)
            return {"config": config, "message": "Simulated sensor capture started" if running else "Sensor capture paused"}

    async def sensor_reset_status(self, sensor_type):
        with self.store.connection() as db:
            return sensor_reset.state_for(self.store._get(db, "synthetic_reset", {}), sensor_type)

    async def reset_sensors(self, sensor_type, operation_id):
        async with self.lock:
            return sensor_reset.local(self.store, sensor_type, operation_id)

    async def sensor_location(self, sensor_id, values):
        async with self.lock:
            return sensor_capture.local_location(self.store, sensor_id, values)
