import copy
from types import SimpleNamespace

import pytest

from app.gods_eye_view import sensor_capture, sensors
from test_gods_eye_view_cloud import Runtime
from test_gods_eye_view_sensors import NOW


def test_partial_sensor_delivery_replays_frozen_batch_after_restart_interval_and_configuration_change(monkeypatch):
    runtime = Runtime()
    runtime.documents["runtime"].update(namespace="namespace", bucket="bucket")
    runtime.documents["configuration"] = {"sensors": {"sensor_count": 100, "capture_running": True}}
    objects, attempts, clock = {}, [], [NOW]

    def put(_namespace, _bucket, key, content, **_kwargs):
        attempts.append(key)
        if len(attempts) == 3:
            raise RuntimeError("storage unavailable")
        objects[key] = content

    monkeypatch.setattr(sensor_capture.time, "time", lambda: clock[0])
    runtime.client.object_storage = SimpleNamespace(put_object=put)
    expected = {"01_landing/prisma/raw/" + key: content
                for key, content in sensors.text_files(sensors.generate_batch(NOW, sensor_count=100)).items()}
    with pytest.raises(RuntimeError, match="storage unavailable"):
        sensor_capture.cloud_tick(runtime)
    partial = dict(objects)
    assert len(partial) == 2
    assert runtime.documents["checkpoint_sensors"].get("anchor") is None

    # A new process has only durable documents and the already-delivered objects.
    restarted = Runtime()
    restarted.documents = copy.deepcopy(runtime.documents)
    restarted.client.object_storage = SimpleNamespace(put_object=put)
    sensor_capture.cloud_save(restarted, {"expected_revision": 1, "interval_minutes": 1,
        "sensor_count": 150, "families": ["temperature", "rainfall"]})
    clock[0] += 300
    assert sensor_capture.cloud_tick(restarted) == 100
    assert objects == expected, "Complete the original five TXT files before producing a new batch"
    assert all(objects[key] == content for key, content in partial.items()), "Do not change bytes Spark may have ingested"
    assert not restarted.documents["checkpoint_sensors"].get("pending")

    # New settings apply only to the subsequent batch, whose object names are distinct.
    assert sensor_capture.cloud_tick(restarted) == 150
    expected.update({"01_landing/prisma/raw/" + key: content for key, content in sensors.text_files(
        sensors.generate_batch(clock[0], sensor_count=150, families=["temperature", "rainfall"])).items()})
    assert objects == expected
    assert sensor_capture.cloud_tick(restarted, force=True) == 0


@pytest.mark.parametrize("force", [False, True])
def test_longer_interval_never_reuses_an_older_delivered_batch_after_configuration_change(force):
    config = sensors.configuration({"capture_running": True, "interval_minutes": 60, "sensor_count": 150})
    checkpoint = {"anchor": NOW + 300, "next_due": NOW + 600}
    batch = sensor_capture._batch(config, NOW + 600, checkpoint, force, {})
    assert checkpoint["anchor"] < batch["anchor"] <= NOW + 600
    assert batch["interval_minutes"] == 60
