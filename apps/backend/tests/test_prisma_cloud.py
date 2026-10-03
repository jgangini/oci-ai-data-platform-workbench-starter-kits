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
        self.job = {"name": "PRISMA", "tasks": [{"key": "tick"}], "timeoutSeconds": 900,
                    "schedule": {"pauseStatus": "PAUSED"}, "key": "not-a-write-field"}

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET":
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
    with pytest.raises(HTTPException) as disabled:
        asyncio.run(runtime.run_source("x"))
    assert disabled.value.status_code == 409 and runtime.client.calls == []
    asyncio.run(runtime.update_source("x", {"enabled": True}))
    assert [call[0] for call in runtime.client.calls] == ["GET", "PUT"]
    runtime.client.calls.clear()
    asyncio.run(runtime.run_source("x"))
    assert [call[0] for call in runtime.client.calls] == ["GET", "PUT", "POST"]
    assert runtime.documents["checkpoint_synthetic"] == checkpoint and runtime.documents["status_synthetic"] == landing_status


def test_run_waiting_behind_disable_save_rechecks_source_and_cannot_reactivate(monkeypatch):
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
        with pytest.raises(HTTPException) as disabled:
            run.result(timeout=5)
    assert disabled.value.status_code == 409
    source = runtime.documents["configuration"]["sources"]["x"]
    assert not source["enabled"] and not source["capture_running"]
    assert "checkpoint_controls" not in runtime.documents
    assert [call[0] for call in runtime.client.calls] == ["GET", "PUT"]


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
    assert source["enabled"] is False and source["mode"] == "simulation" and source["query"] == "Bogotá"
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
