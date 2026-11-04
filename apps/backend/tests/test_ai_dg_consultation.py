import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import hashlib
from unittest.mock import AsyncMock, Mock

import pytest

from app.ai_data_governance.consultation import ConsultationHooks, _catalog_schema_facts
from app.ai_data_governance.contracts import GovIntent
from app.ai_data_governance.governance_graph import measured_quality_evidence, validate_consultation_evidence
from app.ai_data_governance.inventory import identity, table_records


def harness(table_key="catalog.schema.orders", columns=None):
    catalog_key, schema_name, name = table_key.split(".")
    schema_key = catalog_key + "." + schema_name
    binding = {"platform_id": "platform", "workspace_key": "workspace",
        "source_scopes": [{"catalog_key": catalog_key, "schema_key": schema_key}]}
    scope = identity("AI_DG_SCOPE", "platform", "workspace", catalog_key, schema_key)
    at = datetime.now(timezone.utc)
    if columns is None:
        columns = ({"name": "ABC13", "native_key": "native-column",
            "ordinal": 0, "data_type": "STRING", "description": None},)
    table = {"table_key": table_key, "name": name, "native_version": None,
        "description": "Issued orders", "columns": columns}
    records = table_records("platform", scope, "inventory", table, at)
    rows = []
    for projection, version, asset in zip(records.projections, records.versions, records.assets, strict=True):
        facts = json.loads(version["metadata_json"])
        rows.append({"scope_id": scope, "asset_id": asset["asset_id"], "version_id": version["version_id"],
            "asset_kind": asset["asset_kind"], "qualified_name": projection["qualified_name"],
            "description": projection["description"], "description_origin": projection["description_origin"],
            "data_type": projection["data_type"], "owner_ref": None, "metadata_hash": version["metadata_hash"],
            "coverage": projection["coverage_state"], "observed_at": at.isoformat(),
            "table_key": facts["table_key"], "column_name": facts.get("column_name")})
    native = {"key": table["table_key"], "displayName": name, "description": table["description"],
        "tableFields": [{"fieldName": c["name"], "key": c["native_key"], "fieldPosition": c["ordinal"],
            "fieldType": c["data_type"], "fieldDescription": c["description"]} for c in columns]}
    caller = Mock(return_value=(native, {}))
    caller.check_identity = Mock()
    query = AsyncMock(return_value=rows)
    return ConsultationHooks(binding, caller, query), rows, native, caller, query


def resolved(hooks):
    return asyncio.run(hooks.resolve_assets(GovIntent(intent="meaning", asset_mentions=["orders"]), {}))


def test_table_snapshot_keeps_all_verified_columns_not_only_mentioned_assets():
    names = ("source_key", "tenant_id", "owner_id", "name", "connector_revision",
        "allowed_scope_json", "status", "description", "platform", "verified_at")
    columns = tuple({"name": name, "native_key": "column-" + name, "ordinal": index,
        "data_type": "timestamp" if name == "verified_at" else "string", "description": "Declared " + name}
        for index, name in enumerate(names))
    hooks, rows, _, caller, query = harness("oci_medallion.oci_artifacts.ai_mi_sources", columns)
    query.return_value = rows[:1]
    assets = resolved(hooks)
    proofs = asyncio.run(hooks.read("read_asset_snapshot", assets, {}))
    assert validate_consultation_evidence(proofs, assets) == proofs
    facts = proofs[0]["facts"]
    assert [c["name"] for c in facts["columns"]] == list(names)
    assert facts["columns"][-1]["data_type"] == "timestamp"
    assert facts["column_count"] == 10 and facts["columns_omitted"] == 0
    assert facts["columns_complete"] is True
    assert caller.call_count == 2  # Same native read budget as before schema delivery.


@pytest.mark.parametrize("columns,count,complete", [(None, None, False), ([], 0, True)])
def test_unavailable_and_empty_schemas_are_distinct(columns, count, complete):
    facts = _catalog_schema_facts({"columns": columns}, byte_budget=1000)
    assert facts["columns"] == columns and facts["column_count"] == count
    assert facts["columns_complete"] is complete
    assert facts["columns_omitted"] == (0 if complete else None)


