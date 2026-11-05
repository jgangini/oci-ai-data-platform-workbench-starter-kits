"""Native Python-task admission and binding for the inventory phase."""

from __future__ import annotations

from datetime import datetime, timezone

from .inventory import canonical, identity
from .repository import DeltaRepository
from .service_transport import MetadataTransport, runtime_signer
from .sync_inventory import synchronize_scope
from .catalog import CatalogReader
from .task_arguments import parse_task_arguments


def admit_native_sync(request, config: dict, arguments) -> None:
    if arguments.task_key != "sync_inventory":
        raise ValueError("This entry executes only the inventory task")
    run, _ = request("GET", f"/workspaces/{config['workspace_key']}/jobRuns/{arguments.job_run_id}")
    if not isinstance(run, dict) or run.get("key") != arguments.job_run_id or run.get("jobKey") != config["job_key"]:
        raise ValueError("Native execution belongs to a different workflow")
    state = run.get("state")
    if not isinstance(state, dict) or state.get("status") != "RUNNING" or state.get("isUserCanceledOrTimedOut"):
        raise ValueError("Native workflow is no longer running")
    if run.get("taskToTaskRunMap", {}).get(arguments.task_key) != arguments.task_run_id:
        raise ValueError("Native execution does not contain this task attempt")
    task, _ = request("GET", f"/workspaces/{config['workspace_key']}/taskRuns/{arguments.task_run_id}")
    if not isinstance(task, dict) or any(task.get(key) != value for key, value in (
        ("key", arguments.task_run_id), ("jobRunKey", arguments.job_run_id),
        ("jobKey", config["job_key"]), ("taskKey", arguments.task_key))):
        raise ValueError("The native task identity differs from the admitted attempt")
    state = task.get("state")
    if not isinstance(state, dict) or state.get("status") != "RUNNING" or state.get("isUserCanceledOrTimedOut"):
        raise ValueError("The native task is no longer running")


def prepare_sync_context(repository, config: dict, arguments, selected: dict, component_revision: str) -> tuple[str, str]:
    catalog, schema = selected["catalog_key"], selected["schema_key"]
    if not schema.startswith(catalog + "."):
        raise ValueError("A business metadata source schema is required")
    scope_id = identity("AI_DG_SCOPE", config["platform_id"], config["workspace_key"], catalog, schema)
    existing = repository.read_by_keys("AI_DG_SCOPE", [{"scope_id": scope_id}])
    at = datetime.now(timezone.utc)
    if existing:
        scope = existing[0]
        for name, expected in (("platform_key", config["platform_id"]), ("workspace_key", config["workspace_key"]),
                               ("catalog_key", catalog), ("schema_key", schema)):
            if scope[name] != expected:
                raise ValueError("Persisted synchronization scope has a different identity")
        if not scope["enabled"]:
            raise ValueError("The synchronization scope is disabled")
    else:
        scope = {"scope_id": scope_id, "platform_key": config["platform_id"],
            "workspace_key": config["workspace_key"], "catalog_key": catalog, "schema_key": schema,
            "config_version": 1, "enabled": True, "cadence_minutes": 30,
            "limits_json": canonical({"max_tables": 1000}), "updated_at": at,
            "batch_target": 500, "api_parallelism": 1, "retry_backoff_seconds": 30,
            "controller_state_json": canonical({"strategy": "bounded_incremental"}), "control_updated_at": at}
        repository.insert_immutable("AI_DG_SCOPE", [scope])
    execution_key = identity(arguments.job_run_id, arguments.task_key, arguments.repair_count)
    run_id = identity("AI_DG_RUN", scope_id, execution_key, arguments.execution_count)
    existing = repository.read_by_keys("AI_DG_RUN", [{"run_id": run_id}])
    if existing:
        run = existing[0]
        if run["scope_id"] != scope_id or run["component_version"] != component_revision:
            raise ValueError("An existing task attempt has different scope or code")
    else:
        repository.insert_immutable("AI_DG_RUN", [{"run_id": run_id, "scope_id": scope_id,
            "parent_run_id": None, "agent_id": None, "run_kind": "TASK",
            "execution_key": execution_key, "attempt_no": arguments.execution_count,
            "component_version": component_revision, "native_run_ref": arguments.job_run_id + ":" + arguments.task_run_id,
            "status": "RUNNING", "manifest_json": canonical({"config_version": scope["config_version"],
                "job_key": config["job_key"], "job_run_id": arguments.job_run_id, "task_key": arguments.task_key,
                "repair_count": arguments.repair_count}), "started_at": at, "finished_at": None}])
    return scope_id, run_id




def run_native_inventory(config: dict, component_revision: str, secret_get) -> dict:
    import requests
    from pyspark.sql import SparkSession

    arguments = parse_task_arguments()
    spark = SparkSession.builder.getOrCreate()
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    repository = DeltaRepository(spark, config["storage_root"], max_batch_rows=500)
    results = []
    with requests.Session() as session:
        request = MetadataTransport(config, runtime_signer(config, secret_get), session)
        request("GET", "/roles")
        admit_native_sync(request, config, arguments)
        reader = CatalogReader(request, max_pages=100)
        for selected in config["source_scopes"]:
            admit_native_sync(request, config, arguments)
            scope_id, run_id = prepare_sync_context(repository, config, arguments, selected, component_revision)
            result = synchronize_scope(repository, reader, scope_id, run_id,
                check_active=lambda: admit_native_sync(request, config, arguments))
            results.append({"scope_id": scope_id, "run_id": run_id, **result})
    return {"phase": "sync_inventory", "scopes": results, "catalog_writes": 0, "model_calls": 0}
