"""Native Python entry points for the four-task Governance workflow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote

from .catalog import CatalogReader, CatalogScope, is_governance_control_table, is_redacted_metadata
from .catalog_discovery import pipeline_source_scopes
from .catalog_metadata import confirmed_generated_readback
from .code_context import CodeContextTransport, WorkspaceCodeReader
from .inventory import canonical, identity
from .inventory_writer import _one, _retain_original
from .journal import GovernanceJournal
from .lineage import LineageReader, LineageTransport, LineageTaskReader
from .materialize import materialize_documentary_batch
from .model_output import model_json
from .native_sync import admit_native_sync, prepare_sync_context
from .outcome_receipts import verified_documentary_receipt
from .publication import publish_work
from .replication import replicate_work
from .repository import DeltaRepository, _comparison_frame, _restore_timestamps
from .service_transport import MetadataTransport, runtime_signer, runtime_object_storage
from .sync_inventory import synchronize_scope
from .task_arguments import parse_task_arguments
from .workflow_receipts import execution_receipt_key, load_execution_receipt
from .workflow_staging import stage_workflow
from .delta import qualified_table


class DescriptionTransport(CodeContextTransport):
    """Only blank-description PUT payloads on configured source tables."""

    def __call__(self, method, path, *, params=None, payload=None):
        if method != "PUT":
            return super().__call__(method, path, params=params, payload=payload)
        table = unquote(path.removeprefix("/tables/"))
        if (not path.startswith("/tables/") or any(c in table for c in "/\\?#") or params is not None
                or is_governance_control_table(table)
                or not any(table.startswith(s.schema_key + ".") for s in self.scopes)):
            raise ValueError("Description update is outside the installed source scope")
        validate_description_payload(payload)
        return self._exchange("PUT", path, payload=payload)


def validate_description_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("A closed description payload is required")
    if set(payload) == {"description"}:
        text = payload["description"]
    elif set(payload) == {"updateTableFields"}:
        fields = payload["updateTableFields"]
        if not isinstance(fields, list) or len(fields) != 1 or not isinstance(fields[0], dict) or set(fields[0]) != {"fieldName", "fieldDescription"}:
            raise ValueError("Only one admitted column description may be updated")
        text = fields[0]["fieldDescription"]
    else:
        raise ValueError("Only a description may be updated")
    if not isinstance(text, str) or len(text) > 2000 or not text.endswith(" [IA]") or text.count("[IA]") != 1:
        raise ValueError("Generated descriptions require the final [IA] marker")


def pipeline_failure_report(phase, error):
    """Retain bounded diagnostic identifiers, never exception messages or locals."""
    import re

    report = {"phase": phase, "status": "FAILED", "error_type": type(error).__name__}
    frames, trace = [], error.__traceback__
    while trace is not None:
        code = trace.tb_frame.f_code
        filename = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
        if filename in {"sync_inventory.py", "publish_metadata.py", "replicate_model.py"} or (
                trace.tb_frame.f_globals.get("__name__", "").startswith("app.ai_data_governance.")):
            frames.append({"function": code.co_name, "line": trace.tb_lineno})
        trace = trace.tb_next
    report["origin"] = frames[-6:]
    try:
        java = getattr(error, "java_exception", None)
        if java is not None:
            name = java.getClass().getName()
            if isinstance(name, str) and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_.$]{0,199}", name):
                report["java_error_type"] = name
            try:
                category = java.getErrorClass()
                if isinstance(category, str) and re.fullmatch(r"[A-Z][A-Z0-9_.]{0,127}", category):
                    report["spark_error_class"] = category
            except Exception:
                pass
    except Exception:
        pass
    return report


def admit_pipeline_task(request, binding, arguments):
    """Reuse the native identity/state checks for each distinct Python phase."""
    if arguments.task_key == "sync_inventory":
        return admit_native_sync(request, binding, arguments)
    if arguments.task_key not in {"publish_metadata", "replicate_model"}:
        raise ValueError("Unknown native Python task")
    prefix = f"/workspaces/{binding['workspace_key']}"
    run, _ = request("GET", prefix + "/jobRuns/" + arguments.job_run_id)
    if (not isinstance(run, dict) or run.get("key") != arguments.job_run_id or run.get("jobKey") != binding["job_key"]
            or run.get("taskToTaskRunMap", {}).get(arguments.task_key) != arguments.task_run_id):
        raise ValueError("Native Python execution identity changed")
    _pipeline_running(run)
    task, _ = request("GET", prefix + "/taskRuns/" + arguments.task_run_id)
    expected = {"key": arguments.task_run_id, "jobRunKey": arguments.job_run_id,
                "jobKey": binding["job_key"], "taskKey": arguments.task_key}
    if not isinstance(task, dict) or any(task.get(k) != v for k, v in expected.items()):
        raise ValueError("Native Python task attempt changed")
    _pipeline_running(task)


def _pipeline_running(record):
    state = record.get("state")
    if not isinstance(state, dict) or state.get("status") != "RUNNING" or state.get("isUserCanceledOrTimedOut"):
        raise ValueError("The native workflow task is no longer active")


def phase_work(repository, scope_id, phases, limit):
    from pyspark.sql import functions as f

    if not phases or set(phases) - {"DOCUMENT", "PUBLISH", "REPLICATE"} or not 1 <= limit <= 500:
        raise ValueError("Invalid workflow work selector")
    table = repository.tables["AI_DG_WORK"]
    repository._verify_destination(table)
    frame = repository.spark.table(qualified_table("AI_DG_WORK")).where(
        (f.col("scope_id") == f.lit(scope_id)) & f.col("phase").isin(*phases)
        & (~f.col("state").isin("PRESERVED", "UNSUPPORTED", "DONE")))
    if phases == ("DOCUMENT",):
        frame = frame.where(f.col("next_attempt_at").isNull() | (f.col("next_attempt_at") <= f.current_timestamp()))
    rows = _comparison_frame(table, frame.orderBy("updated_at", "work_id").limit(limit)).collect()
    return [_restore_timestamps(table, row.asDict(recursive=True)) for row in rows]


def collect_usage_context(repository, request, binding, asset, version, scope_id, run_id, *, code_reader=None):
    """Preserve returned lineage as evidence; absent usage never creates meaning."""
    scope = _one(repository, "AI_DG_SCOPE", {"scope_id": scope_id})
    selected = CatalogScope(scope["catalog_key"], scope["schema_key"])
    facts = model_json(version["metadata_json"], max_bytes=1048576)
    prefixes = tuple(item["schema_key"] + "." for item in binding["source_scopes"])
    task_reader = LineageTaskReader(request, binding['platform_id'], binding['workspace_key'])
    def visible(node):
        if node.get("type") == "Task":
            return task_reader.visible(node)
        name = node.get("qualifiedName", "")
        return any(name.startswith(p) or name.startswith(f"aidp://catalogs@{binding['platform_id']}/o/" + p)
                   for p in prefixes)
    snapshot = LineageReader(request, binding["platform_id"], admit_node=visible, max_pages=2).read(
        selected, facts["table_key"], column_name=facts.get("column_name"), depth=2)
    code_refs = code_reader.find(facts['table_key'], facts.get('column_name')) if code_reader is not None else []
    if not snapshot.links and not code_refs:
        return "", []
    values = {"target": version["qualified_name"], "catalog_metadata": facts,
        "lineage_nodes": snapshot.nodes, "lineage_links": snapshot.links,
        "omitted_nodes": snapshot.omitted_nodes, "unresolved_links": snapshot.unresolved_links,
        "pagination_complete": snapshot.pagination_complete, "execution_evidence": False}
    lineage_values = dict(values)
    if code_reader is not None:
        values.update(static_code_references=code_refs, static_analysis_complete=False,
                      workspace_scan_truncated=bool(code_reader.truncated))
    context = canonical(values)
    if len(context.encode()) > 65536:
        raise ValueError("Usage context exceeds its documentary budget")
    at = datetime.now(timezone.utc)
    observations = []
    if snapshot.links:
        observations.append(('RUNTIME_LINEAGE', 'lineage', lineage_values,
                             'COMPLETE' if snapshot.pagination_complete and not snapshot.omitted_nodes
                             and not snapshot.unresolved_links else 'PARTIAL'))
    if code_refs:
        observations.append(('STATIC_CODE', 'static-code', {'target': version['qualified_name'], 'references': code_refs,
                             'execution_evidence': False, 'analysis_complete': False}, 'PARTIAL'))
    evidence = []
    for kind, prefix, usage, coverage in observations:
        observed_facts = canonical({'usage_context': usage, 'observation_run_id': run_id})
        evidence_hash = identity(prefix + '-facts', observed_facts)
        evidence.append({'evidence_id': identity(prefix + '-evidence', scope_id, version['version_id'], evidence_hash),
            'scope_id': scope_id, 'asset_version_id': version['version_id'], 'producer_run_id': run_id,
            'evidence_kind': kind, 'source_ref': version['qualified_name'], 'evidence_hash': evidence_hash,
            'facts_json': observed_facts, 'coverage_state': coverage, 'observed_at': at, 'expires_at': at + timedelta(hours=1)})
    return context, evidence


def inventory_readback_reader(repository, reader, scope, confirm_description, check_active):
    """Reconfirm a known, versioned header; an unknown mask stays unavailable."""
    import hashlib

    def readback(method, path, **kwargs):
        detail, headers = reader.request(method, path, **kwargs)
        if (method != "GET" or not isinstance(detail, dict)
                or not is_redacted_metadata(detail.get("description"))):
            return detail, headers
        table_key = detail.get("key")
        if (not isinstance(table_key, str) or path != "/tables/" + quote(table_key, safe="")
                or not table_key.startswith(scope["schema_key"] + ".") or is_governance_control_table(table_key)):
            return detail, headers
        check_active()
        text = known_inventory_header(repository, scope, table_key)
        if text is None:
            return detail, headers
        context = {"scope": scope, "table_key": table_key, "column_name": None,
            "asset": {"asset_kind": "TABLE"},
            "document": {"description_text": text, "text_hash": hashlib.sha256(text.encode()).hexdigest()}}
        confirmed = confirmed_generated_readback(lambda *a, **k: (detail, headers), context,
            confirm_description, check_active)
        return confirmed(method, path, **kwargs)

    return CatalogReader(readback, page_size=reader.page_size, max_pages=reader.max_pages)


def known_inventory_header(repository, scope, table_key):
    asset_id = identity("AI_DG_ASSET", scope["platform_key"], "TABLE", table_key)
    projection = known_inventory_projection(repository, scope, table_key, asset_id)
    if projection is None:
        return None
    text = projection["description"]
    version = _one(repository, "AI_DG_ASSET_VERSION", {"version_id": projection["verified_version_id"]})
    facts = model_json(version["metadata_json"], max_bytes=1048576)
    digest = identity("metadata", facts)
    expected = {"asset_id": asset_id, "qualified_name": table_key, "description": text,
        "metadata_hash": digest, "version_id": identity("AI_DG_ASSET_VERSION", asset_id, digest)}
    if (any(version[k] != value for k, value in expected.items())
            or facts.get("description") != text or facts.get("table_key") != table_key
            or projection["projection_hash"] != digest):
        raise ValueError("Known inventory header differs from its confirmed version")
    return text


def known_inventory_projection(repository, scope, table_key, asset_id):
    projections = repository.read_by_keys("AI_DG_SCOPE_ASSET", [{"scope_id": scope["scope_id"], "asset_id": asset_id}])
    if not projections:
        return None
    projection, = projections
    expected = {"scope_id": scope["scope_id"], "asset_id": asset_id, "asset_kind": "TABLE",
        "qualified_name": table_key, "consistency_state": "VERIFIED"}
    text = projection["description"]
    if (any(projection[k] != value for k, value in expected.items()) or not isinstance(text, str)
            or not text.strip() or is_redacted_metadata(text)):
        return None
    return projection


def pipeline_inventory(repository, journal, request, reader, binding, revision, arguments, *, check_active,
                       confirm_description=None):
    previous = journal.read("executions", execution_receipt_key(arguments.job_run_id))
    if previous is not None:
        return load_execution_receipt(journal, binding, arguments.job_run_id)
    results = []
    for selected in binding["source_scopes"]:
        check_active()
        scope_id, run_id = prepare_sync_context(repository, binding, arguments, selected, revision)
        scoped_reader = reader if confirm_description is None else inventory_readback_reader(repository, reader,
            {**selected, "scope_id": scope_id, "platform_key": binding["platform_id"]}, confirm_description, check_active)
        result = synchronize_scope(repository, scoped_reader, scope_id, run_id,
            lambda detail: {}, check_active=check_active)
        results.append({"scope_id": scope_id, "run_id": run_id, **result})
    code_reader = WorkspaceCodeReader(request, binding['workspace_key'], check_active=check_active)
    return stage_workflow(repository, journal, binding, arguments.job_run_id, results,
        select_work=lambda scope, phases, limit: phase_work(repository, scope, phases, limit),
        collect_context=lambda *args: collect_usage_context(repository, request, binding, *args, code_reader=code_reader), check_active=check_active)


def completed_documentary_task(request, binding, native_run):
    prefix = f"/workspaces/{binding['workspace_key']}"
    run, _ = request("GET", prefix + "/jobRuns/" + native_run)
    if run.get("jobKey") != binding["job_key"] or run.get("key") != native_run:
        raise ValueError("The documentary producer belongs to a different workflow")
    key = run.get("taskToTaskRunMap", {}).get("document_metadata")
    if not isinstance(key, str):
        raise ValueError("The documentary task did not run")
    task, _ = request("GET", prefix + "/taskRuns/" + key)
    if (task.get("key") != key or task.get("jobKey") != binding["job_key"] or task.get("jobRunKey") != native_run
            or task.get("taskKey") != "document_metadata" or task.get("state", {}).get("status") != "SUCCESS"):
        raise ValueError("The documentary native task is not successful")
    spec = task.get("task") or {}
    if spec.get("type") != "AGENT_TASK" or spec.get("agentKey") != binding["documentation_agent_key"]:
        raise ValueError("The task ran a different documentary agent")
    return task


def record_documentary_producer(repository, journal, binding, row, task):
    verified = verified_documentary_receipt(journal, row["batch_ref"])
    execution = verified["batch"]["execution"]
    scope = _one(repository, "AI_DG_SCOPE", {"scope_id": row["scope_id"]})
    if execution != {"job_key": binding["job_key"], "job_run_id": task["jobRunKey"],
            "scope_id": row["scope_id"], "inventory_run_id": row["inventory_run_id"]}:
        raise ValueError("The batch belongs to another inventory execution")
    agent_id = identity("AI_DG_AGENT", binding["platform_id"], binding["workspace_key"], "DOCUMENTATION")
    agent = {"agent_id": agent_id, "platform_key": binding["platform_id"], "workspace_key": binding["workspace_key"],
        "agent_role": "DOCUMENTATION", "native_agent_key": binding["documentation_agent_key"],
        "deployment_version": binding["documentary_revision"],
        "code_path": "/Workspace/.artifact/ai_data_governance/agents/documentation/documentation_agent.py",
        "capabilities_json": canonical(["document_metadata"]), "enabled": True}
    existing_agent = repository.read_by_keys("AI_DG_AGENT", [{"agent_id": agent_id}])
    if existing_agent and (len(existing_agent) != 1 or any(existing_agent[0][k] != v
            for k, v in agent.items() if k != "deployment_version")):
        raise ValueError("The registered documentary agent identity or enabled state changed")
    run_id = identity("documentary-producer", task["jobRunKey"], row["scope_id"])
    if any(a["run_id"] != run_id for a in verified["admissions"]):
        raise ValueError("The documentary admissions identify another producer")
    manifest = {**execution, "task_key": "document_metadata", "config_version": scope["config_version"],
        "batch_ref": row["batch_ref"], "outcomes_digest": verified["outcomes_digest"],
        "component_revision": binding["documentary_revision"]}
    at = datetime.now(timezone.utc)
    record = {"run_id": run_id, "scope_id": scope["scope_id"], "parent_run_id": row["inventory_run_id"],
        "agent_id": agent_id, "run_kind": "AGENT", "execution_key": identity("documentary", row["batch_ref"]),
        "attempt_no": 1, "component_version": binding["documentary_revision"],
        "native_run_ref": task["jobRunKey"] + ":" + task["key"], "status": "SUCCEEDED",
        "manifest_json": canonical(manifest), "started_at": at, "finished_at": at}
    existing = repository.read_by_keys("AI_DG_RUN", [{"run_id": run_id}])
    if existing:
        old, = existing
        original_manifest = model_json(old["manifest_json"])
        if (any(old[k] != record[k] for k in ("scope_id", "parent_run_id", "agent_id", "run_kind", "component_version"))
                or any(original_manifest.get(k) != v for k, v in manifest.items())
                or not old["native_run_ref"].startswith(task["jobRunKey"] + ":")):
            raise ValueError("An existing producer has different documentary provenance")
        return
    if not existing_agent:
        repository.insert_immutable("AI_DG_AGENT", [agent])
    elif existing_agent[0] != agent:
        repository.update_current("AI_DG_AGENT", [agent], expected=existing_agent)
    repository.insert_immutable("AI_DG_RUN", [record])


def prepare_phase_context(repository, binding, arguments, row, revision):
    scope = _one(repository, "AI_DG_SCOPE", {"scope_id": row["scope_id"]})
    execution = identity(arguments.job_run_id, arguments.task_key, arguments.repair_count)
    run_id = identity("AI_DG_RUN", scope["scope_id"], execution, arguments.execution_count)
    at = datetime.now(timezone.utc)
    record = {"run_id": run_id, "scope_id": scope["scope_id"], "parent_run_id": row["inventory_run_id"],
        "agent_id": None, "run_kind": "TASK", "execution_key": execution, "attempt_no": arguments.execution_count,
        "component_version": revision, "native_run_ref": arguments.job_run_id + ":" + arguments.task_run_id,
        "status": "RUNNING", "manifest_json": canonical({"job_key": binding["job_key"],
            "job_run_id": arguments.job_run_id, "task_key": arguments.task_key,
            "config_version": scope["config_version"], "repair_count": arguments.repair_count}),
        "started_at": at, "finished_at": None}
    existing = repository.read_by_keys("AI_DG_RUN", [{"run_id": run_id}])
    if not existing:
        repository.insert_immutable("AI_DG_RUN", [record])
    elif any(existing[0][k] != record[k] for k in ("scope_id", "component_version", "native_run_ref")):
        raise ValueError("The phase attempt changed identity or source")
    return run_id


def pipeline_effects(repository, journal, request, reader, binding, revision, arguments, *, check_active,
                     confirm_description=None):
    manifest = load_execution_receipt(journal, binding, arguments.job_run_id)
    if manifest["documentary_revision"] != binding["documentary_revision"]:
        raise ValueError("The documentary revision changed during the workflow")
    publishing = arguments.task_key == "publish_metadata"
    task = completed_documentary_task(request, binding, arguments.job_run_id) if publishing else None
    results, completed_scopes = [], []
    for row in manifest["scopes"]:
        check_active()
        run_id = prepare_phase_context(repository, binding, arguments, row, revision)
        if publishing:
            record_documentary_producer(repository, journal, binding, row, task)
            materialize_documentary_batch(repository, journal, row["batch_ref"], publisher_run_id=run_id,
                check_active=check_active, observed_at=datetime.now(timezone.utc))
        phases = ("PUBLISH", "REPLICATE")
        works = phase_work(repository, row["scope_id"], phases, 100)
        if confirm_description is not None:
            works = recover_masked_batch_work(repository, journal, row, works, 100, check_active)
        scope_results = []
        for work in works:
            check_active()
            if not publishing and work["phase"] == "PUBLISH" and not work_has_publication(repository, work):
                continue
            operation = publish_work if publishing else replicate_work
            target = request if publishing else reader
            result = operation(repository, journal, target, row["scope_id"], work["work_id"], run_id,
                check_active=check_active, observed_at=datetime.now(timezone.utc),
                **({"confirm_description": confirm_description} if confirm_description is not None else {}))
            results.append(result)
            scope_results.append(result)
        if not publishing:
            verify_batch_replica_coverage(repository, journal, row)
        completed_scopes.append((run_id, scope_results))
    if any(pipeline_result_pending(result, publishing=publishing) for result in results):
        raise RuntimeError("The Governance phase has unresolved operation receipts")
    if not publishing:
        verify_pipeline_completion(request, binding, arguments.job_run_id)
    for run_id, scope_results in completed_scopes:
        _finish_pipeline_phase(repository, run_id, scope_results, check_active)
    return {"phase": arguments.task_key, "items": results, "model_calls": 0}


def recover_masked_batch_work(repository, journal, row, works, limit, check_active):
    """Revisit terminal legacy observations only within this frozen batch."""
    admissions = recovery_batch_admissions(repository, journal, row)
    if not admissions:
        return works
    check_active()
    candidates = recovery_work_candidates(repository, row["scope_id"], admissions)
    if not candidates:
        return works
    masked = masked_recovery_assets(repository, row["scope_id"], candidates)
    selected = {w["work_id"] for w in works}
    recovered = [w for w in candidates if w["asset_id"] in masked and w["work_id"] not in selected
        and work_has_publication(repository, w)]
    if len(works) + len(recovered) > limit:
        raise ValueError("Recovery work exceeds the phase budget")
    return works + recovered


def recovery_batch_admissions(repository, journal, row):
    batch = journal.read("batches", row["batch_ref"])
    admissions = batch.get("batch", {}).get("admissions") if isinstance(batch, dict) else None
    if (not isinstance(admissions, list) or len(admissions) > repository.max_batch_rows
            or any(not isinstance(a, dict) or a.get("scope_id") != row["scope_id"]
                   or not a.get("work_id") or not a.get("asset_id") for a in admissions)):
        raise ValueError("Invalid recovery batch admission")
    if len({a["work_id"] for a in admissions}) != len(admissions):
        raise ValueError("Duplicate recovery work admission")
    return admissions


def recovery_work_candidates(repository, scope_id, admissions):
    candidates = repository.read_by_keys("AI_DG_WORK", [{"work_id": a["work_id"]} for a in admissions])
    expected = {a["work_id"]: a["asset_id"] for a in admissions}
    if (len(candidates) != len(admissions) or {w["work_id"] for w in candidates} != set(expected)
            or any(w["scope_id"] != scope_id or w["asset_id"] != expected[w["work_id"]] for w in candidates)):
        raise ValueError("Recovery work differs from its frozen admission")
    return [w for w in candidates if w["state"] in {"DONE", "PRESERVED"}
        and w["phase"] == "REPLICATE" and w["selected_document_id"]]


def masked_recovery_assets(repository, scope_id, candidates):
    projections = repository.read_by_keys("AI_DG_SCOPE_ASSET", [
        {"scope_id": scope_id, "asset_id": w["asset_id"]} for w in candidates])
    if (len(projections) != len(candidates) or {p["asset_id"] for p in projections} != {w["asset_id"] for w in candidates}
            or any(p["scope_id"] != scope_id for p in projections)):
        raise ValueError("Recovery projection coverage differs")
    return {p["asset_id"] for p in projections if is_redacted_metadata(p["description"])}


def verify_batch_replica_coverage(repository, journal, row):
    batch = journal.read("batches", row["batch_ref"])
    if not isinstance(batch, dict) or not isinstance(batch.get("batch"), dict):
        raise ValueError("The replica batch is unavailable")
    admissions = batch["batch"].get("admissions")
    if (not isinstance(admissions, list) or len(admissions) > repository.max_batch_rows
            or any(not isinstance(a, dict) or a.get("scope_id") != row["scope_id"]
                   or not isinstance(a.get("asset_id"), str) or not a["asset_id"] for a in admissions)):
        raise ValueError("Invalid replica batch coverage")
    assets = {a["asset_id"] for a in admissions}
    if not assets:
        return
    projections = repository.read_by_keys("AI_DG_SCOPE_ASSET", [
        {"scope_id": row["scope_id"], "asset_id": key} for key in sorted(assets)])
    if (len(projections) != len(assets) or {p["asset_id"] for p in projections} != assets
            or any(p["scope_id"] != row["scope_id"] for p in projections)):
        raise RuntimeError("Replica projection coverage is incomplete")
    if any(is_redacted_metadata(p["description"]) for p in projections):
        raise RuntimeError("Replica metadata remains redacted")
    if any(p["consistency_state"] != "VERIFIED" for p in projections):
        raise RuntimeError("Replica metadata remains pending")


def pipeline_result_pending(result, *, publishing):
    if publishing:
        return result.get("catalog_state") in {"PREPARED", "UNKNOWN"}
    return result.get("replica_state") != "VERIFIED"


def verify_pipeline_completion(request, binding, native_run):
    """ALL_DONE permits recovery; it must not hide a failed upstream task."""
    prefix = f"/workspaces/{binding['workspace_key']}"
    run, _ = request("GET", prefix + "/jobRuns/" + native_run)
    if run.get("key") != native_run or run.get("jobKey") != binding["job_key"]:
        raise ValueError("Final verification belongs to another workflow")
    for name in ("sync_inventory", "document_metadata", "publish_metadata"):
        key = run.get("taskToTaskRunMap", {}).get(name)
        if not isinstance(key, str):
            raise RuntimeError("The workflow has an unexecuted upstream phase")
        task, _ = request("GET", prefix + "/taskRuns/" + key)
        expected = {"key": key, "jobRunKey": native_run, "jobKey": binding["job_key"], "taskKey": name}
        if any(task.get(k) != v for k, v in expected.items()) or task.get("state", {}).get("status") != "SUCCESS":
            raise RuntimeError("Replica reconciliation completed but an upstream phase did not succeed")


def work_has_publication(repository, work):
    if not work["selected_document_id"]:
        return False
    version = _one(repository, "AI_DG_ASSET_VERSION", {"version_id": work["expected_version_id"]})
    observation = version["native_version"] or "OBSERVATION:" + version["version_id"]
    key = identity("AI_DG_PUBLICATION", work["selected_document_id"], observation)
    return bool(repository.read_by_keys("AI_DG_PUBLICATION", [{"publication_id": key}]))


def _finish_pipeline_phase(repository, run_id, results, check_active):
    run = _one(repository, "AI_DG_RUN", {"run_id": run_id})
    manifest = model_json(run["manifest_json"])
    check_active()
    repository.update_current("AI_DG_RUN", [{**run, "status": "SUCCEEDED",
        "manifest_json": canonical({**manifest, "results": results}), "finished_at": datetime.now(timezone.utc)}], expected=[run])


def confirm_generated_header_hash(spark, table_key, expected_hash):
    """Compare one known header through the catalog API; never export its value."""
    import hashlib
    import re

    if (not isinstance(table_key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){2}", table_key)
            or is_governance_control_table(table_key) or not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        return False
    from pyspark.sql.utils import toJArray
    catalog_name, schema_name, table_name = table_key.split(".")
    catalog = spark._jsparkSession.sessionState().catalogManager().catalog(catalog_name)
    namespace = toJArray(spark.sparkContext._gateway, spark._jvm.java.lang.String, [schema_name])
    identifier = spark._jvm.org.apache.spark.sql.connector.catalog.Identifier.of(namespace, table_name)
    text = catalog.loadTable(identifier).properties().get("description")
    return (isinstance(text, str) and len(text) <= 2000 and not is_redacted_metadata(text)
        and hashlib.sha256(text.encode()).hexdigest() == expected_hash)


def run_native_pipeline(binding, revision, secret_get, runner):
    import oci
    import requests
    from pyspark.sql import SparkSession

    arguments = parse_task_arguments()
    spark = SparkSession.builder.getOrCreate()
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    repository = DeltaRepository(spark, binding["storage_root"], max_batch_rows=500)
    signer = runtime_signer(binding, secret_get)
    journal = GovernanceJournal(runtime_object_storage(binding, signer, secret_get),
        platform_key=binding["platform_id"], workspace_key=binding["workspace_key"], storage_root=binding["storage_root"])
    with requests.Session() as session:
        request = MetadataTransport(binding, signer, session)
        check_active = lambda: admit_pipeline_task(request, binding, arguments)
        check_active()
        scopes = pipeline_source_scopes(repository, journal, request, binding, arguments, check_active=check_active)
        binding = {**binding, "source_scopes": scopes}
        request = DescriptionTransport(binding, signer, session)
        reader = CatalogReader(request, max_pages=100)
        def confirm_description(table_key, expected_hash):
            check_active()
            confirmed = confirm_generated_header_hash(spark, table_key, expected_hash)
            check_active()
            print(canonical({"phase": "description_hash_confirmation", "task_key": arguments.task_key,
                "table_key": table_key, "text_hash": expected_hash, "confirmed": confirmed}))
            return confirmed
        return runner(repository, journal, request, reader, binding, revision, arguments,
            check_active=check_active, confirm_description=confirm_description)
