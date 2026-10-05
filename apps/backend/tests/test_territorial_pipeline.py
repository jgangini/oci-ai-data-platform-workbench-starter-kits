import copy
import json
from types import SimpleNamespace

import pytest

from app.territorial import pipeline, scheduling, x
from app.territorial.landing import records, write_objects
from app.territorial.core import default_source, normalize_event, simulation_events
from app.territorial.classification import classify
from app.territorial.runtime_secrets import runtime_auth


CONFIG = {"namespace": "namespace", "bucket": "gold-bucket", "region": "test-region", "model_id": "model",
          "compartment_id": "compartment", "catalog": "oci_medallion", "landing_bucket": "landing-bucket",
          "landing_prefix": "01_landing/prisma/raw/", "landing_volume_path": "/Volumes/oci_medallion/prisma_ingest/landing",
          "checkpoint_volume_path": "/Volumes/oci_medallion/prisma_ingest/checkpoints/bronze-v1"}
NOW = 1791209100.0


class Lake:
    def __init__(self, log, objects):
        self.log, self.data, self.fail = log, {"bronze": {}, "silver": {}, "gold": {}}, None
        self.objects = objects

    def stage_snapshot(self, snapshot):
        self.log.append("silver_current")
        if self.fail == "silver_current":
            raise RuntimeError("Injected Silver state failure")
        self.current = copy.deepcopy(snapshot)
        return copy.deepcopy(self.current)

    def consume(self, config):
        for key, body in self.objects.data.items():
            if key.startswith(config["landing_prefix"]) and key.endswith((".csv", ".ndjson")):
                self.put("bronze", records(body, suffix=".csv" if key.endswith(".csv") else ".ndjson"))
        return {"query_id": "test-stream", "microbatches": 1, "last_input_rows": len(self.data["bronze"])}

    def put(self, layer, records):
        self.log.append(layer)
        if self.fail == layer:
            raise RuntimeError("Injected durable write failure")
        for record in records:
            self.data[layer].setdefault(record["id"], copy.deepcopy(record))

    def pending(self, ids=None):
        return [value for key, value in self.data["bronze"].items() if key not in self.data["silver"] and (not ids or key in ids)]

    def pending_count(self):
        return len(self.pending())

    def visible(self, run_id, _now):
        return [value for value in self.data["silver"].values()
                if value["mode"] == "real" or value["raw_metadata"].get("scenario_run_id") == run_id]


class Objects:
    def __init__(self, log):
        self.log, self.data, self.fail = log, {}, None

    def put_object(self, namespace, bucket, key, data, **_kwargs):
        self.log.append(key)
        if self.fail and key.startswith(self.fail):
            raise RuntimeError("Injected Object Storage failure")
        assert namespace == CONFIG["namespace"] and bucket == CONFIG["landing_bucket" if key.startswith("01_landing/") else "bucket"]
        self.data[key] = data

    def get_object(self, namespace, bucket, key):
        assert namespace == CONFIG["namespace"] and bucket == CONFIG["bucket"]
        return SimpleNamespace(data=SimpleNamespace(content=self.data[key]))


@pytest.fixture
def runtime(monkeypatch):
    log, docs, publications = [], {}, {}

    def read(_connection, name):
        return copy.deepcopy(docs.get(name, {"revision": 0}))

    def mutate(connection, name, change):
        current = read(connection, name)
        updated = {**change(current), "revision": current.get("revision", 0) + 1}
        docs[name] = copy.deepcopy(updated)
        log.append("doc:" + name)
        return updated

    def publish(_connection, snapshot):
        log.append("adb")
        previous = publications.setdefault(snapshot["version"], copy.deepcopy(snapshot))
        assert previous == snapshot, "An immutable version changed during retry"

    monkeypatch.setattr(pipeline, "read_document", read)
    monkeypatch.setattr(pipeline, "mutate_document", mutate)
    monkeypatch.setattr(pipeline, "publish", publish)
    monkeypatch.setattr(pipeline, "upsert_posts", lambda _db, records, status, **_: log.append("posts:" + status))
    monkeypatch.setattr(pipeline, "reset_version", lambda _db: 2)
    monkeypatch.setattr(pipeline, "sensor_reset_version", lambda _db: 2)
    objects = Objects(log)
    return log, docs, publications, Lake(log, objects), objects


