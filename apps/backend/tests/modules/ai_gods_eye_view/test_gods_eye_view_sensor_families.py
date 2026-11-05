"""Independent sensor controls preserve legacy identities and immutable pending TXT."""
import copy
import json
from collections import Counter
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.ai_gods_eye_view.runtime import sensor_capture, sensors
from app.modules.ai_gods_eye_view.runtime.core import utc_text
from app.modules.ai_gods_eye_view.runtime.store import GodsEyeViewStore
from tests.modules.ai_gods_eye_view.test_gods_eye_view_cloud import Runtime
from tests.modules.ai_gods_eye_view.test_gods_eye_view_sensors import NOW, sensor_document


def local_configs(store):
    with store.connection() as db:
        return sensors.family_configs(store._get(db, "configuration_sensors", {}), store._get(db, "status_sensors", {}))


def cloud_configs(runtime):
    return sensors.family_configs(runtime._doc("configuration").get("sensors", {}), runtime._doc("status_sensors"))


def test_new_defaults_are_per_family_and_persisted_flat_controls_are_rejected():
    defaults = sensors.family_configs()
    assert {kind: config["sensor_count"] for kind, config in defaults.items()} == {kind: 800 for kind in sensors.SENSOR_TYPES}
    for old in ({"sensor_count": 101, "families": ["rainfall", "temperature", "river_level"],
                 "interval_minutes": 7, "capture_running": True, "config_version": 9}, {"sensor_count": 100}):
        before = copy.deepcopy(old)
        with pytest.raises(ValueError, match="per-family"):
            sensors.family_configs(old)
        assert old == before


def test_single_family_generation_preserves_existing_sensor_ids_and_coordinates_and_allows_one():
    legacy = sensors.generate_batch(NOW, sensor_count=100)
    for kind in sensors.SENSOR_TYPES:
        previous = {row["sensor_id"]: (row["lat"], row["lon"]) for row in legacy if row["sensor_type"] == kind}
        current = sensors.generate_batch(NOW + 300, sensor_count=20, families=[kind])
        assert {row["sensor_id"]: (row["lat"], row["lon"]) for row in current} == previous
        assert len(sensors.generate_batch(NOW, sensor_count=1, families=[kind])) == 1


def test_family_save_run_pause_and_due_times_do_not_change_siblings(tmp_path):
    clock = [NOW]
    store = GodsEyeViewStore(tmp_path / "territorial.db", clock=lambda: clock[0])
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
    store = GodsEyeViewStore(tmp_path / "territorial.db", clock=lambda: NOW)
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
    store = GodsEyeViewStore(tmp_path / "territorial.db", clock=lambda: clock[0])
    sensor_capture.local_save(store, {"sensor_count": 100, "families": ["rainfall"], "interval_minutes": 60})
    sensor_capture.local_control(store, True)
    rainfall = copy.deepcopy(local_configs(store)["rainfall"])
    temperature = sensor_capture.local_save(store, {"sensor_count": 1}, sensor_type="temperature")
    assert not temperature["capture_running"]
    assert temperature["last_run_at"] is None and temperature["next_due"] is None
    with store.connection() as db:
        assert store._get(db, "checkpoint_sensors", {}).get("by_type", {}).get("temperature", {}) == {}
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
    runtime.documents["configuration"] = {"sensors": sensor_document(**{"sensor_count": 100})}
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


def test_old_partial_global_delivery_is_rejected_without_writing_or_rebinding(monkeypatch):
    runtime = Runtime()
    runtime.documents.update(configuration={"sensors": {"sensor_count": 100, "capture_running": True}},
        checkpoint_sensors={"pending": {"anchor": NOW, "families": list(sensors.SENSOR_TYPES), "sensor_count": 100}},
        status_sensors={"last_error": "TimeoutError"})
    before = copy.deepcopy(runtime.documents)
    runtime.client.object_storage = SimpleNamespace(put_object=lambda *_a, **_kw: pytest.fail("Old delivery was adopted"))
    for action in (lambda: sensor_capture.cloud_tick(runtime),
                   lambda: sensor_capture.cloud_save(runtime, {"sensor_count": 1}, sensor_type="rainfall"),
                   lambda: sensor_capture.cloud_control(runtime, True, sensor_type="rainfall")):
        with pytest.raises(ValueError, match="per-family"): action()
        assert runtime.documents == before


