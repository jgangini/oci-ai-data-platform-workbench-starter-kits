import asyncio
import copy
import json
import threading

import pytest
from fastapi import HTTPException

from app.prisma import database, scheduling
from app.prisma.cloud import CloudRuntime
from app.prisma.core import default_source


class Aidp:
    def __init__(self):
        self.calls = []
        self.etag = "revision-1"
        self.run_pages = {None: ({"items": []}, {})}
        self.job = {"name": "PRISMA", "tasks": [{"key": "tick"}], "timeoutSeconds": 900,
                    "schedule": {"pauseStatus": "PAUSED"}, "key": "not-a-write-field"}

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET":
            if path.endswith("/jobRuns"):
                return copy.deepcopy(self.run_pages[kwargs["params"].get("page")])
            return copy.deepcopy(self.job), {"etag": self.etag} if self.etag else {}
        return {"key": "run-1"}


class Runtime(CloudRuntime):
    def __init__(self):
        self.capture_lock = threading.RLock()
        self.documents = {"runtime": {"workspace_key": "workspace", "job_key": "job"}}
        self.client = Aidp()
        self.aidp_factory = lambda: self.client

    def _doc(self, name):
        return copy.deepcopy(self.documents.get(name, {"revision": 0}))

    def _change(self, name, change):
        old = self._doc(name)
        updated = {**change(old), "revision": old.get("revision", 0) + 1}
        self.documents[name] = copy.deepcopy(updated)
        return updated

    def _credential(self, *_):
        raise AssertionError("Invalid changes must not write credentials")

    def _produce(self, force=False, platform=None):
        return False

    def _project_posts(self, events, now, key):
        self.projected = (events, now, key)


def test_review_note_limit_is_enforced_before_cloud_io(monkeypatch):
    runtime = Runtime()
    calls = []
    async def io(function, *args):
        calls.append(args)
        return {"review_status": args[1]}
    monkeypatch.setattr(runtime, "_io", io)
    assert asyncio.run(runtime.review("incident-1", "validated", "n" * 1000)) == {"review_status": "validated"}
    with pytest.raises(ValueError, match="1000"):
        asyncio.run(runtime.review("incident-1", "rejected", "n" * 1001))
    assert calls == [("incident-1", "validated", "n" * 1000)]


@pytest.mark.parametrize("lat,lon", [(True, -74), (float("nan"), -74), (4, float("inf")),
                                      (91, 0), (0, -181), (4, None), (None, -74)])
def test_invalid_review_coordinates_fail_before_cloud_io(monkeypatch, lat, lon):
    runtime = Runtime()
    async def unexpected_io(*_args):
        pytest.fail("Invalid coordinates must not reach cloud I/O")
    monkeypatch.setattr(runtime, "_io", unexpected_io)
    with pytest.raises(ValueError):
        asyncio.run(runtime.review("incident-1", "pending", "", lat=lat, lon=lon))


def test_review_coordinate_lock_uses_durable_control_when_publication_is_stale(monkeypatch):
    runtime = Runtime()
    incident = {"id": "incident-1", "lat": 4.62, "lon": -74.16, "review_status": "pending", "evidence_ids": ["x:1"]}
    monkeypatch.setattr(runtime, "_snapshot", lambda: {"incidents": [copy.deepcopy(incident)]})
    saved = asyncio.run(runtime.review("incident-1", "validated", "Located", ["x:1"], 4.63, -74.15))
    assert (saved["lat"], saved["lon"], saved["location_method"]) == (4.63, -74.15, "human_review")
    assert saved["review_pending_publication"] is True and runtime._snapshot()["incidents"][0] == incident
    previous = copy.deepcopy(runtime.documents["reviews"])
    calls = list(runtime.client.calls)
    for status in ("validated", "pending", "rejected"):
        with pytest.raises(HTTPException) as conflict:
            asyncio.run(runtime.review("incident-1", status, "Must not move yet", ["x:1"], 4.64, -74.15))
        assert conflict.value.status_code == 409
        assert runtime.documents["reviews"] == previous and runtime.client.calls == calls
    # The published snapshot can also lag the unlock: the durable state remains authoritative.
    incident["review_status"] = "validated"
    asyncio.run(runtime.review("incident-1", "pending", "Unlock only"))
    moved = asyncio.run(runtime.review("incident-1", "rejected", "Moved after unlock", ["x:1"], 4.64, -74.15))
    retained = asyncio.run(runtime.review("incident-1", "pending", "Keep coordinates"))
    assert (moved["lat"], retained["lat"], retained["lon"]) == (4.64, 4.64, -74.15)
    assert runtime.documents["reviews"]["items"]["incident-1"]["lat"] == 4.64