def test_old_database_reset_contract_fails_before_ingestion_or_publication(runtime, monkeypatch):
    log, docs, publications, lake, objects = runtime
    monkeypatch.setattr(pipeline, "reset_version", lambda _db: 1)
    with pytest.raises(RuntimeError, match="database contract"):
        pipeline.run(None, None, CONFIG, connection=object(), objects=objects, lake=lake,
                     client=object(), classifier=lambda events: events, clock=lambda: NOW)
    assert log == [] and docs == publications == objects.data == {}


def test_sensor_coordinate_overrides_enter_gold_adb_and_agent_publication_atomically(runtime):
    from app.territorial.sensors import generate_batch
    _log, docs, publications, lake, objects = runtime
    sensor = {**generate_batch(NOW, sensor_count=100)[0], "id": "reading-1"}
    lake.sensors = SimpleNamespace(latest=lambda _: [copy.deepcopy(sensor)])
    before = pipeline.publish_snapshot(object(), objects, lake, CONFIG, [], {}, {}, NOW)
    docs["reviews"] = {"sensor_locations": {sensor["sensor_id"]: {"lat": 4.6, "lon": -74.1}}}
    updated = pipeline.publish_snapshot(object(), objects, lake, CONFIG, [], {}, {}, NOW + 1)
    assert before["version"] != updated["version"]
    assert updated["sensors"][0]["lat"] == 4.6 and updated["sensors"][0]["lon"] == -74.1
    assert publications[updated["version"]] == updated
    assert lake.data["gold"][updated["version"]]["sensors"] == updated["sensors"]
    assert json.loads(objects.data[f"04_gold/prisma/snapshots/{updated['version']}.json"]) == updated
    # The raw immutable measurement and the previous version are retained.
    assert publications[before["version"]]["sensors"][0]["lat"] == sensor["lat"]


def test_source_cursor_commits_after_landing_and_stream_owns_bronze_recovery(runtime):
    log, docs, _, lake, objects = runtime
    objects.fail = CONFIG["landing_prefix"]
    with pytest.raises(RuntimeError):
        pipeline.ingest_page(object(), objects, lake, CONFIG, "x", simulation_events(0), {"cursor": {"since_id": "30"}})
    assert "checkpoint_x" not in docs
    assert objects.data == {}
    objects.fail = None
    pipeline.ingest_page(object(), objects, lake, CONFIG, "x", simulation_events(0), {"cursor": {"since_id": "30"}})
    assert log[-1] == "doc:checkpoint_x" and lake.data["bronze"] == {}
    lake.fail = "bronze"
    with pytest.raises(RuntimeError):
        lake.consume(CONFIG)
    assert docs["checkpoint_x"]["cursor"]["since_id"] == "30"
    lake.fail = None
    lake.consume(CONFIG)
    assert len(lake.data["bronze"]) == 1


def test_snapshot_pointer_moves_only_after_adb_and_snapshot_object_and_retry_is_immutable(runtime):
    log, docs, publications, lake, objects = runtime
    objects.fail = "04_gold/prisma/snapshots/"
    state = {"status": "idle", "elapsed_seconds": 0, "run_id": None}
    with pytest.raises(RuntimeError):
        pipeline.publish_snapshot(object(), objects, lake, CONFIG, [], {}, state, NOW)
    assert len(publications) == 1
    assert "04_gold/prisma/current.json" not in objects.data
    version = docs["runtime"]["publication"]["version"]
    objects.fail = None
    snapshot = pipeline.publish_snapshot(object(), objects, lake, CONFIG, [], {}, state, NOW + 60)
    assert snapshot["version"] == version
    assert snapshot["published_at"] == docs["runtime"]["publication"]["published_at"]
    assert log[-4:] == ["gold", "adb", f"04_gold/prisma/snapshots/{version}.json", "04_gold/prisma/current.json"]


def test_empty_bootstrap_runs_actual_publication_path_without_simulated_data(runtime):
    log, docs, _, lake, objects = runtime
    def no_classification(_events):
        raise AssertionError("An empty deployment must not classify fabricated data")
    snapshot = pipeline.run(None, None, CONFIG, connection=object(), objects=objects, lake=lake,
                            client=object(), classifier=no_classification, clock=lambda: NOW)
    assert snapshot["incidents"] == snapshot["evidence"] == []
    assert snapshot["runtime"] == "aidp"
    assert docs["status_pipeline"]["status"] == "ready"
    assert "04_gold/prisma/current.json" in objects.data
    assert not any(key.startswith("01_landing") for key in objects.data)
    assert len(lake.data["gold"]) == 1


