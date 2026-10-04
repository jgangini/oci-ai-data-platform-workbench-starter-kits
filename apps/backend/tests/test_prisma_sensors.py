import asyncio
import json
from collections import Counter
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

from app.main import create_app
from app.prisma import sensor_capture, sensors, scheduling
from app.prisma.store import PrismaStore
from test_prisma_access import admin_login, local_settings
from test_prisma_cloud import Runtime


NOW = datetime.fromisoformat("2026-10-03T18:00:00+00:00").timestamp()


def test_four_thousand_readings_are_reproducible_diverse_and_explicitly_simulated():
    rows = sensors.generate_batch(NOW)
    assert rows == sensors.generate_batch(NOW)
    assert len({r["sensor_id"] for r in rows}) == len({r["event_id"] for r in rows}) == 4000
    assert Counter(r["sensor_type"] for r in rows) == {kind: 800 for kind in sensors.SENSOR_TYPES}
    assert len({r["department"] for r in rows}) == 33
    assert len({r["observed_at"] for r in rows}) > 100
    files = sensors.text_files(rows)
    assert len(files) == 5 and all(key.startswith("sensors/") and key.endswith(".txt") for key in files)
    decoded = [json.loads(line) for content in files.values() for line in content.decode().splitlines()]
    assert sorted(decoded, key=lambda r: r["event_id"]) == sorted(rows, key=lambda r: r["event_id"])
    later = {r["sensor_id"]: r for r in sensors.generate_batch(NOW + 300)}
    for row in rows:
        following = later[row["sensor_id"]]
        assert (row["lat"], row["lon"]) == (following["lat"], following["lon"])
        assert row["event_id"] != following["event_id"]
        assert row["is_simulated"] is True and row["mode"] == "Synthetic"


@pytest.mark.parametrize("change", [{"is_simulated": False}, {"mode": "real"}, {"value": float("nan")},
    {"lat": True}, {"lon": 2}, {"sensor_id": "../escape"}, {"sensor_type": []}, {"unit": "unknown"},
    {"event_date": "2020-01-01"}, {"observed_at": "2026-10-03T12:00:00-05:00"}, {"observed_at": "2026-10-03"}])
def test_rejects_invalid_sensor_measurements(change):
    with pytest.raises(ValueError):
        sensors.validate_record({**sensors.generate_batch(NOW, sensor_count=100)[0], **change})


def test_local_txt_capture_pause_restart_history_and_snapshot_version(tmp_path):
    clock = [NOW]
    store = PrismaStore(tmp_path / "prisma.db", clock=lambda: clock[0])
    before = store.snapshot()["version"]
    sensor_capture.local_save(store, {"expected_revision": 1, "sensor_count": 100})
    assert sensor_capture.local_tick(store) == 0
    assert sensor_capture.local_control(store, True)["last_received_count"] == 100
    delivered = list((tmp_path / "prisma-landing" / "sensors").rglob("*.txt"))
    assert len(delivered) == 5
    snapshot = store.snapshot()
    assert len(snapshot["sensors"]) == 100 and snapshot["version"] != before
    assert sensor_capture.local_tick(store, force=True) == 0
    assert store.snapshot()["version"] == snapshot["version"]
    sensor_capture.local_control(store, False)
    assert sensor_capture.local_control(store, True)["next_due"]
    sensor_capture.local_control(store, False)
    clock[0] += 300
    assert sensor_capture.local_tick(store) == 0
    restarted = PrismaStore(store.path, clock=lambda: clock[0])
    sensor_capture.local_control(restarted, True)
    assert len(sensor_capture.local_latest(restarted)) == 100
    with restarted.connection() as db:
        assert db.execute("SELECT count(*) FROM sensor_events").fetchone()[0] == 200
    clock[0] += 86401
    assert sensor_capture.local_latest(restarted) == []


def test_sensor_location_survives_restart_reading_refresh_and_future_txt(tmp_path):
    clock = [NOW]
    store = PrismaStore(tmp_path / "prisma.db", clock=lambda: clock[0])
    sensor_capture.local_save(store, {"sensor_count": 100})
    sensor_capture.local_control(store, True)
    initial = store.snapshot()
    row = initial["sensors"][0]
    sensor_id = row["sensor_id"]
    values = {"lat": row["lat"] + .001, "lon": row["lon"] + .001,
              "expected_lat": row["lat"], "expected_lon": row["lon"]}
    # Reading identity changes every capture; only a competing coordinate edit conflicts.
    clock[0] += 300
    sensor_capture.local_tick(store)
    assert store.snapshot()["sensors"][0]["id"] != row["id"]
    result = sensor_capture.local_location(store, sensor_id, values)
    assert result["location_saved"] and not result["location_pending_publication"]
    with pytest.raises(HTTPException) as conflict:
        sensor_capture.local_location(store, sensor_id, values)
    assert conflict.value.status_code == 409
    restored = PrismaStore(store.path, clock=lambda: clock[0])
    updated = restored.snapshot()
    assert updated["version"] != initial["version"]
    assert updated["sensors"][0]["lat"] == values["lat"]
    assert updated["sensors"][0]["lon"] == values["lon"]
    clock[0] += 300
    sensor_capture.local_tick(restored)
    delivered = [json.loads(line) for path in (tmp_path / "prisma-landing").rglob(f"sim-{int(clock[0])}-0.txt")
                 for line in path.read_text(encoding="utf-8").splitlines()]
    moved = next(item for item in delivered if item["sensor_id"] == sensor_id)
    assert (moved["lat"], moved["lon"]) == (values["lat"], values["lon"])


