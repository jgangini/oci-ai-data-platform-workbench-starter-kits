import pytest

from app.ai_data_governance.inventory import canonical
from app.ai_data_governance.legacy_inventory import legacy_column_ids


TABLE = "cat.schema.table"
COLUMN = {"name": "ABC13", "native_key": None}
ROW = {"object_id": "preserved-id", "table_key": TABLE, "column_name": "ABC13",
       "column_key": "", "is_deleted": 0}


def test_exact_legacy_identity_is_preserved_without_similarity_or_deleted_rows():
    key = canonical(["COLUMN", canonical([TABLE, "ABC13"])])
    assert legacy_column_ids(TABLE, [COLUMN], [ROW]) == {key: "preserved-id"}
    assert legacy_column_ids(TABLE, [{**COLUMN, "name": "ABC14"}], [ROW]) == {}
    assert legacy_column_ids(TABLE, [COLUMN], [{**ROW, "is_deleted": 1}]) == {}
    assert legacy_column_ids(TABLE, [COLUMN], [{**ROW, "table_key": "other.table"}]) == {}


def test_changed_native_key_does_not_reuse_a_similar_name():
    assert legacy_column_ids(TABLE, [{**COLUMN, "native_key": "new"}], [{**ROW, "column_key": "old"}]) == {}
    key = canonical(["COLUMN", "same"])
    assert legacy_column_ids(TABLE, [{**COLUMN, "native_key": "same"}], [{**ROW, "column_key": "same"}]) == {key: "preserved-id"}


def test_ambiguous_or_invalid_legacy_id_stops_migration():
    for rows in ([ROW, {**ROW, "object_id": "other"}], [{**ROW, "object_id": ""}], [ROW, ROW]):
        with pytest.raises(ValueError):
            legacy_column_ids(TABLE, [COLUMN], rows)