def test_rules_upgrade_seeds_stable_ids_from_published_pointer_and_preserves_review(runtime):
    _, docs, _, lake, objects = runtime
    event = normalize_event(simulation_events(0)[0])
    old = pipeline.publish_snapshot(object(), objects, lake, CONFIG, [event], {}, {}, NOW)
    incident_id = old["incidents"][0]["id"]
    reviews = {incident_id: {"status": "validated", "note": "Human review"}}
    # An interrupted attempt can leave runtime.publication newer than the durable pointer.
    docs["runtime"]["publication"] = {"version": "gold-" + "a" * 32, "published_at": pipeline.utc_text(NOW)}
    upgraded = pipeline.publish_snapshot(object(), objects, lake, CONFIG, [event], reviews, {}, NOW,
                                         rules={"x": default_source("x")})
    assert upgraded["incidents"][0]["id"] == incident_id
    assert upgraded["incidents"][0]["review_status"] == "validated"
    assert docs["event_registry"]["items"] == upgraded["incidents"]


def test_review_changes_publish_consistently_without_new_input_and_keep_history(runtime):
    _, docs, publications, lake, objects = runtime
    event = normalize_event(simulation_events(0)[0])
    lake.put("silver", [event])
    def tick():
        return pipeline.run(None, None, CONFIG, connection=object(), objects=objects, lake=lake,
            client=object(), classifier=lambda _: pytest.fail("Review changes must not reclassify existing posts"), clock=lambda: NOW)
    original = tick()
    incident_id = original["incidents"][0]["id"]
    versions = {original["version"]}
    for status in ("validated", "rejected", "pending"):
        note = "Human review: " + status
        docs["reviews"] = {"items": {incident_id: {"status": status, "note": note, "evidence_ids": [event["id"]],
                                                 "lat": 4.63, "lon": -74.15}}}
        snapshot = tick()
        assert snapshot["version"] not in versions
        versions.add(snapshot["version"])
        version = snapshot["version"]
        pointer = json.loads(objects.data["04_gold/prisma/current.json"])
        published = json.loads(objects.data[pointer["snapshot_key"]])
        assert pointer["version"] == version == docs["status_pipeline"]["version"]
        for saved in (snapshot, publications[version], lake.data["gold"][version], published):
            incident = saved["incidents"][0]
            assert incident["id"] == incident_id and incident["review_status"] == status
            assert incident["review_note"] == note and incident["reviewed_evidence_ids"] == [event["id"]]
            assert (incident["lat"], incident["lon"], incident["location_method"]) == (4.63, -74.15, "human_review")
        assert snapshot["evidence"] == original["evidence"]
    assert len(publications) == len(lake.data["gold"]) == 4
    assert publications[original["version"]]["incidents"][0]["review_status"] == "pending"
    assert lake.data["silver"][event["id"]] == event


def test_failed_review_publication_keeps_prior_pointer_and_retry_publishes_saved_review(runtime):
    _, docs, publications, lake, objects = runtime
    event = normalize_event(simulation_events(0)[0])
    lake.put("silver", [event])
    def tick():
        return pipeline.run(None, None, CONFIG, connection=object(), objects=objects, lake=lake,
            client=object(), classifier=lambda _: [], clock=lambda: NOW)
    previous = tick()
    incident_id = previous["incidents"][0]["id"]
    saved = {"status": "validated", "note": "Checked evidence", "evidence_ids": [event["id"]], "lat": 4.63, "lon": -74.15}
    docs["reviews"] = {"items": {incident_id: saved}}
    old_pointer = objects.data["04_gold/prisma/current.json"]
    objects.fail = "04_gold/prisma/snapshots/"
    with pytest.raises(RuntimeError):
        tick()
    assert objects.data["04_gold/prisma/current.json"] == old_pointer
    assert docs["reviews"]["items"][incident_id] == saved
    assert publications[previous["version"]]["incidents"][0]["review_status"] == "pending"
    attempted = docs["runtime"]["publication"]["version"]
    objects.fail = None
    recovered = tick()
    assert recovered["version"] == attempted
    assert recovered["incidents"][0]["review_status"] == "validated"
    assert recovered["incidents"][0]["review_note"] == "Checked evidence"
    assert (recovered["incidents"][0]["lat"], recovered["incidents"][0]["lon"]) == (4.63, -74.15)
    assert recovered["evidence"] == previous["evidence"]
    assert json.loads(objects.data["04_gold/prisma/current.json"])["version"] == recovered["version"]


