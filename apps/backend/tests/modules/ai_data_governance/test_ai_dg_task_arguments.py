import json

import pytest

from app.modules.ai_data_governance.task_arguments import command_line_arguments, parse_task_arguments


RUN = "6bd1af22-8dc8-4f1e-91ad-b9738a2ada5a"
TASK = "d40e4c62-71d9-4837-8609-5479d950f6d5"
PATH = "/Workspace/.artifact/ai_data_governance/tasks/sync_inventory.py"


def arguments(**changes):
    values = {"job_run_id": RUN, "task_run_id": TASK, "task_key": "sync_inventory",
              "execution_count": "1", "repair_count": "0", **changes}
    return [part for name, value in values.items() for part in ("--" + name.replace("_", "-"), value)]


def test_native_arguments_keep_separate_run_task_and_repair_identity():
    parsed = parse_task_arguments(arguments(repair_count="2"))
    assert parsed.job_run_id == RUN and parsed.task_run_id == TASK
    assert parsed.execution_count == 1 and parsed.repair_count == 2


@pytest.mark.parametrize("change", [
    {"job_run_id": "{{job.run_id}}"}, {"task_run_id": "not-a-native-id"},
    {"execution_count": "-1"}, {"execution_count": "0"}, {"repair_count": "101"},
])
def test_unexpanded_or_invalid_native_arguments_are_rejected(change):
    with pytest.raises(ValueError):
        parse_task_arguments(arguments(**change))


def test_missing_and_unknown_arguments_are_not_silently_accepted():
    with pytest.raises(SystemExit):
        parse_task_arguments(arguments()[:-2])
    with pytest.raises(SystemExit):
        parse_task_arguments(arguments() + ["--scope", "someone-elses-scope"])


def test_json_array_includes_script_and_only_documented_macros():
    values = json.loads(command_line_arguments(PATH, "sync_inventory"))
    assert values[0] == PATH
    assert values[1:] == arguments(job_run_id="{{job.run_id}}", task_run_id="{{task.run_id}}",
                                   execution_count="{{task.execution_count}}", repair_count="{{job.repair_count}}")
    with pytest.raises(ValueError):
        command_line_arguments("/Workspace/other.py", "sync_inventory")
