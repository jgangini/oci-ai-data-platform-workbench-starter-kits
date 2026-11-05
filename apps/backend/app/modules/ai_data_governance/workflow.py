"""One native DAG, with separate retry and repair boundaries for each effect."""

from __future__ import annotations

from uuid import UUID
import json
from pathlib import Path



ARTIFACT_ROOT = "/Workspace/.artifact/ai_data_governance"


def governance_workflow(*, job_name: str, cluster_key: str, documentation_agent_key: str) -> dict:
    for key in (cluster_key, documentation_agent_key):
        if str(UUID(key)) != key:
            raise ValueError("Native compute and agent keys must be canonical UUIDs")
    if job_name != "wf_ai_dg_sync_master_catalog":
        raise ValueError("The single maintained Governance workflow name is required")
    descriptor = Path(__file__).resolve().parent / "aidp/workspaces/default/workflows/wf_ai_dg_sync_master_catalog.json"
    source = descriptor.read_text(encoding="utf-8")
    source = source.replace("${cluster_key}", cluster_key).replace("${documentation_agent_key}", documentation_agent_key)
    return json.loads(source)