def test_failed_classification_retries_bronze_without_fabricating_success(runtime):
    _, docs, _, lake, objects = runtime
    docs["simulation"] = {"revision": 1, "run_id": "run-one", "status": "running", "started_at": NOW, "elapsed_seconds": 0}
    raw = simulation_events(0)[0]
    write_objects(objects, CONFIG, [{**raw, "source_id": "run-one:" + raw["source_id"],
        "raw_metadata": {**raw["raw_metadata"], "scenario_run_id": "run-one"}}])
    def failure(_events):
        raise ValueError("untrusted-sensitive-error")
    pipeline.run(None, None, CONFIG, connection=object(), objects=objects, lake=lake,
                 client=object(), classifier=failure, clock=lambda: NOW)
    assert "untrusted-sensitive-error" not in json.dumps(docs)
    assert docs["status_pipeline"]["status"] == "pending"
    assert len(lake.data["bronze"]) == 1 and lake.data["silver"] == {}
    assert docs["checkpoint_enrichment"]["retry_at"] == NOW + 30
    # Arrival of another CSV is consumed even while enrichment is backing off.
    write_objects(objects, CONFIG, [{**raw, "source_id": "second-arrival"}])
    pipeline.run(None, None, CONFIG, connection=object(), objects=objects, lake=lake,
                 client=object(), classifier=failure, clock=lambda: NOW + 10)
    assert len(lake.data["bronze"]) == 2 and docs["checkpoint_enrichment"]["attempts"] == 1
    snapshot = pipeline.run(None, None, CONFIG, connection=object(), objects=objects, lake=lake, client=object(),
                            classifier=lambda events: [normalize_event(event) for event in events], clock=lambda: NOW + 31)
    assert len(lake.data["bronze"]) == 2 and len(lake.data["silver"]) == 1
    assert docs["status_pipeline"]["pending_count"] == 1
    assert len(snapshot["evidence"]) == 1
    assert snapshot["evidence"][0]["source_id"].startswith("run-one:")


def test_adb_projection_recovers_after_silver_merge_without_reclassifying(runtime, monkeypatch):
    log, docs, _, lake, _objects = runtime
    post = normalize_event(simulation_events(0)[0])
    lake.put("bronze", [post])
    projections = []
    def project(_db, _records, status, **_):
        projections.append(status)
        if status == "processed" and projections.count("processed") == 1:
            raise RuntimeError("Injected projection outage")
    monkeypatch.setattr(pipeline, "upsert_posts", project)
    with pytest.raises(RuntimeError, match="projection outage"):
        pipeline.enrich_pending(object(), lake, lambda records: records, NOW)
    assert post["id"] in lake.data["silver"]
    assert docs["checkpoint_enrichment"]["prepared"][0]["id"] == post["id"]
    result = pipeline.enrich_pending(object(), lake, lambda _: pytest.fail("Already classified"), NOW + 30)
    assert result["pending_count"] == 0 and docs["checkpoint_enrichment"]["prepared"] == []
    assert projections.count("processed") == 2