@pytest.mark.parametrize("status", ["validated", "rejected", "pending"])
def test_cloud_review_returns_saved_note_and_evidence_but_awaits_publication(monkeypatch, status):
    runtime = Runtime()
    incident = {"id": "incident-1", "review_status": "pending", "review_note": "Old note",
                "evidence_ids": ["x:current"], "reviewed_evidence_ids": ["x:older"]}
    monkeypatch.setattr(runtime, "_snapshot", lambda: {"incidents": [copy.deepcopy(incident)]})
    result = asyncio.run(runtime.review("incident-1", status, "New human review"))
    saved = runtime.documents["reviews"]["items"]["incident-1"]
    assert saved["status"] == result["review_status"] == status
    assert saved["note"] == result["review_note"] == "New human review"
    assert saved["evidence_ids"] == result["reviewed_evidence_ids"] == ["x:current"]
    assert saved["updated_at"] and result["review_saved"] is True and result["review_pending_publication"] is True
    assert runtime._snapshot()["incidents"][0]["review_note"] == "Old note"
    assert [call[0] for call in runtime.client.calls] == ["GET", "PUT", "POST"]
    assert runtime.client.calls[1][2]["payload"]["schedule"]["pauseStatus"] == "PAUSED"


def test_review_wake_failure_retains_saved_control_and_reports_publication_pending(monkeypatch):
    runtime = Runtime()
    monkeypatch.setattr(runtime, "_snapshot", lambda: {"incidents": [{"id": "incident-1", "evidence_ids": ["x:1"]}]})
    def failed_wake(_request_id):
        raise RuntimeError("PRIVATE native job response")
    monkeypatch.setattr(runtime, "_wake", failed_wake)
    result = asyncio.run(runtime.review("incident-1", "validated", "Saved before job failure"))
    assert result["review_saved"] is True and result["review_pending_publication"] is True
    assert result["publication_error"] == "Review saved, but publication could not be started. Try again."
    assert "PRIVATE" not in json.dumps(result)
    assert runtime.documents["reviews"]["items"]["incident-1"]["status"] == "validated"
    assert "event_registry" not in runtime.documents


def test_cloud_review_rejects_changed_evidence_before_saving_or_waking(monkeypatch):
    runtime = Runtime()
    monkeypatch.setattr(runtime, "_snapshot", lambda: {"incidents": [{"id": "incident-1", "evidence_ids": ["x:1", "x:2"]}]})
    with pytest.raises(HTTPException) as conflict:
        asyncio.run(runtime.review("incident-1", "validated", "Only saw first post", ["x:1"]))
    assert conflict.value.status_code == 409
    assert "reviews" not in runtime.documents and not runtime.client.calls
    result = asyncio.run(runtime.review("incident-1", "validated", "Checked both posts", ["x:2", "x:1"]))
    assert result["review_saved"] is True and result["reviewed_evidence_ids"] == ["x:1", "x:2"]