@pytest.mark.parametrize("budget", [1024, 1000000])
def test_wide_schemas_keep_explicit_omissions_and_bounded_payload(budget):
    columns = [{"name": "field_" + str(i), "data_type": "string", "description": "x" * 100}
        for i in range(133)]
    facts = _catalog_schema_facts({"columns": columns}, byte_budget=budget)
    assert len(facts["columns"]) <= 128
    assert len(json.dumps(facts["columns"], ensure_ascii=False, separators=(",", ":")).encode()) <= budget
    assert facts["column_count"] == 133
    assert facts["columns_omitted"] == 133 - len(facts["columns"]) > 0
    assert facts["columns_complete"] is False


def test_revoked_read_cannot_reuse_a_previously_verified_catalog_schema():
    hooks, _, _, caller, _ = harness()
    assets = resolved(hooks)
    assert hooks.catalog_snapshots
    caller.side_effect = PermissionError("revoked")
    with pytest.raises(PermissionError):
        asyncio.run(hooks.read("read_asset_snapshot", assets, {}))
    assert hooks.catalog_snapshots == {}
    with pytest.raises(ValueError, match="not been verified"):
        asyncio.run(hooks._read_admitted("read_asset_snapshot", assets, {}))


def test_column_only_snapshot_does_not_add_parent_or_sibling_metadata():
    hooks, rows, _, _, query = harness()
    query.return_value = rows[1:]
    assets = resolved(hooks)
    proofs = asyncio.run(hooks.read("read_asset_snapshot", assets, {}))
    assert len(proofs) == 1 and proofs[0]["facts"]["qualified_name"].endswith(".ABC13")
    assert "columns" not in proofs[0]["facts"]


def test_schema_evidence_mutation_does_not_change_verified_snapshot():
    hooks, _, _, _, _ = harness()
    assets = resolved(hooks)
    first = asyncio.run(hooks.read("read_asset_snapshot", assets, {}))
    first[0]["facts"]["columns"][0]["name"] = "foreign"
    second = asyncio.run(hooks._read_admitted("read_asset_snapshot", assets, {}))
    assert second[0]["facts"]["columns"][0]["name"] == "ABC13"


def test_verified_schema_reaches_assessment_and_response_without_quality_inference():
    from app.ai_data_governance.governance_graph import GovernanceNodes

    columns = tuple({"name": "field_" + str(i), "native_key": "column-" + str(i),
        "ordinal": i, "data_type": "string", "description": None} for i in range(10))
    hooks, rows, _, _, query = harness(columns=columns)
    query.return_value = rows[:1]
    names = ", ".join(c["name"] for c in columns)
    phases = []
    async def model(phase, payload, schema, config):
        phases.append(phase)
        if phase == "G3":
            proof = payload["evidence"][0]
            assert [c["name"] for c in proof["facts"]["columns"]] == [c["name"] for c in columns]
            assert proof["kind"] == "CATALOG" and proof["facts"]["columns_complete"] is True
            return json.dumps({"evidence_ids": [proof["evidence_id"]], "supported_claims": [
                {"text": names, "evidence_ids": [proof["evidence_id"]], "kind": "observed"}],
                "conflicts": [], "gaps": [], "coverage": "sufficient", "next_action": "answer"})
        assert phase == "G6" and payload["review"]["supported_claims"][0]["text"] == names
        return json.dumps({"answer_markdown": names, "cited_evidence_ids": payload["review"]["evidence_ids"],
            "limitations": [], "case_ref": None, "result_state": "answered"})
    nodes = GovernanceNodes(authorize=hooks.authorize, resolve_assets=hooks.resolve_assets,
        read=hooks._read_admitted, call_model=model, capabilities=hooks.capabilities,
        reauthorize_evidence=hooks._validate_admitted_evidence)
    async def path():
        state = {"question": "List all columns", "intent": GovIntent(intent="meaning", asset_mentions=["orders"]).model_dump(),
            "assets": [], "evidence": [], "gaps": []}
        state.update(await nodes.retrieve(state, {}))
        state.update(await nodes.assess(state, {}))
        state.update(await nodes.respond(state, {}))
        return state
    state = asyncio.run(path())
    assert state["response"]["answer_markdown"] == names and phases == ["G3", "G6"]
    assert all(proof["kind"] == "CATALOG" for proof in state["evidence"])


