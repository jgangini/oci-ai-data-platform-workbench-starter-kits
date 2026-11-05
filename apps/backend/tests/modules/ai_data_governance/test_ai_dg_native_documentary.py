from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.modules.ai_data_governance.native_documentary import admit_documentary_execution


JOB = "d40e4c62-71d9-4837-8609-5479d950f6d5"
RUN = "a40e4c62-71d9-4837-8609-5479d950f6d5"
TASK = "b40e4c62-71d9-4837-8609-5479d950f6d5"
BINDING = {"workspace_key": "workspace", "job_key": JOB, "model_id": "model"}


def batch():
    return {"execution": {"job_key": JOB, "job_run_id": RUN, "scope_id": "scope", "inventory_run_id": "inventory"},
            "batch": {"admissions": [{"scope_id": "scope", "model_ref": "model", "prompt_version": "DGD-PROMPTS-2"}]}}


def state():
    return {"status": "RUNNING", "isUserCanceledOrTimedOut": False}


def responses():
    return [{"key": RUN, "jobKey": JOB, "state": state(), "taskToTaskRunMap": {"document_metadata": TASK}},
            {"key": TASK, "jobKey": JOB, "jobRunKey": RUN, "taskKey": "document_metadata", "state": state(),
             "task": {"type": "AGENT_TASK"}, "startTime": 1793503990689, "endTime": None}]


def test_only_the_configured_active_workflow_and_documentary_task_are_admitted():
    run, task = responses()
    request = Mock(side_effect=[(run, {}), (task, {})])
    admit_documentary_execution(request, BINDING, batch())
    assert [c.args[:2] for c in request.call_args_list] == [
        ("GET", "/workspaces/workspace/jobRuns/" + RUN), ("GET", "/workspaces/workspace/taskRuns/" + TASK)]


def test_native_pending_agent_task_with_start_time_is_admitted_during_a2a_invocation():
    run, task = responses()
    task["state"] = {"status": "PENDING", "isUserCanceledOrTimedOut": None}
    request = Mock(side_effect=[(run, {}), (task, {})])
    assert admit_documentary_execution(request, BINDING, batch()) == TASK


@pytest.mark.parametrize("change", [{"startTime": None}, {"startTime": 0}, {"startTime": True},
    {"endTime": 1793504000000}, {"task": {"type": "PYTHON_TASK"}},
    {"state": {"status": "BLOCKED"}}, {"state": {"status": "SUCCESS"}},
    {"state": {"status": "PENDING", "isUserCanceledOrTimedOut": True}}])
def test_pending_task_cannot_bypass_start_completion_cancellation_or_type_checks(change):
    run, task = responses()
    task["state"] = {"status": "PENDING", "isUserCanceledOrTimedOut": None}
    task.update(change)
    with pytest.raises(ValueError):
        admit_documentary_execution(Mock(side_effect=[(run, {}), (task, {})]), BINDING, batch())


@pytest.mark.parametrize("target,changes", [(0, {"key": TASK}), (0, {"jobKey": "other"}),
    (0, {"state": {"status": "SUCCESS"}}), (0, {"taskToTaskRunMap": {}}),
    (1, {"jobRunKey": "other"}), (1, {"taskKey": "publish_metadata"}),
    (1, {"state": {"status": "RUNNING", "isUserCanceledOrTimedOut": True}}),
    (1, {"state": {"status": "CANCELLED"}})])
def test_changed_or_cancelled_native_execution_is_rejected(target, changes):
    rows = responses()
    rows[target].update(changes)
    request = Mock(side_effect=[(row, {}) for row in rows])
    with pytest.raises(ValueError):
        admit_documentary_execution(request, BINDING, batch())


def test_foreign_scope_and_user_supplied_identity_fields_are_rejected_before_http():
    request = Mock()
    foreign = batch()
    foreign["batch"]["admissions"][0]["scope_id"] = "other"
    with pytest.raises(ValueError):
        admit_documentary_execution(request, BINDING, foreign)
    foreign = batch()
    foreign["execution"]["identity"] = "user-controlled"
    with pytest.raises(ValueError):
        admit_documentary_execution(request, BINDING, foreign)
    request.assert_not_called()


def test_repaired_task_cannot_reauthorize_a_previous_invocation():
    run, task = responses()
    request = Mock(side_effect=[(run, {}), (task, {})])
    pinned = admit_documentary_execution(request, BINDING, batch())
    assert pinned == TASK
    replacement = "c40e4c62-71d9-4837-8609-5479d950f6d5"
    run["taskToTaskRunMap"]["document_metadata"] = replacement
    request = Mock(return_value=(run, {}))
    with pytest.raises(ValueError, match="replacement"):
        admit_documentary_execution(request, BINDING, batch(), expected_task_run_id=pinned)
    assert request.call_count == 1


@pytest.mark.parametrize("change", [{"model_ref": "other"}, {"prompt_version": "old"}])
def test_old_model_or_prompt_provenance_is_rejected_before_http(change):
    value = batch()
    value["batch"]["admissions"][0].update(change)
    request = Mock()
    with pytest.raises(ValueError, match="revision"):
        admit_documentary_execution(request, BINDING, value)
    request.assert_not_called()