def test_cloud_partial_delivery_retries_same_objects_before_advancing_checkpoint(monkeypatch):
    runtime = Runtime()
    runtime.documents["runtime"].update(namespace="namespace", bucket="gold", landing_bucket="landing")
    runtime.documents["configuration"] = {"sensors": {"sensor_count": 100, "capture_running": True}}
    objects, attempts = {}, []
    def put(namespace, bucket, key, content, **kwargs):
        assert bucket == "landing"
        attempts.append(key)
        if len(attempts) == 3:
            raise RuntimeError("storage unavailable")
        objects[key] = content
    runtime.client.object_storage = SimpleNamespace(put_object=put)
    monkeypatch.setattr(sensor_capture.time, "time", lambda: NOW)
    with pytest.raises(RuntimeError):
        sensor_capture.cloud_tick(runtime)
    assert runtime.documents["checkpoint_sensors"]["pending"]["anchor"] == NOW and len(objects) == 2
    assert "anchor" not in runtime.documents["checkpoint_sensors"]
    assert runtime.documents["status_sensors"]["last_error"] == "RuntimeError"
    original = dict(objects)
    station = sensors.generate_batch(NOW, sensor_count=100)[0]
    runtime.documents["reviews"] = {"sensor_locations": {station["sensor_id"]: {"lat": 4.6, "lon": -74.1}}}
    assert sensor_capture.cloud_tick(runtime) == 100
    assert all(objects[key] == content for key, content in original.items())
    decoded = [json.loads(line) for content in objects.values() for line in content.decode().splitlines()]
    assert next(row for row in decoded if row["sensor_id"] == station["sensor_id"])["lat"] == station["lat"]
    assert len(objects) == 5 and attempts[0] == attempts[3]
    assert runtime.documents["status_sensors"]["last_error"] is None
    assert sensor_capture.cloud_tick(runtime, force=True) == 0
    assert scheduling.needs_schedule(runtime.documents["configuration"], {})
    runtime.documents["configuration"]["sensors"]["capture_running"] = False
    assert not scheduling.needs_schedule(runtime.documents["configuration"], {})


def test_cloud_sensor_location_is_durable_until_publication_and_used_by_next_capture(monkeypatch):
    runtime = Runtime()
    sensor = {**sensors.generate_batch(NOW, sensor_count=100)[0], "id": "initial-reading"}
    monkeypatch.setattr(runtime, "_snapshot", lambda: {"version": "gold-original", "sensors": [sensor]})
    values = {"lat": 4.6, "lon": -74.1, "expected_lat": sensor["lat"], "expected_lon": sensor["lon"]}
    saved = asyncio.run(runtime.sensor_location(sensor["sensor_id"], values))
    assert saved["location_saved"] and saved["location_pending_publication"]
    assert runtime._snapshot()["version"] == "gold-original"
    with pytest.raises(HTTPException) as conflict:
        asyncio.run(runtime.sensor_location(sensor["sensor_id"], values))
    assert conflict.value.status_code == 409
    runtime.documents["runtime"].update(namespace="namespace", bucket="gold", landing_bucket="landing")
    runtime.documents["configuration"] = {"sensors": {"sensor_count": 100, "capture_running": True}}
    delivered = []
    runtime.client.object_storage = SimpleNamespace(put_object=lambda _ns, _bucket, _key, body, **_: delivered.extend(json.loads(line) for line in body.decode().splitlines()))
    monkeypatch.setattr(sensor_capture.time, "time", lambda: NOW)
    assert sensor_capture.cloud_tick(runtime) == 100
    moved = next(row for row in delivered if row["sensor_id"] == sensor["sensor_id"])
    assert (moved["lat"], moved["lon"]) == (4.6, -74.1)


def test_cloud_invalid_families_are_validation_errors_not_service_failures():
    runtime = Runtime()
    with pytest.raises(HTTPException) as error:
        asyncio.run(runtime.update_sensors({"expected_revision": 1, "families": ["rainfall", "rainfall"]}))
    assert error.value.status_code == 422
    assert "configuration" not in runtime.documents


