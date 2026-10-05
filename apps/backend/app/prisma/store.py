"""Durable local fixture state; OCI runtime uses the AIDP/ADB adapter instead."""
from __future__ import annotations

import json
import hashlib
import os
import csv
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException

from .core import SYNTHETIC_MODES, canonical_mode, PLATFORMS, SOURCE_FIELDS, build_snapshot, default_source, normalize_event, utc_text, validate_review, review_location
from . import capture, landing
from .core import publication_revisions


class PrismaStore:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS social_posts (
                    capture_seq INTEGER PRIMARY KEY AUTOINCREMENT, post_key TEXT UNIQUE NOT NULL,
                    platform TEXT NOT NULL, payload TEXT NOT NULL, analysis_status TEXT NOT NULL, ingested_at TEXT);
                CREATE INDEX IF NOT EXISTS social_posts_platform_seq ON social_posts(platform,capture_seq);
                CREATE TABLE IF NOT EXISTS sensor_events (event_id TEXT PRIMARY KEY, sensor_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS sensor_events_observed ON sensor_events(observed_at,sensor_id);
            """)
            if not self._get(db, "posts_index_migrated", False):
                for row in db.execute("SELECT payload FROM events ORDER BY rowid").fetchall():
                    self._post(db, json.loads(row[0]))
                self._put(db, "posts_index_migrated", True)
            if self._get(db, "event_registry", None) is None:
                existing = [json.loads(row[0]) for row in db.execute("SELECT payload FROM events")]
                self._put(db, "event_registry", build_snapshot(existing, {}, "", "")["incidents"])
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
            return {**default_source(platform), **self._get(db, f"source:{platform}", {})}

    def sources(self) -> list[dict]:
        return [self.source(platform) for platform in PLATFORMS]

    def capture_schedule(self, kind):
        with self.connection() as db:
            return capture.schedule(self._get(db, kind + "_schedule", {}))

    def save_capture_schedule(self, kind, values):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = capture.update_schedule(self._get(db, kind + "_schedule", {}), values)
            self._put(db, kind + "_schedule", saved)
            return saved

    def update_source(self, platform: str, values: dict) -> dict:
        source = self.source(platform)
        if set(values) - SOURCE_FIELDS:
            raise ValueError("Unsupported source fields")
        changed_query = "query" in values and values["query"] != source["query"]
        source.update(values)
        source["mode"] = canonical_mode(source["mode"])
        with self.connection() as db:
            self._put(db, f"source:{platform}", source)
            if changed_query and not self._get(db, f"cursor:{platform}", {}).get("queries"):
                self._put(db, f"cursor:{platform}", {})
        return source

    record_source = update_source

    def start_capture(self, platform):
        source = self.source(platform)
        source["mode"] = canonical_mode(source["mode"])
        if source["mode"] in SYNTHETIC_MODES:
            self.ensure_synthetic_capture()
        if source["enabled"] and source.get("capture_running"):
            return source
        with self.connection() as db:
            controls = self._get(db, "capture_controls", {})
            saved = controls.get(platform, {})
            completed = source["mode"] in SYNTHETIC_MODES and capture.is_complete(saved, self._get(db, "synthetic:" + platform, {}))
            pending = completed and capture.institutional_pending(saved, controls.get("institutional", {}),
                {name: self._get(db, "synthetic:" + name, {}) for name in capture.INSTITUTIONAL})
            if completed and not pending:
                source.update(capture_running=False, capture_paused=True, status="completed", next_due=None, last_error=None)
                self._put(db, "source:" + platform, source)
                return source
            control = saved if source.get("capture_paused") or pending else None
            anchor = capture.schedule_at(self._get(db, "social_schedule", {}), self.clock())
            control = control or {"run_id": str(uuid4()), "anchor_at": anchor if anchor is not None else self.clock(), "seed": 0}
            if not any(item.get("capture_running") and item["enabled"] and item["mode"] in SYNTHETIC_MODES for item in self.sources()):
                controls["institutional"] = control
            controls[platform] = control
            self._put(db, "capture_controls", controls)
            source["enabled"] = True
            source["capture_running"] = True
            source["capture_paused"] = False
            self._put(db, "source:" + platform, source)
        return source

    def checkpoint(self, platform: str) -> dict:
        with self.connection() as db:
            return self._get(db, f"cursor:{platform}", {})

    def _events(self, db, events: list[dict]) -> None:
        if any(item.get("mode") in SYNTHETIC_MODES for item in events):
            self.ensure_synthetic_capture(db)
        changed = False
        for raw in events:
            event = normalize_event(raw)
            self._post(db, event)
            result = db.execute("INSERT OR IGNORE INTO events VALUES (?,?)", (event["id"], json.dumps(event, ensure_ascii=False)))
            changed = changed or bool(result.rowcount)
        if changed:
            self._revision(db)

    def _post(self, db, event):
        event = {**event, "mode": canonical_mode(event["mode"])}
        db.execute("""INSERT INTO social_posts(post_key,platform,payload,analysis_status,ingested_at)
            VALUES (?,?,?,'processed',?) ON CONFLICT(post_key) DO UPDATE SET payload=excluded.payload""",
            (event["id"], event["platform"], json.dumps(event, ensure_ascii=False), utc_text(self.clock())))

    def posts(self, platform, limit, before_seq=None, max_seq=None):
        if (platform is not None and platform not in PLATFORMS or type(limit) is not int or not 1 <= limit <= 100
                or any(value is not None and (type(value) is not int or value < minimum)
                       for value, minimum in ((before_seq, 1), (max_seq, 0)))):
            raise ValueError("Invalid post pagination")
        with self.connection() as db:
            if max_seq is None:
                max_seq = db.execute("""SELECT COALESCE(MAX(capture_seq),0) FROM social_posts
                    WHERE platform IN ('x','facebook','instagram','tiktok') AND (? IS NULL OR platform=?)""", (platform, platform)).fetchone()[0]
            total = db.execute("""SELECT COUNT(*) FROM social_posts WHERE platform IN ('x','facebook','instagram','tiktok')
                AND (? IS NULL OR platform=?) AND capture_seq<=?""", (platform, platform, max_seq)).fetchone()[0]
            rows = db.execute("""SELECT capture_seq,payload,analysis_status,ingested_at FROM social_posts
                WHERE platform IN ('x','facebook','instagram','tiktok') AND (? IS NULL OR platform=?)
                AND capture_seq<=? AND capture_seq<? ORDER BY capture_seq DESC LIMIT ?""",
                (platform, platform, max_seq, before_seq if before_seq is not None else max_seq + 1, limit + 1)).fetchall()
        items = [{"capture_seq": row[0], "payload": json.loads(row[1]), "analysis_status": row[2],
                  "captured_at": row[3], "ingested_at": row[3]} for row in rows[:limit]]
        return {"items": items, "max_seq": max_seq, "total": total,
                "next_seq": items[-1]["capture_seq"] if len(rows) > limit else None}

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

    def synthetic_reset_status(self) -> dict:
        with self.connection() as db:
            state = self._get(db, "synthetic_reset", {})
        if state.get("sensor_type"):
            return {}
        if state.get("status") == "completed":
            state["stage"] = "completed"
        return {key: state[key] for key in ("operation_id", "status", "stage", "counts", "version", "error") if key in state}

    def ensure_synthetic_capture(self, db=None) -> None:
        state = self._get(db, "synthetic_reset", {}) if db is not None else self.synthetic_reset_status()
        if state.get("status") in {"pending", "error"} and not state.get("sensor_type"):
            raise HTTPException(409, "Retry the pending synthetic reset before capturing synthetic data")

    def _begin_synthetic_reset(self, operation_id):
        if not isinstance(operation_id, str):
            raise ValueError("Invalid synthetic reset operation ID")
        operation_id = str(UUID(operation_id))
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = self._get(db, "synthetic_reset", {})
            from .synthetic_reset import check_scope
            check_scope(saved, operation_id)
            if saved.get("operation_id") == operation_id and saved.get("status") == "completed":
                return saved
            if operation_id in saved.get("completed_ids", []):
                return {"operation_id": operation_id, "status": "completed", "counts": {}}
            if saved.get("operation_id") != operation_id and saved.get("status") in {"pending", "error"}:
                raise HTTPException(409, "A synthetic reset is unfinished; retry its operation ID")
            state = saved if saved.get("operation_id") == operation_id else {
                "operation_id": operation_id, "counts": {}, "stage": "landing", "completed_ids": saved.get("completed_ids", []),
                "operation_scopes": saved.get("operation_scopes", {})}
            state.update(status="pending")
            state.pop("error", None)
            controls = self._get(db, "capture_controls", {})
            for platform in PLATFORMS:
                source = {**default_source(platform), **self._get(db, "source:" + platform, {})}
                source["mode"] = canonical_mode(source["mode"])
                if source["mode"] in SYNTHETIC_MODES:
                    source.update(capture_running=False, capture_paused=False, status="paused", next_due=None,
                                  last_run_at=None, last_error=None, last_received_count=None)
                    self._put(db, "source:" + platform, source)
                    controls.pop(platform, None)
            controls.pop("institutional", None)
            self._put(db, "capture_controls", controls)
            db.execute("DELETE FROM state WHERE key LIKE 'synthetic:%'")
            self._put(db, "simulation", {"status": "idle", "elapsed_seconds": 0, "started_at": self.clock(),
                                         "anchor_at": None, "run_id": None})
            self._put(db, "synthetic_reset", state)
        return state

    def _plan_synthetic_landing(self, directory):
        files, unknown = [], 0
        for path in directory.glob("*.csv"):
            if path.is_symlink() or not path.is_file():
                unknown += 1
                continue
            body = path.read_bytes()
            try:
                records = landing.records(body)
            except (ValueError, TypeError, csv.Error):
                records = []
            if not records:
                unknown += 1
                continue
            synthetic = sum(item["mode"] in SYNTHETIC_MODES for item in records)
            if synthetic:
                files.append({"name": path.name, "sha256": hashlib.sha256(body).hexdigest(),
                              "mixed": synthetic < len(records)})
        return files, unknown

    def _remove_synthetic_landing_file(self, directory, item, operation_id):
        path = directory / item["name"]
        if not path.exists():  # A previous attempt may have committed the replacement and removed this file.
            return
        if path.is_symlink() or path.parent.resolve() != directory.resolve():
            raise ValueError("Invalid Landing file")
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != item["sha256"]:
            raise ValueError("Landing changed during synthetic reset")
        if item["mixed"]:
            landing.write_file(directory, [event for event in landing.records(body) if event["mode"] == "real"],
                               {"synthetic_reset": operation_id, "original_file": item["name"]})
        path.unlink()

    def _reset_synthetic_landing(self, state):
        directory = self.path.parent / "prisma-landing"
        if directory.is_symlink() or directory.resolve().parent != self.path.parent.resolve():
            raise ValueError("Landing directory must not be a symbolic link")
        if "landing_files" not in state:
            files, unknown = self._plan_synthetic_landing(directory)
            state["landing_files"] = files
            state["counts"].update(landing_files=len(files), landing_mixed_files=sum(item["mixed"] for item in files),
                                   landing_unknown_preserved=unknown)
            with self.connection() as db:
                self._put(db, "synthetic_reset", state)
        for item in state["landing_files"]:
            self._remove_synthetic_landing_file(directory, item, state["operation_id"])

    def _reset_synthetic_reviews(self, db, evidence_ids):
        registry = self._get(db, "event_registry", [])
        incident_modes = {item["id"]: item.get("mode") for item in registry}
        reviews = []
        for key, payload in db.execute("SELECT id,payload FROM reviews"):
            evidence = set(json.loads(payload).get("evidence_ids", []))
            if incident_modes.get(key) in SYNTHETIC_MODES or evidence and evidence <= evidence_ids:
                reviews.append(key)
        db.executemany("DELETE FROM reviews WHERE id=?", ((key,) for key in reviews))
        self._put(db, "event_registry", [item for item in registry if item.get("mode") not in SYNTHETIC_MODES])
        return len(reviews)

    def _reset_synthetic_rows(self, state):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            synthetic = {}
            for table, column in (("events", "id"), ("social_posts", "post_key")):
                ids = {row[0] for row in db.execute(f"SELECT {column},payload FROM {table}")
                       if json.loads(row[1]).get("mode") in SYNTHETIC_MODES}
                db.executemany(f"DELETE FROM {table} WHERE {column}=?", ((key,) for key in ids))
                synthetic[table] = ids
                state["counts"][table] = len(ids)
            state["counts"]["reviews"] = self._reset_synthetic_reviews(db, synthetic["events"] | synthetic["social_posts"])
            remaining = sum(path.is_file() for path in (self.path.parent / "prisma-landing").glob("*.csv"))
            self._put(db, "capture_summary", {"status": "ready" if remaining else "idle", "landing_count": remaining,
                                              "last_landing_key": None, "last_run_at": None})
            self._revision(db)
            state["stage"] = "publish"
            self._put(db, "synthetic_reset", state)

    def reset_synthetic(self, operation_id: str) -> dict:
        state = self._begin_synthetic_reset(operation_id)
        if state["status"] == "completed":
            return {**{key: state[key] for key in ("operation_id", "status", "counts", "version") if key in state}, "stage": "completed"}
        try:
            if state["stage"] == "landing":
                self._reset_synthetic_landing(state)
                self._reset_synthetic_rows(state)
            version = self.snapshot()["version"]
            state.update(status="completed", version=version)
            # ponytail: demo receipts fit this document; use a receipt table for long-lived high-volume operation.
            state["completed_ids"].append(state["operation_id"])
            state.pop("landing_files", None)
            with self.connection() as db:
                self._put(db, "synthetic_reset", state)
        except Exception:
            with self.connection() as db:
                state = self._get(db, "synthetic_reset", state)
                state.update(status="error", error="local_synthetic_reset_failed")
                self._put(db, "synthetic_reset", state)
        return self.synthetic_reset_status()

    def control_simulation(self, action: str) -> dict:
        if action not in {"start", "pause", "resume", "reset", "replay"}:
            raise ValueError("Unsupported simulation action")
        self.ensure_synthetic_capture()
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
        snapshot_ids = [item["id"] for item in self.snapshot()["incidents"] if item["mode"] in SYNTHETIC_MODES] if reset else []
        with self.connection() as db:
            if reset:
                db.executemany("DELETE FROM reviews WHERE id=?", ((incident_id,) for incident_id in snapshot_ids))
                ids = [row[0] for row in db.execute("SELECT id,payload FROM events") if json.loads(row[1])["mode"] in SYNTHETIC_MODES]
                db.executemany("DELETE FROM events WHERE id=?", ((event_id,) for event_id in ids))
                if ids:
                    self._revision(db)
            self._put(db, "simulation", state)
        self.advance_simulation(force=True)
        return self.simulation_state()

    def advance_simulation(self, force=False, platform=None) -> None:
        if self.synthetic_reset_status().get("status") in {"pending", "error"}:
            return
        state = self.simulation_state()
        with self.connection() as db:
            controls = self._get(db, "capture_controls", {})
            shared_schedule = self._get(db, "social_schedule", {})
        finished = []
        for source, control, continuous in capture.inputs(self.sources(), controls, state):
            with self.connection() as db:
                name = source["platform"]
                if platform and name in PLATFORMS and name != platform:
                    continue
                cursor = self._get(db, "synthetic:" + name, {})
                pending_key = "synthetic:pending:" + name
                pending = self._get(db, pending_key, None)
                if not pending and continuous and capture.is_complete(control, cursor):
                    if name in PLATFORMS:
                        finished.append(name)
                    continue
                result = pending or (capture.continuous_batch if continuous else capture.batch)(source, control, cursor, self.clock(), force,
                    schedule=shared_schedule)
                if result is None:
                    continue
                events, cursor = result
                if not pending:
                    self._put(db, pending_key, result)
                    db.commit()  # Journal exact bytes/selection before the external Landing write.
                directory = self.path.parent / "prisma-landing"
                key = landing.write_file(directory, events, cursor["batch_key"])
                self._events(db, landing.records((directory / key).read_bytes()))  # Local fixture uses the identical CSV envelope; OCI uses Spark.
                self._put(db, "synthetic:" + name, cursor)
                db.execute("DELETE FROM state WHERE key=?", (pending_key,))
                summary = self._get(db, "capture_summary", {"landing_count": 0})
                self._put(db, "capture_summary", {"status": "ready", "landing_count": summary["landing_count"] + bool(key),
                    "last_landing_key": key or summary.get("last_landing_key"), "last_run_at": utc_text(self.clock()),
                    "last_data_at": utc_text(self.clock()) if events else summary.get("last_data_at")})
                if name in PLATFORMS:
                    self._put(db, "source:" + name, {**source, "mode": canonical_mode(source["mode"]), "status": "simulation", "last_received_count": len(events),
                        "last_run_at": utc_text(self.clock()), "next_due": utc_text(cursor["next_due"]) if cursor["next_due"] is not None else None, "last_error": None})
                    if continuous and capture.is_complete(control, cursor):
                        finished.append(name)
        with self.connection() as db:
            for name in finished:
                if capture.institutional_pending(controls.get(name, {}), controls.get("institutional", {}),
                        {kind: self._get(db, "synthetic:" + kind, {}) for kind in capture.INSTITUTIONAL}):
                    continue
                self._put(db, "source:" + name, {**self._get(db, "source:" + name, {}), "capture_running": False,
                    "capture_paused": True, "status": "completed", "next_due": None, "last_error": None})

    def capture_summary(self):
        with self.connection() as db:
            return self._get(db, "capture_summary", {"landing_count": 0, "status": "idle"})

    def snapshot(self) -> dict:
        self.advance_simulation()
        with self.connection() as db:
            events = [json.loads(row[0]) for row in db.execute("SELECT payload FROM events")]
            reviews = {row[0]: json.loads(row[1]) for row in db.execute("SELECT id,payload FROM reviews")}
            publication = self._get(db, "publication", {"revision": 0, "published_at": None})
            registry = self._get(db, "event_registry", None)
        events = [item for item in events if not item.get("raw_metadata", {}).get("capture_run_id") or item["created_at"] >= utc_text(self.clock() - 86400)]
        rules = {source["platform"]: source for source in self.sources()}
        from .sensor_capture import local_latest
        result = build_snapshot(events, reviews, f"local-v3-{publication['revision']}", publication["published_at"],
                                rules=rules, previous=registry, now=self.clock(), sensors=local_latest(self))
        digest = hashlib.sha256(json.dumps({name: result[name] for name in ("incidents", "evidence", "event_posts", "sensors")}, sort_keys=True).encode()).hexdigest()[:16]
        result["version"] += "-" + digest
        result.update(publication_revisions(result))
        with self.connection() as db:
            saved = self._get(db, "snapshot_publication", {})
            if saved.get("version") != result["version"]:
                saved = {"version": result["version"], "published_at": utc_text(self.clock()),
                    "social_revision": result["social_revision"], "sensor_revision": result["sensor_revision"]}
                self._put(db, "snapshot_publication", saved)
            elif any(saved.get(key) != result[key] for key in ("social_revision", "sensor_revision")):
                saved.update(social_revision=result["social_revision"], sensor_revision=result["sensor_revision"])
                self._put(db, "snapshot_publication", saved)
            result["published_at"] = saved["published_at"]
            if registry != result["incidents"]:
                self._put(db, "event_registry", result["incidents"])
        result.update(runtime="local_fixture", simulation=self.simulation_state())
        return result

    def review(self, incident_id: str, status: str, note: str, expected_evidence_ids=None, lat=None, lon=None) -> dict:
        validate_review(status, note, lat, lon)
        incident = next((item for item in self.snapshot()["incidents"] if item["id"] == incident_id), None)
        if incident is None:
            raise KeyError(incident_id)
        if expected_evidence_ids is not None and set(expected_evidence_ids) != set(incident["evidence_ids"]):
            raise HTTPException(409, "Event evidence changed. Refresh the event and review it again.")
        if incident["mode"] in SYNTHETIC_MODES:
            self.ensure_synthetic_capture()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM reviews WHERE id=?", (incident_id,)).fetchone()
            previous = json.loads(row[0]) if row else {}
            try:
                position = review_location(incident, previous, lat, lon)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            db.execute("INSERT INTO reviews VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (incident_id, json.dumps({**previous, **position, "status": status, "note": note, "reviewed_at": utc_text(self.clock()),
                                                "evidence_ids": incident["evidence_ids"]})))
            self._revision(db)
        return next(item for item in self.snapshot()["incidents"] if item["id"] == incident_id)
