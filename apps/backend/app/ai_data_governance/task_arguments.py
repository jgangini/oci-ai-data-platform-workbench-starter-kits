"""Closed workflow correlation inputs; identifiers do not confer authority."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class TaskArguments:
    job_run_id: str
    task_run_id: str
    task_key: str
    execution_count: int
    repair_count: int


def parse_task_arguments(arguments: list[str] | None = None) -> TaskArguments:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--job-run-id", required=True)
    parser.add_argument("--task-run-id", required=True)
    parser.add_argument("--task-key", required=True,
                        choices=("sync_inventory", "publish_metadata", "replicate_model"))
    parser.add_argument("--execution-count", required=True, type=int)
    parser.add_argument("--repair-count", required=True, type=int)
    args = parser.parse_args(arguments)
    for name in ("job_run_id", "task_run_id"):
        value = getattr(args, name)
        if str(UUID(value)) != value:
            raise ValueError("Native workflow identifiers must be canonical UUIDs")
    if not 1 <= args.execution_count <= 100 or not 0 <= args.repair_count <= 100:
        raise ValueError("Native workflow counters are outside the admitted bound")
    return TaskArguments(**vars(args))


def command_line_arguments(path: str, task_key: str) -> str:
    """AIDP sets this JSON array directly as sys.argv, including argv[0]."""
    import json

    if not isinstance(path, str) or not path.startswith("/Workspace/.artifact/ai_data_governance/") or not path.endswith(".py"):
        raise ValueError("A Governance artifact Python path is required")
    if task_key not in {"sync_inventory", "publish_metadata", "replicate_model"}:
        raise ValueError("Unknown Python workflow task")
    return json.dumps([path, "--job-run-id", "{{job.run_id}}", "--task-run-id", "{{task.run_id}}",
                       "--task-key", task_key, "--execution-count", "{{task.execution_count}}",
                       "--repair-count", "{{job.repair_count}}"])