def test_explicit_finite_run_is_queued_after_idle_schedule_is_paused():
    runtime = Runtime()
    runtime._wake("request-1")
    get, update, create = runtime.client.calls
    assert create[:2] == ("POST", "/workspaces/workspace/jobRuns")
    assert create[2]["payload"] == {"jobKey": "job", "parameters": [], "queue": {"isEnabled": True}}
    assert create[2]["retry_scope"] == "prisma-run:request-1"
    assert get[:2] == ("GET", "/workspaces/workspace/jobs/job")
    assert update[2]["headers"] == {"If-Match": "revision-1"}
    body = update[2]["payload"]
    assert body["maxConcurrentRuns"] == 1 and body["queue"] == {"isEnabled": False}
    assert body["schedule"]["pauseStatus"] == "PAUSED"
    assert body["tasks"] == [{"key": "tick"}] and body["timeoutSeconds"] == 900 and "key" not in body


@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "QUEUED", "CANCELING", "PAUSED_MAINTENANCE", ""])
def test_persistent_wake_reuses_nonterminal_or_unknown_run_without_cron_or_queue(state):
    runtime = Runtime()
    runtime.client.job.update(tasks=[{"taskKey": "prisma_tick", "isStreaming": True}],
                              continuous={"pauseStatus": "UNPAUSED"})
    runtime.client.run_pages[None] = ({"items": [{"key": "live", "jobKey": "job", "state": {"status": state}}]}, {})
    runtime.documents["configuration"] = {"sources": {"x": {"enabled": True, "capture_running": True}}}
    runtime._wake("first")
    runtime._wake("repeat")
    assert [call[0] for call in runtime.client.calls] == ["GET", "PUT", "GET"] * 2
    for _, _, options in runtime.client.calls[1::3]:
        assert options["payload"]["schedule"]["pauseStatus"] == "PAUSED"
        assert options["payload"]["continuous"]["pauseStatus"] == "PAUSED"
        assert options["payload"]["maxConcurrentRuns"] == 1
        assert options["payload"]["queue"] == {"isEnabled": False}


def test_persistent_restart_checks_all_pages_and_never_enqueues_a_run():
    runtime = Runtime()
    runtime.client.run_pages = {
        None: ({"items": [{"key": "done", "jobKey": "job", "state": {"status": "SUCCESS"}}]}, {"opc-next-page": "next"}),
        "next": ({"items": [{"key": "other", "jobKey": "unrelated", "state": {"status": "RUNNING"}},
                            {"key": "failed", "jobKey": "job", "state": {"status": "FAILED"}}]}, {})}
    scheduling.submit_run(runtime.client._request, runtime._doc("runtime"), "restart", persistent=True)
    assert [call[0] for call in runtime.client.calls] == ["GET", "GET", "POST"]
    assert runtime.client.calls[0][2]["params"]["jobKey"] == "job"
    assert runtime.client.calls[-1][2]["payload"]["queue"] == {"isEnabled": False}


@pytest.mark.parametrize("pages", [
    {None: ({"items": [{"jobKey": "job"}]}, {})},
    {None: ({}, {})},
    {None: ({"items": []}, {"opc-next-page": "same"}), "same": ({"items": []}, {"opc-next-page": "same"})},
    {**{None: ({"items": []}, {"opc-next-page": "1"})},
     **{str(page): ({"items": []}, {"opc-next-page": str(page + 1)}) for page in range(1, 6)}},
])
def test_persistent_run_fails_closed_on_incomplete_or_unbounded_inspection(pages):
    runtime = Runtime()
    runtime.client.run_pages = pages
    with pytest.raises(RuntimeError, match="Native run inspection"):
        scheduling.submit_run(runtime.client._request, runtime._doc("runtime"), "unsafe", persistent=True)
    assert all(call[0] == "GET" for call in runtime.client.calls)
    assert len(runtime.client.calls) <= 5


def test_schedule_follows_active_work_and_stops_after_ten_minutes():
    simulation = {"status": "running", "elapsed_seconds": 0, "started_at": 1000, "capture_complete": True}
    assert scheduling.needs_schedule({}, simulation, 1599)
    assert not scheduling.needs_schedule({}, simulation, 1600)
    sources = {"sources": {"x": {"enabled": True, "mode": "real", "capture_running": True}}}
    assert scheduling.needs_schedule(sources, simulation, 1600)
    sources["sources"]["x"]["enabled"] = False
    assert not scheduling.needs_schedule(sources, {"status": "paused"}, 1200)


