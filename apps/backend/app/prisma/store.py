"""Durable local fixture state; OCI runtime uses the AIDP/ADB adapter instead."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .core import PLATFORMS, SOURCE_FIELDS, build_snapshot, default_source, normalize_event, utc_text
from . import capture, landing


class PrismaStore:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _get(db, key, default):
        row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _put(db, key, value):
        db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value, ensure_ascii=False)))

    def source(self, platform: str) -> dict:
        if platform not in PLATFORMS:
            raise ValueError("Unsupported platform")
        with self.connection() as db:
            return self._get(db, f"source:{platform}", default_source(platform))

    def sources(self) -> list[dict]:
        return [self.source(platform) for platform in PLATFORMS]

    def update_source(self, platform: str, values: dict) -> dict:
        source = self.source(platform)
        if set(values) - SOURCE_FIELDS:
            raise ValueError("Unsupported source fields")
        changed_query = "query" in values and values["query"] != source["query"]
        source.update(values)
        with self.connection() as db:
            self._put(db, f"source:{platform}", source)
            if changed_query:
                self._put(db, f"cursor:{platform}", {})
        return source

    record_source = update_source

    def checkpoint(self, platform: str) -> dict:
        with self.connection() as db:
            return self._get(db, f"cursor:{platform}", {})

    def _events(self, db, events: list[dict]) -> None:
        changed = False
        for raw in events:
            event = normalize_event(raw)
            result = db.execute("INSERT OR IGNORE INTO events VALUES (?,?)", (event["id"], json.dumps(event, ensure_ascii=False)))
            changed = changed or bool(result.rowcount)
        if changed:
            self._revision(db)

    def _revision(self, db):
        value = self._get(db, "publication", {"revision": 0})
        self._put(db, "publication", {"revision": value["revision"] + 1, "published_at": utc_text(self.clock())})

    def persist_page(self, platform: str, events: list[dict], checkpoint: dict) -> None:
        with self.connection() as db:
            self._events(db, events)
            self._put(db, f"cursor:{platform}", checkpoint)

    def simulation_state(self) -> dict:
        with self.connection() as db:
            state = self._get(db, "simulation", {"status": "idle", "elapsed_seconds": 0, "started_at": None})
        if state["status"] == "running":
            state["elapsed_seconds"] = min(600, state["elapsed_seconds"] + max(0, self.clock() - state["started_at"]))
            if state["elapsed_seconds"] >= 600:
                state["status"] = "completed"
        return {"status": state["status"], "elapsed_seconds": state["elapsed_seconds"], "duration_seconds": 600,
                "anchor_at": state.get("anchor_at"), "run_id": state.get("run_id")}

    def control_simulation(self, action: str) -> dict:
        if action not in {"start", "pause", "resume", "reset", "replay"}:
            raise ValueError("Unsupported simulation action")
        state = self.simulation_state()
        reset = action in {"reset", "replay", "start"}
        if reset or state.get("anchor_at") is None:
            state["anchor_at"] = self.clock() - (0 if reset else state["elapsed_seconds"])
            state["run_id"] = str(uuid4())
        if action == "reset":
            state.update(status="idle", elapsed_seconds=0)
        elif action == "pause":
            state["status"] = "paused"
        else:
            if reset:
                state["elapsed_seconds"] = 0
            state["status"] = "running"
        state["started_at"] = self.clock()
        snapshot_ids = [item["id"] for item in self.snapshot()["incidents"] if item["mode"] == "simulation"] if reset else []
        with self.connection() as db:
            if reset:
                db.executemany("DELETE FROM reviews WHERE id=?", ((incident_id,) for incident_id in snapshot_ids))
                ids = [row[0] for row in db.execute("SELECT id,payload FROM events") if json.loads(row[1])["mode"] == "simulation"]
                db.executemany("DELETE FROM events WHERE id=?", ((event_id,) for event_id in ids))
                if ids:
                    self._revision(db)
            self._put(db, "simulation", state)
        self.advance_simulation(force=True)
        return self.simulation_state()

    def advance_simulation(self, force=False) -> None:
        state = self.simulation_state()
        if state["status"] == "idle" or (state["status"] == "paused" and state["elapsed_seconds"] == 0):
            return
        for source in capture.sources(self.sources()):
            with self.connection() as db:
                name = source["platform"]
                cursor = self._get(db, "synthetic:" + name, {})
                result = capture.batch(source, state, cursor, self.clock(), force)
                if result is None:
                    continue
                events, cursor = result
                key = landing.write_file(self.path.parent / "prisma-landing", events)
                self._events(db, events)  # Explicit local fixture sink; OCI sends these Landing files to Spark.
                self._put(db, "synthetic:" + name, cursor)
                summary = self._get(db, "capture_summary", {"landing_count": 0})
                self._put(db, "capture_summary", {"status": "ready", "landing_count": summary["landing_count"] + bool(key),
                    "last_landing_key": key or summary.get("last_landing_key"), "last_run_at": utc_text(self.clock())})
                if name in PLATFORMS:
                    self._put(db, "source:" + name, {**source, "status": "simulation", "last_received_count": len(events),
                        "last_run_at": utc_text(self.clock()), "next_due": utc_text(cursor["next_due"]), "last_error": None})

    def capture_summary(self):
        with self.connection() as db:
            return self._get(db, "capture_summary", {"landing_count": 0, "status": "idle"})

    def snapshot(self) -> dict:
        self.advance_simulation()
        with self.connection() as db:
            events = [json.loads(row[0]) for row in db.execute("SELECT payload FROM events")]
            reviews = {row[0]: json.loads(row[1]) for row in db.execute("SELECT id,payload FROM reviews")}
            publication = self._get(db, "publication", {"revision": 0, "published_at": None})
        result = build_snapshot(events, reviews, f"local-{publication['revision']}", publication["published_at"])
        result.update(runtime="local_fixture", simulation=self.simulation_state())
        return result

    def review(self, incident_id: str, status: str, note: str) -> dict:
        if status not in {"pending", "validated", "rejected"}:
            raise ValueError("Unsupported review status")
        if not any(item["id"] == incident_id for item in self.snapshot()["incidents"]):
            raise KeyError(incident_id)
        with self.connection() as db:
            db.execute("INSERT INTO reviews VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (incident_id, json.dumps({"status": status, "note": note[:2000], "reviewed_at": utc_text(self.clock())})))
            self._revision(db)
        return next(item for item in self.snapshot()["incidents"] if item["id"] == incident_id)
