import copy
import json

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
            return copy.deepcopy(self.job), {"etag": self.etag}
        return {"key": "run-1"}


class Runtime(CloudRuntime):
    def __init__(self):
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

    def _produce(self, force=False):
        return False


def test_native_finite_run_is_queued_before_idle_schedule_is_paused():
    runtime = Runtime()
    runtime._wake("request-1")
    create, get, update = runtime.client.calls
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


def test_scheduler_requires_etag_and_does_not_guess_conflicting_update():
    runtime = Runtime()
    runtime.client.etag = None
    with pytest.raises(RuntimeError, match="ETag"):
        scheduling.set_schedule(runtime.client._request, runtime._doc("runtime"), True)
    assert [call[0] for call in runtime.client.calls] == ["GET"]


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
    assert runtime.client.calls[0][0] == "POST"
    assert runtime.client.calls[-1][2]["payload"]["schedule"]["pauseStatus"] == "PAUSED"


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