@pytest.mark.parametrize("mode", ["real", "simulation"])
def test_continuous_schedule_survives_tomorrow_until_source_is_stopped(mode):
    source = {**default_source("x"), "mode": mode, "capture_running": True, "interval_minutes": 5}
    configuration = {"sources": {"x": source}}
    assert scheduling.needs_schedule(configuration, {"status": "completed"}, 1000 + 86400)
    assert scheduling.needs_schedule(configuration, {"status": "idle"}, 1000 + 7 * 86400)
    source["enabled"] = False
    assert not scheduling.needs_schedule(configuration, {}, 1000 + 86400)
    source.update(enabled=True, capture_running=False)
    assert not scheduling.needs_schedule(configuration, {}, 1000 + 86400)
    del source["capture_running"]
    assert not scheduling.needs_schedule(configuration, {}, 1000 + 86400)


@pytest.mark.parametrize("etag", [None, "revision-1"])
def test_scheduler_accepts_optional_etag_and_preserves_native_job_fields(etag):
    runtime = Runtime()
    runtime.client.etag = etag
    preserved = {"runAs": "operator", "continuous": {"pauseStatus": "PAUSED"},
                 "gitConfig": {"url": "https://example.com/source", "branch": "main"},
                 "parameters": [{"name": "area", "default": "Bogota"}],
                 "path": "/Workspace/prisma", "description": "Operational job",
                 "jobClusters": [{"clusterKey": "compute"}]}
    runtime.client.job.update(preserved)
    original = copy.deepcopy(runtime.client.job)
    scheduling.set_schedule(runtime.client._request, runtime._doc("runtime"), True)
    assert [call[0] for call in runtime.client.calls] == ["GET", "PUT"]
    update = runtime.client.calls[-1][2]
    assert update["headers"] == ({"If-Match": etag} if etag else None)
    assert all(update["payload"][name] == value for name, value in preserved.items())
    assert update["payload"]["tasks"] == original["tasks"] and "key" not in update["payload"]
    assert update["payload"]["schedule"]["pauseStatus"] == "UNPAUSED"
    assert runtime.client.job == original


@pytest.mark.parametrize("active", [False, True])
def test_save_only_queues_a_run_when_capture_is_active(active):
    import asyncio
    runtime = Runtime()
    runtime.documents["configuration"] = {"sources": {"x": {**default_source("x"), "capture_running": active}}}
    result = asyncio.run(runtime.update_source("x", {"interval_minutes": 7}))
    assert result["interval_minutes"] == 7 and result["capture_running"] is active
    assert [call[0] for call in runtime.client.calls] == (["GET", "PUT", "POST"] if active else ["GET", "PUT"])
    assert runtime.client.calls[1][2]["payload"]["schedule"]["pauseStatus"] == ("UNPAUSED" if active else "PAUSED")


@pytest.mark.parametrize("job_timeout,task_timeout", [(None, 0), (0, None), (600, 300)])
def test_scheduler_omits_default_timeouts_without_mutating_native_get(job_timeout, task_timeout):
    job = {"name": "PRISMA", "timeoutSeconds": job_timeout,
           "tasks": [{"taskKey": "prisma_tick", "isStreaming": True, "timeoutSeconds": task_timeout}]}
    original, writes = copy.deepcopy(job), []
    def request(method, path, **options):
        if method == "GET":
            return job, {}
        writes.append(options["payload"])
    scheduling.set_schedule(request, {"workspace_key": "workspace", "job_key": "job"}, True)
    payload = writes[0]
    assert ("timeoutSeconds" in payload) == (job_timeout not in (None, 0))
    assert ("timeoutSeconds" in payload["tasks"][0]) == (task_timeout not in (None, 0))
    if job_timeout:
        assert payload["timeoutSeconds"] == job_timeout
        assert payload["tasks"][0]["timeoutSeconds"] == task_timeout
    assert payload["schedule"]["pauseStatus"] == "PAUSED"
    assert job == original


