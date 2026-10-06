import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.gods_eye_view import capture, cloud, landing, pipeline
from app.gods_eye_view.api import CaptureScheduleUpdate, SensorFamilyUpdate, SourceUpdate
from app.gods_eye_view.core import default_source, publication_revisions, utc_text
from app.gods_eye_view.local import LocalGodsEyeViewRuntime
from test_gods_eye_view_capture import Producer, NOW
from test_gods_eye_view_cloud import Runtime


def configured(start=NOW, interval=1):
    return {"start_at": utc_text(start), "interval_minutes": interval, "config_version": 2}


@pytest.mark.parametrize("kind", ["social", "sensor"])
def test_schedule_save_is_atomic_and_preserves_capture_state(tmp_path, kind):
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    with runtime.store.connection() as db:
        runtime.store._put(db, "capture_controls", {"x": {"run_id": "keep", "anchor_at": NOW}})
        runtime.store._put(db, "synthetic:x", {"elapsed": 12, "emitted_ids": ["keep"]})
    before = runtime.store.source("x")
    values = {"start_at": "2026-10-05T09:05:00.999-05:00", "interval_minutes": 1440, "expected_revision": 1}
    result = asyncio.run(runtime.save_capture_schedule(kind, values))
    assert result == {"start_at": "2026-10-05T14:05:00Z", "interval_minutes": 1440, "config_version": 2}
    with pytest.raises(HTTPException) as error:
        asyncio.run(runtime.save_capture_schedule(kind, values))
    assert error.value.status_code == 409
    assert runtime.store.source("x") == before
    with runtime.store.connection() as db:
        assert runtime.store._get(db, "capture_controls", {})["x"]["run_id"] == "keep"
        assert runtime.store._get(db, "synthetic:x", {}) == {"elapsed": 12, "emitted_ids": ["keep"]}
    assert runtime.store.capture_schedule(kind) == result


def test_cloud_schedule_cas_keeps_other_configuration_and_cursors():
    runtime = Runtime()
    runtime.documents["configuration"] = {"sources": {"x": {"capture_running": True}}, "sensors": {"capture_running": False}}
    runtime.documents["checkpoint_synthetic"] = {"sources": {"x": {"elapsed": 600}}}
    before = copy.deepcopy(runtime.documents)
    values = {"start_at": utc_text(NOW + 60), "interval_minutes": 7, "expected_revision": 1}
    result = asyncio.run(runtime.save_capture_schedule("social", values))
    with pytest.raises(HTTPException) as error:
        asyncio.run(runtime.save_capture_schedule("social", values))
    assert error.value.status_code == 409 and result["config_version"] == 2
    assert runtime.documents["configuration"]["sources"] == before["configuration"]["sources"]
    assert runtime.documents["configuration"]["sensors"] == before["configuration"]["sensors"]
    assert runtime.documents["checkpoint_synthetic"] == before["checkpoint_synthetic"]
    assert runtime.client.calls == []


@pytest.mark.parametrize("start", [None, "", "not-date", "2026-10-05T14:00:00"])
def test_schedule_requires_aware_start(start):
    with pytest.raises(ValueError):
        capture.update_schedule({}, {"start_at": start, "interval_minutes": 5, "expected_revision": 1})


@pytest.mark.parametrize("value", [0, 1441, True, 1.5, "5"])
def test_schedule_interval_has_strict_boundary(value):
    with pytest.raises((ValueError, ValidationError)):
        CaptureScheduleUpdate(start_at=utc_text(NOW), interval_minutes=value, expected_revision=1)


@pytest.mark.parametrize("value", [0, 101, True, 1.5, "3"])
def test_batch_max_boundary(value):
    with pytest.raises(ValidationError):
        SourceUpdate(synthetic_batch_max=value)
    with pytest.raises(ValueError):
        capture.validate_source({**default_source("x"), "synthetic_batch_max": value})


