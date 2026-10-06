"""A confirmed cancellation is terminal, retains progress, and never retries its UUID."""
import asyncio
import copy
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.gods_eye_view import pipeline, sensor_capture, sensor_pipeline, sensor_reset, synthetic_reset
from app.gods_eye_view.local import LocalGodsEyeViewRuntime
from test_gods_eye_view_pipeline import NOW
from test_gods_eye_view_sensor_reset import cloud_runtime


def cancelled(kind):
    return {"operation_id": str(uuid4()), "status": "cancelled", "stage": "history", "ready": False,
            "cancelled_at": "2026-10-05T18:49:41Z", "counts": {"sensor_events": 17},
            "replacements": {"old": "clean"}, "revision": 9,
            **({"sensor_type": kind} if kind else {})}


@pytest.mark.parametrize("kind", [None, "river_level", "all"])
@pytest.mark.parametrize("target", [None, "all"])
def test_cancelled_id_never_retries_and_new_operation_preserves_receipt(tmp_path, kind, target):
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    state = cancelled(kind)
    with runtime.store.connection() as db:
        runtime.store._put(db, "synthetic_reset", state)
    for call in (runtime.reset_synthetic(state["operation_id"]), runtime.reset_sensors("all", state["operation_id"]),
                 runtime.reset_sensors("river_level", state["operation_id"])):
        with pytest.raises(HTTPException) as error:
            asyncio.run(call)
        assert error.value.status_code == 409
    if kind:
        assert asyncio.run(runtime.sensor_reset_status("all"))["cancelled_at"] == state["cancelled_at"]
        sensor_reset.guard(state, kind)
    else:
        assert runtime.store.synthetic_reset_status()["status"] == "cancelled"
        runtime.store.ensure_synthetic_capture()
    operation = str(uuid4())
    result = asyncio.run(runtime.reset_sensors(target, operation) if target else runtime.reset_synthetic(operation))
    assert result["status"] == "completed" and result["operation_id"] == operation
    with runtime.store.connection() as db:
        saved = runtime.store._get(db, "synthetic_reset", {})
    assert saved["cancelled_operations"][state["operation_id"]] == state
    assert saved["cancelled_ids"] == [state["operation_id"]]
    assert saved["operation_scopes"][state["operation_id"]] == kind
    for scope in (None, "river_level", "all"):
        with pytest.raises(HTTPException):
            synthetic_reset.check_scope(saved, state["operation_id"], scope)


@pytest.mark.parametrize("kind", [None, "river_level", "all"])
def test_cloud_cancelled_retry_has_no_wake_or_mutation_and_preserves_archive(monkeypatch, kind):
    runtime = cloud_runtime(monkeypatch)
    state = cancelled(kind)
    runtime.documents["checkpoint_reset"] = state
    runtime.documents["runtime"]["synthetic_reset_version"] = 2
    for name in ("status_pipeline", "status_sensorstream"):
        runtime.documents[name]["sensor_reset_version"] = 2
    wake = MagicMock()
    monkeypatch.setattr(runtime, "_wake", wake)
    before = copy.deepcopy(runtime.documents)
    for call in (runtime.reset_synthetic(state["operation_id"]), runtime.reset_sensors("all", state["operation_id"])):
        with pytest.raises(HTTPException) as error:
            asyncio.run(call)
        assert error.value.status_code == 409
    assert runtime.documents == before
    wake.assert_not_called()
    new_id = str(uuid4())
    result = asyncio.run(runtime.reset_sensors("all", new_id))
    assert result["status"] == "pending" and result["operation_id"] == new_id
    assert runtime.documents["checkpoint_reset"]["cancelled_operations"][state["operation_id"]] == state
    wake.assert_called_once_with(new_id)


def test_cancelled_does_not_execute_or_hold_sensor_stream_and_error_is_not_cancellation(monkeypatch):
    state = cancelled("all")
    monkeypatch.setattr(pipeline, "read_document", lambda *_: state)
    assert pipeline.process_reset(None, None, None, {}, NOW) is None
    monkeypatch.setattr(sensor_reset.database, "read_document", lambda *_: state)
    query, lake = MagicMock(), MagicMock()
    assert sensor_pipeline.reset_barrier(None, lake, {}, query) == (query, False)
    query.stop.assert_not_called()
    lake.start.assert_not_called()
    config = {"capture_running": True}
    assert sensor_capture._capture_config(config, state) is config
    state["status"] = "error"
    with pytest.raises(HTTPException):
        sensor_reset.command(state, "all", str(uuid4()))
    assert sensor_capture._capture_config(config, state)["capture_running"] is False


@pytest.mark.parametrize("kind", [None, "rainfall"])
def test_late_submission_error_cannot_downgrade_cancelled(monkeypatch, kind):
    runtime = cloud_runtime(monkeypatch)
    runtime.documents["runtime"]["synthetic_reset_version"] = 2
    identifier = str(uuid4())
    def wake(_):
        runtime.documents["checkpoint_reset"].update(status="cancelled", ready=False, cancelled_at="2026-10-05T18:49:41Z", error=None)
        raise TimeoutError()
    monkeypatch.setattr(runtime, "_wake", wake)
    result = asyncio.run(runtime.reset_sensors(kind, identifier) if kind else runtime.reset_synthetic(identifier))
    assert result["status"] == "cancelled" and result["error"] is None
