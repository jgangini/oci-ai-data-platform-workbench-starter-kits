"""Independent sensor controls preserve legacy identities and immutable pending TXT."""
import copy
import json
from collections import Counter
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.prisma import sensor_capture, sensors
from app.prisma.core import utc_text
from app.prisma.store import PrismaStore
from test_prisma_cloud import Runtime
from test_prisma_sensors import NOW


def local_configs(store):
    with store.connection() as db:
        return sensors.family_configs(store._get(db, "configuration_sensors", {}), store._get(db, "status_sensors", {}))


def cloud_configs(runtime):
    return sensors.family_configs(runtime._doc("configuration").get("sensors", {}), runtime._doc("status_sensors"))


def test_legacy_totals_are_distributed_in_original_family_order_without_creating_stations():
    defaults = sensors.family_configs()
    assert {kind: config["sensor_count"] for kind, config in defaults.items()} == {kind: 800 for kind in sensors.SENSOR_TYPES}
    legacy = {"sensor_count": 101, "families": ["rainfall", "temperature", "river_level"],
              "interval_minutes": 7, "capture_running": True, "config_version": 9}
    migrated = sensors.family_configs(legacy)
    assert [migrated[kind]["sensor_count"] for kind in legacy["families"]] == [34, 34, 33]
    assert all(migrated[kind]["capture_running"] and migrated[kind]["interval_minutes"] == 7 for kind in legacy["families"])
    assert all(not config["capture_running"] for kind, config in migrated.items() if kind not in legacy["families"])
    assert [sensors.family_configs({"sensor_count": 100})[kind]["sensor_count"] for kind in sensors.SENSOR_TYPES] == [20] * 5


def test_single_family_generation_preserves_existing_sensor_ids_and_coordinates_and_allows_one():
    legacy = sensors.generate_batch(NOW, sensor_count=100)
    for kind in sensors.SENSOR_TYPES:
        previous = {row["sensor_id"]: (row["lat"], row["lon"]) for row in legacy if row["sensor_type"] == kind}
        current = sensors.generate_batch(NOW + 300, sensor_count=20, families=[kind])
        assert {row["sensor_id"]: (row["lat"], row["lon"]) for row in current} == previous
        assert len(sensors.generate_batch(NOW, sensor_count=1, families=[kind])) == 1


def test_family_save_run_pause_and_due_times_do_not_change_siblings(tmp_path):
    clock = [NOW]
    store = PrismaStore(tmp_path / "prisma.db", clock=lambda: clock[0])
    original = local_configs(store)
    saved = sensor_capture.local_save(store, {"expected_revision": original["rainfall"]["config_version"],
        "sensor_count": 2, "interval_minutes": 1}, sensor_type="rainfall")
    assert saved["sensor_count"] == 2
    assert local_configs(store)["temperature"] == original["temperature"]
    sensor_capture.local_save(store, {"expected_revision": original["temperature"]["config_version"],
        "sensor_count": 3, "interval_minutes": 10}, sensor_type="temperature")
    with pytest.raises(HTTPException) as conflict:
        sensor_capture.local_save(store, {"expected_revision": original["rainfall"]["config_version"], "sensor_count": 4}, sensor_type="rainfall")
    assert conflict.value.status_code == 409
    sensor_capture.local_control(store, True, sensor_type="rainfall")
    rainfall = copy.deepcopy(local_configs(store)["rainfall"])
    clock[0] += 30
    sensor_capture.local_control(store, True, sensor_type="temperature")
    assert local_configs(store)["rainfall"] == rainfall, "Run now must only force its selected family"
    temperature = copy.deepcopy(local_configs(store)["temperature"])
    sensor_capture.local_control(store, False, sensor_type="rainfall")
    assert local_configs(store)["temperature"] == temperature
    clock[0] = NOW + 60
    assert sensor_capture.local_tick(store) == 0
    clock[0] = NOW + 70
    sensor_capture.local_control(store, True, sensor_type="rainfall")
    assert local_configs(store)["temperature"] == temperature
    with store.connection() as db:
        counts = Counter(json.loads(row[0])["sensor_type"] for row in db.execute("SELECT payload FROM sensor_events"))
    assert counts == {"rainfall": 4, "temperature": 3}


