import asyncio
import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import create_app
from app.gods_eye_view import database, pipeline, sensor_capture, sensor_pipeline, sensor_reset, sensors, synthetic_reset
from app.gods_eye_view.core import utc_text
from app.gods_eye_view.local import LocalGodsEyeViewRuntime
from test_gods_eye_view_access import admin_login, local_settings
from test_gods_eye_view_cloud import Runtime
from test_gods_eye_view_pipeline import CONFIG, NOW, runtime
from test_gods_eye_view_synthetic_reset import resetting  # shared three-store durable fake


def test_local_selected_delete_preserves_other_types_social_config_locations_and_restart(tmp_path):
    clock = [NOW]
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: clock[0])
    for kind in ("rainfall", "temperature"):
        sensor_capture.local_save(runtime.store, {"sensor_count": 1}, kind)
        sensor_capture.local_control(runtime.store, True, kind)
    original = runtime.store.snapshot()
    rainfall = next(item for item in original["sensors"] if item["sensor_type"] == "rainfall")
    with runtime.store.connection() as db:
        runtime.store._put(db, "sensor_locations", {rainfall["sensor_id"]: {"lat": 4.6, "lon": -74.1}})
        sibling = copy.deepcopy(runtime.store._get(db, "checkpoint_sensors", {})["by_type"]["temperature"])
    old_file = next((tmp_path / "prisma-landing/sensors/rainfall").glob("*.txt"))
    other_file = next((tmp_path / "prisma-landing/sensors/temperature").glob("*.txt"))
    other_bytes = other_file.read_bytes()
    operation = str(uuid4())
    result = asyncio.run(runtime.reset_sensors("rainfall", operation))
    assert result["status"] == "completed" and result["sensor_type"] == "rainfall"
    current = runtime.store.snapshot()
    assert current["version"] == result["version"] != original["version"]
    assert current["sensors"] == [item for item in original["sensors"] if item["sensor_type"] != "rainfall"]
    assert all(current[key] == original[key] for key in ("evidence", "incidents", "event_posts"))
    assert not old_file.exists() and other_file.read_bytes() == other_bytes
    with runtime.store.connection() as db:
        assert runtime.store._get(db, "sensor_locations", {})[rainfall["sensor_id"]] == {"lat": 4.6, "lon": -74.1}
        assert runtime.store._get(db, "checkpoint_sensors", {})["by_type"]["temperature"] == sibling
    config = next(item for item in asyncio.run(runtime.sensors())["configs"] if item["sensor_type"] == "rainfall")
    assert config["sensor_count"] == 1 and config["interval_minutes"] == 5 and not config["capture_running"]
    assert config["last_received_count"] is None and config["reset"] == result
    restarted = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: clock[0])
    assert asyncio.run(restarted.reset_sensors("rainfall", operation)) == result
    asyncio.run(restarted.control_sensors(True, "rainfall"))
    assert not old_file.exists()
    clock[0] += 1
    assert sensor_capture.local_tick(restarted.store) == 1
    new_file = next((tmp_path / "prisma-landing/sensors/rainfall").glob("*.txt"))
    assert new_file.name != old_file.name
    assert next(item for item in restarted.store.snapshot()["sensors"] if item["sensor_type"] == "rainfall")["lat"] == 4.6
    assert asyncio.run(restarted.reset_sensors("rainfall", operation)) == result
    assert new_file.exists(), "A completed retry must not erase newly captured readings"


