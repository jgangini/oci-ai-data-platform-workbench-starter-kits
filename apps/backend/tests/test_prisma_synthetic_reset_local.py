import asyncio
import json
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.prisma import landing
from app.prisma.core import simulation_events
from app.prisma.local import LocalPrismaRuntime


NOW = 1791209100.0


def real_event():
    return {**simulation_events(0, NOW)[0], "source_id": "real-123", "mode": "real", "is_simulated": False}


def documents(store):
    with store.connection() as db:
        return {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM state")}


def test_reset_removes_legacy_and_canonical_rows_but_preserves_real(tmp_path):
    runtime = LocalPrismaRuntime(tmp_path, clock=lambda: NOW)
    raw = simulation_events(0, NOW)[0]
    runtime.store.persist_page("x", [{**raw, "source_id": "canonical"}, {**raw, "source_id": "legacy"}, real_event()], {})
    with runtime.store.connection() as db:
        for table, key in (("events", "id"), ("social_posts", "post_key")):
            payload = json.loads(db.execute(f"SELECT payload FROM {table} WHERE {key}=?", ("x:legacy",)).fetchone()[0])
            db.execute(f"UPDATE {table} SET payload=? WHERE {key}=?", (json.dumps({**payload, "mode": "simulation"}), "x:legacy"))
    result = asyncio.run(runtime.reset_synthetic(str(uuid4())))
    assert result["status"] == "completed"
    assert result["counts"]["events"] == result["counts"]["social_posts"] == 2
    with runtime.store.connection() as db:
        for table in ("events", "social_posts"):
            assert [json.loads(row[0])["mode"] for row in db.execute(f"SELECT payload FROM {table}")] == ["real"]


def test_reset_preserves_real_data_configuration_and_restarts_with_a_new_run(tmp_path):
    async def check():
        now = [NOW]
        runtime = LocalPrismaRuntime(tmp_path, clock=lambda: now[0])
        assert await runtime.synthetic_reset_status() == {}
        await runtime.update_source("x", {"query": "#bogota", "interval_minutes": 3, "bearer_token": "local-token"})
        await runtime.run_source("x")
        await runtime.run_source("facebook")
        now[0] += 600
        await runtime.tick()
        runtime.store.persist_page("x", [real_event()], {"since_id": "real-123"})
        snapshot = await runtime.snapshot()
        real_incident = next(item for item in snapshot["incidents"] if item["mode"] == "real")
        synthetic_incident = next(item for item in snapshot["incidents"] if item["mode"] == "Synthetic")
        await runtime.review(real_incident["id"], "validated", "Keep the real review")
        await runtime.review(synthetic_incident["id"], "validated", "Discard this synthetic review")
        before = documents(runtime.store)
        config = {key: runtime.store.source("x")[key] for key in ("query", "interval_minutes", "enabled", "mode",
            "secret_ref", "credential_configured", "config_version", "report_thresholds", "correlation_window_minutes")}
        old_run = before["capture_controls"]["x"]["run_id"]
        with runtime.store.connection() as db:
            real_post = db.execute("SELECT * FROM social_posts WHERE post_key='x:real-123'").fetchone()
        operation = str(uuid4())
        result = await runtime.reset_synthetic(operation)
        assert result["status"] == "completed" and result["counts"]["events"] > 0
        assert result["stage"] == "completed"
        assert result["counts"]["social_posts"] > 0 and result["counts"]["reviews"] == 1
        assert result["version"] != snapshot["version"]
        assert {key: runtime.store.source("x")[key] for key in config} == config
        assert runtime.credentials.get(config["secret_ref"]) == "local-token"
        assert runtime.store.checkpoint("x") == {"since_id": "real-123"}
        after = documents(runtime.store)
        assert not any(key.startswith("synthetic:") for key in after)
        assert not after["capture_controls"] and after["simulation"]["status"] == "idle"
        assert all(not source["capture_running"] and not source["capture_paused"] for source in runtime.store.sources())
        with runtime.store.connection() as db:
            assert db.execute("SELECT * FROM social_posts").fetchall() == [real_post]
            assert [row[0] for row in db.execute("SELECT id FROM reviews")] == [real_incident["id"]]
        now[0] += 600
        restarted = LocalPrismaRuntime(tmp_path, clock=lambda: now[0])
        await restarted.tick()
        await restarted.sources()
        clean = await restarted.snapshot()
        assert [event["mode"] for event in clean["evidence"]] == ["real"]
        assert clean["incidents"][0]["id"] == real_incident["id"]
        assert clean["incidents"][0]["review_note"] == "Keep the real review"
        await restarted.run_source("x")
        fresh = await restarted.snapshot()
        assert any(event["mode"] == "Synthetic" for event in fresh["evidence"])
        assert documents(restarted.store)["capture_controls"]["x"]["run_id"] != old_run
        assert await restarted.reset_synthetic(operation) == result
        assert (await restarted.snapshot())["evidence"] == fresh["evidence"]
        second = str(uuid4())
        await restarted.reset_synthetic(second)
        await restarted.run_source("x")
        newest = await restarted.snapshot()
        assert (await restarted.reset_synthetic(operation))["status"] == "completed"
        assert (await restarted.synthetic_reset_status())["operation_id"] == second
        assert (await restarted.snapshot())["evidence"] == newest["evidence"]
    asyncio.run(check())


def test_local_progress_exposes_resume_stage_and_derives_completion(tmp_path, monkeypatch):
    runtime = LocalPrismaRuntime(tmp_path, clock=lambda: NOW)
    observed = []
    for name, stage in (("_reset_synthetic_landing", "landing"), ("snapshot", "publish")):
        operation = getattr(runtime.store, name)
        def check(*args, _operation=operation, _stage=stage, **kwargs):
            status = runtime.store.synthetic_reset_status()
            assert status["status"] == "pending" and status["stage"] == _stage
            observed.append(_stage)
            return _operation(*args, **kwargs)
        monkeypatch.setattr(runtime.store, name, check)
    result = asyncio.run(runtime.reset_synthetic(str(uuid4())))
    assert observed == ["landing", "publish"]
    assert result["stage"] == "completed"
    assert documents(runtime.store)["synthetic_reset"]["stage"] == "publish"


def test_landing_cleanup_preserves_real_unknown_and_empty_and_rewrites_mixed(tmp_path, monkeypatch):
    async def check():
        runtime = LocalPrismaRuntime(tmp_path, clock=lambda: NOW)
        directory = tmp_path / "prisma-landing"
        synthetic = simulation_events(0, NOW)[:1]
        only_synthetic = landing.write_file(directory, synthetic)
        only_real = landing.write_file(directory, [real_event()])
        mixed = landing.write_file(directory, [*synthetic, real_event()])
        empty = landing.write_file(directory, [], {"empty": True})
        unknown = directory / "unknown.csv"
        unknown.write_bytes(b"unexpected,header\n")
        malformed = directory / "malformed.csv"
        malformed.write_bytes(b"id,payload\norphan-id\n")
        preserved = {name: (directory / name).read_bytes() for name in (only_real, empty, unknown.name, malformed.name)}
        unlink = Path.unlink
        failed = [False]
        def fail_mixed_once(path, *args, **kwargs):
            if path.name == mixed and not failed[0]:
                failed[0] = True
                raise OSError("Injected unlink failure")
            return unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", fail_mixed_once)
        operation = str(uuid4())
        result = await runtime.reset_synthetic(operation)
        assert result["status"] == "error" and result["error"] == "local_synthetic_reset_failed"
        assert (directory / mixed).is_file()  # Original survives until its real-only replacement is durable.
        result = await LocalPrismaRuntime(tmp_path, clock=lambda: NOW).reset_synthetic(operation)
        assert result["status"] == "completed"
        assert result["counts"]["landing_files"] == 2 and result["counts"]["landing_mixed_files"] == 1
        assert result["counts"]["landing_unknown_preserved"] == 3
        assert not (directory / only_synthetic).exists() and not (directory / mixed).exists()
        assert all((directory / name).read_bytes() == body for name, body in preserved.items())
        replacements = [path for path in directory.glob("*.csv") if path.name not in preserved]
        assert len(replacements) == 1
        assert [event["id"] for event in landing.records(replacements[0].read_bytes())] == ["x:real-123"]
    asyncio.run(check())


@pytest.mark.parametrize("durable_status", ["pending", "error"])
def test_unfinished_reset_blocks_synthetic_writers_and_retries_only_same_id(tmp_path, monkeypatch, durable_status):
    async def check():
        runtime = LocalPrismaRuntime(tmp_path, clock=lambda: NOW)
        await runtime.run_source("x")
        operation = str(uuid4())
        def fail_snapshot():
            raise OSError("Injected publication failure")
        monkeypatch.setattr(runtime.store, "snapshot", fail_snapshot)
        failed = await runtime.reset_synthetic(operation)
        assert failed["status"] == "error" and failed["counts"]["events"] > 0
        with runtime.store.connection() as db:
            state = runtime.store._get(db, "synthetic_reset", {})
            state["status"] = durable_status  # Simulate process death after durable pending or an observed error.
            runtime.store._put(db, "synthetic_reset", state)
        restarted = LocalPrismaRuntime(tmp_path, clock=lambda: NOW + 600)
        assert (await restarted.sources())["synthetic_reset"]["status"] == durable_status
        for action in (lambda: restarted.reset_synthetic(str(uuid4())), lambda: restarted.run_source("x"),
                       lambda: restarted.simulation("start"), lambda: restarted.update_source("x", {"mode": "real"})):
            with pytest.raises(HTTPException) as conflict:
                await action()
            assert conflict.value.status_code == 409
        with pytest.raises(HTTPException):
            restarted.store.persist_page("x", simulation_events(0, NOW)[:1], {})
        await restarted.tick()
        assert not (await restarted.snapshot())["evidence"]
        repaired = await restarted.reset_synthetic(operation)
        assert repaired["status"] == "completed" and repaired["counts"] == failed["counts"]
        await restarted.run_source("x")
        assert (await restarted.snapshot())["evidence"]
    asyncio.run(check())


def test_failed_cleanup_blocks_synthetic_review_but_preserves_real_review(tmp_path, monkeypatch):
    async def check():
        runtime = LocalPrismaRuntime(tmp_path, clock=lambda: NOW)
        await runtime.run_source("x")
        runtime.store.persist_page("x", [real_event()], {"since_id": "real-123"})
        incidents = (await runtime.snapshot())["incidents"]
        identifiers = {item["mode"]: item["id"] for item in incidents}
        def unavailable(_):
            raise OSError("Injected Landing failure")
        monkeypatch.setattr(runtime.store, "_reset_synthetic_landing", unavailable)
        assert (await runtime.reset_synthetic(str(uuid4())))["status"] == "error"
        with pytest.raises(HTTPException) as conflict:
            await runtime.review(identifiers["Synthetic"], "validated", "Blocked")
        assert conflict.value.status_code == 409
        assert (await runtime.review(identifiers["real"], "validated", "Real review remains available"))["review_status"] == "validated"
    asyncio.run(check())


def test_reset_waits_for_real_capture_and_preserves_its_checkpoint(tmp_path, monkeypatch):
    async def check():
        runtime = LocalPrismaRuntime(tmp_path, clock=lambda: NOW)
        await runtime.run_source("facebook")
        await runtime.update_source("x", {"mode": "real", "bearer_token": "local-token"})
        started, finish = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        def poll(source, test):
            loop.call_soon_threadsafe(started.set)
            assert finish.wait(5), "Real capture was not released"
            runtime.store.persist_page("x", [real_event()], {"since_id": "real-123"})
            return runtime.store.record_source("x", {"status": "ready", "next_due": None})
        monkeypatch.setattr(runtime, "_poll", poll)
        capture = asyncio.create_task(runtime.run_source("x"))
        await asyncio.wait_for(started.wait(), 5)
        reset = asyncio.create_task(runtime.reset_synthetic(str(uuid4())))
        await asyncio.sleep(0)
        assert not reset.done()
        finish.set()
        await capture
        result = await reset
        assert result["status"] == "completed"
        assert [item["mode"] for item in (await runtime.snapshot())["evidence"]] == ["real"]
        assert runtime.store.checkpoint("x") == {"since_id": "real-123"}
        assert runtime.store.source("x")["capture_running"] is True
    asyncio.run(check())


@pytest.mark.parametrize("operation_id", ["not-a-uuid", None, True])
def test_invalid_operation_id_does_not_change_state(tmp_path, operation_id):
    runtime = LocalPrismaRuntime(tmp_path, clock=lambda: NOW)
    before = documents(runtime.store)
    with pytest.raises(ValueError):
        asyncio.run(runtime.reset_synthetic(operation_id))
    assert documents(runtime.store) == before
