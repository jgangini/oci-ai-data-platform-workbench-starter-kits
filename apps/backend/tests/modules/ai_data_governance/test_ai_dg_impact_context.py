import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.modules.ai_data_governance.contracts import GovIntent
from app.modules.ai_data_governance.governance_graph import GovernanceNodes, READ_OPERATIONS


@pytest.mark.parametrize("intent,change", [("impact", None), ("lineage", "change field type")])
def test_change_impact_reads_code_context_even_when_native_lineage_is_empty(intent, change):
    asset = {"asset_id": "orders", "version_id": "v1", "name": "catalog.schema.orders"}
    proofs = {
        "read_asset_snapshot": ("CATALOG", {"description": "Orders"}),
        "read_lineage": ("RUNTIME_LINEAGE", {"nodes": [], "links": [],
            "absence_proves_global_isolation": False, "execution_evidence": False}),
        "find_code_usage": ("STATIC_CODE", {"references": [{"source_path": "/Workspace/orders.py",
            "line_number": 12, "task_key": "create_orders", "reference_kind": "literal_create"}],
            "execution_evidence": False, "analysis_complete": False}),
    }
    async def read(operation, assets, config):
        assert assets == [asset]
        kind, facts = proofs[operation]
        return [{"evidence_id": operation, "asset_id": asset["asset_id"], "version_id": asset["version_id"],
            "kind": kind, "facts": deepcopy(facts), "coverage": "partial"}]
    reader = AsyncMock(side_effect=read)
    model = AsyncMock()
    nodes = GovernanceNodes(authorize=AsyncMock(), resolve_assets=AsyncMock(return_value=[asset]),
        read=reader, call_model=model, capabilities=READ_OPERATIONS)
    result = asyncio.run(nodes.retrieve({"intent": GovIntent(intent=intent,
        asset_mentions=[asset["name"]], requested_change=change).model_dump()}, {}))
    assert [call.args[0] for call in reader.await_args_list] == [
        "read_asset_snapshot", "read_lineage", "find_code_usage"]
    assert [proof["kind"] for proof in result["evidence"]] == ["CATALOG", "RUNTIME_LINEAGE", "STATIC_CODE"]
    assert result["evidence"][2]["facts"]["execution_evidence"] is False
    assert result["evidence"][1]["facts"]["links"] == []
    model.assert_not_awaited()


@pytest.mark.parametrize("intent,expected", [("meaning", ["read_asset_snapshot"]),
    ("lineage", ["read_asset_snapshot", "read_lineage"]),
    ("usage", ["read_asset_snapshot", "find_code_usage"])])
def test_lookup_without_a_change_preserves_its_existing_tool_scope(intent, expected):
    reader = AsyncMock(return_value=[])
    nodes = GovernanceNodes(authorize=AsyncMock(), resolve_assets=AsyncMock(return_value=[]),
        read=reader, call_model=AsyncMock(), capabilities=READ_OPERATIONS)
    asyncio.run(nodes.retrieve({"intent": GovIntent(intent=intent).model_dump()}, {}))
    assert [call.args[0] for call in reader.await_args_list] == expected


def test_missing_code_capability_is_explicit_in_change_impact():
    nodes = GovernanceNodes(authorize=AsyncMock(), resolve_assets=AsyncMock(return_value=[]),
        read=AsyncMock(return_value=[]), call_model=AsyncMock(),
        capabilities=READ_OPERATIONS - {"find_code_usage"})
    result = asyncio.run(nodes.retrieve({"intent": GovIntent(intent="impact").model_dump()}, {}))
    assert result["gaps"] == ["La evidencia solicitada aún no está disponible: find_code_usage"]


def test_code_read_failure_is_not_silently_reported_as_no_dependencies():
    async def read(operation, assets, config):
        if operation == "find_code_usage":
            raise PermissionError("Source observation unavailable")
        return []
    nodes = GovernanceNodes(authorize=AsyncMock(), resolve_assets=AsyncMock(return_value=[]),
        read=read, call_model=AsyncMock(), capabilities=READ_OPERATIONS)
    with pytest.raises(PermissionError, match="Source observation unavailable"):
        asyncio.run(nodes.retrieve({"intent": GovIntent(intent="impact").model_dump()}, {}))