def test_local_delete_rejects_redirected_landing_without_touching_target_or_rows(tmp_path):
    runtime = LocalGodsEyeViewRuntime(tmp_path / "state", clock=lambda: NOW)
    sensor_capture.local_save(runtime.store, {"sensor_count": 1}, "rainfall")
    sensor_capture.local_control(runtime.store, True, "rainfall")
    original = runtime.store.snapshot()["sensors"]
    directory = runtime.store.path.parent / "prisma-landing/sensors/rainfall"
    outside = tmp_path / "outside-rainfall"
    directory.rename(outside)
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(outside), str(directory))
    else:
        directory.symlink_to(outside, target_is_directory=True)
    try:
        before = {path.name: path.read_bytes() for path in outside.iterdir()}
        result = asyncio.run(runtime.reset_sensors("rainfall", str(uuid4())))
        assert result["status"] == "error"
        assert {path.name: path.read_bytes() for path in outside.iterdir()} == before
        assert runtime.store.snapshot()["sensors"] == original
    finally:
        if directory.is_symlink():
            directory.unlink()
        else:
            directory.rmdir()  # Remove the test junction itself, never its target.


def test_local_failed_delete_blocks_only_selected_and_retains_operation_scope(tmp_path, monkeypatch):
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    sensor_capture.local_save(runtime.store, {"sensor_count": 1}, "rainfall")
    sensor_capture.local_control(runtime.store, True, "rainfall")
    snapshot = runtime.store.snapshot
    monkeypatch.setattr(runtime.store, "snapshot", lambda: (_ for _ in ()).throw(RuntimeError("private details")))
    operation = str(uuid4())
    failed = asyncio.run(runtime.reset_sensors("rainfall", operation))
    assert failed["status"] == "error" and "private" not in failed["error"]
    for call in (lambda: runtime.control_sensors(True, "rainfall"), lambda: runtime.reset_sensors("temperature", operation),
                 lambda: runtime.reset_sensors("temperature", str(uuid4())), lambda: runtime.reset_synthetic(operation)):
        with pytest.raises(HTTPException) as error:
            asyncio.run(call())
        assert error.value.status_code == 409
    assert asyncio.run(runtime.sensor_reset_status("temperature")) == {}
    assert asyncio.run(runtime.synthetic_reset_status()) == {}
    asyncio.run(runtime.control_sensors(True, "temperature"))
    monkeypatch.setattr(runtime.store, "snapshot", snapshot)
    assert asyncio.run(runtime.reset_sensors("rainfall", operation))["status"] == "completed"
    social = str(uuid4())
    asyncio.run(runtime.reset_synthetic(social))
    for scope, identifier in (("rainfall", social), ("temperature", operation)):
        with pytest.raises(HTTPException) as error:
            asyncio.run(runtime.reset_sensors(scope, identifier))
        assert error.value.status_code == 409


def test_reset_api_requires_admin_explicit_confirm_and_exact_family(tmp_path):
    client = TestClient(create_app(local_settings(tmp_path)))
    path = "/api/admin/gods-eye-view/sensors/rainfall/reset"
    operation = str(uuid4())
    assert client.get(path).status_code == 401
    assert client.post(path, json={"operation_id": operation, "confirm": True}).status_code == 401
    admin_login(client)
    for payload in ({"operation_id": operation}, {"operation_id": operation, "confirm": False}):
        assert client.post(path, json=payload).status_code == 422
    assert client.post(path.replace("rainfall", "other"), json={"operation_id": operation, "confirm": True}).status_code == 422
    result = client.post(path, json={"operation_id": operation, "confirm": True})
    assert result.status_code == 200 and result.json()["status"] == "completed"
    assert client.get(path).json() == result.json()


def cloud_runtime(monkeypatch):
    runtime = Runtime()
    runtime.documents["runtime"].update(sensor_job_key="sensor-job", streaming_mode="persistent", pipeline_revision="revision")
    for name in ("status_pipeline", "status_sensorstream"):
        runtime.documents[name] = {"status": "running" if name == "status_sensorstream" else "ready",
            "sensor_reset_version": 1, "pipeline_revision": "revision", "last_run_at": utc_text(NOW)}
    monkeypatch.setattr(sensor_reset.time, "time", lambda: NOW)
    monkeypatch.setattr(runtime, "_wake", lambda _: None)
    sensor_capture.cloud_save(runtime, {"sensor_count": 1}, "rainfall")
    return runtime