def test_projection_candidates_are_authorized_and_share_one_table_read_per_phase():
    hooks, rows, _, caller, query = harness()
    assets = resolved(hooks)
    assert len(assets) == 2 and caller.call_count == 1
    assert caller.call_args.args == ("GET", "/tables/catalog.schema.orders")
    proofs = asyncio.run(hooks.read("read_asset_snapshot", assets, {}))
    assert caller.call_count == 2
    assert validate_consultation_evidence(proofs, assets) == proofs
    assert proofs[0]["facts"]["description"] == "Issued orders"
    assert proofs[1]["facts"]["description"] is None
    assert {r["version_id"] for r in proofs} == {r["version_id"] for r in rows}
    assert query.await_count == 1


@pytest.mark.parametrize("intent,mentions,table_only", [("inventory", [], True),
    ("meaning", [], False), ("inventory", ["orders.ABC13"], False)])
def test_general_inventory_selects_tables_without_changing_other_asset_searches(intent, mentions, table_only):
    hooks, rows, _, caller, query = harness()
    query.return_value = rows[:1] if table_only else rows
    assets = asyncio.run(hooks.resolve_assets(GovIntent(intent=intent, asset_mentions=mentions), {}))
    assert ("p.asset_kind='TABLE'" in query.call_args.args[0]) is table_only
    assert len(assets) == (1 if table_only else 2)
    assert caller.call_count == 1


def test_other_module_metadata_in_artifacts_is_resolved_and_readmitted():
    hooks, rows, _, caller, _ = harness("oci_medallion.oci_artifacts.ai_mi_documents")
    assets = resolved(hooks)
    proofs = asyncio.run(hooks.read("read_asset_snapshot", assets, {}))
    assert {a["name"] for a in assets} == {r["qualified_name"] for r in rows}
    assert validate_consultation_evidence(proofs, assets) == proofs
    assert caller.call_count == 2
    assert all(call.args == ("GET", "/tables/oci_medallion.oci_artifacts.ai_mi_documents")
               for call in caller.call_args_list)


@pytest.mark.parametrize("name", ["ai_dg_run", "data_governance_config"])
def test_own_control_projections_still_fail_before_catalog_reads(name):
    hooks, _, _, caller, _ = harness("oci_medallion.oci_artifacts." + name)
    with pytest.raises(ValueError, match="control tables"):
        resolved(hooks)
    caller.assert_not_called()
    assert hooks.rows == {}


def test_source_access_denied_never_returns_candidate_metadata_or_retains_it():
    hooks, _, _, caller, _ = harness()
    caller.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        resolved(hooks)
    assert hooks.rows == {}


def test_permission_is_checked_again_even_after_projection_was_resolved():
    hooks, _, _, caller, _ = harness()
    assets = resolved(hooks)
    caller.side_effect = PermissionError("revoked")
    with pytest.raises(PermissionError):
        asyncio.run(hooks.read("read_asset_snapshot", assets, {}))


def test_native_metadata_change_is_not_hidden_by_a_still_verified_delta_projection():
    hooks, _, native, _, _ = harness()
    native["description"] = "Changed after synchronization"
    with pytest.raises(RuntimeError, match="needs synchronization"):
        resolved(hooks)
    assert not hooks.rows


def test_masked_header_requires_fresh_exact_hash_confirmation_on_every_phase():
    hooks, _, native, _, _ = harness()
    expected = hashlib.sha256(native["description"].encode()).hexdigest()
    native["description"] = "*********(redacted)"
    hooks.confirm_description = AsyncMock(return_value=True)
    assets = resolved(hooks)
    asyncio.run(hooks.read("read_asset_snapshot", assets, {}))
    assert hooks.confirm_description.await_count == 2
    assert all(call.args == ("catalog.schema.orders", expected) for call in hooks.confirm_description.call_args_list)
    hooks.confirm_description.return_value = False
    with pytest.raises(ValueError, match="redacted"):
        asyncio.run(hooks.authorize({}, assets=assets))


@pytest.mark.parametrize("confirmation", [None, False, "true", 1])
def test_redaction_is_unavailable_without_exact_boolean_confirmation(confirmation):
    hooks, _, native, _, _ = harness()
    native["description"] = "*********(redacted)"
    if confirmation is not None:
        hooks.confirm_description = AsyncMock(return_value=confirmation)
    with pytest.raises(ValueError, match="redacted"):
        resolved(hooks)
    assert not hooks.rows


