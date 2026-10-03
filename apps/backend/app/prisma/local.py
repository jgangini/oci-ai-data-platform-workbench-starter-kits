"""Local-only runtime. Cloud execution must use the separate AIDP adapter."""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from .core import default_source, source_migration, utc_text
from .store import PrismaStore
from .x import XFailure, poll_queries
from . import landing
from .capture import validate_source
from .source_rules import check_revision, source_view, validate_rules


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
        snapshot = self.store.snapshot()
        return {"sources": [source_view(source) for source in self.store.sources()], "simulation": self.store.simulation_state(), "runtime": "local_fixture",
                "capture_summary": self.store.capture_summary(), "pipeline": {"status": "local_fixture", "version": snapshot["version"], "last_run_at": snapshot.get("published_at")}}

    async def update_source(self, platform: str, payload: dict) -> dict:
        async with self.lock:
            values = dict(payload)
            token = values.pop("bearer_token", None)
            expected = values.pop("expected_revision", None)
            current = self.store.source(platform)
            check_revision(current, expected)
            candidate = {**current, **values}
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
            if scheduled:
                due = datetime.fromisoformat(source["next_due"].replace("Z", "+00:00")).timestamp() if source["next_due"] else 0
                if not source.get("capture_running") or due > self.clock():
                    return {"status": "paused" if not source.get("capture_running") else "scheduled", "source": source_view(source)}
            if not source["enabled"]:
                return {"status": "disabled", "message": "Enable the source before running it", "source": source_view(source)}
            if not test and not scheduled:
                source = self.store.start_capture(platform)
            if source["status"] == "rate_limited" and source["next_due"]:
                retry = datetime.fromisoformat(source["next_due"].replace("Z", "+00:00")).timestamp()
                if retry > self.clock():
                    return {"status": "rate_limited", "message": "Waiting for the X quota window", "source": source_view(source)}
            if source["mode"] == "simulation":
                if not test:
                    self.store.advance_simulation(force=True, platform=platform)
                return {"status": "simulation", "message": "Synthetic query validated" if test else "Continuous capture started", "source": source_view(self.store.source(platform))}
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
        return self.store.control_simulation(action)

    async def snapshot(self) -> dict:
        return self.store.snapshot()

    async def review(self, incident_id: str, status: str, note: str) -> dict:
        return self.store.review(incident_id, status, note)

    async def tick(self) -> None:
        self.store.advance_simulation()
        for source in self.store.sources():
            if not source["enabled"] or not source.get("capture_running", False) or source["mode"] != "real":
                continue
            if source["last_error"] and not source["next_due"]:
                continue
            due = datetime.fromisoformat(source["next_due"].replace("Z", "+00:00")).timestamp() if source["next_due"] else 0
            if due <= self.clock():
                await self._capture(source["platform"], False, scheduled=True)