def test_later_rejection_keeps_each_valid_post_and_retries_only_the_selected_tail(runtime, monkeypatch):
    _, docs, _, lake, _ = runtime
    original = normalize_event(simulation_events(0)[0])
    posts = [{**original, "id": f"x:post-{index}", "source_id": f"post-{index}"} for index in range(10)]
    lake.put("bronze", posts)
    calls, processed = [], []
    monkeypatch.setattr(pipeline, "upsert_posts", lambda _db, rows, status, **_: processed.extend(
        item["id"] for item in rows if status == "processed"))

    def classify_one(rows):
        assert len(rows) == 1
        post = rows[0]
        calls.append(post["id"])
        if post["id"] == posts[3]["id"]:
            assert processed == [item["id"] for item in posts[:3]]
            raise ValueError("Claim evidence must quote the original post literally")
        return [{**post, "classification_method": "validated-once"}]

    result = pipeline.enrich_pending(object(), lake, classify_one, NOW, 1)
    saved = copy.deepcopy(lake.data["silver"])
    remaining = [item["id"] for item in posts[3:]]
    assert len(saved) == 3 and result["pending_count"] == 7
    assert docs["checkpoint_enrichment"]["pending_ids"] == remaining
    assert docs["checkpoint_enrichment"]["prepared"] == []
    assert result["last_error_reason"] == docs["checkpoint_enrichment"]["last_error_reason"] == "nonliteral_claim"
    lake.put("bronze", [{**original, "id": "x:new-arrival", "source_id": "new-arrival"}])
    pipeline.enrich_pending(object(), lake, lambda _: pytest.fail("Backoff must suppress model calls"), NOW + 10, 1)
    now = result["retry_at"]
    for attempt in range(2, 6):
        result = pipeline.enrich_pending(object(), lake, classify_one, now, 1)
        assert docs["checkpoint_enrichment"]["attempts"] == attempt
        assert docs["checkpoint_enrichment"]["pending_ids"] == remaining
        now = result["retry_at"] or now + 600
    assert result["needs_attention"] and result["pending_count"] == 8
    pipeline.enrich_pending(object(), lake, lambda _: pytest.fail("Open circuit must suppress model calls"), now, 1)
    assert calls == [item["id"] for item in posts[:3]] + [posts[3]["id"]] * 5
    # Explicit recovery keeps the selected IDs; an unrelated new arrival waits for the next batch.
    docs["checkpoint_enrichment"].update(attempts=0, retry_at=0, circuit_open=False)
    def recovered(rows):
        calls.extend(item["id"] for item in rows)
        return rows
    result = pipeline.enrich_pending(object(), lake, recovered, now, 1)
    assert result["pending_count"] == 1 and result["last_error_reason"] is None
    assert processed == [item["id"] for item in posts]
    assert all(lake.data["silver"][key] == value for key, value in saved.items())
    assert docs["checkpoint_enrichment"]["pending_ids"] == []
    assert docs["checkpoint_enrichment"]["last_error_reason"] is None
    assert calls[-7:] == remaining and "x:new-arrival" not in lake.data["silver"]


@pytest.mark.parametrize("failure_stage", ["silver", "adb", "journal_clear"])
def test_partial_batch_crash_finishes_prepared_post_before_classifying_the_tail(runtime, monkeypatch, failure_stage):
    _, docs, _, lake, _ = runtime
    original = normalize_event(simulation_events(0)[0])
    posts = [{**original, "id": f"x:post-{index}", "source_id": f"post-{index}"} for index in range(3)]
    lake.put("bronze", posts)
    calls, processed, failed = [], set(), False
    original_put, original_mutate = lake.put, pipeline.mutate_document

    def put(layer, rows):
        nonlocal failed
        if layer == "silver" and rows[0]["id"] == posts[1]["id"] and failure_stage == "silver" and not failed:
            failed = True
            raise RuntimeError("Injected storage boundary failure")
        original_put(layer, rows)

    def project(_db, rows, status, **_):
        nonlocal failed
        if status == "processed":
            if rows[0]["id"] == posts[1]["id"] and failure_stage == "adb" and not failed:
                failed = True
                raise RuntimeError("Injected storage boundary failure")
            processed.update(item["id"] for item in rows)

    def mutate(db, name, change):
        nonlocal failed
        current = copy.deepcopy(docs.get(name, {}))
        updated = change(current)
        if (failure_stage == "journal_clear" and not failed and current.get("prepared") and
                current["prepared"][0]["id"] == posts[1]["id"] and updated.get("prepared") == []):
            failed = True
            raise RuntimeError("Injected storage boundary failure")
        return original_mutate(db, name, change)

    def classify_one(rows):
        assert all(key in processed for key in calls), "Finish ADB projection before the next model call"
        calls.extend(item["id"] for item in rows)
        return rows

    monkeypatch.setattr(lake, "put", put)
    monkeypatch.setattr(pipeline, "upsert_posts", project)
    monkeypatch.setattr(pipeline, "mutate_document", mutate)
    with pytest.raises(RuntimeError, match="storage boundary"):
        pipeline.enrich_pending(object(), lake, classify_one, NOW, 1)
    assert calls == [item["id"] for item in posts[:2]]
    assert docs["checkpoint_enrichment"]["prepared"] == [posts[1]]
    assert docs["checkpoint_enrichment"]["pending_ids"] == [item["id"] for item in posts[1:]]
    lake.put("bronze", [{**original, "id": "x:new-arrival", "source_id": "new-arrival"}])
    result = pipeline.enrich_pending(object(), lake, classify_one, NOW + 1, 1)
    assert calls == [item["id"] for item in posts]  # The prepared post is never sent to the model twice.
    assert processed == set(calls) and result["pending_count"] == 1
    assert docs["checkpoint_enrichment"]["prepared"] == docs["checkpoint_enrichment"]["pending_ids"] == []


