"""Reconcile native metadata into the confirmed model without another catalog write."""

from __future__ import annotations

from datetime import datetime

from .catalog import CatalogReader, CatalogScope, is_redacted_metadata
from .catalog_metadata import confirmed_generated_readback
from .inventory import InventoryRecords, _add_observation, canonical, identity
from .inventory_writer import _one, persist_inventory
from .publication import _publication_transition, publication_claim, publication_receipt
from .publication_context import publication_context, require_execution


def replicate_work(repository, journal, reader, scope_id: str, work_id: str, replica_run_id: str, *,
                   check_active, observed_at: datetime, confirm_description=None) -> dict:
    """A task failure leaves recoverable receipts; this path never calls PUT or a model."""
    require_execution(check_active, observed_at)
    context = publication_context(repository, scope_id, work_id, replica_run_id, task_key="replicate_model")
    publication = _replica_intent(repository, context)
    receipt = publication_receipt(journal, context, publication)
    receipt_redacted = receipt is not None and is_redacted_metadata(receipt[0]["observed_description"])
    redacted = is_redacted_metadata(context["projection"]["description"]) or receipt_redacted
    if publication["replica_state"] == "VERIFIED" and context["work"]["state"] in {"DONE", "PRESERVED"} and not redacted:
        return _replica_result(publication)
    claim = _replica_claim(journal, context, publication)
    if confirm_description is not None and claim is not None:
        reader = CatalogReader(confirmed_generated_readback(reader.request, context, confirm_description, check_active),
            page_size=reader.page_size, max_pages=reader.max_pages)
    scope = context["scope"]
    check_active()
    detail = reader.read_table(CatalogScope(scope["catalog_key"], scope["schema_key"]), context["table_key"])
    records, target = _replica_observation(repository, context, detail, observed_at)
    text = target["description"]
    observation_basis = {**publication, "catalog_state": "UNKNOWN"} if receipt_redacted else publication
    result = _replica_catalog_observation(context, observation_basis, text)
    updated = _publication_transition({**publication, "replica_state": "PENDING"}, result, observed_at)
    not_started = result["status"] == "unknown" and claim is None
    if not_started:
        updated = {**publication, "last_error_code": "PUBLICATION_NOT_STARTED"}
    elif result["status"] == "conflict" and (text is None or not text.strip()):
        updated["last_error_code"] = "DESCRIPTION_CLEARED_EXTERNALLY"
    check_active()
    persist_inventory(repository, scope_id, replica_run_id, records, check_active=check_active)
    _confirm_replica_projection(repository, scope_id, context["asset"]["asset_id"], target,
        result["status"] != "unknown" or not_started, check_active)
    confirmed = {**updated, "observed_native_version": detail["native_version"],
        "replica_version_id": target["verified_version_id"],
        "replica_state": "VERIFIED" if result["status"] != "unknown" else "PENDING",
        "replica_verified_at": observed_at if result["status"] != "unknown" else None, "updated_at": observed_at}
    check_active()
    repository.update_current("AI_DG_PUBLICATION", [confirmed], expected=[publication])
    _finish_replica_work(repository, context, confirmed, check_active, observed_at)
    return _replica_result(confirmed)


def _replica_intent(repository, context) -> dict:
    document, version = context["document"], context["version"]
    observation = version["native_version"] or "OBSERVATION:" + version["version_id"]
    key = identity("AI_DG_PUBLICATION", document["document_id"], observation)
    publication = _one(repository, "AI_DG_PUBLICATION", {"publication_id": key})
    if publication["document_id"] != document["document_id"] or publication["expected_native_version"] != observation:
        raise ValueError("Replica intent does not identify the selected document")
    return publication


def _replica_claim(journal, context, publication):
    claim = journal.read("claims", publication["publication_id"])
    if claim is not None and claim != publication_claim(context, publication):
        raise ValueError("Replica start claim has different provenance")
    if claim is None and publication["catalog_state"] != "PREPARED" and publication["replica_state"] != "VERIFIED":
        raise ValueError("A started catalog operation has no durable claim")
    return claim