def test_active_family_capacity_is_checked_without_changing_sibling_state(tmp_path):
    store = PrismaStore(tmp_path / "prisma.db", clock=lambda: NOW)
    sensor_capture.local_save(store, {"sensor_count": 5000}, sensor_type="rainfall")
    sensor_capture.local_control(store, True, sensor_type="rainfall")
    before = local_configs(store)
    with pytest.raises((ValueError, HTTPException)):
        sensor_capture.local_control(store, True, sensor_type="temperature")
    assert local_configs(store) == before
    sensor_capture.local_control(store, False, sensor_type="rainfall")
    assert sensor_capture.local_control(store, True, sensor_type="temperature")["capture_running"]


def test_previously_excluded_family_starts_without_inherited_capture_or_due_time(tmp_path):
    clock = [NOW]
    store = PrismaStore(tmp_path / "prisma.db", clock=lambda: clock[0])
    sensor_capture.local_save(store, {"sensor_count": 100, "families": ["rainfall"], "interval_minutes": 60})
    sensor_capture.local_control(store, True)
    rainfall = copy.deepcopy(local_configs(store)["rainfall"])
    temperature = sensor_capture.local_save(store, {"sensor_count": 1}, sensor_type="temperature")
    assert not temperature["capture_running"]
    assert temperature["last_run_at"] is None and temperature["next_due"] is None
    with store.connection() as db:
        assert store._get(db, "checkpoint_sensors", {})["by_type"]["temperature"] == {}
    clock[0] += 1
    started = sensor_capture.local_control(store, True, sensor_type="temperature")
    assert started["last_received_count"] == 1
    assert started["last_run_at"] == utc_text(clock[0])
    assert local_configs(store)["rainfall"] == rainfall


@pytest.mark.parametrize("values", [{"sensor_count": 0}, {"sensor_count": 5001}, {"sensor_count": True},
    {"interval_minutes": 0}, {"interval_minutes": 61}, {"interval_minutes": False}, {"families": ["temperature"]}])
def test_invalid_family_save_does_not_migrate_or_write_runtime_documents(values):
    runtime = Runtime()
    before = copy.deepcopy(runtime.documents)
    with pytest.raises(ValueError):
        sensor_capture.cloud_save(runtime, values, sensor_type="rainfall")
    assert runtime.documents == before


def test_cloud_failed_family_does_not_skip_other_due_family_and_retries_same_batch(monkeypatch):
    runtime, clock, objects = Runtime(), [NOW], {}
    runtime.documents["runtime"].update(namespace="namespace", bucket="bucket")
    runtime.documents["configuration"] = {"sensors": {"sensor_count": 100}}
    runtime.client.object_storage = SimpleNamespace(put_object=lambda _ns, _bucket, key, content, **_: objects.__setitem__(key, content))
    monkeypatch.setattr(sensor_capture.time, "time", lambda: clock[0])
    for kind in ("rainfall", "temperature"):
        sensor_capture.cloud_save(runtime, {"sensor_count": 2, "interval_minutes": 1}, sensor_type=kind)
        sensor_capture.cloud_control(runtime, True, sensor_type=kind)
    clock[0] += 60
    def fail_rain(_namespace, _bucket, key, content, **_kwargs):
        if "/rainfall/" in key:
            raise RuntimeError("rainfall storage unavailable")
        objects[key] = content
    runtime.client.object_storage.put_object = fail_rain
    with pytest.raises(RuntimeError):
        sensor_capture.cloud_tick(runtime)
    current = cloud_configs(runtime)
    assert current["rainfall"]["last_error"]
    assert current["temperature"]["last_run_at"] == utc_text(clock[0])
    assert any(f"temperature/sim-{int(clock[0])}-0.txt" in key for key in objects)
    temperature = copy.deepcopy(current["temperature"])
    runtime.client.object_storage.put_object = lambda _ns, _bucket, key, content, **_: objects.__setitem__(key, content)
    clock[0] += 30
    sensor_capture.cloud_tick(runtime)
    assert cloud_configs(runtime)["temperature"] == temperature
    assert any(f"rainfall/sim-{int(NOW + 60)}-0.txt" in key for key in objects)
    assert not cloud_configs(runtime)["rainfall"]["last_error"]


