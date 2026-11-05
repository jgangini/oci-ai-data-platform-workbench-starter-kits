"""Recoverable blank-only publication; no generation, SQL from models or CAS claim."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from urllib.parse import quote

from .catalog import is_redacted_metadata
from .catalog_metadata import _read_description, confirmed_generated_readback, publish_generated_description
from .inventory import identity
from .model_output import model_json
from .materialize import _materialization_evidence
from .publication_context import publication_context, require_execution


def publish_work(repository, journal, request, scope_id: str, work_id: str, publisher_run_id: str, *,
                 check_active, observed_at: datetime, confirm_description=None) -> dict:
    """The caller serializes workflow writers and authorizes every external effect.

    A conditional journal claim permits at most one call to the PUT helper. A lost
    claim/write acknowledgement is reconciled by reads, never a second PUT. An
    unresolved blank read remains pending; no model is invoked to replace it.
    """
    require_execution(check_active, observed_at)
    context = publication_context(repository, scope_id, work_id, publisher_run_id, task_key="publish_metadata")
    publication = _publication_intent(repository, context, check_active, observed_at)
    receipt = publication_receipt(journal, context, publication)
    redacted = is_redacted_metadata(context["projection"]["description"]) or (
        receipt is not None and is_redacted_metadata(receipt[0]["observed_description"]))
    if publication["replica_state"] == "VERIFIED" and not redacted:
        return {"publication_id": publication["publication_id"], "catalog_state": publication["catalog_state"],
                "replica_state": "VERIFIED", "write_attempted": False}
    def admitted_request(method, path, **kwargs):
        check_active()
        return request(method, path, **kwargs)
    confirmation = None
    if confirm_description is not None:
        claim = journal.read("claims", publication["publication_id"])
        if claim is not None:
            if claim != publication_claim(context, publication):
                raise ValueError("Publication start claim has different provenance")
            confirmation = confirmed_generated_readback(admitted_request, context, confirm_description, check_active)
    result, read_at = _publication_observation(repository, journal, admitted_request, context, publication,
        check_active, observed_at, receipt=receipt, force_read=redacted, confirmation=confirmation,
        confirm_description=confirm_description)
    check_active()
    basis = {**publication, "replica_state": "PENDING"} if redacted else publication
    updated = _publication_transition(basis, result, read_at)
    repository.update_current("AI_DG_PUBLICATION", [updated], expected=[publication])
    _mark_projection(repository, context, "REPLICA_PENDING" if result["status"] != "unknown" else "UPDATE_PENDING",
        check_active)
    _publication_work(repository, context, updated, check_active, observed_at)
    return {"publication_id": updated["publication_id"], "catalog_state": updated["catalog_state"],
            "replica_state": updated["replica_state"], "write_attempted": result["write_attempted"]}


def _publication_intent(repository, context: dict, check_active, at: datetime) -> dict:
    document, version = context["document"], context["version"]
    # This historical column names an observation; it never authorizes overwrite.
    observation = version["native_version"] or "OBSERVATION:" + version["version_id"]
    key = identity("AI_DG_PUBLICATION", document["document_id"], observation)
    existing = repository.read_by_keys("AI_DG_PUBLICATION", [{"publication_id": key}])
    if existing:
        row, = existing
        if (row["document_id"] != document["document_id"] or row["expected_native_version"] != observation
                or row["catalog_state"] not in {"PREPARED", "UNKNOWN", "VERIFIED", "CONFLICT"}):
            raise ValueError("Publication intent differs from the admitted document")
        return row
    work, projection = context["work"], context["projection"]
    if (work["phase"] != "PUBLISH" or work["state"] not in {"READY", "RETRY", "IN_PROGRESS"}
            or projection["consistency_state"] != "VERIFIED"
            or projection["verified_version_id"] != version["version_id"]):
        raise ValueError("A new publication requires its current confirmed source observation")
    _materialization_evidence(repository, context["admission"], _document_evidence_ids(context, repository), at)
    row = {"publication_id": key, "document_id": document["document_id"],
        "initiating_run_id": context["writer"]["run_id"], "expected_native_version": observation,
        "catalog_state": "PREPARED", "replica_state": "PENDING", "observed_native_version": None,
        "observed_text_hash": None, "catalog_verified_at": None, "replica_version_id": None,
        "replica_verified_at": None, "last_error_code": None, "next_attempt_at": None, "updated_at": at}
    check_active()
    repository.insert_immutable("AI_DG_PUBLICATION", [row])
    return row


def publication_receipt(journal, context, publication):
    receipt = journal.read("publications", publication["publication_id"])
    return _checked_publication_receipt(receipt, context, publication) if receipt is not None else None


def _publication_observation(repository, journal, request, context, publication, check_active, at, *, receipt, force_read,
                             confirmation=None, confirm_description=None):
    key = publication["publication_id"]
    if force_read:
        return _reconcile_publication(confirmation or request, context), at
    if receipt is not None:
        return receipt
    claim = publication_claim(context, publication)
    check_active()
    previous_claim = journal.read("claims", key)
    if previous_claim is not None:
        if previous_claim != claim:
            raise ValueError("Publication start claim has different provenance")
        winner = False
    elif publication["catalog_state"] == "PREPARED":
        _materialization_evidence(repository, context["admission"], _document_evidence_ids(context, repository), at)
        winner = journal.create("claims", key, claim)
    else:
        raise ValueError("An uncertain publication has no matching durable start claim")
    check_active()
    _mark_projection(repository, context, "UPDATE_PENDING", check_active)
    if winner and confirm_description is not None:
        if journal.read("claims", key) != claim:
            raise ValueError("Publication start claim changed before readback confirmation")
        confirmation = confirmed_generated_readback(request, context, confirm_description, check_active)
    result = (publish_generated_description(request, context["table_key"], context["document"]["description_text"],
                  column_name=context["column_name"], expected_column_key=context["column_key"],
                  readback_request=confirmation)
        if winner else _reconcile_publication(confirmation or request, context))
    if result["status"] != "unknown":
        receipt = {"schema_version": 1, "publication_id": key, "document_id": context["document"]["document_id"],
            "text_hash": context["document"]["text_hash"], "observed_at": at.isoformat(), "result": result}
        check_active()
        journal.create("publications", key, receipt)
    return result, at


def publication_claim(context: dict, publication: dict) -> dict:
    return {"schema_version": 1, "phase": "publish_metadata", "publication_id": publication["publication_id"],
        "document_id": context["document"]["document_id"], "scope_id": context["scope"]["scope_id"],
        "initiating_run_id": publication["initiating_run_id"], "table_key": context["table_key"],
        "column_name": context["column_name"], "column_key": context["column_key"],
        "text_hash": context["document"]["text_hash"]}


def _document_evidence_ids(context: dict, repository) -> list[str]:
    producer, = repository.read_by_keys("AI_DG_RUN", [{"run_id": context["document"]["producer_run_id"]}])

    manifest = model_json(producer["manifest_json"], max_bytes=1048576)
    snapshot = model_json(manifest["document_snapshots"][context["document"]["document_id"]], max_bytes=131072)
    used = snapshot.get("used_evidence_ids")
    if (not isinstance(used, list) or not used or len(used) > 128 or len(set(used)) != len(used)
            or not set(used).issubset(context["admission"].evidence_ids)
            or identity("AI_DG_DOC_EVIDENCE", sorted(used)) != context["document"]["evidence_digest"]):
        raise ValueError("Publication evidence differs from its original documentary snapshot")
    links = repository.read_by_keys("AI_DG_DOC_EVIDENCE", [
        {"document_id": context["document"]["document_id"], "evidence_id": key} for key in used])
    if {row["evidence_id"] for row in links} != set(used):
        raise ValueError("Publication document evidence links are incomplete")
    return used


def _checked_publication_receipt(receipt: dict, context: dict, publication: dict) -> dict:
    expected = {"schema_version": 1, "publication_id": publication["publication_id"],
                "document_id": context["document"]["document_id"], "text_hash": context["document"]["text_hash"]}
    if (set(receipt) != {*expected, "observed_at", "result"}
            or any(receipt.get(key) != value for key, value in expected.items())):
        raise ValueError("Publication receipt has different provenance")
    at = datetime.fromisoformat(receipt["observed_at"])
    result = receipt["result"]
    if at.utcoffset() is None or not isinstance(result, dict) or set(result) != {
            "status", "write_attempted", "write_acknowledged", "observed_description"}:
        raise ValueError("Publication receipt has an invalid observation")
    text = result["observed_description"]
    if (result["status"] not in {"verified", "preserved", "conflict"} or not isinstance(text, str) or not text.strip()
            or type(result["write_attempted"]) is not bool or type(result["write_acknowledged"]) is not bool):
        raise ValueError("Publication receipt is not a terminal observation")
    if result["status"] == "verified" and text != context["document"]["description_text"]:
        raise ValueError("Verified receipt does not contain the intended text")
    return {**result, "write_attempted": False, "write_acknowledged": False}, at


def _reconcile_publication(request, context: dict) -> dict:
    try:
        observed = _read_description(request, "/tables/" + quote(context["table_key"], safe=""),
            context["table_key"], context["column_name"], context["column_key"])
    except Exception:
        observed = None
    status = "unknown"
    if observed is not None and observed.strip():
        status = "verified" if observed == context["document"]["description_text"] else "preserved"
    return {"status": status, "write_attempted": False, "write_acknowledged": False,
            "observed_description": observed}


def _publication_transition(before: dict, result: dict, at: datetime) -> dict:
    if before["replica_state"] == "VERIFIED":
        return before
    status = result["status"]
    state = {"verified": "VERIFIED", "preserved": "CONFLICT", "conflict": "CONFLICT", "unknown": "UNKNOWN"}[status]
    text = result["observed_description"]
    values = {"catalog_state": state, "observed_text_hash": hashlib.sha256(text.encode()).hexdigest()
        if isinstance(text, str) else None, "last_error_code": None if status == "verified" else {
            "preserved": "DESCRIPTION_PRESERVED", "conflict": "DESCRIPTION_CHANGED", "unknown": "CATALOG_OUTCOME_UNKNOWN"}[status]}
    if all(before[key] == value for key, value in values.items()):
        return before
    return {**before, **values, "catalog_verified_at": at if status == "verified" else None,
            "next_attempt_at": at + timedelta(seconds=30) if status == "unknown" else None, "updated_at": at}


def _mark_projection(repository, context: dict, state: str, check_active) -> None:
    previous, = repository.read_by_keys("AI_DG_SCOPE_ASSET", [{"scope_id": context["scope"]["scope_id"],
                                                             "asset_id": context["asset"]["asset_id"]}])
    if previous["consistency_state"] == state or previous["consistency_state"] == "REPLICA_PENDING":
        return
    check_active()
    repository.update_current("AI_DG_SCOPE_ASSET", [{**previous, "consistency_state": state}], expected=[previous])


def _publication_work(repository, context: dict, publication: dict, check_active, at) -> None:
    previous, = repository.read_by_keys("AI_DG_WORK", [{"work_id": context["work"]["work_id"]}])
    if (previous["selected_document_id"] != context["document"]["document_id"]
            or previous["expected_version_id"] != context["work"]["expected_version_id"]):
        raise ValueError("Publication work changed before its completion")
    if previous["phase"] == "REPLICATE" and previous["state"] not in {"DONE", "PRESERVED"}:
        return
    check_active()
    repository.update_current("AI_DG_WORK", [{**previous, "phase": "REPLICATE", "state": "RETRY",
        "last_run_id": context["writer"]["run_id"], "reason_code": "REPLICA_PENDING",
        "next_attempt_at": publication["next_attempt_at"], "updated_at": at}], expected=[previous])
