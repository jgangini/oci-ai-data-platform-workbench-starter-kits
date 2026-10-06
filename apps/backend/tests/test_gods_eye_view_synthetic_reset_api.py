import asyncio
import copy
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import LOCAL_COOKIE_NAME, create_app
from app.gods_eye_view.cloud import CloudRuntime
from app.gods_eye_view.core import default_source
from app.security import issue_session
from test_gods_eye_view_cloud import Runtime


def cloud_runtime():
    runtime = Runtime()
    runtime.documents["runtime"]["synthetic_reset_version"] = 2
    runtime.documents["configuration"] = {"sources": {
        "x": {**default_source("x"), "mode": "real", "capture_running": True, "credential_configured": True},
        "facebook": {**default_source("facebook"), "capture_running": True, "query": "#mydemo"}}}
    runtime.documents["checkpoint_x"] = {"queries": {"#real": {"since_id": "50"}}}
    runtime.documents["simulation"] = {"status": "running", "started_at": 1, "elapsed_seconds": 0}
    return runtime


def test_cloud_request_stops_only_synthetic_and_waits_for_native_completion(monkeypatch):
    runtime = cloud_runtime()
    prepare = runtime._prepare_reset
    def preparing():
        assert runtime.documents["checkpoint_reset"]["stage"] == "preparing"
        return prepare()
    monkeypatch.setattr(runtime, "_prepare_reset", preparing)
    real = copy.deepcopy(runtime.documents["configuration"]["sources"]["x"])
    checkpoint = copy.deepcopy(runtime.documents["checkpoint_x"])
    operation = str(uuid4())
    result = asyncio.run(runtime.reset_synthetic(operation))
    assert result["status"] == "pending" and result["ready"] and result["operation_id"] == operation
    assert result["stage"] == "waiting_for_aidp"
    assert runtime.documents["configuration"]["sources"]["x"] == real
    assert runtime.documents["checkpoint_x"] == checkpoint
    source = runtime.documents["configuration"]["sources"]["facebook"]
    assert source["query"] == "#mydemo" and not source["capture_running"] and not source["capture_paused"]
    assert runtime.documents["simulation"]["status"] == "paused"
    assert runtime._sources()["synthetic_reset"]["status"] == "pending"
    assert CloudRuntime._produce(runtime, force=True) is False
    for action in (lambda: runtime._request_source("facebook", "run"),
                   lambda: runtime._simulation("start"),
                   lambda: asyncio.run(runtime.update_source("facebook", {"mode": "real"})),
                   lambda: runtime._review("old", "validated", "review")):
        with pytest.raises(HTTPException) as error:
            action()
        assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        runtime._reset_synthetic(str(uuid4()))
    assert error.value.status_code == 409


def test_cloud_retry_keeps_operation_identity_and_completed_replays_never_delete_new_data(monkeypatch):
    runtime = cloud_runtime()
    operation = str(uuid4())
    wake = runtime._wake
    monkeypatch.setattr(runtime, "_wake", lambda *_: (_ for _ in ()).throw(RuntimeError("private connection details")))
    failed = runtime._reset_synthetic(operation)
    assert failed["status"] == "error" and failed["ready"]
    assert "private" not in failed["error"]
    configuration = copy.deepcopy(runtime.documents["configuration"])
    monkeypatch.setattr(runtime, "_wake", wake)
    assert runtime._reset_synthetic(operation)["status"] == "pending"
    assert runtime.documents["configuration"] == configuration
    runtime.documents["checkpoint_reset"].update(status="completed", version="clean", completed_ids=[operation])
    runtime.documents["configuration"]["sources"]["facebook"]["capture_running"] = True
    assert runtime._reset_synthetic(operation)["status"] == "completed"
    assert runtime.documents["configuration"]["sources"]["facebook"]["capture_running"]
    next_operation = str(uuid4())
    runtime._reset_synthetic(next_operation)
    state = copy.deepcopy(runtime.documents)
    assert runtime._reset_synthetic(operation) == {"operation_id": operation, "status": "completed", "stage": "completed"}
    assert runtime.documents == state


@pytest.mark.parametrize("status", ["pending", "completed"])
def test_cloud_same_operation_keeps_native_progress_on_retry(status):
    runtime = cloud_runtime()
    operation = str(uuid4())
    runtime._reset_synthetic(operation)
    runtime.documents["checkpoint_reset"].update(status=status, stage="history" if status == "pending" else "completed")
    result = runtime._reset_synthetic(operation)
    assert result["status"] == status
    assert result["stage"] == ("history" if status == "pending" else "completed")


@pytest.mark.parametrize("version", [None, 1])
def test_cloud_old_workflow_is_rejected_before_any_mutation(version):
    runtime = Runtime()
    runtime.documents["runtime"]["synthetic_reset_version"] = version
    before = copy.deepcopy(runtime.documents)
    with pytest.raises(HTTPException) as error:
        runtime._reset_synthetic(str(uuid4()))
    assert error.value.status_code == 501
    assert runtime.documents == before and runtime.client.calls == []


def test_admin_reset_requires_explicit_confirmation_and_global_scope(tmp_path):
    settings = Settings(local_development_mode=True, cookie_secure=False,
        aidp_settings_file=str(tmp_path / "settings.json"), session_secret_file=str(tmp_path / "session.key"))
    app = create_app(settings)
    path = "/api/admin/gods-eye-view/synthetic/reset"
    operation = str(uuid4())
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        assert client.post(path, json={"operation_id": operation, "confirm": True}).status_code == 401
        client.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
        for payload in ({"operation_id": operation}, {"operation_id": operation, "confirm": False},
                        {"operation_id": operation, "confirm": "true"}, {"operation_id": operation, "confirm": 1},
                        {"operation_id": "invalid", "confirm": True},
                        {"operation_id": operation, "confirm": True, "platform": "x"}):
            assert client.post(path, json=payload).status_code == 422
        assert not client.get(path).json().get("operation_id")
        result = client.post(path, json={"operation_id": operation, "confirm": True})
        assert result.status_code == 200 and result.json()["status"] == "completed"
        assert client.get(path).json()["operation_id"] == operation