@pytest.mark.parametrize("error", [ValueError("credential=do-not-persist"),
    ValueError("Claim evidence must quote the original post literally: credential=do-not-persist"),
    RuntimeError("Claim evidence must quote the original post literally")])
def test_enrichment_error_reason_does_not_persist_untrusted_exception_text(runtime, error):
    _, docs, _, lake, _ = runtime
    lake.put("bronze", [normalize_event(simulation_events(0)[0])])
    def reject(_):
        raise error
    result = pipeline.enrich_pending(object(), lake, reject, NOW, 1)
    assert result["last_error_reason"] is None and docs["checkpoint_enrichment"]["last_error_reason"] is None
    assert str(error) not in json.dumps({"result": result, "documents": docs})


def test_persistent_runtime_keeps_ingestion_running_during_llm_failure(runtime, monkeypatch):
    _, docs, _, lake, objects = runtime
    docs["status_sensorstream"] = {"sensor_layers_version": 2}
    post = normalize_event(simulation_events(0)[0])
    lake.put("bronze", [post])
    query = SimpleNamespace(id="csv-query", isActive=True, recentProgress=[], lastProgress={}, exception=lambda: None)
    stopped = []
    query.stop = lambda: stopped.append(True)
    monkeypatch.setattr(pipeline, "start_landing", lambda *_args, **_kwargs: [("csv", query)])
    def next_batch(_seconds):
        assert docs["status_pipeline"]["status"] == "pending" and query.isActive
        lake.put("bronze", [{**post, "id": "x:second", "source_id": "second"}])
        raise KeyboardInterrupt()
    monkeypatch.setattr(pipeline.time, "sleep", next_batch)
    def limited(_events):
        raise RuntimeError("LLM unavailable")
    with pytest.raises(KeyboardInterrupt):
        pipeline.run_persistent(None, object(), objects, lake, CONFIG, None, limited, object(), lambda: NOW)
    assert len(lake.data["bronze"]) == 2 and lake.data["silver"] == {} and stopped == [True]


def test_publication_reads_silver_state_and_retains_previous_pointer_on_stage_failure(runtime):
    log, _, publications, lake, objects = runtime
    first = pipeline.publish_snapshot(object(), objects, lake, CONFIG, [], {}, {}, NOW)
    assert lake.current == first == publications[first["version"]]
    assert log.index("silver_current") < log.index("gold") < log.index("adb")
    pointer = objects.data["04_gold/prisma/current.json"]
    lake.fail = "silver_current"
    with pytest.raises(RuntimeError, match="Silver state"):
        pipeline.publish_snapshot(object(), objects, lake, CONFIG, [normalize_event(simulation_events(0)[0])], {}, {}, NOW + 1)
    assert objects.data["04_gold/prisma/current.json"] == pointer
    assert len(publications) == 1
    lake.fail = None
    retried = pipeline.publish_snapshot(object(), objects, lake, CONFIG, [normalize_event(simulation_events(0)[0])], {}, {}, NOW + 2)
    assert retried == lake.current == publications[retried["version"]]


def test_publisher_waits_for_matching_sensor_migration_without_stopping_landing(runtime, monkeypatch):
    _, docs, publications, lake, objects = runtime
    docs["status_sensorstream"] = {"sensor_layers_version": 2, "pipeline_revision": "older"}
    docs["checkpoint_reset"] = {"status": "pending", "ready": True}
    query = SimpleNamespace(isActive=True, exception=lambda: None, stop=lambda: None)
    monkeypatch.setattr(pipeline, "start_landing", lambda *_a, **_kw: [("csv", query)])
    monkeypatch.setattr(pipeline, "process_reset", lambda *_: pytest.fail("Reset cannot publish before the sensor migration"))
    def waiting(_seconds):
        assert query.isActive and not publications and "status_pipeline" not in docs
        raise KeyboardInterrupt()
    monkeypatch.setattr(pipeline.time, "sleep", waiting)
    with pytest.raises(KeyboardInterrupt):
        pipeline.run_persistent(None, object(), objects, lake, {**CONFIG, "pipeline_revision": "new"}, None, None, None, lambda: NOW)


