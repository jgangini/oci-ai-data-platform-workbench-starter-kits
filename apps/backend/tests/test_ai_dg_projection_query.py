import json

import pytest

from app.ai_data_governance.inventory import identity
from app.ai_data_governance.projection_query import consultation_scope_ids, decode_projection_rows
from app.ai_data_governance.projection_query import projection_evidence, projection_quality, projection_search


def config():
    return {"platform_id": "platform", "workspace_key": "workspace",
        "source_scopes": [{"catalog_key": "catalog", "schema_key": "catalog.schema"}]}


def versions():
    return [{"scope_id": "a" * 64, "version_id": "b" * 64}]


def test_search_is_scoped_to_confirmed_current_projections_and_has_an_overflow_sentinel():
    sql = projection_search(config(), [])
    assert "p.consistency_state='VERIFIED'" in sql and "p.projection_hash=v.metadata_hash" in sql
    assert "a.current_version_id=v.version_id" in sql and "s.enabled=TRUE" in sql
    assert sql.endswith("LIMIT 33")
    assert consultation_scope_ids(config()) == (identity("AI_DG_SCOPE", "platform", "workspace", "catalog", "catalog.schema"),)


def test_all_native_json_payloads_preserve_null_fields_required_by_closed_dtos():
    statements = (projection_search(config(), []), projection_evidence(versions(), ("STATIC_CODE",)), projection_quality(versions()))
    assert all("map('ignoreNullFields','false')" in sql for sql in statements)
    assert decode_projection_rows({"rows": [{"payload": '{"column_name":null,"data_type":null,"owner_ref":null}'}]}, 1) == [
        {"column_name": None, "data_type": None, "owner_ref": None}]


def test_terms_are_literal_hex_values_not_sql_or_like_patterns():
    term = "_%'; DROP TABLE anything --"
    sql = projection_search(config(), [term])
    assert term not in sql and term.encode().hex() in sql
    assert ";" not in sql and "instr(lower(p.qualified_name)" in sql
    assert sql.startswith("WITH matched AS (SELECT ")
    assert "min(priority) OVER (PARTITION BY search_term)" in sql
    assert "substring_index(p.qualified_name,'.',-1)" in sql


def test_exact_name_ranking_is_independent_for_each_mention():
    sql = projection_search(config(), ["orders", "supplier"])
    assert "explode(array(" in sql and "PARTITION BY search_term" in sql
    assert "GROUP BY payload,sort_name,sort_id" in sql
    assert "min(priority) FROM matched" not in sql


def test_general_table_inventory_filters_before_ranking_and_preserves_overflow():
    sql = projection_search(config(), [], tables_only=True)
    assert "AND p.asset_kind='TABLE'" in sql
    assert sql.index("AND p.asset_kind='TABLE'") < sql.index(", ranked AS")
    assert sql.endswith("LIMIT 33")
    with pytest.raises(ValueError, match="row bound"):
        decode_projection_rows({"rows": [{"payload": "{}"}] * 33}, 32)


@pytest.mark.parametrize("kwargs", [{}, {"tables_only": True}, {"asset_ids": ["column-id"], "tables_only": True}])
def test_explicit_column_search_and_resume_do_not_receive_table_only_filter(kwargs):
    sql = projection_search(config(), ["orders.ABC13"], **kwargs)
    assert "p.asset_kind='TABLE'" not in sql


@pytest.mark.parametrize("scopes", [[], [{"catalog_key": "c", "schema_key": "other.schema"}],
    [{"catalog_key": "c", "schema_key": "c.s"}] * 2])
def test_invalid_or_duplicate_scopes_are_rejected(scopes):
    with pytest.raises(ValueError):
        projection_search({**config(), "source_scopes": scopes}, [])


def test_artifact_schema_shares_the_inventory_scope_for_other_modules():
    binding = {**config(), "source_scopes": [
        {"catalog_key": "oci_medallion", "schema_key": "oci_medallion.oci_artifacts"}]}
    expected = identity("AI_DG_SCOPE", "platform", "workspace", "oci_medallion", "oci_medallion.oci_artifacts")
    assert consultation_scope_ids(binding) == (expected,)
    assert expected.encode().hex() in projection_search(binding, ["ai_mi_documents"])


