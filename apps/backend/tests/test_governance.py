import ast
import hashlib
import json
import uuid

import pytest

from app.governance import (
    GOVERNANCE_TABLES,
    agent_source,
    governance_sync_notebook,
    resolve_column_identities,
)


def rendered_agent() -> str:
    return agent_source(
        model_id="ocid1.generativeaimodel.oc1.us-chicago-1.example",
        region="us-chicago-1",
        compartment_id="ocid1.compartment.oc1..example",
        platform_id="ocid1.aidataplatform.oc1..example",
    ).decode("utf-8")


def rendered_sync(*, desired_enabled=None, bootstrap_snapshot=False) -> str:
    notebook = governance_sync_notebook(
        namespace="namespace",
        platform_id="ocid1.aidataplatform.oc1..example",
        region="us-chicago-1",
        desired_enabled=desired_enabled,
        bootstrap_snapshot=bootstrap_snapshot,
        workspace_key="workspace-key",
        job_key="job-key",
    )
    return "".join(notebook["cells"][0]["source"])


def _function(source: str, name: str, namespace: dict) -> object:
    tree = ast.parse(source)
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "generated.py", "exec"), namespace)
    return namespace[name]


def _generated_identity_resolver(source: str) -> object:
    namespace = {"uuid": uuid}
    for name in (
        "_identity_indexes",
        "_unique_existing_id",
        "_existing_column_id",
        "_has_retired_name",
        "_rename_column_id",
        "_new_column_id",
        "_resolve_identities",
    ):
        _function(source, name, namespace)
    return namespace["_resolve_identities"]


def test_agent_is_global_two_tool_read_only_and_uses_official_aidp_api() -> None:
    source = rendered_agent()
    compile(source, "governance_agent.py", "exec")
    assert source.count("    @tool\n") == 2
    tree = ast.parse(source)
    tools = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(decorator, ast.Name) and decorator.id == "tool" for decorator in node.decorator_list)
    }
    assert tools == {"catalog_inventory", "catalog_lineage"}
    assert "20260430/" in source and "aiDataPlatforms/" in source
    assert '== "ACTIVE"' in source
    assert 'catalog_name == "oci_medallion" and _name(schema) == "oci_artifacts"' in source
    for forbidden in (
        "governance_" + "access_token",
        "governance_" + "policy_explain",
        "governed_" + "query",
        "gateway" + "_url",
        "Authorization",
    ):
        assert forbidden not in source


def test_generated_agent_pagination_fails_closed_on_repeated_token() -> None:
    source = rendered_agent()
    calls = 0

    def request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"items": []}, {"opc-next-page": "same"}

    namespace = {"_request": request, "_items": lambda body: body["items"]}
    list_all = _function(source, "_list", namespace)
    with pytest.raises(RuntimeError, match="repeated pagination token"):
        list_all(None, None, "/catalogs")
    assert calls == 2


def test_catalog_lineage_removes_control_nodes_descendants_and_links() -> None:
    source = rendered_agent()
    namespace: dict = {}
    _function(source, "_is_control_qualified_name", namespace)
    _function(source, "_is_control_lineage_node", namespace)
    filter_lineage = _function(source, "_filter_control_lineage", namespace)
    graph = {
        "nodes": [
            {"id": "source", "qualifiedName": "aidp://catalogs@p/o/business.sales.orders"},
            {"id": "control", "qualifiedName": "aidp://catalogs@p/o/oci_medallion.oci_artifacts.data_governance_config"},
            {"id": "control-column", "parentId": "control", "qualifiedName": "opaque-column"},
            {
                "id": "control-from-properties",
                "properties": {"default": {"catalogName": "oci_medallion", "schemaName": "oci_artifacts"}},
            },
            {"id": "control-direct", "catalogName": "oci_medallion", "schemaName": "oci_artifacts"},
            {"id": "target", "qualifiedName": "aidp://catalogs@p/o/business.gold.report"},
            {"id": "safe", "qualifiedName": "not_oci_medallion.oci_artifacts_backup.table"},
        ],
        "links": [
            {"fromNodeId": "source", "toNodeId": "control"},
            {"fromNodeId": "control", "toNodeId": "control-column"},
            {"fromNodeId": "control-column", "toNodeId": "target"},
            {"fromNodeId": "control-from-properties", "toNodeId": "target"},
            {"fromNodeId": "source", "toNodeId": "control-direct"},
            {"fromNodeId": "source", "toNodeId": "target"},
            {"fromNodeId": "target", "toNodeId": "safe"},
        ],
        "requestId": "preserved",
    }

    filtered = filter_lineage(graph)

    assert [node["id"] for node in filtered["nodes"]] == ["source", "target", "safe"]
    assert filtered["links"] == [
        {"fromNodeId": "source", "toNodeId": "target"},
        {"fromNodeId": "target", "toNodeId": "safe"},
    ]
    assert filtered["requestId"] == "preserved"
    assert '"lineage": _filter_control_lineage(graph)' in source


