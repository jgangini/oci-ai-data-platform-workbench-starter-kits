import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.prisma.local import LocalPrismaRuntime
from app.prisma.module import TerritorialModule, run_state


def test_native_run_state_accepts_nested_or_top_level_status_without_false_readiness():
    assert run_state({"status": "succeeded"}) == "SUCCEEDED"
    assert run_state({"state": {"status": "running"}, "status": "succeeded"}) == "RUNNING"
    assert run_state({}) == ""


def test_local_activation_is_persistent_idempotent_and_preserves_source_state(tmp_path):
    runtime = LocalPrismaRuntime(tmp_path)
    runtime.store.update_source("x", {"query": "Bogotá inundación", "enabled": False})
    settings = SimpleNamespace(local_development_mode=True, prisma_enabled=True)
    module = TerritorialModule(settings, runtime)
    first = asyncio.run(module.status(True))
    second = asyncio.run(TerritorialModule(settings, LocalPrismaRuntime(tmp_path)).status(True))
    assert first == second and first["runtime"] == "local_fixture" and first["enabled"]
    assert runtime.store.source("x")["query"] == "Bogotá inundación"
    assert runtime.store.simulation_state()["status"] == "idle"


def test_missing_infrastructure_never_attempts_native_or_vm_mutation():
    module = TerritorialModule(SimpleNamespace(local_development_mode=False, prisma_enabled=False), None)
    assert asyncio.run(module.status())["status"] == "deployment_required"
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.status(True))
    assert error.value.status_code == 409


@pytest.mark.parametrize("task_status", ["SUCCEEDED", "FAILED", "RUNNING"])
def test_cloud_activation_requires_native_job_task_and_snapshot_and_deduplicates(monkeypatch, task_status):
    document, calls = {}, []
    module = TerritorialModule(SimpleNamespace(local_development_mode=False, prisma_enabled=True),
                               SimpleNamespace(_snapshot=lambda: {"version": "native-publication"}))
    client = SimpleNamespace(
        _request=lambda method, path, **_: calls.append((method, path)) or (
            {"key": "run"} if method == "POST" else {"state": {"status": "SUCCEEDED"}}),
        _list=lambda *_args, **_kwargs: [{"taskKey": "prisma_tick", "state": {"status": task_status}}],
    )
    monkeypatch.setattr(module, "_prerequisites", lambda: (client, {"workspace_key": "workspace", "job_key": "job"}))
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
    module = TerritorialModule(SimpleNamespace(local_development_mode=False, prisma_enabled=True), None)
    monkeypatch.setattr(module, "_read", lambda: {})
    monkeypatch.setattr(module, "_prerequisites", lambda: (_ for _ in ()).throw(RuntimeError("not ready")))
    monkeypatch.setattr(module, "_write", lambda _: pytest.fail("No state mutation before native readiness"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.status(True))
    assert error.value.status_code == 503
