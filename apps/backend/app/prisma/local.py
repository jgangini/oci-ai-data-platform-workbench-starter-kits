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

from .core import utc_text
from .store import PrismaStore
from .x import XFailure, fetch_page
from .capture import search_terms


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
        self.clock, self.client_factory = clock, client_factory
        self.lock = asyncio.Lock()

    async def sources(self) -> dict:
        snapshot = self.store.snapshot()
        return {"sources": self.store.sources(), "simulation": self.store.simulation_state(), "runtime": "local_fixture",
                "capture_summary": self.store.capture_summary(), "pipeline": {"status": "local_fixture", "version": snapshot["version"], "last_run_at": snapshot.get("published_at")}}

    async def update_source(self, platform: str, payload: dict) -> dict:
        async with self.lock:
            values = dict(payload)
            token = values.pop("bearer_token", None)
            current = self.store.source(platform)
            candidate = {**current, **values}
            if candidate["mode"] == "real" and platform != "x":
                raise ValueError("Only X has a real connector; other platforms are prepared for simulation")
            if candidate["mode"] == "real" and not candidate["query"].strip():
                raise ValueError("Real X capture requires a query")
            if candidate["mode"] == "simulation":
                search_terms(candidate["query"])
            if token:
                self.credentials.put(candidate["secret_ref"], token)
            values["credential_configured"] = self.credentials.exists(candidate["secret_ref"])
            values.update(status="ready" if candidate["mode"] == "real" else "simulation", last_error=None, next_due=None)
            if not candidate["enabled"]:
                values["status"] = "disabled"
            return self.store.update_source(platform, values)

    async def _capture(self, platform: str, test: bool) -> dict:
        async with self.lock:
            source = self.store.source(platform)
            if not source["enabled"]:
                return {"status": "disabled", "message": "Fuente pausada", "source": source}
            if source["status"] == "rate_limited" and source["next_due"]:
                retry = datetime.fromisoformat(source["next_due"].replace("Z", "+00:00")).timestamp()
                if retry > self.clock():
                    return {"status": "rate_limited", "message": "Esperando la ventana de cuota de X", "source": source}
            if source["mode"] == "simulation":
                self.store.advance_simulation(force=not test)
                return {"status": "simulation", "message": "Simulated source; the network was not contacted", "source": source}
            result = await asyncio.to_thread(self._poll, source, test)
            return {"status": result["status"], "message": "Prueba finalizada" if test else "Captura procesada", "source": result}

    def _poll(self, source: dict, test: bool) -> dict:
        now, platform = self.clock(), source["platform"]
        try:
            token = self.credentials.get(source["secret_ref"])
            cursor = {} if test else self.store.checkpoint(platform)
            received = 0
            with self.client_factory() as client:
                for _ in range(1 if test else 2):
                    events, cursor = fetch_page(client, token, source["query"], cursor, now, page_size=10 if test else 50)
                    received += len(events)
                    if not test:
                        self.store.persist_page(platform, events, cursor)
                    if not cursor.get("next_token"):
                        break
            status = "tested" if test else ("backlog" if cursor.get("next_token") else "ready")
            wait = 60 if cursor.get("next_token") and not test else source["interval_minutes"] * 60
            return self.store.record_source(platform, {"status": status, "last_error": None,
                "last_run_at": utc_text(now), "next_due": utc_text(now + wait),
                **({"last_received_count": received} if not test else {})})
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

    async def simulation(self, action: str) -> dict:
        return self.store.control_simulation(action)

    async def snapshot(self) -> dict:
        return self.store.snapshot()

    async def review(self, incident_id: str, status: str, note: str) -> dict:
        return self.store.review(incident_id, status, note)

    async def tick(self) -> None:
        self.store.advance_simulation()
        for source in self.store.sources():
            if not source["enabled"] or source["mode"] != "real":
                continue
            if source["last_error"] and not source["next_due"]:
                continue
            due = datetime.fromisoformat(source["next_due"].replace("Z", "+00:00")).timestamp() if source["next_due"] else 0
            if due <= self.clock():
                await self.run_source(source["platform"])