def test_partial_migration_state_is_rejected_without_overwriting_current_documents(monkeypatch):
    runtime = Runtime()
    current = sensor_document(sensor_count=100, families=["rainfall"], capture_running=True)
    runtime.documents.update(configuration={"sensors": current},
        checkpoint_sensors={"anchor": NOW, "by_type": {"rainfall": {"anchor": NOW}}},
        status_sensors={"by_type": {"rainfall": {"last_run_at": utc_text(NOW)}}})
    before = copy.deepcopy(runtime.documents)
    for action in (lambda: sensor_capture.cloud_configuration(runtime),
                   lambda: sensor_capture.cloud_save(runtime, {"sensor_count": 1}, sensor_type="temperature"),
                   lambda: sensor_capture.cloud_control(runtime, False)):
        with pytest.raises(ValueError, match="per-family"): action()
        assert runtime.documents == before


def test_resume_same_second_restores_due_and_interval_save_retimes_only_selected_family(tmp_path):
    clock = [NOW]
    store = GodsEyeViewStore(tmp_path / "territorial.db", clock=lambda: clock[0])
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


def test_resumed_family_keeps_its_cursor_and_cannot_overwrite_a_same_second_batch(tmp_path):
    clock = [NOW]
    store = GodsEyeViewStore(tmp_path / "territorial.db", clock=lambda: clock[0])
    sensor_capture.local_save(store, {"sensor_count": 100})
    sensor_capture.local_control(store, True)
    existing = tmp_path / "prisma-landing" / "sensors" / "temperature" / f"sim-{int(NOW)}-0.txt"
    original = existing.read_bytes()
    sensor_capture.local_save(store, {"families": ["rainfall"]})
    started = sensor_capture.local_control(store, True, sensor_type="temperature")
    assert started["capture_running"] and started["last_run_at"] == utc_text(NOW)
    assert started["next_due"] == utc_text(NOW + 300)
    assert existing.read_bytes() == original
    clock[0] += 300
    assert sensor_capture.local_tick(store, sensor_type="temperature") == 20
    assert existing.read_bytes() == original


def test_family_api_requires_admin_strict_values_and_revision_without_modifying_siblings(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.modules.ai_gods_eye_view.test_gods_eye_view_access import admin_login, local_settings
    client = TestClient(create_app(local_settings(tmp_path)))
    base = "/api/admin/gods-eye-view/sensors"
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


@pytest.mark.parametrize("target,old", [("configuration_sensors", {"sensor_count": 100}),
    ("checkpoint_sensors", {"anchor": NOW}), ("status_sensors", {"last_received_count": 100})])
def test_local_old_documents_fail_before_any_persistent_change(tmp_path, target, old):
    store = GodsEyeViewStore(tmp_path / "sensors.db", clock=lambda: NOW)
    with store.connection() as db:
        store._put(db, "configuration_sensors", sensor_document())
        store._put(db, target, old)
        before = {name: store._get(db, name, {}) for name in ("configuration_sensors", "checkpoint_sensors", "status_sensors")}
    for action in (lambda: sensor_capture.local_configuration(store),
                   lambda: sensor_capture.local_save(store, {"sensor_count": 1}, sensor_type="rainfall"),
                   lambda: sensor_capture.local_control(store, True, sensor_type="rainfall")):
        with pytest.raises(ValueError, match="per-family"): action()
        with store.connection() as db:
            assert {name: store._get(db, name, {}) for name in before} == before


def test_new_family_capture_rejects_existing_different_txt_without_overwriting_it(tmp_path):
    store = GodsEyeViewStore(tmp_path / "sensors.db", clock=lambda: NOW)
    sensor_capture.local_save(store, {"sensor_count": 1}, sensor_type="rainfall")
    existing = tmp_path / "prisma-landing" / "sensors" / "rainfall" / f"sim-{int(NOW)}-0.txt"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"previous unsupported data\n")
    with pytest.raises(ValueError, match="overwrite"):
        sensor_capture.local_control(store, True, sensor_type="rainfall")
    assert existing.read_bytes() == b"previous unsupported data\n"
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM sensor_events").fetchone()[0] == 0
        pending = store._get(db, "checkpoint_sensors", {})["by_type"]["rainfall"]["pending"]
        assert pending["families"] == ["rainfall"]
        assert store._get(db, "status_sensors", {})["by_type"]["rainfall"]["last_error"] == "ValueError"