def test_enrichment_circuit_bounds_failures_and_configuration_revision_reopens_it(runtime):
    _, docs, _, lake, _objects = runtime
    lake.put("bronze", [normalize_event(simulation_events(0)[0])])
    attempts = []
    def failure(events):
        attempts.append([item["id"] for item in events])
        raise RuntimeError("Provider unavailable")
    now = NOW
    for _ in range(5):
        result = pipeline.enrich_pending(object(), lake, failure, now, 1)
        now = result["retry_at"] or now + 600
    assert result["needs_attention"] and result["retry_at"] is None
    assert docs["checkpoint_enrichment"]["attempts"] == 5
    pipeline.enrich_pending(object(), lake, failure, now + 3600, 1)
    assert len(attempts) == 5
    result = pipeline.enrich_pending(object(), lake, lambda events: events, now + 3600, 2)
    assert result["pending_count"] == 0 and not result["needs_attention"]
    assert docs["checkpoint_enrichment"]["prepared"] == []


@pytest.mark.parametrize("pending,attention,capturing,enabled", [(2, False, False, True), (2, True, False, False),
                                                                (0, False, False, False), (2, True, True, True)])
def test_paused_capture_drains_pending_work_until_the_enrichment_circuit_opens(monkeypatch, pending, attention, capturing, enabled):
    documents = {"configuration": {"sources": {"x": {"enabled": True, "capture_running": capturing}}},
        "simulation": {}, "runtime": {}, "status_pipeline": {"pending_count": pending, "needs_attention": attention}}
    decisions = []
    monkeypatch.setattr(scheduling, "read_document", lambda _db, name: documents[name])
    monkeypatch.setattr(scheduling, "set_schedule", lambda _request, _runtime, value: decisions.append(value))
    scheduling.reconcile_after_tick(object(), object(), NOW)
    assert decisions == [enabled]


def test_queued_manual_run_cannot_bypass_persisted_x_rate_limit(runtime):
    _, docs, _, lake, objects = runtime
    docs["checkpoint_x"] = {"retry_at": NOW + 90, "cursor": {"next_token": "page2"}, "revision": 1}
    source = {**default_source("x"), "mode": "real", "credential_configured": True, "query": "#Bogota"}
    pipeline.poll_source(object(), objects, lake, CONFIG, source, {"requested_action": "run"}, None, NOW, object())
    assert docs["status_x"]["status"] == "rate_limited"
    assert docs["checkpoint_x"]["cursor"] == {"next_token": "page2"}
    assert docs["status_x"]["next_due"] == pipeline.utc_text(NOW + 90)


def test_disabled_real_source_allows_only_bounded_test_without_ingesting_or_advancing(runtime, monkeypatch):
    _, docs, _, lake, objects = runtime
    source = {**default_source("x"), "mode": "real", "enabled": False, "capture_running": True,
              "credential_configured": True, "query": "#Bogota", "query_version": 1}
    checkpoint = {"cursor": {"since_id": "15"}, "query_version": source["query_version"]}
    docs["checkpoint_x"] = copy.deepcopy(checkpoint)
    calls = []
    def fetch(*args, **kwargs):
        calls.append((args, kwargs))
        return [simulation_events(0)[0]], {"since_id": "30"}
    monkeypatch.setattr(x, "fetch_page", fetch)
    for status in ({}, {"requested_action": "run"}):
        pipeline.poll_source(object(), objects, lake, CONFIG, source, status, lambda **_: "test-token", NOW, object())
    assert calls == [] and "status_x" not in docs
    pipeline.poll_source(object(), objects, lake, CONFIG, source, {"requested_action": "test", "request_id": "test-1"},
                         lambda **_: "test-token", NOW, object())
    assert len(calls) == 1 and calls[0][1]["page_size"] == 10
    assert docs["status_x"]["status"] == "tested" and docs["status_x"]["requested_action"] is None
    assert docs["checkpoint_x"] == checkpoint and objects.data == {} and lake.data["bronze"] == {}
    assert not source["enabled"]  # Testing does not repair or start legacy configuration.
    docs["checkpoint_x"]["retry_at"] = NOW + 90
    pipeline.poll_source(object(), objects, lake, CONFIG, source, {"requested_action": "test"},
                         lambda **_: pytest.fail("Rate limit precedes credential read"), NOW, object())
    assert len(calls) == 1 and docs["status_x"]["status"] == "rate_limited"
    assert docs["checkpoint_x"]["cursor"] == checkpoint["cursor"]


