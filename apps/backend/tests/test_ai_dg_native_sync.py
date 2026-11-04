from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai_data_governance.native_sync import admit_native_sync, prepare_sync_context
from app.ai_data_governance.task_arguments import TaskArguments
from test_ai_dg_inventory_writer import MemoryRepository


ARGS = TaskArguments("6bd1af22-8dc8-4f1e-91ad-b9738a2ada5a", "d40e4c62-71d9-4837-8609-5479d950f6d5", "sync_inventory", 1, 0)
CONFIG = {"platform_id": "platform", "workspace_key": "workspace", "job_key": "job"}
SELECTED = {"catalog_key": "catalog", "schema_key": "catalog.schema"}


def native_run(**changes):
    return {"key": ARGS.job_run_id, "jobKey": "job", "state": {"status": "RUNNING"},
            "taskToTaskRunMap": {"sync_inventory": ARGS.task_run_id}, **changes}


def test_native_admission_checks_job_task_and_active_status():
    task = {"key": ARGS.task_run_id, "jobRunKey": ARGS.job_run_id, "jobKey": "job",
            "taskKey": ARGS.task_key, "state": {"status": "RUNNING"}}
    request = Mock(side_effect=[(native_run(), {}), (task, {})])
    admit_native_sync(request, CONFIG, ARGS)
    assert request.call_args.args == ("GET", "/workspaces/workspace/taskRuns/" + ARGS.task_run_id)
    for change in ({"jobKey": "other"}, {"state": {"status": "CANCELLED"}},
                   {"taskToTaskRunMap": {}}, {"key": "other"},
                   {"state": {"status": "RUNNING", "isUserCanceledOrTimedOut": True}}):
        request.side_effect = [(native_run(**change), {})]
        with pytest.raises(ValueError):
            admit_native_sync(request, CONFIG, ARGS)
    for change in ({"state": {"status": "CANCELLED"}}, {"jobRunKey": "other"}, {"taskKey": "other"}):
        request.side_effect = [(native_run(), {}), ({**task, **change}, {})]
        with pytest.raises(ValueError):
            admit_native_sync(request, CONFIG, ARGS)


def test_real_scope_and_attempt_receipts_are_reused_without_mutating_defaults():
    repository = MemoryRepository()
    scope, run = prepare_sync_context(repository, CONFIG, ARGS, SELECTED, "revision")
    calls = len(repository.calls)
    assert prepare_sync_context(repository, CONFIG, ARGS, SELECTED, "revision") == (scope, run)
    assert len(repository.calls) == calls
    scope_row = repository.data["AI_DG_SCOPE"][(scope,)]
    scope_row["batch_target"] = 250
    prepare_sync_context(repository, CONFIG, ARGS, SELECTED, "revision")
    assert scope_row["batch_target"] == 250
    row = repository.data["AI_DG_RUN"][(run,)]
    assert row["run_kind"] == "TASK" and row["attempt_no"] == 1
    assert row["native_run_ref"] == ARGS.job_run_id + ":" + ARGS.task_run_id


def test_same_attempt_with_changed_code_and_disabled_scope_are_rejected():
    repository = MemoryRepository()
    scope, _ = prepare_sync_context(repository, CONFIG, ARGS, SELECTED, "revision")
    with pytest.raises(ValueError):
        prepare_sync_context(repository, CONFIG, ARGS, SELECTED, "different")
    repository.data["AI_DG_SCOPE"][(scope,)]["enabled"] = False
    with pytest.raises(ValueError):
        prepare_sync_context(repository, CONFIG, ARGS, SELECTED, "revision")


def test_new_attempt_and_repair_have_distinct_receipts_in_same_scope():
    repository = MemoryRepository()
    first = prepare_sync_context(repository, CONFIG, ARGS, SELECTED, "revision")
    next_attempt = TaskArguments(ARGS.job_run_id, ARGS.task_run_id, ARGS.task_key, 2, 0)
    repair = TaskArguments(ARGS.job_run_id, ARGS.task_run_id, ARGS.task_key, 1, 1)
    second = prepare_sync_context(repository, CONFIG, next_attempt, SELECTED, "revision")
    third = prepare_sync_context(repository, CONFIG, repair, SELECTED, "revision")
    assert first[0] == second[0] == third[0]
    assert len({first[1], second[1], third[1]}) == 3