def test_column_confirmation_uses_parent_header_not_column_description():
    hooks, rows, native, _, query = harness()
    expected = hashlib.sha256(native["description"].encode()).hexdigest()
    native["description"] = "*********(redacted)"
    hooks.confirm_description = AsyncMock(return_value=True)
    query.side_effect = [rows[1:], rows[:1]]
    assets = resolved(hooks)
    assert len(assets) == 1 and assets[0]["name"].endswith("ABC13")
    hooks.confirm_description.assert_awaited_once_with("catalog.schema.orders", expected)
    assert "catalog.schema.orders".encode().hex() in query.call_args.args[0]


def test_masked_column_can_readmit_a_parent_with_a_preserved_non_hash_identity():
    hooks, rows, native, _, query = harness()
    rows[0]["asset_id"] = "existing-table-id"
    rows[0]["version_id"] = identity("AI_DG_ASSET_VERSION", rows[0]["asset_id"], rows[0]["metadata_hash"])
    native["description"] = "*********(redacted)"
    hooks.confirm_description = AsyncMock(return_value=True)
    query.side_effect = [rows[1:], rows[:1]]
    assert resolved(hooks)[0]["name"].endswith("ABC13")
    hooks.confirm_description.assert_awaited_once()


@pytest.mark.parametrize("change", ["column", "header-name", "identity", "scope"])
def test_hash_confirmation_cannot_hide_other_metadata_changes(change):
    hooks, _, native, _, _ = harness()
    native["description"] = "*********(redacted)"
    hooks.confirm_description = AsyncMock(return_value=True)
    if change == "column":
        native["tableFields"][0]["fieldType"] = "LONG"
    elif change == "header-name":
        native["displayName"] = "other"
    elif change == "identity":
        native["key"] = "catalog.schema.other"
    else:
        native["schemaKey"] = "catalog.foreign"
    with pytest.raises((ValueError, RuntimeError)):
        resolved(hooks)
    if change in {"identity", "scope"}:
        hooks.confirm_description.assert_not_awaited()


def test_masked_column_without_current_parent_projection_is_unavailable():
    hooks, rows, native, _, query = harness()
    native["description"] = "*********(redacted)"
    hooks.confirm_description = AsyncMock(return_value=True)
    query.side_effect = [rows[1:], []]
    with pytest.raises(ValueError, match="known parent"):
        resolved(hooks)
    hooks.confirm_description.assert_not_awaited()


def batch_harness():
    hooks, rows, native, caller, query = harness()
    _, other_rows, other_native, _, _ = harness('catalog.schema.shipping')
    native['description'] = other_native['description'] = '*********(redacted)'
    tables = {'catalog.schema.orders':native, 'catalog.schema.shipping':other_native}
    caller.side_effect = lambda method, path: (tables[path.removeprefix('/tables/')], {})
    query.return_value = rows + other_rows[:1]
    hooks.confirm_descriptions = AsyncMock(return_value={table:True for table in tables})
    return hooks, tables, caller, query


def test_masked_tables_and_columns_share_one_confirmation_batch_on_each_fresh_phase():
    hooks, tables, caller, query = batch_harness()
    assets = resolved(hooks)
    assert len(assets) == 3 and caller.call_count == 2
    expected = [{'table_key':table, 'expected_hash':hashlib.sha256(b'Issued orders').hexdigest()}
        for table in tables]
    hooks.confirm_descriptions.assert_awaited_once_with(expected)
    assert query.await_count == 1
    proofs = asyncio.run(hooks.read('read_asset_snapshot', assets, {}))
    assert len(proofs) == 3 and caller.call_count == 4
    assert hooks.confirm_descriptions.await_count == 2
    assert all(call.args == (expected,) for call in hooks.confirm_descriptions.call_args_list)
    tables['catalog.schema.orders']['tableFields'][0]['fieldType'] = 'LONG'
    with pytest.raises(RuntimeError, match='needs synchronization'):
        asyncio.run(hooks.authorize({}, assets=assets))
    assert hooks.confirm_descriptions.await_count == 3


def test_unmasked_phase_never_calls_the_batch_confirmer():
    hooks, _, _, _, _ = harness()
    hooks.confirm_descriptions = AsyncMock()
    assets = resolved(hooks)
    assert asyncio.run(hooks.read('read_asset_snapshot', assets, {}))
    hooks.confirm_descriptions.assert_not_awaited()