def test_sync_notebook_declares_exact_delta_contract_and_continuous_delay() -> None:
    source = rendered_sync(bootstrap_snapshot=True)
    compile(source, "data_governance_sync.py", "exec")
    for table in GOVERNANCE_TABLES:
        assert f".{table}" in source
    assert 'return f"oci://oci_artifacts@{CONFIG[\'namespace\']}/oci_artifacts/{table}"' in source
    assert "has_access INT" in source
    assert "enabled INT" in source
    assert "time.sleep(max(0, 30 - elapsed))" in source
    assert "data_governance_access_policy target" not in source
    assert 'status="ERROR"' in source
    assert "error_code = hashlib.sha256(type(exc).__name__.encode())" in source


def test_sync_state_frame_uses_explicit_schema_for_nullable_fields() -> None:
    source = rendered_sync()

    class FakeSpark:
        def createDataFrame(self, rows, schema=None):
            self.rows = rows
            self.schema = schema
            return "sync-state-frame"

    fake_spark = FakeSpark()
    sync_state_frame = _function(
        source,
        "_sync_state_frame",
        {"spark": fake_spark, "Row": lambda **values: values},
    )

    result = sync_state_frame(
        source="master_catalog",
        snapshot_version="",
        snapshot_hash="",
        status="ERROR",
        observed_count=0,
        inserted_count=0,
        updated_count=0,
        deleted_count=0,
        started_at="now",
        last_success_at=None,
        error_code=None,
    )

    assert result == "sync-state-frame"
    assert "last_success_at TIMESTAMP" in fake_spark.schema
    assert "error_code STRING" in fake_spark.schema
    assert fake_spark.rows[0]["last_success_at"] is None
    assert fake_spark.rows[0]["error_code"] is None


def test_disabled_sync_records_disabled_without_snapshot_or_policy_mutation() -> None:
    source = rendered_sync()
    disabled = source.index('CONFIG["desired_enabled"] is False or')
    snapshot = source.index("records, existing_metadata = _snapshot()")
    assert disabled < snapshot
    assert 'status, current_timestamp() started_at' in source
    assert 'dbutils.notebook.exit("DISABLED")' in source
    assert '"continuous" = {"pauseStatus": "PAUSED"}' not in source
    assert 'payload["continuous"] = {"pauseStatus": "PAUSED"}' in source
    assert 'if CONFIG["desired_enabled"] is None:\n        _pause_workflow()' in source
    assert "data_governance_access_policy" in source
    assert "DeltaTable.forName(spark, f\"{CONTROL_SCHEMA}.data_governance_access_policy\")" not in source


def test_vm_disable_does_not_self_pause_but_external_config_disable_does() -> None:
    vm_source = rendered_sync(desired_enabled=False)
    external_source = rendered_sync(desired_enabled=None)
    assert '"desired_enabled": false' in vm_source
    assert '"desired_enabled": null' in external_source
    disabled_start = vm_source.index("if SHOULD_DISABLE:")
    disabled_block = vm_source[disabled_start:vm_source.index("\ntry:", disabled_start)]
    assert 'if CONFIG["desired_enabled"] is None:\n        _pause_workflow()' in disabled_block
    assert disabled_block.count("_pause_workflow()") == 1
    assert disabled_block.index('status, current_timestamp() started_at') < disabled_block.index(
        'dbutils.notebook.exit("DISABLED")'
    )


