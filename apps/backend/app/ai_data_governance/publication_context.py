"""Resolve a selected documentary result without trusting model-supplied targets."""

from __future__ import annotations

import hashlib
from datetime import datetime

from .documentary_graph import _admission
from .documentation import documentation_generation_key
from .inventory import canonical, identity
from .inventory_writer import _one
from .model_output import model_json


def publication_context(repository, scope_id: str, work_id: str, run_id: str, *, task_key: str) -> dict:
    scope = _one(repository, "AI_DG_SCOPE", {"scope_id": scope_id})
    writer = _one(repository, "AI_DG_RUN", {"run_id": run_id})
    if (not scope["enabled"] or writer["scope_id"] != scope_id or writer["status"] != "RUNNING"
            or writer["run_kind"] != "TASK" or writer["agent_id"] is not None):
        raise ValueError("Publication requires an enabled scope and its active Python run")
    manifest = model_json(writer["manifest_json"], max_bytes=1048576)
    if (manifest.get("task_key") != task_key or not isinstance(manifest.get("job_key"), str)
            or not manifest["job_key"] or manifest.get("config_version") != scope["config_version"]):
        raise ValueError("Publication writer belongs to another workflow phase or configuration")
    work = _one(repository, "AI_DG_WORK", {"work_id": work_id})
    if work["scope_id"] != scope_id or not work["selected_document_id"] or work["phase"] not in {"PUBLISH", "REPLICATE"}:
        raise ValueError("Publication requires selected documentary work in this scope")
    document = _one(repository, "AI_DG_DOCUMENT", {"document_id": work["selected_document_id"]})
    admission = _document_admission(repository, scope, work, document, manifest)
    version = _one(repository, "AI_DG_ASSET_VERSION", {"version_id": work["expected_version_id"]})
    asset = _one(repository, "AI_DG_ASSET", {"asset_id": work["asset_id"]})
    projection = _one(repository, "AI_DG_SCOPE_ASSET", {"scope_id": scope_id, "asset_id": work["asset_id"]})
    target = _publication_target(repository, scope, asset, version, admission)
    return {"scope": scope, "writer": writer, "work": work, "document": document, "admission": admission,
            "version": version, "asset": asset, "projection": projection, **target}


def _document_admission(repository, scope: dict, work: dict, document: dict, writer_manifest: dict):
    if document["work_id"] != work["work_id"] or document["validation_status"] != "VALID":
        raise ValueError("The selected document is not valid for this work")
    producer, manifest = _documentary_producer(repository, scope, document, writer_manifest)
    snapshot = model_json(manifest.get("document_snapshots", {}).get(document["document_id"]), max_bytes=131072)
    admission = _admission(snapshot["admission"])
    expected = {"scope_id": scope["scope_id"], "run_id": producer["run_id"], "work_id": work["work_id"],
        "asset_id": work["asset_id"], "asset_version_id": work["expected_version_id"], "field_path": "description",
        "model_ref": document["model_ref"], "prompt_version": document["prompt_version"]}
    if any(getattr(admission, key) != value for key, value in expected.items()):
        raise ValueError("The documentary snapshot does not match the selected result")
    _validate_document_text(admission, document, snapshot)
    return admission


def _documentary_producer(repository, scope, document, writer_manifest) -> tuple:
    producer = _one(repository, "AI_DG_RUN", {"run_id": document["producer_run_id"]})
    if (producer["scope_id"] != scope["scope_id"] or producer["run_kind"] != "AGENT"
            or not producer["agent_id"] or producer["status"] not in {"SUCCEEDED", "PARTIAL"}):
        raise ValueError("The document lacks its completed producer")
    agent = _one(repository, "AI_DG_AGENT", {"agent_id": producer["agent_id"]})
    if (agent["agent_role"] != "DOCUMENTATION" or not agent["enabled"]
            or agent["platform_key"] != scope["platform_key"] or agent["workspace_key"] != scope["workspace_key"]):
        raise ValueError("The documentary producer belongs to another installation")
    manifest = model_json(producer["manifest_json"], max_bytes=1048576)
    if (manifest.get("job_key") != writer_manifest["job_key"] or manifest.get("task_key") != "document_metadata"
            or manifest.get("config_version") != scope["config_version"]):
        raise ValueError("The documentary producer belongs to another workflow or configuration")
    return producer, manifest


def _validate_document_text(admission, document: dict, snapshot: dict) -> None:
    generation = documentation_generation_key(admission)
    text = document["description_text"]
    if (snapshot.get("document_id") != document["document_id"] or snapshot.get("generation_key") != generation
            or document["generation_key"] != generation
            or document["document_id"] != identity("AI_DG_DOCUMENT", admission.work_id, generation)):
        raise ValueError("The selected document identity differs from its original admission")
    if (not admission.update_supported or bool(admission.observed_description and admission.observed_description.strip())
            or len(text) > admission.max_description_length or not text.endswith(" [IA]")
            or text.count("[IA]") != 1 or not text[:-5].strip()
            or hashlib.sha256(text.encode()).hexdigest() != document["text_hash"]):
        raise ValueError("Invalid selected documentary text")


def _publication_target(repository, scope: dict, asset: dict, version: dict, admission) -> dict:
    facts = model_json(version["metadata_json"], max_bytes=1048576)
    if (asset["platform_key"] != scope["platform_key"] or version["asset_id"] != asset["asset_id"]
            or admission.source_version != version["metadata_hash"]
            or version["metadata_hash"] != identity("metadata", facts)):
        raise ValueError("Publication snapshot has a different asset, platform or metadata revision")
    table_key = facts.get("table_key")
    if not isinstance(table_key, str) or not table_key.startswith(scope["schema_key"] + "."):
        raise ValueError("Publication destination is outside the configured source schema")
    if asset["asset_kind"] == "TABLE" and asset["parent_asset_id"] is None and asset["native_key"] == table_key:
        return {"table_key": table_key, "column_name": None, "column_key": None}
    if asset["asset_kind"] != "COLUMN" or not asset["parent_asset_id"]:
        raise ValueError("This destination has no admitted description update capability")
    return _publication_column(repository, scope, asset, facts, table_key)


def _publication_column(repository, scope, asset, facts, table_key) -> dict:
    parent = _one(repository, "AI_DG_ASSET", {"asset_id": asset["parent_asset_id"]})
    name = facts.get("column_name")
    if (parent["asset_kind"] != "TABLE" or parent["platform_key"] != scope["platform_key"]
            or parent["native_key"] != table_key or not isinstance(name, str) or not name
            or asset["native_key"] != (facts.get("native_key") or canonical([table_key, name]))):
        raise ValueError("The column and its parent do not identify the admitted destination")
    return {"table_key": table_key, "column_name": name, "column_key": facts.get("native_key")}


def require_execution(check_active, observed_at: datetime) -> None:
    if not callable(check_active) or not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
        raise ValueError("An active execution callback and an aware observation time are required")
    check_active()