def test_sensor_mode_rejects_unimplemented_real_before_io(tmp_path):
    with pytest.raises(ValidationError):
        SensorFamilyUpdate(expected_revision=1, mode="real")
    with pytest.raises(ValueError, match="Synthetic"):
        asyncio.run(LocalGodsEyeViewRuntime(tmp_path).update_sensors({"mode": "real", "expected_revision": 1}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(Runtime().update_sensors({"mode": "real", "expected_revision": 1}))
    assert error.value.status_code == 422


def test_shared_slots_skip_missed_intervals_and_force_cannot_repeat():
    shared = configured(NOW + 60, 2)
    assert capture.schedule_at(shared, NOW) == NOW + 60
    assert capture.schedule_at(shared, NOW + 550) == NOW + 540
    assert capture.schedule_at(shared, NOW + 550, NOW + 540) == NOW + 660
    source, control = default_source("x"), {"run_id": "slots", "anchor_at": NOW}
    assert capture.continuous_batch(source, control, {}, NOW, True, shared) is None
    _events, cursor = capture.continuous_batch(source, control, {}, NOW + 550, True, shared)
    assert cursor["capture_slot"] == NOW + 540
    assert capture.continuous_batch(source, control, cursor, NOW + 551, True, shared) is None


def test_random_bounded_progress_drains_all_records_once_across_restart_and_query_change():
    source = {**default_source("x"), "query": "", "synthetic_batch_max": 3}
    control = {"run_id": "bounded", "anchor_at": NOW, "dataset_version": "bogota-v2", "seed": 0}
    generation = capture.continuous_generation(source, control, {})
    expected = {item["source_id"] for item in capture.continuous_window(source, control, generation, 0, -1, 600)}
    cursor, seen, batches = {}, set(), []
    for step in range(20):
        result = capture.continuous_batch(source, control, copy.deepcopy(cursor), NOW + 600 + step * 60, True, configured())
        if result is None:
            break
        rows, cursor = result
        ids = {row["source_id"] for row in rows}
        assert len(rows) <= 3 and not ids & seen
        assert all(row["created_at"] <= utc_text(NOW + 600) for row in rows)
        seen |= ids
        batches.append(ids)
    assert seen == expected and len(batches) > 1 and capture.is_complete(control, cursor)
    assert cursor["next_due"] is None
    assert capture.continuous_batch({**source, "query": "Bogota"}, control, cursor, NOW + 86400, True, configured()) is None
    assert batches[0] != set(sorted(expected)[:3])  # Selection is not a chronological prefix.


def test_legacy_prefix_is_not_republished_when_migrating_bounded_cursor():
    source = {**default_source("x"), "query": "", "synthetic_batch_max": 1}
    control = {"run_id": "old", "anchor_at": NOW}
    previous = {"run_id": "old", "query": "", "elapsed": 90, "dataset_version": "bogota-v2", "dataset_seed": 0}
    generation = capture.continuous_generation(source, control, previous)
    prefix = {row["source_id"] for row in capture.continuous_window(source, control, generation, 0, -1, 90)}
    rows, migrated = capture.continuous_batch(source, control, previous, NOW + 600, True)
    assert len(rows) == 1 and not {row["source_id"] for row in rows} & prefix
    assert prefix < set(migrated["emitted_ids"])
    assert not migrated["complete"]
    assert previous["elapsed"] == 90 and "emitted_ids" not in previous


def test_cloud_retry_keeps_exact_pending_selection_even_after_schedule_moves_future(monkeypatch):
    clock = [NOW + 600]
    monkeypatch.setattr(cloud.time, "time", lambda: clock[0])
    runtime = Producer()
    runtime.docs.update({"simulation": {"status": "idle"},
        "configuration": {"sources": {"x": {**default_source("x"), "query": "", "capture_running": True}}, "social_schedule": configured()},
        "checkpoint_controls": {"x": {"run_id": "retry", "anchor_at": NOW, "dataset_version": "bogota-v2"}}})
    project = runtime._project_posts
    monkeypatch.setattr(runtime, "_project_posts", lambda *_: (_ for _ in ()).throw(RuntimeError("projection failed")))
    with pytest.raises(RuntimeError):
        runtime._produce(platform="x")
    pending = copy.deepcopy(runtime.docs["checkpoint_synthetic"]["pending"]["x"])
    files = copy.deepcopy(runtime.objects)
    assert not runtime.docs["checkpoint_synthetic"].get("sources")
    runtime.docs["configuration"]["social_schedule"] = configured(NOW + 10000)
    clock[0] += 120
    monkeypatch.setattr(runtime, "_project_posts", project)
    runtime._produce(platform="x")
    assert runtime.docs["checkpoint_synthetic"]["sources"]["x"] == pending[1]
    assert runtime.objects == files and not runtime.docs["checkpoint_synthetic"]["pending"]
    runtime._produce(platform="x", force=True)
    assert runtime.objects == files


def test_local_future_run_anchors_at_schedule_and_preserves_on_save(tmp_path):
    clock = [NOW]
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: clock[0])
    asyncio.run(runtime.save_capture_schedule("social", {"start_at": utc_text(NOW + 600), "interval_minutes": 1, "expected_revision": 1}))
    asyncio.run(runtime.run_source("x"))
    assert runtime.store.posts("x", 20)["total"] == 0
    with runtime.store.connection() as db:
        control = runtime.store._get(db, "capture_controls", {})["x"]
    assert control["anchor_at"] == NOW + 600
    assert asyncio.run(runtime.capture_status("social"))["next_capture_at"] == utc_text(NOW + 600)
    clock[0] += 600
    asyncio.run(runtime.tick())
    assert runtime.store.posts("x", 20)["total"] == 1
    asyncio.run(runtime.save_capture_schedule("social", {"start_at": utc_text(NOW + 1200), "interval_minutes": 2, "expected_revision": 2}))
    with runtime.store.connection() as db:
        assert runtime.store._get(db, "capture_controls", {})["x"] == control
    assert runtime.store.source("x")["capture_running"]


def test_publication_tokens_are_kind_specific_and_do_not_mutate_snapshot():
    original = {"incidents": [{"id": "one", "revision": 1, "updated_at": "first", "review_status": "pending",
        "correlation_context": {"social": {"posts": 1}, "sensors": {"count": 1}}}],
        "evidence": [{"id": "post"}], "event_posts": [], "sensors": [{"id": "s", "value": 1}]}
    previous = copy.deepcopy(original)
    tokens = publication_revisions(original)
    changed = copy.deepcopy(original)
    changed["sensors"][0]["value"] = 2
    changed["incidents"][0].update(revision=2, updated_at="second")
    changed["incidents"][0]["correlation_context"]["sensors"]["count"] = 2
    after = publication_revisions(changed)
    assert tokens["social_revision"] == after["social_revision"]
    assert tokens["sensor_revision"] != after["sensor_revision"]
    changed["incidents"][0]["review_status"] = "validated"
    assert publication_revisions(changed)["social_revision"] != tokens["social_revision"]
    assert original == previous


def test_cloud_capture_status_reads_pointer_only_and_does_not_claim_queue_completion(monkeypatch):
    runtime = Runtime()
    runtime.settings = SimpleNamespace(objectstorage_namespace="ns", bucket_name="gold")
    pointer = {"version": "gold-version", "social_revision": "social-1", "sensor_revision": "sensor-2", "published_at": utc_text(NOW)}
    keys = []
    def get_object(_namespace, _bucket, key):
        keys.append(key)
        return SimpleNamespace(data=SimpleNamespace(content=json.dumps(pointer).encode()))
    runtime.client.object_storage = SimpleNamespace(get_object=get_object)
    runtime.documents["configuration"] = {"social_schedule": configured(NOW + 60), "sources": {"x": {"capture_running": True}}}
    runtime.documents["status_pipeline"] = {"pending_count": 3}
    monkeypatch.setattr(cloud.time, "time", lambda: NOW)
    result = asyncio.run(runtime.capture_status("social"))
    assert keys == ["04_gold/prisma/current.json"]
    assert result["publication_revision"] == "social-1" and result["publication_version"] == "gold-version"
    assert result["processing_pending"] is True and result["next_capture_at"] == utc_text(NOW + 60)
    pointer.pop("social_revision")
    assert asyncio.run(runtime.capture_status("social"))["publication_revision"] is None


def test_native_real_x_run_obeys_future_and_same_slot_without_touching_cursor(monkeypatch):
    statuses, calls = [], []
    monkeypatch.setattr(pipeline, "_status", lambda _connection, _platform, values, _request: statuses.append(values))
    monkeypatch.setattr(pipeline, "_poll_x", lambda *_: calls.append("capture") or {"status": "ready"})
    source = {**default_source("x"), "mode": "real", "capture_running": True}
    config = {"social_schedule": configured(NOW + 60)}
    pipeline.poll_source(None, None, None, config, source, {"requested_action": "run"}, None, NOW, None)
    assert calls == [] and statuses[-1]["next_due"] == utc_text(NOW + 60)
    pipeline.poll_source(None, None, None, config, source, {"requested_action": "run"}, None, NOW + 60, None)
    assert calls == ["capture"] and statuses[-1]["capture_slot"] == NOW + 60
    pipeline.poll_source(None, None, None, config, source, {**statuses[-1], "requested_action": "run"}, None, NOW + 61, None)
    assert calls == ["capture"] and statuses[-1]["next_due"] == utc_text(NOW + 120)


def test_capture_status_respects_x_quota_without_mutating_it(tmp_path, monkeypatch):
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    runtime.store.update_source("x", {"mode": "real", "capture_running": True, "status": "rate_limited", "next_due": utc_text(NOW + 600)})
    asyncio.run(runtime.save_capture_schedule("social", {"start_at": utc_text(NOW), "interval_minutes": 1, "expected_revision": 1}))
    with runtime.store.connection() as db:
        runtime.store._put(db, "cursor:x", {"retry_at": NOW + 900})
    assert asyncio.run(runtime.capture_status("social"))["next_capture_at"] == utc_text(NOW + 900)
    assert runtime.store.checkpoint("x") == {"retry_at": NOW + 900}
    native = Runtime()
    native.settings = SimpleNamespace(objectstorage_namespace="ns", bucket_name="gold")
    native.client.object_storage = SimpleNamespace(get_object=lambda *_: SimpleNamespace(data=SimpleNamespace(content=b'{}')))
    native.documents.update({"configuration": {"social_schedule": configured(), "sources": {"x": {"mode": "real", "capture_running": True}}},
        "status_x": {"status": "rate_limited", "next_due": utc_text(NOW + 600)}, "checkpoint_x": {"retry_at": NOW + 900}})
    monkeypatch.setattr(cloud.time, "time", lambda: NOW)
    assert asyncio.run(native.capture_status("social"))["next_capture_at"] == utc_text(NOW + 900)
    native.documents["configuration"]["social_schedule"] = configured(NOW + 1200)
    assert asyncio.run(native.capture_status("social"))["next_capture_at"] == utc_text(NOW + 1200)
    assert native.documents["checkpoint_x"] == {"retry_at": NOW + 900}


def test_local_retry_persists_pending_before_landing_and_resumes_exactly(tmp_path, monkeypatch):
    clock = [NOW]
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: clock[0])
    asyncio.run(runtime.run_source("x"))
    clock[0] += 600
    original = runtime.store._events
    monkeypatch.setattr(runtime.store, "_events", lambda *_: (_ for _ in ()).throw(RuntimeError("projection failed")))
    with pytest.raises(RuntimeError):
        runtime.store.advance_simulation(platform="x")
    with runtime.store.connection() as db:
        pending = runtime.store._get(db, "synthetic:pending:x", None)
        assert pending and runtime.store._get(db, "synthetic:x", {})["elapsed"] == 0
    files = {path.name: path.read_bytes() for path in (tmp_path / "prisma-landing").glob("x-*.csv")}
    monkeypatch.setattr(runtime.store, "_events", original)
    asyncio.run(runtime.save_capture_schedule("social", {"start_at": utc_text(NOW + 10000), "interval_minutes": 5, "expected_revision": 1}))
    clock[0] += 60
    restarted = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: clock[0])
    restarted.store.advance_simulation(platform="x")
    with restarted.store.connection() as db:
        assert restarted.store._get(db, "synthetic:x", {}) == pending[1]
        assert restarted.store._get(db, "synthetic:pending:x", None) is None
    assert {path.name: path.read_bytes() for path in (tmp_path / "prisma-landing").glob("x-*.csv")} == files


