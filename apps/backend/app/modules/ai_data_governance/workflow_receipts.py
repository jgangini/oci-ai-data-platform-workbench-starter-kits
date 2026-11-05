"""Immutable run handoff: task parameters never carry generated text or authority."""

from __future__ import annotations

import re
from uuid import UUID

from .inventory import identity


def execution_receipt_key(job_run_id: str) -> str:
    if not isinstance(job_run_id, str) or str(UUID(job_run_id)) != job_run_id:
        raise ValueError("A canonical native workflow run is required")
    return identity("workflow-execution", job_run_id)


def load_execution_receipt(journal, binding: dict, job_run_id: str) -> dict:
    value = journal.read("executions", execution_receipt_key(job_run_id))
    fields = {"schema_version", "job_key", "job_run_id", "documentation_agent_key", "documentary_revision", "scopes"}
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != 1:
        raise ValueError("The inventory handoff is missing or incompatible")
    if value["job_key"] != binding["job_key"] or value["job_run_id"] != job_run_id:
        raise ValueError("The inventory handoff belongs to another workflow")
    _validate_receipt_scopes(value["scopes"])
    if not re.fullmatch(r"[a-f0-9]{64}", value["documentary_revision"]):
        raise ValueError("The documentary source revision is unavailable")
    if str(UUID(value["documentation_agent_key"])) != value["documentation_agent_key"]:
        raise ValueError("The documentary agent identity is unavailable")
    return value


def _validate_receipt_scopes(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError("A bounded inventory scope set is required")
    scopes, batches = set(), set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"scope_id", "inventory_run_id", "batch_ref"}:
            raise ValueError("Invalid inventory handoff scope")
        if any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for v in row.values()):
            raise ValueError("Invalid inventory handoff identity")
        if row["scope_id"] in scopes or row["batch_ref"] in batches:
            raise ValueError("Duplicate inventory handoff scope or batch")
        scopes.add(row["scope_id"])
        batches.add(row["batch_ref"])
