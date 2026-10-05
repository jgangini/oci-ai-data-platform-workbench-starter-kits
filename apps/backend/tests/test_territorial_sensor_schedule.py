"""Global sensor slots align families without rewriting committed TXT deliveries."""
import copy
import json
from types import SimpleNamespace

import pytest

from app.territorial import capture, sensor_capture, sensors
from app.territorial.core import utc_text
from app.territorial.store import TerritorialStore
from test_territorial_cloud import Runtime
from test_territorial_sensors import NOW


def plan(start, minutes=5):
    return capture.update_schedule({}, {"expected_revision": 1, "start_at": utc_text(start), "interval_minutes": minutes})


@pytest.mark.parametrize("paused", [False, True])
def test_legacy_family_projection_reports_actual_due_without_changing_capture(monkeypatch, paused):
    runtime, clock, delivered = Runtime(), [NOW], {}
    kinds = tuple(sensors.SENSOR_TYPES)
    config = {"by_type": {kind: {"capture_running": not (paused and index == 0),
        "sensor_count": 1, "interval_minutes": 5} for index, kind in enumerate(kinds)}}
    status = {"by_type": {kind: {"last_run_at": utc_text(NOW - index), "next_due": utc_text(NOW + 60 * (index + 1))}
                         for index, kind in enumerate(kinds)}}
    runtime.documents.update(configuration={"sensors": config}, status_sensors=status,
        checkpoint_sensors={"by_type": {kind: {"anchor": NOW - 300 + 60 * (index + 1),
            "next_due": NOW + 60 * (index + 1)} for index, kind in enumerate(kinds)}})
    monkeypatch.setattr(sensor_capture.time, "time", lambda: clock[0])
    monkeypatch.setattr(sensor_capture, "_cloud_deliver", lambda _runtime, rows: delivered.update(sensors.text_files(rows)))
    original = copy.deepcopy(runtime.documents)
    view = sensor_capture.cloud_configuration(runtime)
    assert view["sensor_schedule"]["start_at"] is None
    assert view["next_due"] == utc_text(NOW + (120 if paused else 60))
    assert view["last_run_at"] == utc_text(NOW)
    assert view["configs"] == list(sensors.family_configs(config, status).values())
    assert runtime.documents == original
    assert sensor_capture.cloud_tick(runtime) == 0 and delivered == {}
    assert runtime.documents == original

    clock[0] = NOW + 301
    assert sensor_capture.cloud_tick(runtime) == len(kinds) - int(paused)
    expected = {}
    for kind in kinds[int(paused):]:
        expected.update(sensors.text_files(sensors.generate_batch(clock[0], sensor_count=1, families=[kind])))
    assert delivered == expected


@pytest.mark.parametrize("force", [False, True])
def test_future_schedule_blocks_new_sensor_batches_but_preserves_committed_pending(force):
    config = sensors.configuration({"capture_running": True, "sensor_count": 100})
    schedule = plan(NOW + 600)
    assert sensor_capture._batch(config, NOW, {}, force, {}, schedule) is None
    pending = {"anchor": NOW - 30, "sensor_count": 100, "families": list(sensors.SENSOR_TYPES),
               "interval_minutes": 5, "locations": {}}
    assert sensor_capture._batch(config, NOW, {"pending": pending}, force, {}, schedule) is pending
    assert sensor_capture._batch({**config, "capture_running": False}, NOW, {"pending": pending}, force, {}, schedule) is None


def test_local_sensor_families_share_slots_and_force_cannot_repeat_them(tmp_path):
    clock = [NOW]
    store = TerritorialStore(tmp_path / "territorial.db", clock=lambda: clock[0])
    for index, kind in enumerate(sensors.SENSOR_TYPES):
        sensor_capture.local_save(store, {"sensor_count": 1, "interval_minutes": index + 1}, kind)
    start = NOW + 120
    with store.connection() as db:
        store._put(db, "sensor_schedule", plan(start, 3))
    for kind in sensors.SENSOR_TYPES:
        configured = sensor_capture.local_control(store, True, kind)
        assert configured["next_due"] == utc_text(start) and configured["last_run_at"] is None
        assert configured["interval_minutes"] == 3
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM sensor_events").fetchone()[0] == 0
    assert not (tmp_path / "prisma-landing").exists()

    clock[0] = start + 17
    assert sensor_capture.local_tick(store) == 5
    delivered = {p.relative_to(tmp_path / "prisma-landing").as_posix(): p.read_bytes()
                 for p in (tmp_path / "prisma-landing").rglob("*.txt")}
    assert set(delivered) == {f"sensors/{kind}/sim-{int(start)}-0.txt" for kind in sensors.SENSOR_TYPES}
    assert sensor_capture.local_tick(store, force=True) == 0
    assert {row["next_due"] for row in sensor_capture.local_configuration(store)["configs"]} == {utc_text(start + 180)}

    # Pause/Run and an old per-family interval edit cannot bypass the global slot.
    sensor_capture.local_control(store, False, "rainfall")
    sensor_capture.local_save(store, {"interval_minutes": 1}, "rainfall")
    resumed = sensor_capture.local_control(store, True, "rainfall")
    assert resumed["next_due"] == utc_text(start + 180) and resumed["interval_minutes"] == 3
    clock[0] = start + 189
    assert sensor_capture.local_tick(store) == 5
    assert all((tmp_path / "prisma-landing" / path).read_bytes() == content for path, content in delivered.items())
    with store.connection() as db:
        rows = [json.loads(row[0]) for row in db.execute("SELECT payload FROM sensor_events")]
    assert {row["batch_id"] for row in rows} == {f"sim-{int(start)}-0", f"sim-{int(start + 180)}-0"}
    assert len(rows) == 10


