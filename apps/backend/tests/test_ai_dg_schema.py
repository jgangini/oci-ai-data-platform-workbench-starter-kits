import copy

import pytest

from app.ai_data_governance.schema import descriptions, load_model, validate_model


def test_model_keeps_eighteen_tables_and_documents_all_columns():
    model = load_model()
    documentation = descriptions()
    assert len(documentation) == 18
    assert sum(len(table["columns"]) for table in model["tables"]) == 192
    assert sum(len(table["foreign_keys"]) for table in model["tables"]) == 47
    for table in model["tables"]:
        assert documentation[table["name"]]["table"].strip()
        assert set(documentation[table["name"]]) == {"table", *(c["name"] for c in table["columns"])}
        assert all(value.strip() for value in documentation[table["name"]].values())


@pytest.mark.parametrize("column_index", range(15))
def test_a_missing_scope_column_description_is_rejected(column_index):
    model = load_model()
    model["tables"][0]["columns"][column_index]["comment"] = "  "
    with pytest.raises(ValueError, match="description"):
        validate_model(model)


def test_model_rejects_partial_reference_to_a_composite_key():
    model = load_model()
    work = next(t for t in model["tables"] if t["name"] == "AI_DG_WORK")
    work["foreign_keys"][0]["target"] = ["asset_id"]
    work["foreign_keys"][0]["columns"] = ["asset_id"]
    with pytest.raises(ValueError, match="complete candidate key"):
        validate_model(model)


def test_schema_validation_does_not_mutate_its_source():
    model = load_model()
    original = copy.deepcopy(model)
    validate_model(model)
    assert model == original
