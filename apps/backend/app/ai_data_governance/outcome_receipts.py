"""Verify documentary receipts before the Python materializer may use them.

A model message or a successful Agent task is not a validated Delta document.
Reconstruct deterministic admission and semantic-review outcomes from the exact
stored model inputs, schemas and bounded responses. This module performs reads
only; it neither publishes nor grants the caller permission to commit.
"""

from __future__ import annotations

from dataclasses import asdict, fields

from .contracts import DocumentationResult, DocumentReviews
from .documentary_graph import _admission, _batch_snapshot, _reviewed_outcomes
from .documentary_hooks import _cached_model_response, _response_generation_keys, _reusable_generation
from .documentation import AdmittedDraft, admit_documentation_items, admit_document_reviews
from .documentation import documentation_generation_key
from .inventory import identity


def verified_documentary_receipt(journal, batch_ref: str) -> dict:
    batch = journal.read("batches", batch_ref)
    if not isinstance(batch, dict) or set(batch) != {"schema_version", "batch", "execution"} or batch["schema_version"] != 1:
        raise ValueError("A durable documentary batch is required")
    snapshot = _batch_snapshot(batch_ref, batch["batch"])
    receipt = journal.read("outcomes", batch_ref)
    outcomes = _outcome_envelope(receipt, batch_ref, snapshot["admissions"])
    documented = {row["work_id"]: row for row in outcomes if row["status"] == "documented"}
    if documented:
        candidates = _generation_receipt(journal, batch_ref, snapshot)
        reviewed = _review_receipt(journal, batch_ref, snapshot, candidates)
        by_work = {row["work_id"]: row for row in reviewed if row["status"] == "documented"}
        if any(by_work.get(work) != row for work, row in documented.items()):
            raise ValueError("A persisted document lacks matching generation and supported-review receipts")
    return {"batch": batch, "admissions": snapshot["admissions"], "outcomes": outcomes,
            "outcomes_digest": identity("documentary-outcomes", receipt)}


def _outcome_envelope(receipt: dict, batch_ref: str, admissions: list) -> list:
    if (not isinstance(receipt, dict) or set(receipt) != {"schema_version", "batch_ref", "outcomes"}
            or receipt["schema_version"] != 1 or receipt["batch_ref"] != batch_ref):
        raise ValueError("A matching durable documentary outcome is required")
    rows = receipt["outcomes"]
    allowed = {row["work_id"] for row in admissions}
    if not isinstance(rows, list) or len(rows) != len(allowed):
        raise ValueError("Documentary outcomes do not cover the admitted work")
    expected = {field.name for field in fields(AdmittedDraft)}
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected or row.get("work_id") not in allowed:
            raise ValueError("Invalid documentary outcome identity or columns")
        _check_outcome_status(row)
    if len({row["work_id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate documentary outcomes")
    return rows


def _check_outcome_status(row: dict) -> None:
    states = {"documented", "insufficient_context", "unsupported", "preserved", "invalid_output", "generation_pending"}
    if row["status"] not in states:
        raise ValueError("Invalid documentary outcome state")
    reason = row["reason_code"]
    if reason is not None and (not isinstance(reason, str) or not 1 <= len(reason) <= 2000):
        raise ValueError("Invalid documentary reason")
    if row["status"] != "documented":
        if not reason or any(row[key] is not None for key in (
                "document_id", "description_text", "generation_key", "text_hash", "manifest_snapshot")):
            raise ValueError("An abstention cannot contain document data")


def _generation_receipt(journal, batch_ref: str, snapshot: dict) -> list[dict]:
    receipt = journal.read("responses", identity("model-response", "document", batch_ref))
    if not isinstance(receipt, dict) or not isinstance(receipt.get("generation_keys"), list):
        raise ValueError("The generation response receipt is unavailable")
    by_key = {documentation_generation_key(_admission(row)): row for row in snapshot["admissions"]}
    keys = receipt["generation_keys"]
    if not keys or len(keys) > 16 or any(not isinstance(key, str) or key not in by_key for key in keys):
        raise ValueError("Generation receipt includes unadmitted work")
    rows = [row for row in snapshot["admissions"] if documentation_generation_key(_admission(row)) in keys]
    payload = {"batch_ref": batch_ref, "admissions": rows,
               "contexts": {row["work_id"]: snapshot["contexts"][row["work_id"]] for row in rows}}
    expected = _response_generation_keys("document", payload, snapshot)
    response = _cached_model_response(receipt, identity("model-input", payload, DocumentationResult.model_json_schema()), expected)
    _verify_generation_reuse(journal, batch_ref, payload, expected, response)
    return [asdict(row) for row in admit_documentation_items(batch_ref, tuple(_admission(row) for row in rows), response)]


def _verify_generation_reuse(journal, batch_ref, payload, keys, response):
    seal = journal.read("claims", identity("model-phase", "document", batch_ref))
    if not isinstance(seal, dict) or not seal.get("reuse_source"):
        return
    reuse = journal.read("reuses", identity("model-response", "document", batch_ref))
    source = reuse.get("source_batch_ref") if isinstance(reuse, dict) else None
    if (not isinstance(source, str) or len(source) != 64 or source == batch_ref
            or seal.get("reuse_source") != source or seal.get("generation_keys") != keys):
        raise ValueError("Invalid documentary reuse provenance")
    schema = DocumentationResult.model_json_schema()
    candidate = _reusable_generation(journal, source, payload, schema, keys)
    if candidate is None:
        raise ValueError("Reused generation has no equivalent source input")
    original, source_digest = candidate
    expected = {"schema_version": 1, "source_batch_ref": source, "source_input_digest": source_digest,
        "input_digest": identity("model-input", payload, schema), "generation_keys": keys}
    if (seal.get("input_digest") != expected["input_digest"] or reuse != expected
            or response != {**original, "batch_ref": batch_ref}):
        raise ValueError("Reused generation differs from its original response")


def _review_receipt(journal, batch_ref: str, snapshot: dict, drafts: list[dict]) -> list[dict]:
    candidates = [row for row in drafts if row["status"] == "documented"]
    if not candidates:
        raise ValueError("No generated candidates support the declared document")
    payload = {"batch_ref": batch_ref, "candidates": candidates,
               "contexts": {row["work_id"]: snapshot["contexts"][row["work_id"]] for row in candidates}}
    receipt = journal.read("responses", identity("model-response", "review", batch_ref))
    if not isinstance(receipt, dict):
        raise ValueError("The semantic-review receipt is unavailable")
    keys = _response_generation_keys("review", payload, snapshot)
    response = _cached_model_response(receipt, identity("model-input", payload, DocumentReviews.model_json_schema()), keys)
    by_work = {row["work_id"]: row for row in snapshot["admissions"]}
    reviews, failures = admit_document_reviews(batch_ref, tuple(_admission(by_work[row["work_id"]]) for row in candidates), response)
    return _reviewed_outcomes(drafts, reviews, failures)
