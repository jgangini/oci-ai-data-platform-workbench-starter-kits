import json

import pytest
from pydantic import ValidationError

from app.modules.ai_data_governance.contracts import DocumentationItem, DocumentationResult, GovIntent, PlanStep
from app.modules.ai_data_governance.documentation import DocumentationAdmission, admit_documentation, admit_documentation_items


def admission(**changes):
    values = dict(scope_id="scope-a", run_id="run-1", work_id="work-1", asset_id="asset-1",
                  asset_version_id="version-1", source_version="etag-1", field_path="description",
                  observed_description=None, context_hash="context-1", prompt_version="DGD-PROMPTS-1",
                  model_ref="configured-model", evidence_ids=("evidence-1",), update_supported=True,
                  max_description_length=128)
    return DocumentationAdmission(**{**values, **changes})


def result(**changes):
    values = dict(work_id="work-1", status="documented", description_draft="Valor derivado de la tarea verificada.",
                  evidence_ids=["evidence-1"], supported_facts=["La tarea produce el campo."])
    return DocumentationResult(batch_ref="batch-1", items=[DocumentationItem(**{**values, **changes})])


@pytest.mark.parametrize("invalid", [{"approved": True}, {"evidence_ids": ["outside"]},
                                    {"description_draft": "Invalid [IA] marker"}, {"description_draft": ""}])
def test_one_invalid_model_item_does_not_discard_other_valid_work(invalid):
    response = result().model_dump()
    response["items"].append({**response["items"][0], "work_id": "work-2", **invalid})
    valid, failed = admit_documentation_items("batch-1", (admission(), admission(work_id="work-2")), response)
    assert valid.status == "documented" and valid.document_id and valid.manifest_snapshot
    assert failed.work_id == "work-2" and failed.status == "invalid_output"
    assert failed.document_id is None and failed.description_text is None


def test_duplicate_and_missing_outputs_are_recorded_per_work_without_fabrication():
    response = result().model_dump()
    response["items"].append(dict(response["items"][0]))
    duplicate, missing = admit_documentation_items("batch-1", (admission(), admission(work_id="work-2")), response)
    assert duplicate.reason_code == "DUPLICATE_OUTPUT" and duplicate.document_id is None
    assert missing.reason_code == "MISSING_OUTPUT" and missing.document_id is None


@pytest.mark.parametrize("change", [{"batch_ref": "another"}, {"approved": True}, {"schema_version": True},
                                   {"items": [{"work_id": "outside"}]}])
def test_partial_admission_does_not_relax_batch_or_authority_boundaries(change):
    with pytest.raises(ValueError):
        admit_documentation_items("batch-1", (admission(),), {**result().model_dump(), **change})


@pytest.mark.parametrize("change", [{"schema_version": 1.0}, {"schema_version": "1"},
                                   {"schema_version": 2}, {"items": "not a list"},
                                   {"items": [None]}, {"items": [{}] * 129}])
def test_partial_admission_rejects_invalid_envelopes_before_work_processing(change):
    with pytest.raises(ValueError):
        admit_documentation_items("batch-1", (admission(),), {**result().model_dump(), **change})


@pytest.mark.parametrize("extra", [{"scope_id": "other"}, {"approved": True}, {"sql": "DROP TABLE x"}])
def test_model_intent_cannot_add_authority_or_sql(extra):
    with pytest.raises(ValidationError):
        GovIntent(intent="meaning", **extra)


def test_plan_disallows_catalog_publication_as_model_tool():
    with pytest.raises(ValidationError):
        PlanStep(operation="publish_metadata", asset_ids=[], version_ids=[], purpose="change", limit=1)


@pytest.mark.parametrize("description", [None, "", "  \t\n"])
def test_only_empty_descriptions_admit_new_text(description):
    item, = admit_documentation("batch-1", (admission(observed_description=description),), result())
    assert item.description_text == "Valor derivado de la tarea verificada. [IA]"
    assert item.document_id and item.text_hash