@pytest.mark.parametrize('result', [{}, {'catalog.schema.orders':True},
    {'catalog.schema.orders':True,'catalog.schema.shipping':1},
    {'catalog.schema.orders':True,'catalog.schema.shipping':'true'},
    {'catalog.schema.orders':True,'catalog.schema.shipping':True,'foreign':True},
    {'catalog.schema.orders':True,'catalog.schema.shipping':False}])
def test_batch_confirmation_cannot_partially_admit_assets(result):
    hooks, _, _, _ = batch_harness()
    hooks.confirm_descriptions.return_value = result
    with pytest.raises(ValueError):resolved(hooks)
    assert hooks.rows == {}


def test_batch_column_only_resolution_confirms_its_parent_once():
    hooks, rows, native, caller, query = harness()
    native['description'] = '*********(redacted)'
    query.side_effect = [rows[1:], rows[:1]]
    hooks.confirm_descriptions = AsyncMock(return_value={'catalog.schema.orders':True})
    assert len(resolved(hooks)) == 1 and caller.call_count == 1
    hooks.confirm_descriptions.assert_awaited_once_with([{'table_key':'catalog.schema.orders',
        'expected_hash':hashlib.sha256(b'Issued orders').hexdigest()}])


def test_batch_confirmer_error_and_cancellation_never_return_admitted_candidates():
    hooks, _, _, _ = batch_harness()
    hooks.confirm_descriptions.side_effect = RuntimeError('failed')
    with pytest.raises(RuntimeError):resolved(hooks)
    assert hooks.rows == {}
    hooks.confirm_descriptions.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):resolved(hooks)
    assert hooks.rows == {}


@pytest.mark.parametrize("change", [
    {"scope_id": "foreign"}, {"version_id": "f" * 64}, {"asset_kind": "UNSUPPORTED"},
    {"table_key": "other.schema.orders"}, {"qualified_name": "forged.name"}, {"coverage": "UNKNOWN"},
    {"column_name": "not-a-table"}, {"credential": "not-an-admitted-field"},
])
def test_foreign_malformed_or_forged_projection_is_rejected_before_catalog_http(change):
    hooks, rows, _, caller, _ = harness()
    rows[0].update(change)
    with pytest.raises(ValueError):
        resolved(hooks)
    caller.assert_not_called()


@pytest.mark.parametrize('missing_fields', [[], ['table']])
def test_no_assets_or_a_required_clarification_do_not_invent_observations(missing_fields):
    hooks, _, _, caller, query = harness()
    query.return_value = []
    assert resolved(hooks) == []
    assert asyncio.run(hooks.read("read_asset_snapshot", [], {})) == []
    caller.assert_not_called()
    query.reset_mock()
    assert asyncio.run(hooks.resolve_assets(GovIntent(intent="meaning", clarification="Which table?", missing_fields=missing_fields), {})) == []
    query.assert_not_called()


def test_resumed_assets_are_resolved_from_projections_and_authorized_again():
    hooks, rows, _, caller, query = harness()
    assets = [{"asset_id": rows[0]["asset_id"], "version_id": rows[0]["version_id"], "name": rows[0]["qualified_name"]}]
    asyncio.run(hooks.authorize({}, assets=assets))
    assert "p.asset_id IN" in query.call_args.args[0] and caller.call_count == 1
    foreign = [{**assets[0], "version_id": "f" * 64}]
    with pytest.raises(PermissionError):
        asyncio.run(hooks.authorize({}, assets=foreign))


def test_access_evidence_only_attests_metadata_read_not_data_permissions():
    hooks, _, _, _, _ = harness()
    assets = resolved(hooks)
    proofs = asyncio.run(hooks.read("read_access_evidence", assets, {}))
    assert all(p["kind"] == "ACCESS_OBSERVATION" and p["facts"]["operation"] == "READ_METADATA" for p in proofs)
    assert all(p["facts"]["data_access_inferred"] is False for p in proofs)


def test_central_access_evidence_never_claims_the_user_has_direct_permissions():
    hooks, _, _, caller, _ = harness()
    caller.access_mode = "CENTRAL_METADATA"
    proofs = asyncio.run(hooks.read("read_access_evidence", resolved(hooks), {}))
    assert proofs and all(p["facts"]["access_basis"] == "CENTRAL_METADATA" for p in proofs)
    assert all(p["facts"]["caller_permissions_evaluated"] is False for p in proofs)