@pytest.mark.parametrize("change", [{"sensor_reset_version": 0}, {"pipeline_revision": "old"}, {"last_run_at": utc_text(NOW - 601)}, {"status": "error"}])
def test_cloud_reset_fails_closed_before_mutation_if_either_workflow_is_old(monkeypatch, change):
    runtime = cloud_runtime(monkeypatch)
    for name in ("status_pipeline", "status_sensorstream"):
        original = copy.deepcopy(runtime.documents[name])
        runtime.documents[name].update(change)
        before = copy.deepcopy(runtime.documents)
        with pytest.raises(HTTPException) as error:
            asyncio.run(runtime.reset_sensors("rainfall", str(uuid4())))
        assert error.value.status_code == 501 and runtime.documents == before
        runtime.documents[name] = original


def test_cloud_reset_retains_scope_and_does_not_claim_native_completion(monkeypatch):
    runtime = cloud_runtime(monkeypatch)
    before = sensors.family_configs(runtime.documents["configuration"]["sensors"])
    operation = str(uuid4())
    result = asyncio.run(runtime.reset_sensors("rainfall", operation))
    assert result["status"] == "pending" and result["stage"] == "waiting_for_sensor_stream"
    assert asyncio.run(runtime.synthetic_reset_status()) == {}
    assert asyncio.run(runtime.sensor_reset_status("temperature")) == {}
    after = sensors.family_configs(runtime.documents["configuration"]["sensors"])
    assert all(after[kind] == before[kind] for kind in before if kind != "rainfall")
    for scope in ("temperature", None):
        with pytest.raises(HTTPException) as error:
            asyncio.run(runtime.reset_sensors(scope, operation) if scope else runtime.reset_synthetic(operation))
        assert error.value.status_code == 409


def test_sensor_stream_stops_joins_drains_and_acknowledges_only_exact_operation(monkeypatch):
    operation = str(uuid4())
    state = {"operation_id": operation, "sensor_type": "rainfall", "status": "pending", "ready": True,
             "sensor_drained_operation_id": "older-operation", "sensor_drained_revision": "revision"}
    monkeypatch.setattr(database, "read_document", lambda *_: copy.deepcopy(state))
    def mutate(_db, _name, change):
        state.update(change(copy.deepcopy(state)))
        return copy.deepcopy(state)
    monkeypatch.setattr(database, "mutate_document", mutate)
    config = {"sensor_landing_volume_path": "/Volumes/c/prisma_ingest/landing/sensors",
              "sensor_checkpoint_volume_path": "/Volumes/c/prisma_ingest/checkpoints/sensors-v1", "pipeline_revision": "revision"}
    active, drain = MagicMock(), MagicMock()
    drain.exception.return_value = None
    drain.awaitTermination.return_value = True
    lake = SimpleNamespace(start=MagicMock(return_value=drain), restore_history=MagicMock())
    query, held = sensor_pipeline.reset_barrier(object(), lake, config, active)
    assert query is None and held
    active.stop.assert_called_once(); active.awaitTermination.assert_called_once()
    lake.start.assert_called_once_with(config["sensor_landing_volume_path"], config["sensor_checkpoint_volume_path"])
    assert state["sensor_drained_operation_id"] == operation
    lake.restore_history.assert_called_once_with()
    sensor_pipeline.reset_barrier(object(), lake, config, None)
    assert lake.start.call_count == 1
    state["status"] = "completed"
    assert sensor_pipeline.reset_barrier(object(), lake, config, None) == (None, False)


