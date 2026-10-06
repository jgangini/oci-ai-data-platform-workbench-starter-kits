"""Task source and argument identity must survive updates and legacy job admission."""
from copy import deepcopy
import json

import pytest

from app.aidp import AidpClient
from app.gods_eye_view.scheduling import task_outcome


@pytest.mark.parametrize("field,value", [
    (None, None), ("type", "NOTEBOOK_TASK"), ("filePath", "/other.py"),
    ("source", "GIT"), ("commandLineArguments", "--other"),
    ("taskKey", "unexpected"), ("cluster", {"clusterKey": "other"}),
])
def test_python_task_reconciliation_checks_executable_and_arguments(field, value):
    expected = {"type": "PYTHON_TASK", "taskKey": "social_network", "source": "WORKSPACE",
                "filePath": "/Workspace/territorial/releases/version/social_network.py",
                "commandLineArguments": json.dumps(["social_network.py", "--runtime-root", "/Workspace/territorial/releases/version"]),
                "dependsOn": [], "runIf": "ALL_SUCCESS", "cluster": {"clusterKey": "compute"}}
    actual = deepcopy(expected)
    if field:
        actual[field] = value
    assert AidpClient._job_task_matches(actual, expected, "compute") is (field is None)


@pytest.mark.parametrize("key,expected", [
    ("social_network", "SUCCESS"), ("prisma_tick", "SUCCESS"), ("unrelated", "FAILED"),
])
def test_social_task_history_accepts_only_current_or_legacy_key(key, expected):
    assert task_outcome([{"taskKey": key, "state": {"status": "SUCCESS"}}]) == expected