def test_independent_streams_reuse_active_social_and_restart_failed_sensor_run():
    runtime = Runtime()
    runtime.documents["runtime"].update(streaming_mode="persistent", sensor_job_key="sensors-job")
    runtime.client.run_pages[None] = ({"items": [
        {"key": "social-run", "jobKey": "job", "state": {"status": "RUNNING"}},
        {"key": "sensor-run", "jobKey": "sensors-job", "state": {"status": "FAILED"}}]}, {})
    runtime._wake("resume")
    submitted = [entry[2]["payload"] for entry in runtime.client.calls if entry[0] == "POST"]
    assert submitted == [{"jobKey": "sensors-job", "parameters": [], "queue": {"isEnabled": False}}]
    assert not any(entry[0] == "PUT" for entry in runtime.client.calls)
    # No source must be Running to keep these two explicitly permanent streams alive.
    assert "configuration" not in runtime.documents


def test_sensor_capture_cannot_claim_running_before_independent_workflow_is_installed():
    runtime = Runtime()
    with pytest.raises(HTTPException) as error:
        asyncio.run(runtime.control_sensors(True))
    assert error.value.status_code == 409 and "configuration" not in runtime.documents


def test_one_failed_workflow_does_not_prevent_starting_the_other():
    calls = []
    def request(method, path, **kwargs):
        job = kwargs.get("params", kwargs.get("payload", {}))["jobKey"]
        calls.append(job)
        if job == "social":
            raise RuntimeError("social unavailable")
        return ({"items": []}, {}) if method == "GET" else {"key": "started"}
    with pytest.raises(RuntimeError):
        scheduling.keep_streams_running(request, {"streaming_mode": "persistent", "workspace_key": "workspace",
            "job_key": "social", "sensor_job_key": "sensors"}, "tick")
    assert calls == ["social", "sensors", "sensors"]


def test_failed_sensor_delivery_does_not_skip_social_capture(monkeypatch):
    runtime, captured = Runtime(), []
    def fail(*_):
        raise RuntimeError("sensor storage failure")
    monkeypatch.setattr(sensor_capture, "cloud_tick", fail)
    monkeypatch.setattr(runtime, "_produce", lambda: captured.append(True))
    with pytest.raises(HTTPException):
        asyncio.run(runtime.tick())
    assert captured == [True]


def test_admin_sensor_permissions_revision_and_run_pause(tmp_path):
    client = TestClient(create_app(local_settings(tmp_path)))
    url = "/api/admin/prisma/sensors"
    assert client.get(url).status_code == client.post(url + "/run").status_code == 401
    admin_login(client)
    original = client.get(url).json()["config"]
    assert not original["capture_running"] and original["sensor_count"] == 4000
    saved = client.put(url, json={"expected_revision": 1, "sensor_count": 100})
    assert saved.status_code == 200 and saved.json()["config"]["config_version"] == 2
    assert client.put(url, json={"expected_revision": 1, "sensor_count": 200}).status_code == 409
    for value in ({"mode": "real"}, {"sensor_count": 5001}, {"sensor_count": True}, {"families": ["camera"]}):
        assert client.put(url, json={"expected_revision": 2, **value}).status_code == 422
    assert client.post(url + "/run").json()["config"]["capture_running"]
    assert len(client.get("/api/prisma/snapshot").json()["sensors"]) == 100
    assert not client.post(url + "/pause").json()["config"]["capture_running"]
    assert client.get(url).json()["config"]["last_received_count"] == 100


def test_sensor_coordinate_endpoint_is_admin_only_and_rejects_invalid_or_stale_edits(tmp_path):
    app = create_app(local_settings(tmp_path))
    client = TestClient(app)
    payload = {"lat": 4.6, "lon": -74.1, "expected_lat": 4.5, "expected_lon": -74.0}
    assert client.post("/api/prisma/sensors/unknown/location", json=payload).status_code == 401
    admin_login(client)
    assert client.post("/api/prisma/sensors/unknown/location", json=payload).status_code == 404
    client.put("/api/admin/prisma/sensors", json={"expected_revision": 1, "sensor_count": 100})
    client.post("/api/admin/prisma/sensors/run")
    row = client.get("/api/prisma/snapshot").json()["sensors"][0]
    url = f"/api/prisma/sensors/{row['sensor_id']}/location"
    payload.update(expected_lat=row["lat"], expected_lon=row["lon"])
    for field, value in (("lat", True), ("lat", "4.6"), ("lon", -82), ("expected_lat", None), ("extra", 1)):
        assert client.post(url, json={**payload, field: value}).status_code == 422
    assert client.post(url, json={key: value for key, value in payload.items() if key != "expected_lon"}).status_code == 422
    saved = client.post(url, json=payload)
    assert saved.status_code == 200 and saved.json()["location_saved"]
    assert client.post(url, json=payload).status_code == 409
