import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from app.prisma import capture, cloud, landing, pipeline, scheduling
from app.prisma.core import default_source


NOW = 1791209100.0
CONFIG = {"catalog": "oci_medallion", "namespace": "ns", "landing_bucket": "landing",
          "landing_prefix": "01_landing/prisma/raw/", "landing_volume_path": "/Volumes/oci_medallion/prisma_ingest/landing",
          "checkpoint_volume_path": "/Volumes/oci_medallion/prisma_ingest/checkpoints/bronze-v1"}


@pytest.mark.parametrize("query,expected", [("Bogota inundacion", True), ('#Bogota AND "lluvia intensa"', True),
    ("(incendio OR inundación) -humo", True), ("Bogota -inundacion", False), ("incendio", False)])
def test_synthetic_query_contract(query, expected):
    assert capture.matches("Lluvia intensa e inundación en Kennedy · Bogotá", capture.search_terms(query)) is expected


@pytest.mark.parametrize("query", ["from:account", "()", "Bogota OR", "AND Bogota", "(Bogota", '"Bogota'])
def test_unknown_or_malformed_synthetic_queries_fail_closed(query):
    with pytest.raises(ValueError):
        capture.search_terms(query)


def test_synthetic_window_is_incremental_and_flush_ignores_next_due():
    state = {"status": "running", "run_id": "run1", "anchor_at": NOW, "elapsed_seconds": 0}
    source = default_source("x")
    events, cursor = capture.batch(source, state, {}, NOW)
    assert len(events) == 2 and events[0]["source_id"] == events[1]["source_id"]
    assert capture.batch(source, state, cursor, NOW + 60) is None
    state["elapsed_seconds"] = 420
    events, cursor = capture.batch(source, state, cursor, NOW + 420)
    assert [event["source_id"] for event in events] == ["run1:ubicacion-1"]
    state["elapsed_seconds"] = 600
    _, final = capture.batch(source, state, cursor, NOW + 600)
    assert final["elapsed"] == 600  # due was NOW+720, but final flush cannot wait for another interval.


class Producer(cloud.CloudRuntime):
    def __init__(self):
        self.capture_lock = threading.RLock()
        self.docs = {"runtime": CONFIG, "simulation": {"run_id": "run1", "anchor_at": NOW, "started_at": NOW,
                     "status": "running", "elapsed_seconds": 0}}
        self.objects, self.fail_upload, self.fail_wake, self.wakes = {}, False, False, []
        self.aidp_factory = lambda: SimpleNamespace(object_storage=self)

    def _doc(self, name):
        return copy.deepcopy(self.docs.get(name, {"revision": 0}))

    def _change(self, name, change):
        old = self._doc(name)
        self.docs[name] = {**change(old), "revision": old.get("revision", 0) + 1}
        return self.docs[name]

    def put_object(self, namespace, bucket, key, body, **_):
        assert (namespace, bucket) == ("ns", "landing")
        if self.fail_upload:
            raise RuntimeError("Injected upload failure")
        self.objects[key] = body

    def _wake(self, request_id):
        if self.fail_wake:
            raise RuntimeError("Injected native trigger failure")
        self.wakes.append(request_id)


def test_failed_vm_upload_never_advances_cursor_and_retry_has_same_landing_key(monkeypatch):
    monkeypatch.setattr(cloud.time, "time", lambda: NOW)
    producer = Producer()
    producer.fail_upload = True
    with pytest.raises(RuntimeError):
        producer._produce()
    assert "checkpoint_synthetic" not in producer.docs and producer.objects == {}
    producer.fail_upload = False
    producer._produce()
    first = copy.deepcopy(producer.objects)
    producer._produce()
    assert producer.objects == first and producer.docs["status_synthetic"]["landing_count"] == 1
    assert producer.docs["status_x"]["next_due"] and producer.docs["status_x"]["last_received_count"] == 2


def test_completion_flushes_institutional_sources_and_retries_final_trigger(monkeypatch):
    monkeypatch.setattr(cloud.time, "time", lambda: NOW + 600)
    producer = Producer()
    producer.fail_wake = True
    with pytest.raises(Exception):
        asyncio.run(producer.tick())
    assert producer.docs["simulation"]["capture_complete"] and producer.docs["simulation"]["final_job_pending"]
    assert scheduling.needs_schedule({}, producer.docs["simulation"], NOW + 600)
    records = [json.loads(json.loads(line)["payload"]) for body in producer.objects.values() for line in body.splitlines()]
    assert {"sensor", "sire", "linea123"} <= {item["platform"] for item in records}
    producer.fail_wake = False
    asyncio.run(producer.tick())
    assert producer.wakes == ["run1-final"]
    assert not scheduling.needs_schedule({}, producer.docs["simulation"], NOW + 600)


def test_volume_contract_is_verified_before_stream_or_table_creation():
    class Spark:
        def __init__(self):
            self.calls, self.wrong = [], False
        def sql(self, statement):
            self.calls.append(statement)
            if statement.startswith("DESCRIBE VOLUME"):
                external = statement.endswith(".landing")
                return SimpleNamespace(collect=lambda: [("Volume type", "EXTERNAL" if external else "MANAGED"),
                    ("Location", "oci://wrong@ns/raw" if self.wrong else "oci://landing@ns/01_landing/prisma/raw/")])
            return SimpleNamespace()
    spark = Spark()
    pipeline.ensure_volumes(spark, CONFIG)
    assert sum(item.startswith("DESCRIBE VOLUME") for item in spark.calls) == 2
    spark.wrong = True
    with pytest.raises(RuntimeError, match="volume type or storage"):
        pipeline.DeltaLake(spark, CONFIG)
    assert not any(item.startswith("CREATE TABLE") for item in spark.calls)
    with pytest.raises(ValueError, match="governed volume"):
        pipeline.consume_landing(None, None, "oci://landing@ns/raw", "/Volumes/checkpoints")


def test_file_and_oci_landing_use_identical_immutable_bytes(tmp_path):
    source = default_source("x")
    events, _ = capture.batch(source, {"status": "running", "run_id": "run1", "anchor_at": NOW, "elapsed_seconds": 0}, {}, NOW)
    producer = Producer()
    key = landing.write_objects(producer, CONFIG, events)
    name = landing.write_file(tmp_path, events)
    assert producer.objects[key] == (tmp_path / name).read_bytes()