def test_legacy_partial_delivery_keeps_exact_txt_bytes_when_family_controls_migrate(monkeypatch):
    runtime, objects, attempts, clock = Runtime(), {}, [], [NOW]
    runtime.documents["runtime"].update(namespace="namespace", bucket="bucket")
    runtime.documents["configuration"] = {"sensors": {"sensor_count": 100, "capture_running": True}}
    expected = {"01_landing/prisma/raw/" + key: content for key, content in sensors.text_files(sensors.generate_batch(NOW, sensor_count=100)).items()}
    def put(_namespace, _bucket, key, content, **_kwargs):
        attempts.append(key)
        if len(attempts) == 3:
            raise RuntimeError("storage unavailable")
        objects[key] = content
    runtime.client.object_storage = SimpleNamespace(put_object=put)
    monkeypatch.setattr(sensor_capture.time, "time", lambda: clock[0])
    with pytest.raises(RuntimeError):
        sensor_capture.cloud_tick(runtime)
    partial = dict(objects)
    restarted = Runtime()
    restarted.documents = copy.deepcopy(runtime.documents)
    restarted.client.object_storage = SimpleNamespace(put_object=put)
    rainfall = cloud_configs(restarted)["rainfall"]
    sensor_capture.cloud_save(restarted, {"expected_revision": rainfall["config_version"], "sensor_count": 1,
        "interval_minutes": 1}, sensor_type="rainfall")
    clock[0] += 300
    sensor_capture.cloud_tick(restarted)
    assert objects == expected, "Migrated pending families must replay the old globally shuffled TXT bytes"
    assert all(objects[key] == content for key, content in partial.items())


def test_interrupted_cloud_migration_rebuilds_from_latest_legacy_checkpoint(monkeypatch):
    runtime, clock = Runtime(), [NOW]
    runtime.documents["runtime"].update(namespace="namespace", bucket="bucket")
    runtime.documents["configuration"] = {"sensors": {"sensor_count": 100, "families": ["rainfall"], "capture_running": True}}
    runtime.client.object_storage = SimpleNamespace(put_object=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sensor_capture.time, "time", lambda: clock[0])
    assert sensor_capture.cloud_tick(runtime) == 100
    change = runtime._change
    def fail_commit(name, mutate):
        if name == "configuration":
            raise RuntimeError("configuration temporarily unavailable")
        return change(name, mutate)
    monkeypatch.setattr(runtime, "_change", fail_commit)
    with pytest.raises(RuntimeError):
        sensor_capture.cloud_save(runtime, {"sensor_count": 1}, sensor_type="temperature")
    assert "by_type" not in runtime.documents["configuration"]["sensors"]
    monkeypatch.setattr(runtime, "_change", change)
    clock[0] += 300
    assert sensor_capture.cloud_tick(runtime) == 100, "Legacy scheduling stays authoritative until migration commits"
    readback = {config["sensor_type"]: config for config in sensor_capture.cloud_configuration(runtime)["configs"]}
    assert readback["rainfall"]["last_run_at"] == utc_text(clock[0])
    sensor_capture.cloud_save(runtime, {"sensor_count": 1}, sensor_type="temperature")
    checkpoint = runtime.documents["checkpoint_sensors"]["by_type"]
    assert checkpoint["rainfall"]["anchor"] == clock[0]
    assert checkpoint["rainfall"]["next_due"] == clock[0] + 300
    assert checkpoint["temperature"] == {}
    configs = cloud_configs(runtime)
    assert configs["rainfall"]["last_run_at"] == utc_text(clock[0])
    assert configs["temperature"]["last_run_at"] is None
    clock[0] += 1
    assert sensor_capture.cloud_tick(runtime) == 0, "A stale partial migration must not recapture a delivered batch"


