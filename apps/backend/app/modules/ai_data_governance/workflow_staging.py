"""Freeze bounded documentary work from verified inventory and usage evidence."""

from __future__ import annotations

from datetime import datetime, timezone

from .documentary_graph import _batch_snapshot
from .documentation import DocumentationAdmission
from .inventory import canonical, identity
from .inventory_writer import _one, _retain_original
from .model_output import model_json
from .workflow_receipts import execution_receipt_key, load_execution_receipt


def stage_workflow(repository, journal, binding: dict, job_run_id: str, results: list, *, select_work, collect_context,
                   check_active) -> dict:
    check_active()
    previous = journal.read("executions", execution_receipt_key(job_run_id))
    if previous is not None:
        return load_execution_receipt(journal, binding, job_run_id)
    scopes = []
    for result in results:
        check_active()
        scope = _one(repository, "AI_DG_SCOPE", {"scope_id": result["scope_id"]})
        inventory = _one(repository, "AI_DG_RUN", {"run_id": result["run_id"]})
        if (not scope["enabled"] or inventory["scope_id"] != scope["scope_id"]
                or inventory["status"] != "SUCCEEDED" or not result["enumeration_complete"]):
            raise ValueError("Incomplete inventory cannot admit documentary work")
        rows = select_work(scope["scope_id"], ("DOCUMENT",), 16)
        batch_ref = _stage_scope_batch(repository, journal, binding, job_run_id, inventory, rows,
            collect_context=collect_context, check_active=check_active)
        scopes.append({"scope_id": scope["scope_id"], "inventory_run_id": inventory["run_id"], "batch_ref": batch_ref})
    value = {"schema_version": 1, "job_key": binding["job_key"], "job_run_id": job_run_id,
        "documentation_agent_key": binding["documentation_agent_key"],
        "documentary_revision": binding["documentary_revision"], "scopes": scopes}
    check_active()
    journal.create("executions", execution_receipt_key(job_run_id), value)
    return load_execution_receipt(journal, binding, job_run_id)


def _stage_scope_batch(repository, journal, binding, native_run, inventory, rows, *, collect_context, check_active):
    if not isinstance(rows, list) or len(rows) > 16:
        raise ValueError("The documentary selector exceeded its admission budget")
    producer_id = identity("documentary-producer", native_run, inventory["scope_id"])
    admissions, contexts = [], {}
    for work in rows:
        check_active()
        admission, context = stage_documentary_item(repository, binding, work, inventory, producer_id, collect_context,
            check_active=check_active)
        admissions.append(admission)
        contexts[work["work_id"]] = context
    batch_ref = identity("workflow-batch", native_run, inventory["scope_id"], admissions, contexts)
    snapshot = {"batch_ref": batch_ref, "admissions": admissions, "contexts": contexts}
    _batch_snapshot(batch_ref, snapshot)
    check_active()
    journal.create("batches", batch_ref, {"schema_version": 1, "batch": snapshot,
        "execution": {"job_key": binding["job_key"], "job_run_id": native_run,
            "scope_id": inventory["scope_id"], "inventory_run_id": inventory["run_id"]}})
    return batch_ref


def stage_documentary_item(repository, binding, work, inventory, producer_id, collect_context, *, check_active):
    scope_id = inventory["scope_id"]
    if work["scope_id"] != scope_id or work["phase"] != "DOCUMENT" or work["selected_document_id"]:
        raise ValueError("Only unselected documentary work in this scope is admissible")
    asset = _one(repository, "AI_DG_ASSET", {"asset_id": work["asset_id"]})
    version = _one(repository, "AI_DG_ASSET_VERSION", {"version_id": work["expected_version_id"]})
    projection = _one(repository, "AI_DG_SCOPE_ASSET", {"scope_id": scope_id, "asset_id": asset["asset_id"]})
    if (projection["consistency_state"] != "VERIFIED" or projection["verified_version_id"] != version["version_id"]
            or version["asset_id"] != asset["asset_id"] or version["description"] and version["description"].strip()):
        raise ValueError("Documentary work no longer identifies a confirmed blank description")
    context, evidence = collect_context(asset, version, scope_id, inventory["run_id"])
    _check_context_evidence(evidence, scope_id, version["version_id"])
    evidence = _retain_original(repository, "AI_DG_EVIDENCE", evidence, {"observed_at", "expires_at"})
    check_active()
    repository.insert_immutable("AI_DG_EVIDENCE", evidence)
    evidence_ids = tuple(sorted(row["evidence_id"] for row in evidence))
    admission = DocumentationAdmission(scope_id=scope_id, run_id=producer_id, work_id=work["work_id"],
        asset_id=asset["asset_id"], asset_version_id=version["version_id"], source_version=version["metadata_hash"],
        field_path="description", observed_description=version["description"], context_hash=identity("usage-context", context),
        prompt_version="DGD-PROMPTS-2", model_ref=binding["model_id"], evidence_ids=evidence_ids,
        update_supported=asset["asset_kind"] in {"TABLE", "COLUMN"}, max_description_length=2000)
    return admission.model_dump(mode="json"), context


def _check_context_evidence(rows, scope_id, version_id):
    if not isinstance(rows, list) or len(rows) > 16:
        raise ValueError("Usage evidence exceeds its admission budget")
    for row in rows:
        if (row["scope_id"] != scope_id or row["asset_version_id"] != version_id
                or row["coverage_state"] not in {"COMPLETE", "PARTIAL"}):
            raise ValueError("Usage evidence belongs to another scope or version")
