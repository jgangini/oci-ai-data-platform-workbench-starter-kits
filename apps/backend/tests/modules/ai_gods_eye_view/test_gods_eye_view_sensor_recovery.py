import copy
from types import SimpleNamespace

import pytest

from app.modules.ai_gods_eye_view.runtime import sensor_capture, sensors
from tests.modules.ai_gods_eye_view.test_gods_eye_view_cloud import Runtime
from tests.modules.ai_gods_eye_view.test_gods_eye_view_sensors import NOW, sensor_document


def test_partial_sensor_delivery_replays_frozen_batch_after_restart_interval_and_configuration_change(monkeypatch):
    runtime = Runtime()
    runtime.documents["runtime"].update(namespace="namespace", bucket="bucket")
    runtime.documents["configuration"] = {"sensors": sensor_document(sensor_count=100, families=["rainfall"], capture_running=True)}
    objects, attempts, clock = {}, [], [NOW]
    def put(_namespace, _bucket, key, content, **_kwargs):
        attempts.append(key)
        objects[key] = content
        if len(attempts) == 1:
            raise RuntimeError("storage unavailable after delivery")
    monkeypatch.setattr(sensor_capture.time, "time", lambda: clock[0])
    runtime.client.object_storage = SimpleNamespace(put_object=put)
    expected = {"01_landing/prisma/raw/" + key: content for key, content in sensors.text_files(
        sensors.generate_batch(NOW, sensor_count=100, families=["rainfall"])).items()}
    with pytest.raises(RuntimeError, match="storage unavailable"):
        sensor_capture.cloud_tick(runtime)
    partial = dict(objects)
    assert len(partial) == 1 and runtime.documents["checkpoint_sensors"]["by_type"]["rainfall"]["pending"]
    restarted = Runtime()
    restarted.documents = copy.deepcopy(runtime.documents)
    restarted.client.object_storage = SimpleNamespace(put_object=put)
    sensor_capture.cloud_save(restarted, {"expected_revision": 1, "interval_minutes": 1,
        "sensor_count": 150, "families": ["rainfall"]})
    clock[0] += 300
    assert sensor_capture.cloud_tick(restarted) == 100
    assert objects == expected and all(objects[key] == content for key, content in partial.items())
    assert not restarted.documents["checkpoint_sensors"]["by_type"]["rainfall"].get("pending")
    assert sensor_capture.cloud_tick(restarted) == 150
    expected.update({"01_landing/prisma/raw/" + key: content for key, content in sensors.text_files(
        sensors.generate_batch(clock[0], sensor_count=150, families=["rainfall"])).items()})
    assert objects == expected and attempts[0] == attempts[1]
    assert sensor_capture.cloud_tick(restarted, force=True) == 0


@pytest.mark.parametrize("force", [False, True])
def test_longer_interval_never_reuses_an_older_delivered_batch_after_configuration_change(force):
    config = sensor_document(**{"capture_running": True, "interval_minutes": 60, "sensor_count": 150})
    checkpoint = {"anchor": NOW + 300, "next_due": NOW + 600}
    batch = sensor_capture._batch(config, NOW + 600, checkpoint, force, {})
    assert checkpoint["anchor"] < batch["anchor"] <= NOW + 600
    assert batch["interval_minutes"] == 60
