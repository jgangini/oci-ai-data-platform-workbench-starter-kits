"""One native DAG, with separate retry and repair boundaries for each effect."""

from __future__ import annotations

from uuid import UUID

from .task_arguments import command_line_arguments


ARTIFACT_ROOT = "/Workspace/.artifact/ai_data_governance"


def governance_workflow(*, job_name: str, cluster_key: str, documentation_agent_key: str) -> dict:
    for key in (cluster_key, documentation_agent_key):
        if str(UUID(key)) != key:
            raise ValueError("Native compute and agent keys must be canonical UUIDs")
    if job_name != "wf_ai_dg_sync_master_catalog":
        raise ValueError("The single maintained Governance workflow name is required")
    tasks = []
    for name, parent, condition in (("sync_inventory", None, "ALL_SUCCESS"),
            ("publish_metadata", "document_metadata", "ALL_SUCCESS"),
            ("replicate_model", "publish_metadata", "ALL_DONE")):
        path = ARTIFACT_ROOT + "/tasks/" + name + ".py"
        tasks.append({"type": "PYTHON_TASK", "taskKey": name, "filePath": path,
            "commandLineArguments": command_line_arguments(path, name),
            "cluster": {"clusterKey": cluster_key},
            "dependsOn": [] if parent is None else [{"taskKey": parent}], "runIf": condition,
            "maxRetries": 1, "minRetryIntervalMillis": 30000,
            "isRetryOnTimeout": False, "timeoutSeconds": 1800})
    tasks.insert(1, {"type": "AGENT_TASK", "taskKey": "document_metadata",
        "agentKey": documentation_agent_key, "prompt": "{{job.run_id}}:{{job.repair_count}}",
        "parameters": [], "dependsOn": [{"taskKey": "sync_inventory"}], "runIf": "ALL_SUCCESS",
        "maxRetries": 1, "minRetryIntervalMillis": 30000, "isRetryOnTimeout": False})
    return {"name": job_name, "path": "/Workspace/jobs", "maxConcurrentRuns": 1,
        "description": "AI Data Governance: inventario, agente documental, descripciones vacías y réplica. Cuatro tareas recuperables.",
        "queue": {"isEnabled": False}, "jobClusters": [{"clusterKey": cluster_key}], "tasks": tasks}
