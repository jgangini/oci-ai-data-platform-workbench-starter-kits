"""Materialize verified documentary outcomes before metadata publication.

One externally serialized workflow writer owns these commits. Each table commit
is independent; snapshots precede documents, evidence links precede selecting a
document on WORK, and a journal receipt is last. No catalog writes occur here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json

from .documentary_graph import _admission
from .inventory import canonical, identity
from .inventory_writer import _one, _retain_original
from .model_output import model_json
from .outcome_receipts import verified_documentary_receipt
from .publication_context import _documentary_producer


def materialize_documentary_batch(repository, journal, batch_ref: str, *, publisher_run_id: str,
                                  check_active, observed_at: datetime) -> dict:
    if not callable(check_active) or not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
        raise ValueError("Materialization requires active execution admission and an aware timestamp")
    check_active()
    verified = verified_documentary_receipt(journal, batch_ref)
    scope, writer = _materialization_context(repository, verified, publisher_run_id)
    admissions = {row["work_id"]: _admission(row) for row in verified["admissions"]}
    documents, links, snapshots, desired, previous = _materialization_records(
        repository, admissions, verified["outcomes"], scope, writer, observed_at)
    check_active()
    _save_document_snapshots(repository, snapshots, check_active)
    for name, rows in (("AI_DG_DOCUMENT", documents), ("AI_DG_DOC_EVIDENCE", links)):
        check_active()
        repository.insert_immutable(name, rows)
    check_active()
    repository.update_current("AI_DG_WORK", desired, expected=previous)
    marker = {"schema_version": 1, "batch_ref": batch_ref, "scope_id": scope["scope_id"],
              "outcomes_digest": verified["outcomes_digest"],
              "document_ids": sorted(row["document_id"] for row in documents)}
    check_active()
    journal.create("materialized", batch_ref, marker)
    return {"documents_confirmed": len(documents), "outcomes_confirmed": len(desired), "catalog_writes": 0}


def _materialization_context(repository, verified: dict, publisher_run_id: str) -> tuple[dict, dict]:
    execution = verified["batch"]["execution"]
    if not isinstance(execution, dict) or set(execution) != {"scope_id", "job_key", "job_run_id", "inventory_run_id"}:
        raise ValueError("The materialization scope is unavailable")
    scope = _one(repository, "AI_DG_SCOPE", {"scope_id": execution["scope_id"]})
    writer = _one(repository, "AI_DG_RUN", {"run_id": publisher_run_id})
    if (not scope["enabled"] or writer["scope_id"] != scope["scope_id"] or writer["status"] != "RUNNING"
            or writer["run_kind"] != "TASK" or writer["agent_id"] is not None):
        raise ValueError("Materialization requires an enabled scope and its active Python run")
    _materialization_run_binding(writer, execution, scope, "publish_metadata")
    sources = {row["run_id"] for row in verified["admissions"]}
    if any(row["scope_id"] != scope["scope_id"] for row in verified["admissions"]):
        raise ValueError("Documentary admissions belong to another scope")
    for source_id in sources:
        _materialization_producer(repository, source_id, verified, scope)
    return scope, writer


def _materialization_producer(repository, source_id: str, verified: dict, scope: dict) -> None:
    execution = verified["batch"]["execution"]
    source = _one(repository, "AI_DG_RUN", {"run_id": source_id})
    if (source["scope_id"] != scope["scope_id"]
            or source["run_kind"] != "AGENT" or not source["agent_id"] or source["status"] not in {"SUCCEEDED", "PARTIAL"}):
        raise ValueError("Documentary provenance lacks its completed agent run")
    manifest = _materialization_run_binding(source, execution, scope, "document_metadata")
    if (manifest.get("batch_ref") != verified["batch"]["batch"]["batch_ref"]
            or manifest.get("outcomes_digest") != verified["outcomes_digest"]
            or manifest.get("inventory_run_id") != execution["inventory_run_id"]
            or manifest.get("component_revision") != source["component_version"]):
        raise ValueError("The producer run does not identify this documentary batch and outcome receipt")
    agent = _one(repository, "AI_DG_AGENT", {"agent_id": source["agent_id"]})
    if (agent["agent_role"] != "DOCUMENTATION" or not agent["enabled"]
            or agent["platform_key"] != scope["platform_key"] or agent["workspace_key"] != scope["workspace_key"]):
        raise ValueError("Documentary provenance belongs to another agent or installation")


def _materialization_run_binding(run: dict, execution: dict, scope: dict, task_key: str) -> dict:
    manifest = model_json(run["manifest_json"], max_bytes=1048576)
    expected = {"job_key": execution["job_key"], "job_run_id": execution["job_run_id"],
                "task_key": task_key, "config_version": scope["config_version"]}
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("The materialization run belongs to a different workflow, phase or configuration")
    return manifest


def _materialization_records(repository, admissions: dict, outcomes: list, scope: dict,
                              writer: dict, at: datetime) -> tuple:
    documents, links, snapshots, desired, previous = [], [], {}, [], []
    inventory = _materialization_inventory(repository, admissions, scope)
    for outcome in outcomes:
        admission = admissions[outcome["work_id"]]
        work = _materialization_work(admission, scope, inventory)
        if outcome["status"] == "documented":
            document, evidence, snapshot = _document_records(repository, admission, outcome, at)
            documents.append(document)
            links.extend(evidence)
            snapshots.setdefault(document["producer_run_id"], {})[document["document_id"]] = snapshot
        updated = _documentary_work_transition(work, admission, outcome, writer["run_id"], at)
        previous.append(work)
        desired.append(updated)
    if len(links) > repository.max_batch_rows:
        raise ValueError("Document evidence exceeds the materialization batch bound")
    return documents, links, snapshots, desired, previous


def _materialization_inventory(repository, admissions, scope):
    requests = {
        "AI_DG_WORK": [{"work_id": a.work_id} for a in admissions.values()],
        "AI_DG_ASSET_VERSION": [{"version_id": a.asset_version_id} for a in admissions.values()],
        "AI_DG_ASSET": [{"asset_id": a.asset_id} for a in admissions.values()],
        "AI_DG_SCOPE_ASSET": [{"scope_id": scope["scope_id"], "asset_id": a.asset_id} for a in admissions.values()],
    }
    inventory = {}
    for name, keys in requests.items():
        primary = repository.tables[name]["pk"]
        requested = {tuple(row[k] for k in primary): row for row in keys}
        rows = repository.read_by_keys(name, list(requested.values()))
        indexed = {tuple(row[k] for k in primary): row for row in rows}
        if len(indexed) != len(rows) or indexed.keys() != requested.keys():
            raise ValueError("Documentary inventory is missing or ambiguous")
        inventory[name] = indexed
    return inventory


def _materialization_work(admission, scope: dict, inventory: dict) -> dict:
    work = inventory["AI_DG_WORK"][(admission.work_id,)]
    if any(work[key] != value for key, value in (("scope_id", scope["scope_id"]), ("asset_id", admission.asset_id),
            ("field_path", admission.field_path), ("expected_version_id", admission.asset_version_id))):
        raise ValueError("Documentary work changed since the frozen admission")
    version = inventory["AI_DG_ASSET_VERSION"][(admission.asset_version_id,)]
    asset = inventory["AI_DG_ASSET"][(admission.asset_id,)]
    if (version["asset_id"] != admission.asset_id or asset["platform_key"] != scope["platform_key"]
            or not version["qualified_name"].startswith(scope["schema_key"] + ".")
            or version["description"] != admission.observed_description):
        raise ValueError("Documentary asset/version belongs to another platform or asset")
    return work


def _document_records(repository, admission, outcome: dict, at: datetime) -> tuple:
    snapshot = json.loads(outcome["manifest_snapshot"])
    used = snapshot["used_evidence_ids"]
    document = {"document_id": outcome["document_id"], "work_id": admission.work_id,
        "producer_run_id": admission.run_id, "generation_key": outcome["generation_key"],
        "description_text": outcome["description_text"], "text_hash": outcome["text_hash"],
        "evidence_digest": identity("AI_DG_DOC_EVIDENCE", sorted(used)), "model_ref": admission.model_ref,
        "prompt_version": admission.prompt_version, "validation_status": "VALID", "validation_reason": None,
        "created_at": at}
    document, = _retain_original(repository, "AI_DG_DOCUMENT", [document], {"created_at", "producer_run_id"})
    snapshot_text = outcome["manifest_snapshot"]
    if document["producer_run_id"] != admission.run_id:
        snapshot_text = _original_document_snapshot(repository, document, snapshot)
    _materialization_evidence(repository, admission, used, document["created_at"])
    links = [{"document_id": document["document_id"], "evidence_id": key, "support_role": "DEFINITION"} for key in used]
    return document, links, snapshot_text


def _original_document_snapshot(repository, document, snapshot):
    scope = _one(repository, "AI_DG_SCOPE", {"scope_id": snapshot["admission"]["scope_id"]})
    current = _one(repository, "AI_DG_RUN", {"run_id": snapshot["admission"]["run_id"]})
    current_manifest = model_json(current["manifest_json"], max_bytes=1048576)
    producer, manifest = _documentary_producer(repository, scope, document, current_manifest)
    text = manifest.get("document_snapshots", {}).get(document["document_id"])
    original = model_json(text, max_bytes=131072)
    if original.get("admission", {}).get("run_id") != document["producer_run_id"]:
        raise ValueError("Original documentary snapshot has a different producer")
    def equivalent(value):
        return {**value, "admission": {k: v for k, v in value["admission"].items() if k != "run_id"}}
    if equivalent(original) != equivalent(snapshot):
        raise ValueError("Documentary reuse cannot replace original evidence or provenance")
    return text


def _materialization_evidence(repository, admission, used: list, admitted_at: datetime) -> None:
    found = repository.read_by_keys("AI_DG_EVIDENCE", [{"evidence_id": key} for key in used])
    if {row["evidence_id"] for row in found} != set(used):
        raise ValueError("Documentary evidence is missing")
    for evidence in found:
        if evidence["scope_id"] != admission.scope_id or evidence["asset_version_id"] != admission.asset_version_id:
            raise ValueError("Document evidence belongs to a different scope or asset revision")
        if evidence["coverage_state"] not in {"COMPLETE", "PARTIAL"}:
            raise ValueError("Failed or unsupported evidence cannot support a valid document")
        if evidence["expires_at"] is not None and evidence["expires_at"] <= admitted_at:
            raise ValueError("Document evidence expired before materialization")


def _documentary_work_transition(work: dict, admission, outcome: dict, writer: str, at: datetime) -> dict:
    if work["selected_document_id"] is not None or work["phase"] != "DOCUMENT":
        if outcome["status"] != "documented" or work["selected_document_id"] != outcome["document_id"]:
            raise ValueError("Materialization cannot replace an already selected document")
        return work
    states = {"documented": "READY", "insufficient_context": "WAIT_CONTEXT", "invalid_output": "WAIT_CONTEXT",
              "unsupported": "UNSUPPORTED", "preserved": "PRESERVED", "generation_pending": "RETRY"}
    changes = {"phase": "PUBLISH" if outcome["status"] == "documented" else "DOCUMENT",
        "state": states[outcome["status"]], "context_hash": admission.context_hash,
        "selected_document_id": outcome["document_id"], "reason_code": outcome["reason_code"],
        "next_attempt_at": at + timedelta(minutes=30) if states[outcome["status"]] == "WAIT_CONTEXT" else None}
    if all(work[key] == value for key, value in changes.items()):
        return work
    return {**work, **changes, "last_run_id": writer, "updated_at": at}


def _save_document_snapshots(repository, snapshots: dict, check_active):
    for run_id, records in snapshots.items():
        run = _one(repository, "AI_DG_RUN", {"run_id": run_id})
        manifest = model_json(run["manifest_json"], max_bytes=1048576)
        if not isinstance(manifest.get("document_snapshots", {}), dict):
            raise ValueError("Documentary run manifest is invalid")
        original = manifest.get("document_snapshots", {})
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in original.items()):
            raise ValueError("Documentary run snapshots have invalid identities or values")
        if any(key in original and original[key] != value for key, value in records.items()):
            raise ValueError("A documentary snapshot is already recorded with different provenance")
        updated = canonical({**manifest, "document_snapshots": {**original, **records}})
        if len(updated.encode("utf-8")) > 1048576:
            raise ValueError("Documentary run manifest exceeds its bound")
        if updated != run["manifest_json"]:
            check_active()
            repository.update_current("AI_DG_RUN", [{**run, "manifest_json": updated}], expected=[run])