@pytest.mark.parametrize("mode", ["real", "simulation"])
@pytest.mark.parametrize("other_active", [False, True])
def test_stopping_capture_queues_one_final_drain_then_idle_save_does_not(mode, other_active):
    import asyncio
    runtime = Runtime()
    runtime.documents["configuration"] = {"sources": {
        "x": {**default_source("x"), "mode": mode, "capture_running": True},
        "facebook": {**default_source("facebook"), "capture_running": other_active}}}
    runtime.documents["checkpoint_synthetic"] = {"sources": {"x": {"elapsed": 60, "batch_key": "retained"}}}
    checkpoint = copy.deepcopy(runtime.documents["checkpoint_synthetic"])
    result = asyncio.run(runtime.update_source("x", {"enabled": False}))
    assert not result["enabled"] and not result["capture_running"]
    assert [call[0] for call in runtime.client.calls] == ["GET", "PUT", "POST"]
    assert runtime.client.calls[1][2]["payload"]["schedule"]["pauseStatus"] == ("UNPAUSED" if other_active else "PAUSED")
    assert runtime.documents["checkpoint_synthetic"] == checkpoint
    runtime.client.calls.clear()
    asyncio.run(runtime.update_source("x", {"enabled": False}))
    assert [call[0] for call in runtime.client.calls] == (["GET", "PUT", "POST"] if other_active else ["GET", "PUT"])


def test_mode_switch_stops_capture_and_queues_final_drain():
    import asyncio
    runtime = Runtime()
    runtime.documents["configuration"] = {"sources": {"x": {**default_source("x"), "capture_running": True}}}
    result = asyncio.run(runtime.update_source("x", {"mode": "real"}))
    assert result["mode"] == "real" and not result["capture_running"]
    assert [call[0] for call in runtime.client.calls] == ["GET", "PUT", "POST"]
    assert runtime.client.calls[1][2]["payload"]["schedule"]["pauseStatus"] == "PAUSED"


def test_failed_final_drain_is_observable_and_explicit_run_can_retry(monkeypatch):
    import asyncio
    runtime = Runtime()
    runtime.documents["configuration"] = {"sources": {"x": {**default_source("x"), "capture_running": True}}}
    runtime.documents["checkpoint_synthetic"] = {"sources": {"x": {"elapsed": 60, "batch_key": "retained"}}}
    runtime.documents["status_synthetic"] = {"last_landing_key": "01_landing/prisma/raw/retained.csv"}
    checkpoint, landing_status = copy.deepcopy(runtime.documents["checkpoint_synthetic"]), copy.deepcopy(runtime.documents["status_synthetic"])
    request = runtime.client._request
    def fail_submit(method, path, **kwargs):
        result = request(method, path, **kwargs)
        if method == "POST":
            raise RuntimeError("Injected submit failure")
        return result
    monkeypatch.setattr(runtime.client, "_request", fail_submit)
    with pytest.raises(HTTPException) as error:
        asyncio.run(runtime.update_source("x", {"enabled": False}))
    assert error.value.status_code == 503
    assert not runtime.documents["configuration"]["sources"]["x"]["capture_running"]
    assert runtime.documents["checkpoint_synthetic"] == checkpoint and runtime.documents["status_synthetic"] == landing_status
    monkeypatch.setattr(runtime.client, "_request", request)
    runtime.client.calls.clear()
    result = asyncio.run(runtime.run_source("x"))
    assert result["source"]["enabled"] and result["source"]["capture_running"]
    assert [call[0] for call in runtime.client.calls] == ["GET", "PUT", "POST"]
    assert runtime.documents["checkpoint_synthetic"] == checkpoint and runtime.documents["status_synthetic"] == landing_status