def test_native_sensor_delete_rewrites_all_publications_preserving_social_and_other_families(resetting, monkeypatch):
    log, docs, publications, lake, objects, *_ = resetting
    rows = sensors.generate_batch(NOW, sensor_count=5)
    retained = [row for row in rows if row["sensor_type"] != "rainfall"]
    def delete(kind):
        before = len(rows)
        rows[:] = [row for row in rows if row["sensor_type"] != kind]
        return before - len(rows)
    lake.sensors = SimpleNamespace(latest=lambda _: copy.deepcopy(rows), delete_family=delete)
    def replace(_db, _op, kind, old, new):
        assert synthetic_reset.prune_publication(publications[new], kind) is None
        assert all(publications[new][key] == publications[old][key] for key in ("evidence", "incidents", "event_posts"))
        publications.pop(old)
    monkeypatch.setattr(database, "replace_sensor_publication", replace)
    old = pipeline.publish_snapshot(object(), objects, lake, CONFIG, list(lake.data["silver"].values()), {}, {}, NOW)
    for key, body in sensors.text_files(rows).items():
        objects.data[CONFIG["landing_prefix"] + key] = body
    social = copy.deepcopy(lake.data["silver"])
    selected = next(row for row in rows if row["sensor_type"] == "rainfall")
    docs["reviews"]["sensor_locations"] = {selected["sensor_id"]: {"lat": 4.6, "lon": -74.1}}
    operation = str(uuid4())
    docs["checkpoint_reset"] = {"operation_id": operation, "sensor_type": "rainfall", "status": "pending", "ready": True,
        "reset_at": NOW, "sensor_drained_operation_id": "wrong", "sensor_drained_revision": "revision"}
    config = {**CONFIG, "pipeline_revision": "revision"}
    assert pipeline.process_reset(object(), objects, lake, config, NOW) is None
    assert len(rows) == 5 and old["version"] in publications
    docs["checkpoint_reset"]["sensor_drained_operation_id"] = operation
    result = pipeline.process_reset(object(), objects, lake, config, NOW)
    assert result["sensors"] == retained
    assert lake.data["silver"] == social and docs["reviews"]["sensor_locations"]
    assert docs["checkpoint_reset"]["status"] == "completed"
    assert old["version"] not in publications
    assert all(synthetic_reset.prune_publication(item, "rainfall") is None for item in publications.values())
    assert not any("/sensors/rainfall/" in key for key in objects.data)
    assert any("/sensors/temperature/" in key for key in objects.data)


def test_sensor_reset_publication_failure_retries_without_reingesting_deleted_family(resetting, monkeypatch):
    _log, docs, publications, lake, objects, *_ = resetting
    rows = sensors.generate_batch(NOW, sensor_count=5)
    def delete(kind):
        before = len(rows)
        rows[:] = [row for row in rows if row["sensor_type"] != kind]
        return before - len(rows)
    lake.sensors = SimpleNamespace(latest=lambda _: copy.deepcopy(rows), delete_family=delete)
    monkeypatch.setattr(database, "replace_sensor_publication", lambda _db, _op, _kind, old, new: publications.pop(old, None))
    old = pipeline.publish_snapshot(object(), objects, lake, CONFIG, list(lake.data["silver"].values()), {}, {}, NOW)
    pointer = objects.data["04_gold/prisma/current.json"]
    operation = str(uuid4())
    docs["simulation"] = {"status": "paused", "run_id": "active-social-scenario", "elapsed_seconds": 1, "started_at": None}
    seen = []
    visible = lake.visible
    lake.visible = lambda run_id, now: seen.append(run_id) or visible(None, now)
    docs["checkpoint_reset"] = {"operation_id": operation, "sensor_type": "rainfall", "status": "pending", "ready": True,
        "reset_at": NOW, "sensor_drained_operation_id": operation, "sensor_drained_revision": "revision"}
    config = {**CONFIG, "pipeline_revision": "revision"}
    objects.fail = "04_gold/prisma/current.json"
    with pytest.raises(RuntimeError, match="incomplete"):
        pipeline.process_reset(object(), objects, lake, config, NOW)
    assert docs["checkpoint_reset"]["status"] == "error" and objects.data["04_gold/prisma/current.json"] == pointer
    assert old["version"] in publications and all(row["sensor_type"] != "rainfall" for row in rows)
    objects.fail = None
    docs["checkpoint_reset"]["status"] = "pending"
    result = pipeline.process_reset(object(), objects, lake, config, NOW)
    assert docs["checkpoint_reset"]["status"] == "completed"
    assert json.loads(objects.data["04_gold/prisma/current.json"])["version"] == result["version"]
    assert old["version"] not in publications and seen == ["active-social-scenario", "active-social-scenario"]


