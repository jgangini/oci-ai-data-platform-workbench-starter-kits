import pytest

from app.ai_data_governance.catalog import CatalogReader, CatalogScope, normalize_columns


SCOPE = CatalogScope("catalog-a", "schema-a")


def test_reader_follows_native_pages_without_collecting_all_tables():
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if len(calls) == 1:
            return {"items": [{"key": "table-1"}]}, {"Opc-Next-Page": "next"}
        return [{"key": "table-2"}], {}

    pages = iter(CatalogReader(request).table_pages(SCOPE))
    assert next(pages).items == ({"key": "table-1"},)
    assert len(calls) == 1
    assert next(pages).next_page is None
    assert len(list(pages)) == 0
    assert calls[1][2]["params"] == {"catalogKey": "catalog-a", "schemaKey": "schema-a", "limit": "100", "page": "next"}


def test_page_failure_is_not_converted_to_an_empty_inventory():
    def request(*args, **kwargs):
        raise RuntimeError("native failure")

    with pytest.raises(RuntimeError, match="native failure"):
        list(CatalogReader(request).table_pages(SCOPE))


@pytest.mark.parametrize("body", [{}, {"items": None}, {"items": [None]}, "not-json-list"])
def test_malformed_pages_do_not_establish_inventory_completion(body):
    with pytest.raises(ValueError, match="invalid catalog page"):
        list(CatalogReader(lambda *a, **kw: (body, {})).table_pages(SCOPE))


def test_repeated_pagination_tokens_are_rejected():
    reader = CatalogReader(lambda *a, **kw: ({"items": []}, {"opc-next-page": "same"}))
    with pytest.raises(ValueError, match="repeated"):
        list(reader.table_pages(SCOPE))


def test_page_budget_exhaustion_is_an_incomplete_inventory():
    reader = CatalogReader(lambda *a, **kw: ({"items": []}, {"opc-next-page": "next"}), max_pages=1)
    with pytest.raises(ValueError, match="incomplete"):
        list(reader.table_pages(SCOPE))


def test_real_empty_catalog_is_a_valid_completed_page():
    pages = list(CatalogReader(lambda *a, **kw: ({"items": []}, {})).table_pages(SCOPE))
    assert len(pages) == 1 and pages[0].next_page is None and pages[0].items == ()


def test_scope_mismatch_is_rejected():
    body = {"items": [{"key": "table-a", "schemaKey": "another-schema"}]}
    with pytest.raises(ValueError, match="outside"):
        list(CatalogReader(lambda *a, **kw: (body, {})).table_pages(SCOPE))


def test_read_table_preserves_etag_null_description_and_zero_ordinal():
    def request(method, path, **kwargs):
        assert method == "GET" and path == "/tables/table-a"
        return {"key": "table-a", "description": None, "tableFields": [
            {"key": "field-a", "fieldName": "ABC13", "fieldPosition": 0, "fieldDescription": ""}
        ]}, {"ETag": "version-a"}

    table = CatalogReader(request).read_table(SCOPE, "table-a")
    assert table["description"] is None and table["native_version"] == "version-a"
    assert table["columns"][0]["ordinal"] == 0 and table["columns"][0]["description"] == ""


def test_missing_field_coverage_does_not_mean_zero_columns():
    assert normalize_columns({}) is None
    assert normalize_columns({"tableFields": []}) == ()


def test_wrong_native_table_identity_is_rejected():
    with pytest.raises(ValueError, match="mismatched"):
        CatalogReader(lambda *a, **kw: ({"key": "other"}, {})).read_table(SCOPE, "table-a")


@pytest.mark.parametrize("detail", [
    {"key": "table-a", "tableFields": []},
    {"key": "table-a", "description": None, "tableFields": [{"fieldName": "code"}]},
])
def test_unavailable_description_is_not_silently_normalized_to_blank(detail):
    with pytest.raises(ValueError, match="description coverage"):
        CatalogReader(lambda *a, **kw: (detail, {})).read_table(SCOPE, "table-a")


def test_shared_artifacts_schema_includes_other_modules_but_never_self_documents():
    from unittest.mock import Mock
    scope=CatalogScope("oci_medallion","oci_medallion.oci_artifacts")
    keys=["ai_dg_work","AI_DG_SCOPE","data_governance_metadata","ai_mi_topics","other_project_table"]
    request=Mock(return_value=({"items":[{"key":scope.schema_key+"."+key} for key in keys]},{}))
    pages=list(CatalogReader(request).table_pages(scope))
    assert [x["key"] for x in pages[0].items]==[scope.schema_key+".ai_mi_topics",scope.schema_key+".other_project_table"]
    request.reset_mock()
    with pytest.raises(ValueError,match="cannot document themselves"):
        CatalogReader(request).read_table(scope,scope.schema_key+".ai_dg_work")
    request.assert_not_called()


@pytest.mark.parametrize("column", [False, True])
def test_redacted_metadata_is_not_available_description_coverage(column):
    detail = {"key": "table-a", "description": None, "tableFields": []}
    if column:
        detail["tableFields"] = [{"fieldName": "code", "fieldDescription": "*********(redacted)"}]
    else:
        detail["description"] = "*********(redacted)"
    with pytest.raises(ValueError, match="redacted"):
        CatalogReader(lambda *a, **kw: (detail, {})).read_table(SCOPE, "table-a")