def _replica_observation(repository, context: dict, detail: dict, at: datetime) -> tuple:
    scope, asset = context["scope"], context["asset"]
    if detail.get("table_key") != context["table_key"]:
        raise ValueError("Replica returned a different native table")
    if "description" not in detail:
        raise ValueError("Replica table description coverage is unavailable")
    records = InventoryRecords()
    parent = asset if context["column_name"] is None else _one(repository, "AI_DG_ASSET", {
        "asset_id": asset["parent_asset_id"]})
    facts = {name: detail.get(name) for name in ("table_key", "name", "description", "native_version", "columns")}
    _add_observation(records, scope["platform_key"], scope["scope_id"], context["writer"]["run_id"], at,
        parent["asset_id"], "TABLE", parent["native_key"], None, facts, detail.get("columns") is not None)
    if context["column_name"] is not None:
        fields = detail.get("columns")
        if fields is None:
            raise ValueError("Replica column coverage is unavailable")
        matches = [field for field in fields if field.get("name") == context["column_name"]]
        if len(matches) != 1:
            raise ValueError("Replica column identity is missing or ambiguous")
        column, = matches
        native_key = column.get("native_key") or canonical([context["table_key"], column["name"]])
        if native_key != asset["native_key"]:
            raise ValueError("Replica column identity changed since admission")
        facts = {**column, "table_key": context["table_key"], "column_name": column["name"],
                 "native_version": detail.get("native_version")}
        _add_observation(records, scope["platform_key"], scope["scope_id"], context["writer"]["run_id"], at,
            asset["asset_id"], "COLUMN", native_key, parent["asset_id"], facts, True)
    records.work = []
    target, = [row for row in records.projections if row["asset_id"] == asset["asset_id"]]
    return records, target


def _replica_catalog_observation(context: dict, publication: dict, text: str | None) -> dict:
    status = "unknown"
    if text is not None and text.strip():
        status = "verified" if text == context["document"]["description_text"] else "preserved"
        if publication["catalog_state"] == "CONFLICT":
            status = "preserved"
    elif publication["catalog_state"] in {"VERIFIED", "CONFLICT"}:
        status = "conflict"
    return {"status": status, "write_attempted": False, "write_acknowledged": False,
            "observed_description": text}


def _confirm_replica_projection(repository, scope_id, asset_id, target, resolved: bool, check_active) -> None:
    before = _one(repository, "AI_DG_SCOPE_ASSET", {"scope_id": scope_id, "asset_id": asset_id})
    after = {**target, "consistency_state": "VERIFIED" if resolved else "UPDATE_PENDING"}
    check_active()
    repository.update_current("AI_DG_SCOPE_ASSET", [after], expected=[before])


def _finish_replica_work(repository, context, publication, check_active, at):
    before = _one(repository, "AI_DG_WORK", {"work_id": context["work"]["work_id"]})
    if (before["selected_document_id"] != context["document"]["document_id"]
            or before["expected_version_id"] != context["work"]["expected_version_id"]):
        raise ValueError("Replica work changed before its completion")
    state = {"VERIFIED": "DONE", "CONFLICT": "PRESERVED", "UNKNOWN": "RETRY", "PREPARED": "RETRY"}[publication["catalog_state"]]
    after = {**before, "phase": "PUBLISH" if publication["catalog_state"] == "PREPARED" else "REPLICATE",
        "state": state, "last_run_id": context["writer"]["run_id"],
        "reason_code": publication["last_error_code"], "next_attempt_at": publication["next_attempt_at"], "updated_at": at}
    check_active()
    repository.update_current("AI_DG_WORK", [after], expected=[before])


def _replica_result(publication: dict) -> dict:
    return {"publication_id": publication["publication_id"], "catalog_state": publication["catalog_state"],
            "replica_state": publication["replica_state"], "catalog_writes": 0, "model_calls": 0}