def test_schedule_api_auth_validation_and_direct_response(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import LOCAL_COOKIE_NAME, create_app
    from app.security import issue_session
    app = create_app(Settings(local_development_mode=True, cookie_secure=False,
        aidp_settings_file=str(tmp_path / "settings.json"), session_secret_file=str(tmp_path / "session.key")))
    with TestClient(app) as client:
        assert client.get("/api/gods-eye-view/capture-status").status_code == 401
        client.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
        for kind, collection in (("social", "sources"), ("sensor", "sensors")):
            before = client.get("/api/admin/gods-eye-view/" + collection).json()
            assert before[kind + "_schedule"] == capture.schedule()
            endpoint = "/api/admin/gods-eye-view/" + kind + "-schedule"
            assert client.put(endpoint, json={"interval_minutes": 5, "expected_revision": 1}).status_code == 422
            values = {"start_at": utc_text(NOW + 86400), "interval_minutes": 9, "expected_revision": 1}
            response = client.put(endpoint, json=values)
            assert response.status_code == 200 and response.json() == configured(NOW + 86400, 9)
            assert client.put(endpoint, json=values).status_code == 409
        assert client.put("/api/admin/gods-eye-view/sensors/river_level", json={"mode": "real", "expected_revision": 1}).status_code == 422
        status = client.get("/api/gods-eye-view/capture-status?kind=sensors")
        assert status.status_code == 200 and status.json()["schedule"]["interval_minutes"] == 9
        assert client.get("/api/gods-eye-view/capture-status?kind=wrong").status_code == 422


@pytest.mark.parametrize("continuous", [False, True])
def test_future_start_blocks_new_legacy_auxiliary_records_even_forced(continuous):
    source = {"platform": "sire", "query": "", "interval_minutes": 1}
    control = {"run_id": "legacy", "anchor_at": NOW, "status": "running", "elapsed_seconds": 600}
    fn = capture.continuous_batch if continuous else capture.batch
    assert fn(source, control, {}, NOW + 600, True, configured(NOW + 900)) is None
    rows, _cursor = fn(source, control, {}, NOW + 900, True, configured(NOW + 900))
    assert rows and {row["platform"] for row in rows} == {"sire"}


def test_completed_social_waits_for_deferred_auxiliary_tail(tmp_path):
    clock = [NOW]
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: clock[0])
    asyncio.run(runtime.update_source("x", {"synthetic_batch_max": 100}))
    asyncio.run(runtime.run_source("x"))
    with runtime.store.connection() as db:
        controls = runtime.store._get(db, "capture_controls", {})
        source = runtime.store.source("x")
        previous = runtime.store._get(db, "synthetic:x", {})
        _rows, completed = capture.continuous_batch(source, controls["x"], previous, NOW + 600, True)
        runtime.store._put(db, "synthetic:x", completed)
    asyncio.run(runtime.save_capture_schedule("social", {"start_at": utc_text(NOW + 900), "interval_minutes": 1, "expected_revision": 1}))
    clock[0] = NOW + 600
    runtime.store.advance_simulation()
    assert runtime.store.source("x")["capture_running"]
    with runtime.store.connection() as db:
        assert runtime.store._get(db, "synthetic:sire", {})["elapsed"] == 0
    clock[0] = NOW + 900
    runtime.store.advance_simulation()
    assert not runtime.store.source("x")["capture_running"]
    with runtime.store.connection() as db:
        assert all(runtime.store._get(db, "synthetic:" + kind, {})["elapsed"] == 600 for kind in capture.INSTITUTIONAL)


