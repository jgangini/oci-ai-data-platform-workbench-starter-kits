import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.aidp import AidpClient
from app.config import Settings
from app.territorial.local import LocalTerritorialRuntime
from app.territorial.module import TerritorialModule, run_state


def test_native_run_state_accepts_nested_or_top_level_status_without_false_readiness():
    assert run_state({"status": "succeeded"}) == "SUCCEEDED"
    assert run_state({"state": {"status": "running"}, "status": "succeeded"}) == "RUNNING"
    assert run_state({}) == ""


def test_local_activation_is_persistent_idempotent_and_preserves_source_state(tmp_path):
    runtime = LocalTerritorialRuntime(tmp_path)
    runtime.store.update_source("x", {"query": "Bogotá inundación", "enabled": False})
    settings = Settings(local_development_mode=True, territorial_enabled=True)
    module = TerritorialModule(settings, runtime)
    first = asyncio.run(module.status(True))
    second = asyncio.run(TerritorialModule(settings, LocalTerritorialRuntime(tmp_path)).status(True))
    assert first == second and first["runtime"] == "local_fixture" and first["enabled"]
    assert runtime.store.source("x")["query"] == "Bogotá inundación"
    assert runtime.store.simulation_state()["status"] == "idle"


def test_missing_infrastructure_never_attempts_native_or_vm_mutation():
    module = TerritorialModule(Settings(local_development_mode=False, territorial_enabled=False), None)
    assert asyncio.run(module.status())["status"] == "deployment_required"
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.status(True))
    assert error.value.status_code == 409


@pytest.mark.parametrize("task_status", ["SUCCEEDED", "FAILED", "RUNNING"])
def test_cloud_activation_requires_native_job_task_and_snapshot_and_deduplicates(monkeypatch, task_status):
    document, calls = {}, []
    module = TerritorialModule(Settings(local_development_mode=False, territorial_enabled=True),
                               SimpleNamespace(_snapshot=lambda: {"version": "native-publication"}))
    client = SimpleNamespace(
        _request=lambda method, path, **_: calls.append((method, path)) or (
            {"key": "run"} if method == "POST" else {"state": {"status": "SUCCEEDED"}}),
        _list=lambda *_args, **_kwargs: [{"taskKey": "prisma_tick", "state": {"status": task_status}}],
    )
    monkeypatch.setattr(module, "_prerequisites", lambda **_: (client, {"workspace_key": "workspace", "job_key": "job"}, []))
    monkeypatch.setattr(module, "_read", lambda: dict(document))
    monkeypatch.setattr(module, "_write", lambda values: document.update(values) or dict(document))
    first = asyncio.run(module.status(True))
    assert first["status"] == "activating" and not first["enabled"]
    second = asyncio.run(module.status())
    assert second["enabled"] is (task_status == "SUCCEEDED")
    assert second["status"] == {"SUCCEEDED": "ready", "FAILED": "failed", "RUNNING": "activating"}[task_status]
    if task_status != "FAILED":
        asyncio.run(module.status(True))
    assert len([call for call in calls if call[0] == "POST"]) == 1