def test_resume_same_second_restores_due_and_interval_save_retimes_only_selected_family(tmp_path):
    clock = [NOW]
    store = PrismaStore(tmp_path / "prisma.db", clock=lambda: clock[0])
    for kind in ("rainfall", "temperature"):
        sensor_capture.local_save(store, {"sensor_count": 1}, sensor_type=kind)
        sensor_capture.local_control(store, True, sensor_type=kind)
    sibling = copy.deepcopy(local_configs(store)["temperature"])
    sensor_capture.local_control(store, False, sensor_type="rainfall")
    resumed = sensor_capture.local_control(store, True, sensor_type="rainfall")
    assert resumed["next_due"] == utc_text(NOW + 300)
    saved = sensor_capture.local_save(store, {"expected_revision": resumed["config_version"], "interval_minutes": 1}, sensor_type="rainfall")
    assert saved["next_due"] == utc_text(NOW + 60) and saved["last_received_count"] == 1
    assert local_configs(store)["temperature"] == sibling
    clock[0] += 60
    assert sensor_capture.local_tick(store) == 1
    assert local_configs(store)["temperature"] == sibling


def test_cloud_interval_save_keeps_pending_descriptor_and_retains_sibling_status(monkeypatch):
    runtime, clock = Runtime(), [NOW]
    runtime.documents["runtime"].update(namespace="namespace", bucket="bucket")
    runtime.client.object_storage = SimpleNamespace(put_object=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sensor_capture.time, "time", lambda: clock[0])
    for kind in ("rainfall", "temperature"):
        sensor_capture.cloud_save(runtime, {"sensor_count": 1}, sensor_type=kind)
        sensor_capture.cloud_control(runtime, True, sensor_type=kind)
    sibling = copy.deepcopy(cloud_configs(runtime)["temperature"])
    saved = sensor_capture.cloud_save(runtime, {"interval_minutes": 1}, sensor_type="rainfall")
    assert saved["next_due"] == utc_text(NOW + 60)
    assert cloud_configs(runtime)["temperature"] == sibling
    clock[0] += 60
    def fail(*_args, **_kwargs):
        raise RuntimeError("unavailable")
    runtime.client.object_storage.put_object = fail
    with pytest.raises(RuntimeError):
        sensor_capture.cloud_tick(runtime)
    frozen = copy.deepcopy(runtime.documents["checkpoint_sensors"]["by_type"]["rainfall"]["pending"])
    sensor_capture.cloud_save(runtime, {"interval_minutes": 10}, sensor_type="rainfall")
    assert runtime.documents["checkpoint_sensors"]["by_type"]["rainfall"]["pending"] == frozen
    assert cloud_configs(runtime)["temperature"] == sibling
    delivered = {}
    runtime.client.object_storage.put_object = lambda _ns, _bucket, key, content, **_kwargs: delivered.setdefault(key, content)
    assert sensor_capture.cloud_tick(runtime) == 1
    expected = sensors.text_files(sensor_capture._family_rows(frozen, "rainfall"))
    assert delivered == {"01_landing/prisma/raw/" + key: content for key, content in expected.items()}
    assert cloud_configs(runtime)["rainfall"]["next_due"] == utc_text(frozen["anchor"] + 600)
    assert runtime.documents["checkpoint_sensors"]["by_type"]["rainfall"]["next_due"] == frozen["anchor"] + 600
    assert cloud_configs(runtime)["temperature"] == sibling