def test_cloud_future_start_drains_prepared_tail_but_defers_new_auxiliary_records(monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(cloud.time, "time", lambda: clock[0])
    runtime = Producer()
    runtime.docs.update({"simulation": {"status": "idle"}, "configuration": {"sources": {"x": {"synthetic_batch_max": 100}}}})
    runtime._request_source("x", "run")
    upload = runtime.put_object
    def fail_sensor(namespace, bucket, key, body, **kwargs):
        if key.rsplit("/", 1)[-1].startswith("sensor-"):
            raise RuntimeError("sensor tail failed")
        return upload(namespace, bucket, key, body, **kwargs)
    monkeypatch.setattr(runtime, "put_object", fail_sensor)
    clock[0] += 600
    with pytest.raises(RuntimeError, match="sensor tail"):
        runtime._produce()
    prepared = copy.deepcopy(runtime.docs["checkpoint_synthetic"]["pending"]["sensor"])
    social = {key: body for key, body in runtime.objects.items() if key.rsplit("/", 1)[-1].startswith("x-")}
    runtime.docs["configuration"]["social_schedule"] = configured(NOW + 900)
    monkeypatch.setattr(runtime, "put_object", upload)
    runtime._produce(force=True)
    assert runtime.docs["checkpoint_synthetic"]["sources"]["sensor"] == prepared[1]
    assert runtime.docs["checkpoint_synthetic"]["sources"]["sire"]["elapsed"] == 0
    assert runtime.docs["configuration"]["sources"]["x"]["capture_running"]
    clock[0] += 300
    runtime._produce()
    assert not runtime.docs["configuration"]["sources"]["x"]["capture_running"]
    assert {key: body for key, body in runtime.objects.items() if key.rsplit("/", 1)[-1].startswith("x-")} == social