def test_explicit_run_waiting_behind_disable_save_reactivates_after_save(monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    runtime = Runtime()
    runtime.documents["configuration"] = {"sources": {"x": default_source("x")}}
    save_started, finish_save, run_waiting = threading.Event(), threading.Event(), threading.Event()
    lock = threading.RLock()
    class CaptureLock:
        def __enter__(self):
            if save_started.is_set():
                run_waiting.set()
            lock.acquire()
        def __exit__(self, *_):
            lock.release()
    runtime.capture_lock = CaptureLock()
    update = runtime._update
    def delayed_update(platform, payload):
        save_started.set()
        assert finish_save.wait(5), "Test did not release the Save"
        return update(platform, payload)
    monkeypatch.setattr(runtime, "_update", delayed_update)
    with ThreadPoolExecutor(max_workers=2) as workers:
        save = workers.submit(lambda: asyncio.run(runtime.update_source("x", {"enabled": False})))
        try:
            assert save_started.wait(5)
            run = workers.submit(lambda: asyncio.run(runtime.run_source("x")))
            assert run_waiting.wait(5), "Run did not acquire the Save's lock"
        finally:
            finish_save.set()
        assert not save.result(timeout=5)["enabled"]
        result = run.result(timeout=5)
    assert result["source"]["enabled"] and result["source"]["capture_running"]
    source = runtime.documents["configuration"]["sources"]["x"]
    assert source["enabled"] and source["capture_running"] and not source["capture_paused"]
    assert runtime.documents["checkpoint_controls"]["x"]["run_id"]
    assert [call[0] for call in runtime.client.calls] == ["GET", "PUT", "GET", "PUT", "POST"]


@pytest.mark.parametrize("running", [False, True])
def test_explicit_run_repairs_legacy_disabled_source_even_with_stale_running_flag(running):
    runtime = Runtime()
    runtime.documents["configuration"] = {"sources": {"x": {**default_source("x"), "enabled": False, "capture_running": running}}}
    result = asyncio.run(runtime.run_source("x"))
    saved = runtime.documents["configuration"]["sources"]["x"]
    assert saved["enabled"] and saved["capture_running"] and not saved["capture_paused"]
    assert result["source"]["enabled"] and runtime.documents["checkpoint_controls"]["x"]["run_id"]


@pytest.mark.parametrize("mode", ["Synthetic", "real"])
def test_connection_test_of_disabled_source_never_enables_capture(mode):
    runtime = Runtime()
    source = {**default_source("x"), "mode": mode, "enabled": False, "capture_running": False, "credential_configured": True}
    runtime.documents["configuration"] = {"sources": {"x": source}}
    runtime.documents["checkpoint_x"] = {"cursor": {"since_id": "15"}}
    configuration, checkpoint = copy.deepcopy(runtime.documents["configuration"]), copy.deepcopy(runtime.documents["checkpoint_x"])
    result = asyncio.run(runtime.test_source("x"))
    assert result["status"] == ("queued" if mode == "real" else "simulation")
    assert runtime.documents["configuration"] == configuration and runtime.documents["checkpoint_x"] == checkpoint
    assert "checkpoint_controls" not in runtime.documents
    if mode == "real":
        assert runtime.documents["status_x"]["requested_action"] == "test"
        assert runtime.client.calls[1][2]["payload"]["schedule"]["pauseStatus"] == "PAUSED"
    else:
        assert runtime.client.calls == []


@pytest.mark.parametrize("action", ["test_source", "run_source"])
def test_disabled_real_source_without_credential_cannot_activate_or_queue(action):
    runtime = Runtime()
    runtime.documents["configuration"] = {"sources": {"x": {**default_source("x"), "mode": "real", "enabled": False}}}
    before = copy.deepcopy(runtime.documents)
    with pytest.raises(HTTPException) as missing:
        asyncio.run(getattr(runtime, action)("x"))
    assert missing.value.status_code == 409
    assert runtime.documents == before and runtime.client.calls == []


def test_stop_waits_for_inflight_csv_before_disabling_and_submitting_drain(monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from app.prisma import cloud
    runtime = Runtime()
    source = {**default_source("x"), "capture_running": True}
    runtime.documents["configuration"] = {"sources": {"x": source}}
    runtime.client.object_storage = object()
    upload_started, finish_upload, save_waiting = threading.Event(), threading.Event(), threading.Event()
    order = []
    class CaptureLock:
        def __enter__(self):
            if upload_started.is_set():
                save_waiting.set()
            lock.acquire()
        def __exit__(self, *_):
            lock.release()
    lock = threading.RLock()
    runtime.capture_lock = CaptureLock()
    monkeypatch.setattr(cloud.capture, "inputs", lambda *_: [(source, {}, True)])
    monkeypatch.setattr(cloud.capture, "continuous_batch", lambda *_: ([{"id": "last-evidence"}], {"batch_key": "last", "next_due": 1000}))
    def upload(*_):
        upload_started.set()
        assert finish_upload.wait(5), "Test did not release the in-flight upload"
        order.append("csv_durable")
        return "01_landing/prisma/raw/last.csv"
    monkeypatch.setattr(cloud.landing, "write_objects", upload)
    request = runtime.client._request
    def record_request(method, path, **kwargs):
        order.append(method)
        return request(method, path, **kwargs)
    monkeypatch.setattr(runtime.client, "_request", record_request)
    with ThreadPoolExecutor(max_workers=2) as workers:
        producer = workers.submit(CloudRuntime._produce, runtime)
        try:
            assert upload_started.wait(5)
            stop = workers.submit(lambda: asyncio.run(runtime.update_source("x", {"enabled": False})))
            assert save_waiting.wait(5), "Save did not acquire the producer's lock"
            assert runtime.documents["configuration"]["sources"]["x"]["enabled"] and runtime.client.calls == []
        finally:
            finish_upload.set()
        producer.result(timeout=5)
        result = stop.result(timeout=5)
    assert not result["capture_running"]
    assert order == ["csv_durable", "GET", "PUT", "POST"]
    assert runtime.documents["checkpoint_synthetic"]["sources"]["x"]["batch_key"] == "last"
    assert runtime.documents["status_synthetic"]["last_landing_key"] == "01_landing/prisma/raw/last.csv"


def test_schedule_failure_does_not_submit_a_finite_run(monkeypatch):
    runtime = Runtime()
    request = runtime.client._request
    def conflict(method, path, **kwargs):
        result = request(method, path, **kwargs)
        if method == "PUT":
            raise RuntimeError("HTTP 412: job changed")
        return result
    monkeypatch.setattr(runtime.client, "_request", conflict)
    with pytest.raises(RuntimeError, match="412"):
        runtime._wake("explicit-request")
    assert [call[0] for call in runtime.client.calls] == ["GET", "PUT"]


def test_tick_rechecks_fresh_control_state_before_pausing(monkeypatch):
    runtime = Runtime()
    runtime.documents["simulation"] = {"status": "running", "started_at": 1000, "elapsed_seconds": 0, "capture_complete": True}
    monkeypatch.setattr(scheduling, "read_document", lambda _connection, name: runtime._doc(name))
    scheduling.reconcile_after_tick(object(), runtime.client._request, 1600)
    assert runtime.client.calls[-1][2]["payload"]["schedule"]["pauseStatus"] == "PAUSED"


@pytest.mark.parametrize("platform", ["facebook", "instagram", "tiktok"])
def test_real_platform_rejected_before_storing_credential(platform):
    runtime = Runtime()
    with pytest.raises(HTTPException) as error:
        runtime._update(platform, {"mode": "real", "bearer_token": "test-token"})
    assert error.value.status_code == 422 and "configuration" not in runtime.documents


def test_invalid_synthetic_query_remains_a_validation_error_through_cloud_io():
    import asyncio
    runtime = Runtime()
    with pytest.raises(HTTPException) as error:
        asyncio.run(runtime.update_source("x", {"query": "Bogota OR"}))
    assert error.value.status_code == 422 and "Invalid synthetic query" in error.value.detail
    assert "configuration" not in runtime.documents


def test_stale_source_status_cannot_replace_current_configuration():
    runtime = Runtime()
    runtime.documents["configuration"] = {"revision": 2, "sources": {"x": {
        **default_source("x"), "enabled": False, "mode": "simulation", "query": "Bogotá"}}}
    runtime.documents["status_x"] = {"configuration_revision": 1, "enabled": True,
        "mode": "real", "query": "wrong", "status": "ready", "next_due": "2099-01-01T00:00:00Z"}
    source = runtime._sources()["sources"][0]
    assert source["enabled"] is False and source["mode"] == "Synthetic" and source["query"] == "Bogotá"
    assert source["status"] == "disabled" and source["next_due"] is None


def test_disabling_source_cancels_its_pending_run_without_erasing_cursor():
    runtime = Runtime()
    runtime.documents["configuration"] = {"sources": {"x": {**default_source("x"), "mode": "real", "capture_running": True}}}
    runtime.documents["status_x"] = {"requested_action": "run", "request_id": "pending", "last_received_count": 7}
    runtime.documents["checkpoint_x"] = {"cursor": {"since_id": "30"}}
    assert runtime._update("x", {"enabled": False})["capture_running"] is False
    runtime._update("x", {"enabled": True})
    assert runtime.documents["status_x"]["requested_action"] is None
    assert runtime.documents["status_x"]["last_received_count"] == 7
    assert runtime.documents["checkpoint_x"]["cursor"]["since_id"] == "30"
    assert not scheduling.needs_schedule(runtime.documents["configuration"], {})


def test_pause_keeps_pending_request_and_queues_finite_publication():
    runtime = Runtime()
    runtime.documents["status_x"] = {"requested_action": "test", "request_id": "earlier"}
    runtime._simulation("pause")
    assert runtime.documents["status_x"]["request_id"] == "earlier"
    assert [call[0] for call in runtime.client.calls] == ["GET", "PUT", "POST"]
    assert runtime.client.calls[1][2]["payload"]["schedule"]["pauseStatus"] == "PAUSED"


def test_cloud_pause_resume_keeps_event_anchor_and_replay_replaces_it(monkeypatch):
    from app.prisma import cloud
    clock = [1000]
    monkeypatch.setattr(cloud.time, "time", lambda: clock[0])
    runtime = Runtime()
    started = runtime._simulation("start")
    clock[0] += 120
    paused = runtime._simulation("pause")
    clock[0] += 90
    resumed = runtime._simulation("resume")
    assert paused["elapsed_seconds"] == resumed["elapsed_seconds"] == 120
    assert started["anchor_at"] == resumed["anchor_at"] == 1000
    replayed = runtime._simulation("replay")
    assert replayed["anchor_at"] == 1210 and replayed["run_id"] != started["run_id"]


@pytest.mark.parametrize("name,expected", [(None, 0), ("configuration", None), ("configuration", -1),
    ("configuration", 0.5), ("configuration", True), ("configuration\n", 0)])
def test_database_guards_reject_invalid_input_before_cursor(name, expected):
    with pytest.raises(ValueError):
        database.write_document(object(), name, {}, expected)


def test_database_rejects_nan_and_missing_publication_contract_before_cursor():
    with pytest.raises(ValueError):
        database.write_document(object(), "configuration", {"value": float("nan")}, 0)
    for snapshot in ({"version": None}, {"version": "gold-x", "incidents": None, "evidence": []}):
        with pytest.raises(ValueError):
            database.publish(object(), snapshot)
    # Oracle three-valued logic requires explicit NULL guards in the privileged package too.
    assert "v_revision IS NULL" in database.PACKAGE_BODY and "p_expected IS NULL" in database.PACKAGE_BODY
    assert "v_version IS NULL" in database.PACKAGE_BODY and "p_document IS NULL" in database.PACKAGE_BODY


def test_document_read_rejects_missing_revision():
    class Connection:
        def cursor(self):
            return self
        def callfunc(self, *_):
            return json.dumps({"revision": None})
    with pytest.raises(ValueError, match="revision"):
        database.read_document(Connection(), "configuration")