def test_cloud_global_schedule_aligns_legacy_all_family_delivery_and_recovers_after_restart(monkeypatch):
    runtime, clock, objects = Runtime(), [NOW], {}
    runtime.documents["runtime"].update(namespace="namespace", bucket="bucket")
    start = NOW + 120
    runtime.documents["configuration"] = {"sensors": {"sensor_count": 100}, "sensor_schedule": plan(start, 3)}
    runtime.client.object_storage = SimpleNamespace(put_object=lambda _ns, _bucket, key, body, **_: objects.__setitem__(key, body))
    monkeypatch.setattr(sensor_capture.time, "time", lambda: clock[0])
    configured = sensor_capture.cloud_control(runtime, True)
    assert configured["next_due"] == utc_text(start) and objects == {}
    clock[0] = start + 11
    assert sensor_capture.cloud_tick(runtime, force=True) == 100
    assert len(objects) == 5 and all(f"sim-{int(start)}-0.txt" in key for key in objects)
    original = copy.deepcopy(objects)
    restarted = Runtime()
    restarted.documents = copy.deepcopy(runtime.documents)
    restarted.client.object_storage = runtime.client.object_storage
    assert sensor_capture.cloud_tick(restarted, force=True) == 0
    clock[0] = start + 195
    assert sensor_capture.cloud_tick(restarted) == 100
    assert len(objects) == 10 and all(objects[key] == value for key, value in original.items())
    assert restarted.documents["checkpoint_sensors"]["next_due"] == start + 360


def test_cloud_schedule_change_preserves_pending_family_bytes_and_next_due(monkeypatch):
    runtime, clock, objects = Runtime(), [NOW], {}
    runtime.documents["runtime"].update(namespace="namespace", bucket="bucket")
    monkeypatch.setattr(sensor_capture.time, "time", lambda: clock[0])
    for kind in ("rainfall", "temperature"):
        sensor_capture.cloud_save(runtime, {"sensor_count": 1}, kind)
    runtime.documents["configuration"]["sensor_schedule"] = plan(NOW, 3)

    def fail_rain(_ns, _bucket, key, body, **_):
        if "/rainfall/" in key:
            raise RuntimeError("Landing unavailable")
        objects[key] = body

    runtime.client.object_storage = SimpleNamespace(put_object=fail_rain)
    with pytest.raises(RuntimeError, match="Landing unavailable"):
        sensor_capture.cloud_control(runtime, True, "rainfall")
    sensor_capture.cloud_control(runtime, True, "temperature")
    pending = copy.deepcopy(runtime.documents["checkpoint_sensors"]["by_type"]["rainfall"]["pending"])
    sibling_bytes = copy.deepcopy(objects)
    future = NOW + 600
    runtime.documents["configuration"]["sensor_schedule"] = plan(future, 7)
    runtime.client.object_storage.put_object = lambda _ns, _bucket, key, body, **_: objects.__setitem__(key, body)
    clock[0] += 30
    assert sensor_capture.cloud_tick(runtime, force=True) == 1
    expected = sensors.text_files(sensor_capture._family_rows(pending, "rainfall"))
    assert all(objects["01_landing/prisma/raw/" + key] == body for key, body in expected.items())
    assert all(objects[key] == body for key, body in sibling_bytes.items())
    assert runtime.documents["checkpoint_sensors"]["by_type"]["rainfall"]["next_due"] == future
    assert sensor_capture.cloud_tick(runtime, force=True) == 0
    active = [row for row in sensor_capture.cloud_configuration(runtime)["configs"] if row["capture_running"]]
    assert {row["next_due"] for row in active} == {utc_text(future)}
    clock[0] = future + 13
    assert sensor_capture.cloud_tick(runtime) == 2
    assert all(f"01_landing/prisma/raw/sensors/{kind}/sim-{int(future)}-0.txt" in objects for kind in ("rainfall", "temperature"))


def test_migrated_family_status_respects_legacy_consumed_global_slot(tmp_path):
    store = TerritorialStore(tmp_path / "territorial.db", clock=lambda: NOW)
    sensor_capture.local_save(store, {"sensor_count": 100})
    with store.connection() as db:
        store._put(db, "sensor_schedule", plan(NOW))
    sensor_capture.local_control(store, True)
    old_file = tmp_path / "prisma-landing" / "sensors" / "temperature" / f"sim-{int(NOW)}-0.txt"
    old_bytes = old_file.read_bytes()
    sensor_capture.local_save(store, {"families": ["rainfall"]})
    started = sensor_capture.local_control(store, True, "temperature")
    assert started["next_due"] == utc_text(NOW + 300)
    assert sensor_capture.local_tick(store, force=True, sensor_type="temperature") == 0
    assert old_file.read_bytes() == old_bytes