def test_failed_prerequisites_cannot_start_or_enable_module(monkeypatch):
    module = TerritorialModule(Settings(local_development_mode=False, territorial_enabled=True), None)
    monkeypatch.setattr(module, "_read", lambda: {})
    monkeypatch.setattr(module, "_prerequisites", lambda **_: (_ for _ in ()).throw(RuntimeError("not ready")))
    monkeypatch.setattr(module, "_write", lambda _: pytest.fail("No state mutation before native readiness"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.status(True))
    assert error.value.status_code == 503


@pytest.mark.parametrize("state", [{}, {"status": "activating", "run_key": None, "enabled": False}])
def test_persistent_job_cannot_be_queued_for_finite_module_activation(monkeypatch, state):
    calls = []
    client = SimpleNamespace(_request=lambda method, path, **_: calls.append((method, path)) or {
        "tasks": [{"taskKey": "prisma_tick", "isStreaming": True}]})
    runtime = SimpleNamespace(_doc=lambda _: {"workspace_key": "ws", "job_key": "job"}, aidp_factory=lambda: client)
    module = TerritorialModule(Settings(local_development_mode=False, territorial_enabled=True), runtime)
    monkeypatch.setattr(module, "_read", lambda: state)
    monkeypatch.setattr(module, "_write", lambda _: pytest.fail("Activation must not mutate state or enqueue streaming"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.status(True))
    assert error.value.status_code == 409
    assert calls == [("GET", "/workspaces/ws/jobs/job")]


@pytest.mark.parametrize("failure", [None, "missing_sensor", "same_job", "wrong_task", "finite_sensor", "foreign_run",
                                    "pending_run", "failed_task", "missing_task", "inactive_agent", "viewer", "snapshot", "restarted_task"])
def test_existing_persistent_workflows_are_verified_without_starting_or_changing_jobs(monkeypatch, failure):
    from app.territorial import module as implementation

    calls, writes, document = [], [], {"status": "activating", "enabled": False, "operation_id": "existing-operation"}
    config = {"workspace_key": "ws", "job_key": "social", "sensor_job_key": "sensors", "streaming_mode": "persistent",
              "namespace": "ns", "bucket": "bucket", "region": "us-chicago-1"}
    if failure == "missing_sensor":
        config.pop("sensor_job_key")
    if failure == "same_job":
        config["sensor_job_key"] = "social"

    def request(method, path, **kwargs):
        calls.append((method, path))
        assert method == "GET", "Activation must not create or alter native jobs, compute or agents"
        if "/jobs/" in path:
            sensor = path.endswith("/sensors")
            return {"tasks": [{"taskKey": "wrong" if sensor and failure == "wrong_task" else "sensor_stream" if sensor else "prisma_tick",
                                "isStreaming": not (sensor and failure == "finite_sensor")}]}
        if path.endswith("/jobRuns"):
            job = kwargs["params"]["jobKey"]
            return {"items": [{"key": job + "-run", "jobKey": "foreign" if failure == "foreign_run" else job,
                                "status": "PENDING" if failure == "pending_run" else "RUNNING"}]}, {}
        assert path == "/workspaces/ws/agents/agent/deployments/deployment"
        return {"lifecycleState": "FAILED" if failure == "inactive_agent" else "ACTIVE"}

    def tasks(path, *, params):
        calls.append(("GET", path))
        assert params["sortBy"] == "timeCreated" and params["sortOrder"] == "ASC"
        if failure == "restarted_task":
            task = "sensor_stream" if params["jobRunKey"] == "sensors-run" else "prisma_tick"
            return [{"taskKey": task, "status": "FAILED"}, {"taskKey": task, "status": "RUNNING"}]
        return [] if failure == "missing_task" else [{"taskKey": "sensor_stream" if params["jobRunKey"] == "sensors-run" else "prisma_tick",
                                                     "status": "FAILED" if failure == "failed_task" else "RUNNING"}]

    def get_object(namespace, bucket, key):
        assert (namespace, bucket, key) == ("ns", "bucket", ".control/prisma/agent.json")
        calls.append(("GET", key))
        return SimpleNamespace(data=SimpleNamespace(content=json.dumps({"agent_key": "agent", "deployment_key": "deployment",
            "endpoint": "https://gateway.aidp.us-chicago-1.oci.oraclecloud.com/agentendpoint/fixture/chat"})))

    class Viewer:
        def __init__(self, **_):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def get(self, url):
            assert url == "http://private-viewer/ready"
            calls.append(("GET", url))
            if failure == "viewer":
                raise RuntimeError("not ready")
            return SimpleNamespace(raise_for_status=lambda: None)

    def snapshot():
        calls.append(("READ", "snapshot"))
        if failure == "snapshot":
            raise RuntimeError("incomplete publication")
        return {"version": "gold-verified"}

    client = SimpleNamespace(_request=request, _list=tasks, object_storage=SimpleNamespace(get_object=get_object))
    runtime = SimpleNamespace(_doc=lambda _: dict(config), aidp_factory=lambda: client, _snapshot=snapshot)
    module = TerritorialModule(Settings(local_development_mode=False, territorial_enabled=True), runtime)
    monkeypatch.setenv("PRISMA_VIEWER_URL", "http://private-viewer/")
    monkeypatch.setattr(implementation.httpx, "Client", Viewer)
    monkeypatch.setattr(module, "_read", lambda: dict(document))
    monkeypatch.setattr(module, "_write", lambda value: writes.append(value) or document.update(value) or dict(document))
    if failure not in (None, "restarted_task"):
        with pytest.raises(HTTPException):
            asyncio.run(module.status(True))
        assert writes == []
    else:
        before = asyncio.run(module.status())
        assert not before["enabled"] and writes == [], "Reading readiness must not activate the module"
        activated = asyncio.run(module.status(True))
        assert activated["status"] == "ready" and activated["enabled"] and activated["operation_id"] == "existing-operation"
        assert document["run_key"] == "social-run" and document["sensor_run_key"] == "sensors-run"
        assert document["snapshot_version"] == "gold-verified"
        assert asyncio.run(module.status(True)) == activated
        assert len(writes) == 1, "Rechecking an enabled module must be idempotent"
    assert all(method in {"GET", "READ"} for method, _ in calls)


@pytest.mark.parametrize("task_key,task_status,expected", [
    ("prisma_tick", "SUCCESS", "ready"), ("other", "SUCCESS", "failed"),
    ("prisma_tick", "RUNNING", "activating"), ("prisma_tick", "FAILED", "failed"),
    ("prisma_tick", "INTERNAL_ERROR", "failed"), ("prisma_tick", "UPSTREAM_FAILED", "failed"),
    ("prisma_tick", "UPSTREAM_CANCELED", "failed"), ("prisma_tick", "EXCLUDED", "failed"),
])
def test_activation_checks_all_native_task_attempts_across_pages(monkeypatch, task_key, task_status, expected):
    pages = []
    def request(method, path, **kwargs):
        assert method == "GET"
        if "/jobRuns/" in path:
            return {"state": {"status": "SUCCESS"}}
        assert path.endswith("/taskRuns")
        query = kwargs["params"]
        assert {key: query[key] for key in ("jobRunKey", "sortBy", "sortOrder", "limit")} == {
            "jobRunKey": "run", "sortBy": "timeCreated", "sortOrder": "ASC", "limit": 100}
        pages.append(query.get("page"))
        task = {"taskKey": task_key, "state": {"status": task_status}} if query.get("page") else {
            "taskKey": "prisma_tick", "state": {"status": "SUCCESS"}}
        return {"items": [task]}, {} if query.get("page") else {"opc-next-page": "second"}
    client = SimpleNamespace(_request=request, _page_items=AidpClient._page_items)
    client._list = lambda path, **kwargs: AidpClient._list(client, path, **kwargs)
    module = TerritorialModule(Settings(local_development_mode=False),
        SimpleNamespace(_snapshot=lambda: {"version": "verified"}))
    monkeypatch.setattr(module, "_write", lambda value: value)
    result = module._poll({"status": "activating", "enabled": False, "run_key": "run"}, client, {"workspace_key": "ws"})
    assert result["status"] == expected and result["enabled"] is (expected == "ready")
    assert pages == [None, "second"]