def test_sensor_drain_timeout_never_acknowledges_delete(monkeypatch):
    operation = str(uuid4())
    state = {"operation_id": operation, "sensor_type": "rainfall", "status": "pending", "ready": True}
    monkeypatch.setattr(database, "read_document", lambda *_: state)
    writes = []
    monkeypatch.setattr(database, "mutate_document", lambda *args: writes.append(args))
    drain = MagicMock()
    drain.awaitTermination.return_value = False
    config = {"pipeline_revision": "revision", "sensor_landing_volume_path": "landing", "sensor_checkpoint_volume_path": "checkpoint"}
    with pytest.raises(RuntimeError, match="drained"):
        sensor_pipeline.reset_barrier(object(), SimpleNamespace(start=lambda *_: drain, restore_history=lambda: None), config, None)
    assert writes == [] and not state.get("sensor_drained_operation_id")
    drain.stop.assert_called_once()


@pytest.mark.parametrize("latest_status,foreign", [("completed", False), ("pending", True)])
def test_late_publication_error_cannot_downgrade_completion_or_a_new_operation(monkeypatch, latest_status, foreign):
    operation = str(uuid4())
    state = {"operation_id": operation, "sensor_type": "rainfall", "status": "pending", "sensor_drained_operation_id": operation}
    original = copy.deepcopy(state)
    def mutate(_db, _name, change):
        state.update(change(copy.deepcopy(state)))
        return state
    monkeypatch.setattr(database, "mutate_document", mutate)
    def fail(*_):
        state.update(status=latest_status, operation_id=str(uuid4()) if foreign else operation)
        raise RuntimeError("ambiguous response after commit")
    monkeypatch.setattr(sensor_reset, "clean_landing", fail)
    with pytest.raises(RuntimeError, match="incomplete"):
        sensor_reset.execute(object(), None, None, {}, NOW, original, None)
    assert state["status"] == latest_status and "error" not in state
    assert (state["operation_id"] != operation) is foreign


def test_sensor_stream_late_failure_preserves_a_completed_reset(monkeypatch):
    from app.gods_eye_view import landing
    operation = str(uuid4())
    stale = {"operation_id": operation, "sensor_type": "rainfall", "status": "pending", "ready": False}
    completed = {**stale, "status": "completed", "version": "gold-clean"}
    checkpoint_writes = []
    monkeypatch.setattr(database, "read_document", lambda *_: stale)
    monkeypatch.setattr(database, "sensor_reset_version", lambda _: 2)
    def mutate(_db, name, change):
        result = change(copy.deepcopy(completed) if name == "checkpoint_reset" else {})
        if name == "checkpoint_reset":
            checkpoint_writes.append(result)
        return result
    monkeypatch.setattr(database, "mutate_document", mutate)
    monkeypatch.setattr(landing, "ensure_volumes", lambda *_: None)
    query = MagicMock()
    query.exception.side_effect = RuntimeError("late stream failure")
    with pytest.raises(RuntimeError, match="checkpoint retained"):
        sensor_pipeline.run(None, None, {"sensor_landing_volume_path": "landing", "sensor_checkpoint_volume_path": "checkpoint"},
            connection=object(), lake=SimpleNamespace(start=lambda *_a, **_kw: query, restore_history=lambda: None), clock=lambda: NOW)
    assert checkpoint_writes == [completed]


def test_native_bundle_import_does_not_require_backend_fastapi():
    code = """import builtins
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'fastapi' or name.startswith('fastapi.'): raise ImportError('API-only dependency')
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
from app.gods_eye_view.sensor_reset import execute
from app.gods_eye_view.pipeline import process_reset
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
