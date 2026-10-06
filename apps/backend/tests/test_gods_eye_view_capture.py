import asyncio
import copy
import threading
from types import SimpleNamespace

import pytest

from app.gods_eye_view import capture, cloud, database, landing, pipeline, scheduling
from app.gods_eye_view.core import default_source


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


def test_synthetic_window_is_incremental_and_final_drain_respects_cadence():
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
    assert capture.batch(source, state, cursor, NOW + 600) is None
    _, final = capture.batch(source, state, cursor, NOW + 720)
    assert final["elapsed"] == 600 and final["complete"]


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

    def _documents(self, names):
        return {name: self._doc(name) for name in names}

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
    assert not producer.docs["checkpoint_synthetic"].get("sources") and producer.objects == {}
    assert producer.docs["checkpoint_synthetic"]["pending"]["x"]
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
    assert durable and not producer.docs["checkpoint_synthetic"].get("sources")
    assert producer.docs["checkpoint_synthetic"]["pending"]["x"]
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


def test_cloud_run_finishes_once_and_run_never_republishes_exhausted_articles(monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(cloud.time, "time", lambda: clock[0])
    producer = Producer()
    producer.docs["simulation"] = {"status": "idle"}
    producer.docs["configuration"] = {"sources": {"x": {"synthetic_batch_max": 100}}}
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
    assert len(rain) == len({event["source_id"] for event in rain}) == 1
    assert rain[0]["created_at"] == first[0]["created_at"]
    assert restarted.docs["status_x"]["status"] == "completed"
    assert restarted.docs["status_x"]["next_due"] is None
    assert not restarted.docs["configuration"]["sources"]["x"]["capture_running"]
    assert restarted.docs["configuration"]["sources"]["x"]["capture_paused"]
    assert not restarted.docs["status_synthetic"]["final_job_pending"]
    assert not scheduling.needs_schedule(restarted.docs["configuration"], restarted.docs["simulation"], clock[0])
    control = copy.deepcopy(restarted.docs["checkpoint_controls"])
    checkpoint = copy.deepcopy(restarted.docs["checkpoint_synthetic"])
    completed_files = copy.deepcopy(restarted.objects)
    assert restarted._request_source("x", "run")["status"] == "completed"
    assert restarted.objects == completed_files
    assert restarted.docs["checkpoint_controls"] == control and restarted.docs["checkpoint_synthetic"] == checkpoint

    stopped = restarted._update("x", {"enabled": False})
    assert stopped["capture_running"] is False
    before_stop = copy.deepcopy(restarted.objects)
    clock[0] += 86400
    asyncio.run(restarted.tick())
    assert restarted.objects == before_stop
    assert not scheduling.needs_schedule(restarted.docs["configuration"], restarted.docs["simulation"], clock[0])
    restarted._update("x", {"enabled": True, "query": "another query"})
    assert restarted._request_source("x", "run")["status"] == "completed"
    assert restarted.objects == before_stop
    assert restarted.docs["checkpoint_controls"] == control and restarted.docs["checkpoint_synthetic"] == checkpoint


def test_local_completed_capture_survives_restart_save_and_run_without_new_records(tmp_path):
    from app.gods_eye_view.local import LocalGodsEyeViewRuntime
    clock = [NOW]
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: clock[0])
    asyncio.run(runtime.update_source("x", {"query": "", "synthetic_batch_max": 100}))
    asyncio.run(runtime.run_source("x"))
    clock[0] += 600
    asyncio.run(runtime.tick())
    source = runtime.store.source("x")
    assert source["status"] == "completed" and not source["capture_running"] and source["capture_paused"]
    assert source["next_due"] is None
    records = runtime.store.posts("x", 100)
    assert records["total"] == 30
    with runtime.store.connection() as db:
        controls = runtime.store._get(db, "capture_controls", {})
        cursor = runtime.store._get(db, "synthetic:x", {})
    files = {path.name: path.read_bytes() for path in (tmp_path / "prisma-landing").glob("*.csv")}
    clock[0] += 86400
    restarted = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: clock[0])
    asyncio.run(restarted.update_source("x", {"enabled": False}))
    asyncio.run(restarted.update_source("x", {"enabled": True, "query": "another query"}))
    assert asyncio.run(restarted.run_source("x"))["status"] == "completed"
    asyncio.run(restarted.tick())
    assert restarted.store.posts("x", 100) == records
    with restarted.store.connection() as db:
        assert restarted.store._get(db, "capture_controls", {}) == controls
        assert restarted.store._get(db, "synthetic:x", {}) == cursor
    assert {path.name: path.read_bytes() for path in (tmp_path / "prisma-landing").glob("*.csv")} == files