@pytest.mark.parametrize("description", ["Definición humana", "Definición anterior [IA]", "  texto existente  "])
def test_generation_never_overwrites_nonempty_descriptions(description):
    with pytest.raises(ValueError, match="preserved"):
        admit_documentation("batch-1", (admission(observed_description=description),), result())


def test_preserved_text_does_not_become_a_document():
    output = result(status="preserved", description_draft=None, reason_code="EXISTING_DESCRIPTION")
    item, = admit_documentation("batch-1", (admission(observed_description="Texto humano"),), output)
    assert item.status == "preserved" and item.document_id is None
    assert item.description_text is None and item.manifest_snapshot is None


def test_abstention_updates_work_without_an_empty_document():
    output = result(status="insufficient_context", description_draft=None,
                    evidence_ids=[], supported_facts=[], reason_code="NO_LINEAGE")
    item, = admit_documentation("batch-1", (admission(),), output)
    assert item.reason_code == "NO_LINEAGE" and item.document_id is None


@pytest.mark.parametrize("change", [{"evidence_ids": ["outside"]}, {"work_id": "outside"}])
def test_result_cannot_use_unadmitted_evidence_or_work(change):
    with pytest.raises(ValueError):
        admit_documentation("batch-1", (admission(),), result(**change))


def test_batch_reference_is_bound_to_the_call():
    with pytest.raises(ValueError, match="batch"):
        admit_documentation("another-batch", (admission(),), result())


def test_scopes_cannot_be_mixed_in_one_model_call():
    with pytest.raises(ValueError, match="mix scopes"):
        admit_documentation("batch-1", (admission(), admission(scope_id="other", work_id="work-2")), result())


def test_unsupported_destination_cannot_be_documented():
    with pytest.raises(ValueError, match="Unsupported"):
        admit_documentation("batch-1", (admission(update_supported=False),), result())


@pytest.mark.parametrize("change", [{"description_draft": ""}, {"evidence_ids": []}, {"supported_facts": []}])
def test_documented_output_requires_text_and_support(change):
    with pytest.raises(ValidationError):
        result(**change)


def test_destination_length_includes_provenance_marker():
    with pytest.raises(ValueError, match="exceeds"):
        admit_documentation("batch-1", (admission(max_description_length=5),), result(description_draft="ab"))


def test_model_cannot_add_its_own_final_marker():
    with pytest.raises(ValueError, match="marker"):
        admit_documentation("batch-1", (admission(),), result(description_draft="Definición [IA]"))


def test_marker_inside_a_draft_is_rejected_before_appending_provenance():
    with pytest.raises(ValueError, match="marker"):
        admit_documentation("batch-1", (admission(),), result(description_draft="Valor [IA] derivado de la tarea."))


def test_mutated_results_are_revalidated_at_the_admission_boundary():
    output = result()
    output.items.append(output.items[0])
    with pytest.raises(ValidationError, match="Duplicate"):
        admit_documentation("batch-1", (admission(),), output)


def test_retry_uses_same_identity_and_keeps_original_snapshot():
    first, = admit_documentation("batch-1", (admission(),), result())
    retry, = admit_documentation("batch-1", (admission(run_id="run-2"),), result())
    assert first.document_id == retry.document_id
    original = json.loads(first.manifest_snapshot)
    assert original["admission"]["asset_version_id"] == "version-1"
    assert original["admission"]["source_version"] == "etag-1"
    assert original["admission"]["prompt_version"] == "DGD-PROMPTS-1"
    assert original["admission"]["run_id"] == "run-1"


def test_revised_context_has_a_different_generation_identity():
    first, = admit_documentation("batch-1", (admission(),), result())
    changed, = admit_documentation("batch-1", (admission(context_hash="context-2"),), result())
    assert first.document_id != changed.document_id


def test_duplicate_model_work_is_rejected():
    item = result().items[0]
    with pytest.raises(ValidationError, match="Duplicate"):
        DocumentationResult(batch_ref="batch-1", items=[item, item])