def test_sync_config_is_an_exact_binary_singleton() -> None:
    source = rendered_sync()
    validate = _function(source, "_validated_enabled", {})
    assert validate([{"enabled": 0}]) == 0
    assert validate([{"enabled": 1}]) == 1
    for invalid in ([], [{"enabled": 0}, {"enabled": 1}], [{"enabled": 2}], [{"enabled": None}]):
        with pytest.raises(ValueError, match="config singleton"):
            validate(invalid)
    assert ".limit(2).collect()" in source
    assert 'status="ERROR"' in source


def test_identity_resolver_preserves_exact_and_unambiguous_rename() -> None:
    existing = [{
        "object_id": "stable",
        "table_fingerprint": "table-a",
        "column_name": "old_name",
        "column_ordinal": 2,
        "data_type": "STRING",
        "is_deleted": 0,
    }]
    exact = [{"table_fingerprint": "table-a", "column_name": "old_name", "column_ordinal": 2, "data_type": "STRING"}]
    renamed = [{"table_fingerprint": "table-a", "column_name": "new_name", "column_ordinal": 2, "data_type": "string"}]
    assert resolve_column_identities(exact, existing)[0][1:] == ("stable", "EXACT")
    assert resolve_column_identities(renamed, existing)[0][1:] == ("stable", "INFERRED_RENAME")


def test_identity_resolver_never_inherits_policy_for_ambiguous_change() -> None:
    existing = [
        {"object_id": "old-a", "table_fingerprint": "table-a", "column_name": "a", "column_ordinal": 1, "data_type": "STRING", "is_deleted": 0},
        {"object_id": "old-b", "table_fingerprint": "table-a", "column_name": "b", "column_ordinal": 2, "data_type": "STRING", "is_deleted": 0},
    ]
    incoming = [
        {"table_fingerprint": "table-a", "column_name": "x", "column_ordinal": 1, "data_type": "STRING"},
        {"table_fingerprint": "table-a", "column_name": "y", "column_ordinal": 2, "data_type": "STRING"},
    ]
    resolved = resolve_column_identities(incoming, existing)
    assert {status for _, _, status in resolved} == {"NEW"}
    assert not {object_id for _, object_id, _ in resolved} & {"old-a", "old-b"}


def test_identity_resolver_prefers_stable_column_keys_for_simultaneous_renames() -> None:
    existing = [
        {"object_id": "id-a", "table_fingerprint": "table-a", "column_key": "key-a", "column_name": "a", "column_ordinal": 1, "data_type": "STRING", "is_deleted": 0},
        {"object_id": "id-b", "table_fingerprint": "table-a", "column_key": "key-b", "column_name": "b", "column_ordinal": 2, "data_type": "STRING", "is_deleted": 0},
    ]
    incoming = [
        {"table_fingerprint": "table-a", "column_key": "key-a", "column_name": "renamed_a", "column_ordinal": 1, "data_type": "STRING"},
        {"table_fingerprint": "table-a", "column_key": "key-b", "column_name": "renamed_b", "column_ordinal": 2, "data_type": "STRING"},
    ]
    assert [(object_id, status) for _, object_id, status in resolve_column_identities(incoming, existing)] == [
        ("id-a", "EXACT"),
        ("id-b", "EXACT"),
    ]


def test_identity_resolver_only_recovers_retired_rows_by_stable_key() -> None:
    source = rendered_sync(bootstrap_snapshot=True)
    generated = _generated_identity_resolver(source)
    resolvers = (resolve_column_identities, generated)
    retired = [{
        "object_id": "retired-id",
        "table_fingerprint": "table-a",
        "column_key": "",
        "column_name": "customer_id",
        "column_ordinal": 1,
        "data_type": "STRING",
        "fingerprint": "old-fingerprint",
        "source_version": "v1",
        "is_deleted": 1,
    }]
    recreated = [{
        "table_fingerprint": "table-a",
        "column_key": "",
        "column_name": "customer_id",
        "column_ordinal": 1,
        "data_type": "STRING",
        "fingerprint": "new-fingerprint",
        "source_version": "v2",
    }]
    stable_retired = [{**retired[0], "column_key": "stable-key"}]
    stable_recreated = [{**recreated[0], "column_key": "stable-key", "column_name": "renamed_id"}]
    active_other_name = {
        **retired[0],
        "object_id": "active-other",
        "column_name": "other_name",
        "is_deleted": 0,
    }

    for resolver in resolvers:
        _, new_id, status = resolver(recreated, retired)[0]
        assert status == "NEW"
        assert new_id != "retired-id"
        assert resolver(recreated, retired + [active_other_name])[0][2] == "NEW"
        assert resolver(recreated, retired)[0][1] == new_id
        assert resolver([{**recreated[0], "source_version": "v3"}], retired)[0][1] != new_id
        assert resolver([{**recreated[0], "fingerprint": "another-fingerprint"}], retired)[0][1] != new_id
        assert resolver(recreated, retired + [{**retired[0], "object_id": "older-id"}])[0][1] != new_id
        assert resolver(stable_recreated, stable_retired)[0][1:] == ("retired-id", "EXACT")


