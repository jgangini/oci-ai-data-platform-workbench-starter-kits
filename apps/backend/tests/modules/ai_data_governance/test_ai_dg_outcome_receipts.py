from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from app.modules.ai_data_governance.contracts import DocumentationResult, DocumentReviews
from app.modules.ai_data_governance.documentation import DocumentationAdmission, admit_documentation_items
from app.modules.ai_data_governance.documentation import documentation_generation_key
from app.modules.ai_data_governance.inventory import identity
from app.modules.ai_data_governance.outcome_receipts import verified_documentary_receipt

from tests.modules.ai_data_governance.test_ai_dg_documentary_hooks import Store, admission, BATCH


def receipts(row=None):
    store = Store()
    row = admission() if row is None else row
    work, evidence = row["work_id"], row["evidence_ids"][0]
    batch = {"batch_ref": BATCH, "admissions": [row], "contexts": {work: "Permitted usage evidence"}}
    store.create("batches", BATCH, {"schema_version": 1, "batch": batch, "execution": {"scope_id": "scope"}})
    payload = {"batch_ref": BATCH, "admissions": [row], "contexts": batch["contexts"]}
    result = {"batch_ref": BATCH, "items": [{"work_id": work, "status": "documented",
        "description_draft": "Valor producido por la transformación observada.", "evidence_ids": [evidence],
        "supported_facts": ["La transformación se identifica en el contexto suministrado."]}]}
    drafts = admit_documentation_items(BATCH, (DocumentationAdmission.model_validate_json(json.dumps(row)),), result)
    outcomes = [asdict(draft) for draft in drafts]
    key = documentation_generation_key(DocumentationAdmission.model_validate_json(json.dumps(row)))
    store.create("responses", identity("model-response", "document", BATCH), {"schema_version": 1,
        "input_digest": identity("model-input", payload, DocumentationResult.model_json_schema()),
        "generation_keys": [key], "response": result})
    review_payload = {"batch_ref": BATCH, "candidates": outcomes, "contexts": batch["contexts"]}
    review = {"batch_ref": BATCH, "reviews": [{"work_id": work, "verdict": "supported",
              "unsupported_claims": [], "evidence_ids": [evidence], "reason_code": None}]}
    store.create("responses", identity("model-response", "review", BATCH), {"schema_version": 1,
        "input_digest": identity("model-input", review_payload, DocumentReviews.model_json_schema()),
        "generation_keys": [identity("AI_DG_REVIEW", sorted(draft.document_id for draft in drafts))], "response": review})
    store.create("outcomes", BATCH, {"schema_version": 1, "batch_ref": BATCH, "outcomes": outcomes})
    return store, outcomes


def test_only_documents_with_exact_generation_and_supported_review_receipts_are_accepted():
    store, outcomes = receipts()
    before = deepcopy(store.rows)
    verified = verified_documentary_receipt(store, BATCH)
    assert verified["outcomes"] == outcomes
    assert verified["outcomes"][0]["description_text"].endswith(" [IA]")
    assert len(verified["outcomes_digest"]) == 64
    assert store.rows == before


@pytest.mark.parametrize("phase", ["document", "review"])
def test_successful_task_or_outcome_without_the_model_receipt_is_insufficient(phase):
    store, _ = receipts()
    del store.rows["responses", identity("model-response", phase, BATCH)]
    with pytest.raises(ValueError):
        verified_documentary_receipt(store, BATCH)


@pytest.mark.parametrize("change", [
    {"description_text": "Texto cambiado [IA]"}, {"text_hash": "different"},
    {"document_id": "foreign"}, {"generation_key": "foreign"}, {"manifest_snapshot": "{}"}])
def test_changed_document_or_provenance_is_rejected_before_materialization(change):
    store, _ = receipts()
    store.rows["outcomes", BATCH]["outcomes"][0].update(change)
    with pytest.raises(ValueError, match="lacks matching"):
        verified_documentary_receipt(store, BATCH)


@pytest.mark.parametrize("verdict", ["rejected", "insufficient"])
def test_semantically_rejected_text_cannot_be_materialized_as_valid(verdict):
    store, _ = receipts()
    store.rows["responses", identity("model-response", "review", BATCH)]["response"]["reviews"][0]["verdict"] = verdict
    with pytest.raises(ValueError, match="lacks matching"):
        verified_documentary_receipt(store, BATCH)


def test_changed_input_receipt_and_foreign_generation_keys_are_rejected():
    store, _ = receipts()
    response = store.rows["responses", identity("model-response", "document", BATCH)]
    response["input_digest"] = "foreign"
    with pytest.raises(ValueError, match="different input"):
        verified_documentary_receipt(store, BATCH)
    response["generation_keys"] = ["foreign"]
    with pytest.raises(ValueError, match="unadmitted"):
        verified_documentary_receipt(store, BATCH)


def test_empty_batch_requires_no_model_receipts_and_creates_no_documents():
    store = Store()
    store.create("batches", BATCH, {"schema_version": 1,
        "batch": {"batch_ref": BATCH, "admissions": [], "contexts": {}}, "execution": {}})
    store.create("outcomes", BATCH, {"schema_version": 1, "batch_ref": BATCH, "outcomes": []})
    assert verified_documentary_receipt(store, BATCH)["outcomes"] == []


def test_unknown_work_or_abstention_with_a_hidden_document_is_rejected():
    store, _ = receipts()
    row = store.rows["outcomes", BATCH]["outcomes"][0]
    row["work_id"] = "unknown"
    with pytest.raises(ValueError, match="identity"):
        verified_documentary_receipt(store, BATCH)
    row["work_id"] = "work"
    row.update(status="insufficient_context", reason_code="INSUFFICIENT")
    with pytest.raises(ValueError, match="cannot contain"):
        verified_documentary_receipt(store, BATCH)