def test_evidence_query_is_bound_to_exact_scope_and_asset_version_and_expiry():
    sql = projection_evidence(versions(), ("STATIC_CODE", "EXECUTION"))
    assert "e.asset_version_id=" in sql and "e.scope_id=" in sql
    assert "e.expires_at>current_timestamp()" in sql and sql.endswith("LIMIT 129")
    with pytest.raises(ValueError):
        projection_evidence(versions(), ("ARBITRARY",))
    with pytest.raises(ValueError):
        projection_evidence([{"scope_id": "a" * 64, "version_id": "injected"}], ("STATIC_CODE",))


def test_quality_query_binds_rule_version_evidence_and_current_measurement():
    sql = projection_quality(versions())
    assert "r.rule_version=q.rule_version" in sql and "e.asset_version_id=q.asset_version_id" in sql
    assert "e.scope_id=r.scope_id" in sql and "q.expires_at>current_timestamp()" in sql
    assert "q.measured_at<=current_timestamp()" in sql
    assert "r.rule_kind='QUALITY'" in sql and "e.evidence_kind='QUALITY'" in sql


def test_native_sqltool_result_shapes_decode_only_bounded_payload_objects():
    value = {"result": {"structuredContent": {"rows": [{"payload": json.dumps({"asset_id": "a"})}]}}}
    assert decode_projection_rows(value, 32) == [{"asset_id": "a"}]
    assert decode_projection_rows(json.dumps(value), 32) == [{"asset_id": "a"}]
    assert decode_projection_rows({"rows": []}, 32) == []
    assert decode_projection_rows({"rows": [{"PAYLOAD": '{"value":null}'}]}, 1) == [{"value": None}]


@pytest.mark.parametrize("response", [
    {"isError": True}, {"result": {"error": "private error text"}},
    {"result": {"structuredContent": {"metadata": {"error": "query failed"}}}},
    {"rows": [{"wrong": "shape"}]}, {"rows": [{"payload": '[]'}]},
    {"rows": [{"payload": '{"a":1,"a":2}'}]}, {"rows": [{"payload": '{"a":NaN}'}]},
    {"rows": [{"payload": '{}'}] * 33}, {"metadata": {"error": "private detail"}},
    {"metadata": "malformed"}, {"rows": [{"payload": '{}', "PAYLOAD": '{}'}]},
])
def test_native_errors_invalid_json_and_overflow_are_not_empty_success(response):
    with pytest.raises((RuntimeError, ValueError)):
        decode_projection_rows(response, 32)


@pytest.mark.parametrize("count", [1, -1, True, None, "0", 0.0])
@pytest.mark.parametrize("location", ["result", "metadata", "outer_metadata"])
def test_declared_row_counts_cannot_hide_missing_or_invalid_results(count, location):
    result = {"rows": []}
    response = {"result": {"structuredContent": result}}
    if location == "result":
        result["rows_fetched"] = count
    elif location == "metadata":
        result["metadata"] = {"rows_fetched": count}
    else:
        response["metadata"] = {"rows_fetched": count}
    with pytest.raises(ValueError):
        decode_projection_rows(response, 32)


@pytest.mark.parametrize("location", ["result", "metadata", "outer_metadata"])
def test_explicit_truncation_cannot_be_reported_as_complete_evidence(location):
    result = {"rows": [{"payload": '{"asset_id":"a"}'}], "rows_fetched": 1}
    response = {"result": {"structuredContent": result}}
    if location == "result":
        result["results_truncated"] = True
    elif location == "metadata":
        result["metadata"] = {"results_truncated": True}
    else:
        response["metadata"] = {"results_truncated": True}
    with pytest.raises(ValueError):
        decode_projection_rows(response, 32)


def test_consistent_native_counts_and_untruncated_empty_results_remain_valid():
    response = {"metadata": {"rows_fetched": 1}, "result": {"structuredContent": {
        "rows": [{"payload": '{"asset_id":"a"}'}], "rows_fetched": 1, "results_truncated": False}}}
    assert decode_projection_rows(response, 1) == [{"asset_id": "a"}]
    assert decode_projection_rows({"rows": [], "rows_fetched": 0, "results_truncated": False}, 1) == []


@pytest.mark.parametrize("kind", ["STATIC_CODE", "EXECUTION"])
def test_projected_usage_kind_is_admitted_by_the_governance_graph(kind):
    from app.ai_data_governance.governance_graph import has_required_consultation_evidence
    sql = projection_evidence(versions(), (kind,))
    assert kind.encode().hex() in sql
    assert has_required_consultation_evidence("usage", [{"kind": kind}])
    assert not has_required_consultation_evidence("usage", [{"kind": "CATALOG"}])
