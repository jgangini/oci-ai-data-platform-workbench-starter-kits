import asyncio
import copy
import threading
from types import SimpleNamespace

import pytest

from app.prisma import capture, cloud, database, landing, pipeline, scheduling
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
    assert len(events) == 1 and events[0]["source_id"] == "run1:lluvia-1"
    assert events[0]["raw_metadata"]["matched_queries"] == ["#bogota #inundacion", "#desastre"]
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
        assert database.DOCUMENT_NAME.fullmatch(name), "Exercise the real ADB document name boundary"
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

    def _project_posts(self, events, now, key):
        self.projected = (events, now, key)


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
    assert producer.objects == first and producer.docs["status_synthetic"]["landing_count"] == 7
    assert all(key.endswith(".csv") for key in first)
    assert sum(len(landing.records(body)) for body in first.values()) == 1
    assert producer.docs["status_x"]["next_due"] and producer.docs["status_x"]["last_received_count"] == 1


def test_failed_post_projection_keeps_durable_csv_and_retries_before_advancing(monkeypatch):
    monkeypatch.setattr(cloud.time, "time", lambda: NOW)
    producer = Producer()
    def unavailable(*_):
        raise RuntimeError("Injected post-index failure")
    monkeypatch.setattr(producer, "_project_posts", unavailable)
    with pytest.raises(RuntimeError, match="post-index"):
        producer._produce()
    durable = copy.deepcopy(producer.objects)
    assert durable and "checkpoint_synthetic" not in producer.docs
    monkeypatch.setattr(producer, "_project_posts", lambda *_: None)
    producer._produce()
    assert all(producer.objects[key] == value for key, value in durable.items())
    assert producer.docs["checkpoint_synthetic"]["sources"]["x"]


def test_completion_flushes_institutional_sources_and_retries_final_trigger(monkeypatch):
    monkeypatch.setattr(cloud.time, "time", lambda: NOW + 600)
    producer = Producer()
    producer.fail_wake = True
    with pytest.raises(Exception):
        asyncio.run(producer.tick())
    assert producer.docs["simulation"]["capture_complete"] and producer.docs["simulation"]["final_job_pending"]
    assert scheduling.needs_schedule({}, producer.docs["simulation"], NOW + 600)
    records = [record for body in producer.objects.values() for record in landing.records(body)]
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
                row = {"name": "landing" if external else "checkpoints", "catalog": "oci_medallion", "database": "prisma_ingest",
                    "volumeType": "External" if external else "Managed",
                    "storageLocation": "oci://wrong@ns/raw" if self.wrong else "oci://landing@ns/01_landing/prisma/raw/"}
                return SimpleNamespace(collect=lambda: [SimpleNamespace(asDict=lambda: row)])
            return SimpleNamespace()
    spark = Spark()
    pipeline.ensure_volumes(spark, CONFIG)
    assert sum(item.startswith("DESCRIBE VOLUME") for item in spark.calls) == 2
    assert not any(item.startswith("CREATE") for item in spark.calls)
    spark.wrong = True
    with pytest.raises(RuntimeError, match="volume type or storage"):
        pipeline.DeltaLake(spark, CONFIG)
    assert not any(item.startswith("CREATE TABLE") for item in spark.calls)
    with pytest.raises(ValueError, match="governed volume"):
        pipeline.consume_landing(None, None, "oci://landing@ns/raw", "/Volumes/checkpoints")


@pytest.mark.parametrize("name,kind", [("landing", "MANAGED"), ("checkpoints", "EXTERNAL"), ("landing", None)])
def test_volume_validation_rejects_missing_or_wrong_type_before_any_write(name, kind):
    class Spark:
        def sql(self, statement):
            assert statement.startswith("DESCRIBE VOLUME "), "Pipeline must not provision or write before volume validation"
            target = statement.endswith("." + name)
            if target and kind is None:
                raise RuntimeError("Required volume does not exist")
            external = statement.endswith(".landing")
            row = {"name": "landing" if external else "checkpoints", "catalog": "oci_medallion", "database": "prisma_ingest",
                "volumeType": kind if target else ("External" if external else "Managed"),
                "storageLocation": "oci://landing@ns/01_landing/prisma/raw/"}
            return SimpleNamespace(collect=lambda: [SimpleNamespace(asDict=lambda: row)])
    with pytest.raises(RuntimeError, match="volume"):
        pipeline.DeltaLake(Spark(), CONFIG)


@pytest.mark.parametrize("mismatch", [0, 2, {"name": "other"}, {"catalog": "foreign"}, {"database": "other"}])
def test_native_volume_description_requires_one_row_and_matching_identity(mismatch):
    def describe(statement):
        assert statement.startswith("DESCRIBE VOLUME "), "No write is allowed before validation"
        row = {"name": "landing", "catalog": "oci_medallion", "database": "prisma_ingest", "volumeType": "External",
            "storageLocation": "oci://landing@ns/01_landing/prisma/raw/"}
        if isinstance(mismatch, dict):
            row.update(mismatch)
        count = mismatch if isinstance(mismatch, int) else 1
        return SimpleNamespace(collect=lambda: [SimpleNamespace(asDict=lambda: row)] * count)
    with pytest.raises(RuntimeError, match="volume description|volume identity"):
        pipeline.DeltaLake(SimpleNamespace(sql=describe), CONFIG)


def test_file_and_oci_landing_use_identical_immutable_bytes(tmp_path):
    source = default_source("x")
    events, _ = capture.batch(source, {"status": "running", "run_id": "run1", "anchor_at": NOW, "elapsed_seconds": 0}, {}, NOW)
    producer = Producer()
    key = landing.write_objects(producer, CONFIG, events)
    name = landing.write_file(tmp_path, events)
    assert producer.objects[key] == (tmp_path / name).read_bytes()


def test_cloud_run_starts_persistent_continuous_capture_and_disable_stops_uploads(monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(cloud.time, "time", lambda: clock[0])
    producer = Producer()
    producer.docs["simulation"] = {"status": "idle"}
    response = producer._request_source("x", "run")
    assert response["source"]["capture_running"] is True
    assert producer.docs["checkpoint_controls"]["x"]["run_id"]
    first = [event for body in producer.objects.values() for event in landing.records(body)]
    assert len(first) == 1 and "Kennedy" in first[0]["text"]
    assert producer.docs["status_x"]["last_received_count"] == 1
    assert scheduling.needs_schedule(producer.docs["configuration"], producer.docs["simulation"], NOW + 86400)

    restarted = Producer()
    restarted.docs, restarted.objects = copy.deepcopy(producer.docs), copy.deepcopy(producer.objects)
    clock[0] += 600
    asyncio.run(restarted.tick())
    events = [event for body in restarted.objects.values() for event in landing.records(body)]
    assert {"sensor", "sire", "linea123"} <= {event["platform"] for event in events}
    rain = [event for event in events if event["platform"] == "x" and event["source_id"].endswith(":post-0001")]
    assert len(rain) == len({event["source_id"] for event in rain}) == 2
    assert rain[0]["created_at"] != rain[1]["created_at"]

    stopped = restarted._update("x", {"enabled": False})
    assert stopped["capture_running"] is False
    before_stop = copy.deepcopy(restarted.objects)
    clock[0] += 86400
    asyncio.run(restarted.tick())
    assert restarted.objects == before_stop
    assert not scheduling.needs_schedule(restarted.docs["configuration"], restarted.docs["simulation"], clock[0])