def test_identity_exact_name_ignores_retired_rows_when_an_active_generation_exists() -> None:
    existing = [
        {"object_id": "retired", "table_fingerprint": "table-a", "column_name": "id", "column_ordinal": 1, "data_type": "STRING", "is_deleted": 1},
        {"object_id": "active", "table_fingerprint": "table-a", "column_name": "id", "column_ordinal": 1, "data_type": "STRING", "is_deleted": 0},
    ]
    incoming = [{"table_fingerprint": "table-a", "column_name": "id", "column_ordinal": 1, "data_type": "STRING"}]
    assert resolve_column_identities(incoming, existing)[0][1:] == ("active", "EXACT")


def test_sync_source_uses_creation_identity_and_rejects_ambiguous_tables() -> None:
    source = rendered_sync(bootstrap_snapshot=True)
    assert 'catalog.get("catalogGuid") or catalog["key"]' in source
    assert '"\\0".join((catalog_guid, created_at, created_by, entity_type))' in source
    assert '"\\0".join((catalog_guid, table_key))' in source
    assert "AIDP returned ambiguous table creation identities" in source
    assert "if len(columns) != 1:" in source
    assert "if len(candidates) != 1:" in source


def test_generated_sync_pagination_and_table_key_are_hardened() -> None:
    source = rendered_sync(bootstrap_snapshot=True)
    assert "seen_pages = set()" in source
    assert "AIDP pagination exceeded the safety limit" in source
    assert 'quote(table_key, safe=\'\')' in source


def test_sync_fingerprint_covers_names_ordinals_types_descriptions_and_source_version() -> None:
    source = rendered_sync(bootstrap_snapshot=True)
    fingerprint = _function(source, "_metadata_fingerprint", {"hashlib": hashlib, "json": json})
    base = ["catalog-key", "catalog-guid", "catalog", "schema-key", "schema", "table-key", "table-fp", "table", "created", "creator", "TABLE", "column-key", "column", 1, "STRING", "description", "v1"]
    original = fingerprint(*base)
    for index in (2, 4, 7, 12, 13, 14, 15, 16):
        changed = list(base)
        changed[index] = f"changed-{index}"
        assert fingerprint(*changed) != original


def test_sync_counts_no_change_insert_update_delete_and_empty_snapshot() -> None:
    source = rendered_sync(bootstrap_snapshot=True)
    counts = _function(source, "_change_counts", {})
    existing = [
        {"object_id": "same", "fingerprint": "one", "is_deleted": 0},
        {"object_id": "changed", "fingerprint": "old", "is_deleted": 0},
        {"object_id": "deleted", "fingerprint": "gone", "is_deleted": 0},
    ]
    incoming = [
        {"object_id": "same", "fingerprint": "one"},
        {"object_id": "changed", "fingerprint": "new"},
        {"object_id": "inserted", "fingerprint": "new"},
    ]
    assert counts(incoming, existing) == (1, 1, 1)
    assert counts(incoming, existing, True) == (0, 0, 0)
    assert counts([], existing) == (0, 0, 3)
    assert 'if records:' in source
    assert 'deletion_condition = col("is_deleted") == 0' in source


def test_sync_uses_source_ordinal_and_one_based_fallback() -> None:
    source = rendered_sync(bootstrap_snapshot=True)
    assert "enumerate(columns, start=1)" in source
    assert 'column.get("fieldPosition")' in source
    assert 'column.get("ordinalPosition")' in source