def test_final_capture_retries_upload_and_native_drain_without_republishing(monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(cloud.time, "time", lambda: clock[0])
    producer = Producer()
    producer.docs["simulation"] = {"status": "idle"}
    producer.docs["configuration"] = {"sources": {"x": {"synthetic_batch_max": 100}}}
    producer._request_source("x", "run")
    cursor = copy.deepcopy(producer.docs["checkpoint_synthetic"])
    clock[0] += 600
    producer.fail_upload = True
    with pytest.raises(Exception):
        asyncio.run(producer.tick())
    assert producer.docs["checkpoint_synthetic"]["sources"] == cursor["sources"]
    assert producer.docs["checkpoint_synthetic"]["pending"]["x"]
    assert producer.docs["configuration"]["sources"]["x"]["capture_running"]
    producer.fail_upload = False
    producer.fail_wake = True
    with pytest.raises(Exception):
        asyncio.run(producer.tick())
    assert producer.docs["status_x"]["status"] == "completed"
    assert producer.docs["status_synthetic"]["final_job_pending"]
    files = copy.deepcopy(producer.objects)
    cursor = copy.deepcopy(producer.docs["checkpoint_synthetic"])
    request_id = producer.docs["status_synthetic"]["final_request_id"]
    producer.fail_wake = False
    clock[0] += 86400
    asyncio.run(producer.tick())
    assert producer.wakes[-1] == request_id
    assert not producer.docs["status_synthetic"]["final_job_pending"]
    assert producer.objects == files and producer.docs["checkpoint_synthetic"] == cursor


def test_pause_run_retries_final_institutional_batch_without_replaying_completed_social(monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(cloud.time, "time", lambda: clock[0])
    producer = Producer()
    producer.docs["simulation"] = {"status": "idle"}
    producer.docs["configuration"] = {"sources": {"x": {"synthetic_batch_max": 100}}}
    producer._request_source("x", "run")
    control = copy.deepcopy(producer.docs["checkpoint_controls"])
    upload = producer.put_object
    def fail_sensor(namespace, bucket, key, body, **kwargs):
        if key.rsplit("/", 1)[-1].startswith("sensor-"):
            raise RuntimeError("Injected final institutional upload failure")
        return upload(namespace, bucket, key, body, **kwargs)
    monkeypatch.setattr(producer, "put_object", fail_sensor)
    clock[0] += 600
    with pytest.raises(RuntimeError, match="institutional"):
        producer._produce()
    cursors = producer.docs["checkpoint_synthetic"]["sources"]
    assert cursors["x"]["elapsed"] == 600 and cursors["sensor"]["elapsed"] == 0
    social = {key: body for key, body in producer.objects.items() if key.rsplit("/", 1)[-1].startswith("x-")}
    asyncio.run(producer.pause_source("x"))
    monkeypatch.setattr(producer, "put_object", upload)
    assert producer._request_source("x", "run")["status"] == "completed"
    asyncio.run(producer.tick())
    assert all(producer.docs["checkpoint_controls"][name] == control[name] for name in ("x", "institutional"))
    assert all(producer.docs["checkpoint_synthetic"]["sources"][name]["elapsed"] == 600 for name in ("x", "sensor", "sire", "linea123"))
    assert {key: body for key, body in producer.objects.items() if key.rsplit("/", 1)[-1].startswith("x-")} == social
    assert not producer.docs["configuration"]["sources"]["x"]["capture_running"]
    assert not producer.docs["status_synthetic"]["final_job_pending"]


def test_local_pause_run_retries_final_institutional_file_with_the_same_control(tmp_path, monkeypatch):
    from app.gods_eye_view.local import LocalGodsEyeViewRuntime
    clock = [NOW]
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: clock[0])
    asyncio.run(runtime.update_source("x", {"synthetic_batch_max": 100}))
    asyncio.run(runtime.run_source("x"))
    with runtime.store.connection() as db:
        controls = runtime.store._get(db, "capture_controls", {})
    write_file = landing.write_file
    def fail_sensor(directory, events, batch_key=None):
        if batch_key and batch_key["platform"] == "sensor":
            raise RuntimeError("Injected final institutional file failure")
        return write_file(directory, events, batch_key)
    monkeypatch.setattr(landing, "write_file", fail_sensor)
    clock[0] += 600
    with pytest.raises(RuntimeError, match="institutional"):
        runtime.store.advance_simulation()
    social = {path.name: path.read_bytes() for path in (tmp_path / "prisma-landing").glob("x-*.csv")}
    with runtime.store.connection() as db:
        assert runtime.store._get(db, "synthetic:x", {})["elapsed"] == 600
        assert runtime.store._get(db, "synthetic:sensor", {})["elapsed"] == 0
    asyncio.run(runtime.pause_source("x"))
    monkeypatch.setattr(landing, "write_file", write_file)
    assert asyncio.run(runtime.run_source("x"))["status"] == "completed"
    with runtime.store.connection() as db:
        assert runtime.store._get(db, "capture_controls", {}) == controls
        assert all(runtime.store._get(db, "synthetic:" + name, {})["elapsed"] == 600 for name in ("x", "sensor", "sire", "linea123"))
    assert {path.name: path.read_bytes() for path in (tmp_path / "prisma-landing").glob("x-*.csv")} == social
    assert not runtime.store.source("x")["capture_running"]


def test_completion_state_write_failure_retries_from_committed_cursor(monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(cloud.time, "time", lambda: clock[0])
    producer = Producer()
    producer.docs["simulation"] = {"status": "idle"}
    producer.docs["configuration"] = {"sources": {"x": {"synthetic_batch_max": 100}}}
    producer._request_source("x", "run")
    change = producer._change
    def failing(name, update):
        if name == "configuration":
            raise RuntimeError("Injected completion state failure")
        return change(name, update)
    monkeypatch.setattr(producer, "_change", failing)
    clock[0] += 600
    with pytest.raises(Exception):
        asyncio.run(producer.tick())
    cursor = copy.deepcopy(producer.docs["checkpoint_synthetic"])
    assert cursor["sources"]["x"]["elapsed"] == 600
    assert producer.docs["configuration"]["sources"]["x"]["capture_running"]
    files = copy.deepcopy(producer.objects)
    monkeypatch.setattr(producer, "_change", change)
    asyncio.run(producer.tick())
    assert not producer.docs["configuration"]["sources"]["x"]["capture_running"]
    assert producer.docs["status_x"]["status"] == "completed"
    assert producer.objects == files and producer.docs["checkpoint_synthetic"] == cursor