def test_only_fresh_version_bound_measured_quality_can_support_a_quality_answer():
    hooks, rows, _, _, query = harness()
    assets = resolved(hooks)
    now = datetime.now(timezone.utc)
    assessment = {"assessment_id": "measurement", "rule_id": "not-null", "rule_version": 1,
        "rule_kind": "QUALITY", "evidence_kind": "QUALITY",
        "scope_id": rows[0]["scope_id"], "version_id": rows[0]["version_id"], "status": "PASS",
        "result_json": json.dumps({"method": "COUNT_NULLS", "coverage": "COMPLETE", "nulls": 0}),
        "evidence_id": "observed-measurement", "coverage": "COMPLETE", "measured_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat()}
    query.return_value = [assessment]
    proof = asyncio.run(hooks.read("read_quality_results", assets, {}))[0]
    assert measured_quality_evidence(proof)
    assessment["coverage"] = "PARTIAL"
    assert not measured_quality_evidence(asyncio.run(hooks.read("read_quality_results", assets, {}))[0])
    assessment["version_id"] = "f" * 64
    with pytest.raises(ValueError):
        asyncio.run(hooks.read("read_quality_results", assets, {}))
    assessment["version_id"] = rows[0]["version_id"]
    assessment["rule_kind"] = "ACCESS_DECLARATION"
    with pytest.raises(ValueError, match="Non-quality"):
        asyncio.run(hooks.read("read_quality_results", assets, {}))


def test_valid_identifier_asset_keys_are_not_restricted_to_hashes():
    hooks, rows, _, _, query = harness()
    rows[0]["asset_id"] = "existing-column-id"
    rows[0]["version_id"] = identity("AI_DG_ASSET_VERSION", rows[0]["asset_id"], rows[0]["metadata_hash"])
    assert resolved(hooks)[0]["asset_id"] == "existing-column-id"
    hooks.rows.clear()
    asyncio.run(hooks.authorize({}, assets=[{"asset_id": rows[0]["asset_id"], "version_id": rows[0]["version_id"], "name": rows[0]["qualified_name"]}]))
    assert "existing-column-id".encode().hex() in query.call_args.args[0]


def test_lineage_does_not_expose_an_unreadable_neighbor_or_claim_execution():
    hooks, _, native, caller, _ = harness()
    assets = resolved(hooks)[:1]
    nodes = [{"id": "visible", "qualifiedName": "catalog.schema.orders"},
        {"id": "hidden", "qualifiedName": "catalog.schema.restricted"}]
    def exchange(method, path, **kwargs):
        if method == "POST":
            return {"nodes": nodes, "links": [{"fromNodeId": "visible", "toNodeId": "hidden", "type": "DERIVED"}]}, {}
        if path.endswith("restricted"):
            raise PermissionError("denied")
        return native, {}
    caller.side_effect = exchange
    proof = asyncio.run(hooks.read("read_lineage", assets, {}))[0]
    assert proof["coverage"] == "partial" and proof["kind"] == "RUNTIME_LINEAGE"
    assert proof["facts"]["links"] == [] and proof["facts"]["omitted_nodes"] == 1
    assert "restricted" not in json.dumps(proof)
    assert proof["facts"]["execution_evidence"] is False
    assert proof["facts"]["absence_proves_global_isolation"] is False


def test_lineage_permission_resolution_has_a_fixed_budget_even_for_large_graphs():
    hooks, _, native, caller, _ = harness()
    assets = resolved(hooks)[:1]
    nodes = [{"id": str(i), "qualifiedName": "catalog.schema.table_" + str(i)} for i in range(100)]
    def exchange(method, path, **kwargs):
        if method == "POST":
            return {"nodes": nodes, "links": []}, {}
        if path.endswith("orders"):
            return native, {}
        raise PermissionError("denied")
    caller.side_effect = exchange
    caller.reset_mock()
    proof = asyncio.run(hooks.read("read_lineage", assets, {}))[0]
    assert proof["coverage"] == "partial" and proof["facts"]["omitted_nodes"] == 100
    assert caller.call_count == 18  # Anchor admission, lineage action, at most sixteen visibility probes.
