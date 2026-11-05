"""Admit bounded documentary output without performing catalog or SQL writes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, ValidationError

from .contracts import Contract, DocumentationEnvelope, DocumentationItem, DocumentationResult, Identifier
from .contracts import DocumentReview, DocumentReviewEnvelope


class DocumentationAdmission(Contract):
    """Constructed by the authorized executor, never from a model response."""

    scope_id: Identifier
    run_id: Identifier
    work_id: Identifier
    asset_id: Identifier
    asset_version_id: Identifier
    source_version: Identifier
    field_path: Literal["description"]
    observed_description: str | None
    context_hash: Identifier
    prompt_version: Identifier
    model_ref: Identifier
    evidence_ids: tuple[Identifier, ...] = Field(max_length=128)
    update_supported: bool
    max_description_length: int = Field(ge=5, le=16000)


@dataclass(frozen=True)
class AdmittedDraft:
    work_id: str
    status: str
    reason_code: str | None
    document_id: str | None = None
    description_text: str | None = None
    generation_key: str | None = None
    text_hash: str | None = None
    manifest_snapshot: str | None = None


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def documentation_generation_key(admission: DocumentationAdmission) -> str:
    original = admission.model_dump(mode="json")
    original["evidence_ids"] = sorted(original["evidence_ids"])
    return _digest(_canonical({key: value for key, value in original.items() if key != "run_id"}))


def _check_eligibility(admission: DocumentationAdmission, item: DocumentationItem) -> None:
    has_description = bool(admission.observed_description and admission.observed_description.strip())
    if has_description and item.status != "preserved":
        raise ValueError("Existing descriptions must be preserved")
    if not has_description and item.status == "preserved":
        raise ValueError("Cannot report an empty description as preserved")
    if not has_description and not admission.update_supported and item.status != "unsupported":
        raise ValueError("Unsupported destinations must not produce documentation")
    if not set(item.evidence_ids).issubset(admission.evidence_ids):
        raise ValueError("Evidence does not belong to the admitted context")


def _admit_item(admission: DocumentationAdmission, item: DocumentationItem) -> AdmittedDraft:
    _check_eligibility(admission, item)
    if item.status != "documented":
        # An abstention updates WORK; it must not create a fake, empty DOCUMENT.
        return AdmittedDraft(item.work_id, item.status, item.reason_code)
    draft = item.description_draft.strip()
    if "[IA]" in draft:
        raise ValueError("The executor, not the model, adds the provenance marker")
    text = draft + " [IA]"
    if len(text) > admission.max_description_length:
        raise ValueError("Description exceeds the destination capability")
    original = admission.model_dump(mode="json")
    original["evidence_ids"] = sorted(original["evidence_ids"])
    generation_key = documentation_generation_key(admission)
    document_id = _digest(_canonical(["AI_DG_DOCUMENT", admission.work_id, generation_key]))
    snapshot = _canonical({
        "schema_version": 1,
        "document_id": document_id,
        "generation_key": generation_key,
        "admission": original,
        "used_evidence_ids": sorted(item.evidence_ids),
        "supported_facts": item.supported_facts,
    })
    return AdmittedDraft(item.work_id, item.status, item.reason_code, document_id,
                         text, generation_key, _digest(text), snapshot)


def admit_documentation(
    batch_ref: str,
    admissions: tuple[DocumentationAdmission, ...],
    result: DocumentationResult,
) -> tuple[AdmittedDraft, ...]:
    """Validate eligibility/identity; semantic grounding and publication remain separate gates."""
    # Frozen DTOs still contain mutable lists; validate a fresh boundary snapshot.
    result = DocumentationResult.model_validate(result.model_dump())
    if not admissions:
        raise ValueError("A documentation call requires admitted work")
    by_work = {item.work_id: item for item in admissions}
    if len(by_work) != len(admissions):
        raise ValueError("Duplicate admitted work")
    if len({(item.scope_id, item.run_id) for item in admissions}) != 1:
        raise ValueError("A documentation batch cannot mix scopes or executions")
    if result.batch_ref != batch_ref or {item.work_id for item in result.items} != set(by_work):
        raise ValueError("The response does not match the admitted batch")
    return tuple(_admit_item(by_work[item.work_id], item) for item in result.items)


def admit_documentation_items(
    batch_ref: str, admissions: tuple[DocumentationAdmission, ...], response: dict,
) -> tuple[AdmittedDraft, ...]:
    """Keep valid work when other items fail, without retrying model generation.

    Envelope/unknown-identity errors reject the batch. Missing, duplicate or invalid
    results for a known work become per-work failures; their raw text is not retained.
    The caller must durably record each outcome before retrying any failed work.
    """
    # Reuse the strict admission gate to validate the admitted batch identities.
    if not admissions or len(admissions) > 128:
        raise ValueError("A bounded admitted batch is required")
    by_work = {item.work_id: item for item in admissions}
    if len(by_work) != len(admissions) or len({(a.scope_id, a.run_id) for a in admissions}) != 1:
        raise ValueError("Invalid admitted work identities")
    envelope = DocumentationEnvelope.model_validate(response)
    if envelope.batch_ref != batch_ref:
        raise ValueError("The response does not match the admitted batch")
    grouped: dict[str, list[dict]] = {work: [] for work in by_work}
    for raw in envelope.items:
        if not isinstance(raw.get("work_id"), str) or raw["work_id"] not in by_work:
            raise ValueError("Unexpected documentation work identity")
        grouped[raw["work_id"]].append(raw)
    outcomes = []
    for work_id, admission in by_work.items():
        candidates = grouped[work_id]
        if len(candidates) != 1:
            reason = "MISSING_OUTPUT" if not candidates else "DUPLICATE_OUTPUT"
            outcomes.append(AdmittedDraft(work_id, "invalid_output", reason))
            continue
        try:
            item = DocumentationItem.model_validate(candidates[0])
            outcomes.append(_admit_item(admission, item))
        except (ValidationError, ValueError):
            outcomes.append(AdmittedDraft(work_id, "invalid_output", "INVALID_DOCUMENT"))
    return tuple(outcomes)


def admit_document_reviews(batch_ref: str, admissions: tuple[DocumentationAdmission, ...],
                           response: dict) -> tuple[dict[str, DocumentReview], dict[str, str]]:
    """Reject foreign identities, but isolate malformed reviews of known work."""
    by_work = {item.work_id: item for item in admissions}
    if not admissions or len(by_work) != len(admissions) or len(admissions) > 16 or len({(a.scope_id, a.run_id) for a in admissions}) != 1:
        raise ValueError("Invalid documentary review admission")
    envelope = DocumentReviewEnvelope.model_validate(response)
    if envelope.batch_ref != batch_ref:
        raise ValueError("Document review refers to an unadmitted batch")
    grouped = _known_reviews(envelope.reviews, by_work)
    valid, failures = {}, {}
    for work_id, admission in by_work.items():
        candidates = grouped[work_id]
        if len(candidates) != 1:
            failures[work_id] = "REVIEW_MISSING" if not candidates else "REVIEW_DUPLICATE"
            continue
        try:
            review = DocumentReview.model_validate(candidates[0])
            if not set(review.evidence_ids).issubset(admission.evidence_ids):
                raise ValueError("Review evidence is outside the admitted context")
            valid[work_id] = review
        except (ValidationError, ValueError):
            failures[work_id] = "REVIEW_INVALID"
    return valid, failures


def _known_reviews(reviews: list[dict], by_work: dict) -> dict:
    grouped = {work_id: [] for work_id in by_work}
    for raw in reviews:
        if not isinstance(raw.get("work_id"), str) or raw["work_id"] not in by_work:
            raise ValueError("Document review refers to unadmitted work")
        grouped[raw["work_id"]].append(raw)
    return grouped