@pytest.mark.parametrize("retry_save", [False, True])
def test_cloud_interval_commit_recovers_after_checkpoint_write_failure(monkeypatch, retry_save):
    runtime, clock = Runtime(), [NOW]
    runtime.documents["runtime"].update(namespace="namespace", bucket="bucket")
    runtime.client.object_storage = SimpleNamespace(put_object=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sensor_capture.time, "time", lambda: clock[0])
    sensor_capture.cloud_save(runtime, {"sensor_count": 1, "interval_minutes": 60}, sensor_type="rainfall")
    sensor_capture.cloud_control(runtime, True, sensor_type="rainfall")
    siblings = {kind: copy.deepcopy(value) for kind, value in cloud_configs(runtime).items() if kind != "rainfall"}
    change = runtime._change

    def fail_checkpoint(name, mutate):
        if name == "checkpoint_sensors":
            raise RuntimeError("checkpoint unavailable")
        return change(name, mutate)

    monkeypatch.setattr(runtime, "_change", fail_checkpoint)
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        sensor_capture.cloud_save(runtime, {"interval_minutes": 1}, sensor_type="rainfall")
    assert cloud_configs(runtime)["rainfall"]["interval_minutes"] == 1
    assert runtime.documents["checkpoint_sensors"]["by_type"]["rainfall"]["next_due"] == NOW + 3600
    monkeypatch.setattr(runtime, "_change", change)
    if retry_save:
        saved = sensor_capture.cloud_save(runtime, {"interval_minutes": 1}, sensor_type="rainfall")
        assert saved["next_due"] == utc_text(NOW + 60)
    clock[0] += 60
    assert sensor_capture.cloud_tick(runtime) == 1
    assert cloud_configs(runtime)["rainfall"]["next_due"] == utc_text(NOW + 120)
    assert all(cloud_configs(runtime)[kind] == value for kind, value in siblings.items())


def test_formerly_excluded_family_cannot_overwrite_a_legacy_txt_in_the_same_second(tmp_path):
    clock = [NOW]
    store = PrismaStore(tmp_path / "prisma.db", clock=lambda: clock[0])
    sensor_capture.local_save(store, {"sensor_count": 100})
    sensor_capture.local_control(store, True)
    existing = tmp_path / "prisma-landing" / "sensors" / "temperature" / f"sim-{int(NOW)}-0.txt"
    original = existing.read_bytes()
    sensor_capture.local_save(store, {"families": ["rainfall"]})
    started = sensor_capture.local_control(store, True, sensor_type="temperature")
    assert started["capture_running"] and started["last_run_at"] is None
    assert existing.read_bytes() == original
    clock[0] += 1
    assert sensor_capture.local_tick(store) == 800
    assert existing.read_bytes() == original


def test_family_api_requires_admin_strict_values_and_revision_without_modifying_siblings(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_prisma_access import admin_login, local_settings
    client = TestClient(create_app(local_settings(tmp_path)))
    base = "/api/admin/prisma/sensors"
    assert client.put(base + "/rainfall", json={"expected_revision": 1}).status_code == 401
    assert client.post(base + "/rainfall/run").status_code == 401
    admin_login(client)
    original = {config["sensor_type"]: config for config in client.get(base).json()["configs"]}
    assert len(original) == 5 and sum(config["sensor_count"] for config in original.values()) == 4000
    for endpoint, values in (("camera", {}), ("rainfall", {"sensor_count": True}), ("rainfall", {"families": ["temperature"]})):
        assert client.put(base + "/" + endpoint, json={"expected_revision": 1, **values}).status_code == 422
    saved = client.put(base + "/rainfall", json={"expected_revision": 1, "sensor_count": 1, "interval_minutes": 1})
    assert saved.status_code == 200 and saved.json()["config"]["sensor_type"] == "rainfall"
    assert client.put(base + "/rainfall", json={"expected_revision": 1, "sensor_count": 2}).status_code == 409
    assert client.post(base + "/rainfall/run").json()["config"]["capture_running"]
    assert not client.post(base + "/rainfall/pause").json()["config"]["capture_running"]
    configs = {config["sensor_type"]: config for config in client.get(base).json()["configs"]}
    assert all(configs[kind] == original[kind] for kind in original if kind != "rainfall")