def test_reset_paused_at_zero_has_no_new_synthetic_evidence(runtime):
    _, docs, _, lake, objects = runtime
    docs["simulation"] = {"status": "paused", "elapsed_seconds": 0, "run_id": "new-reset-run"}
    snapshot = pipeline.run(None, None, CONFIG, connection=object(), objects=objects, lake=lake,
        client=object(), classifier=lambda events: [normalize_event(item) for item in events], clock=lambda: NOW)
    assert snapshot["evidence"] == []


def test_old_tick_does_not_clear_newer_pending_request(runtime):
    _, docs, _, _, _ = runtime
    docs["status_x"] = {"requested_action": "test", "request_id": "new-request", "status": "queued"}
    pipeline._status(object(), "x", {"status": "ready", "last_run_at": pipeline.utc_text(NOW)}, "old-request")
    assert docs["status_x"]["requested_action"] == "test" and docs["status_x"]["status"] == "queued"
    assert pipeline._due({"configuration_revision": 1, "next_due": pipeline.utc_text(NOW + 300)}, 2, NOW)


def test_x_received_count_tracks_successful_pages_and_survives_later_error(runtime, monkeypatch):
    _, docs, _, lake, objects = runtime
    source = {**default_source("x"), "mode": "real", "credential_configured": True, "query": "#Bogota", "capture_running": True}
    pages = [([simulation_events(0)[0]], {"next_token": "page2"}), ([], {"since_id": "30"})]
    monkeypatch.setattr(x, "fetch_page", lambda *_args, **_kwargs: pages.pop(0))
    pipeline.poll_source(object(), objects, lake, CONFIG, source, {}, lambda **_: "fake-token", NOW, object())
    assert docs["status_x"]["last_received_count"] == 1
    def fail(*_args, **_kwargs):
        raise pipeline.XFailure("access_denied")
    monkeypatch.setattr(x, "fetch_page", fail)
    pipeline.poll_source(object(), objects, lake, CONFIG, source, {"requested_action": "run"}, lambda **_: "fake-token", NOW, object())
    assert docs["status_x"]["last_received_count"] == 1 and docs["status_x"]["last_error"] == "access_denied"


def test_classifier_closes_output_contract_and_preserves_simulation_provenance():
    event = normalize_event(simulation_events(0)[0])
    label = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "high", "confidence": 0.9, "mode": "real"}
    class Model:
        def chat(self, _request):
            text = json.dumps({"items": [label]})
            return SimpleNamespace(data=SimpleNamespace(chat_response=SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content=[SimpleNamespace(text=text)]))])))
    result = classify([event], CONFIG, client=Model())
    assert result[0]["mode"] == "Synthetic"
    assert result[0]["classification_method"] == "oci_genai:model"
    label["confidence"] = 8
    with pytest.raises(ValueError, match="confidence"):
        classify([event], CONFIG, client=Model())
    label.update(locality="Sin localizar", category="incendio", confidence=0.2)
    result = classify([event], CONFIG, client=Model())[0]
    assert result["category"] == "incendio" and result["lat"] is None and result["lon"] is None


def test_runtime_auth_constructs_ordinary_signer_sdk_clients_without_key_files():
    import oci
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    credentials = {"tenancy": "ocid1.tenancy.oc1..test", "user": "ocid1.user.oc1..test", "fingerprint": ":".join(["aa"] * 16), "private_key": key}
    config, signed = runtime_auth(lambda name, key: credentials[key], "us-ashburn-1")
    assert "key_file" not in config and config["key_content"] == key
    assert config["region"] == "us-ashburn-1"
    # Constructing clients exercises OCI's config validation without sending any request.
    oci.object_storage.ObjectStorageClient(config, signer=signed)
    oci.generative_ai_inference.GenerativeAiInferenceClient(config, signer=signed)
