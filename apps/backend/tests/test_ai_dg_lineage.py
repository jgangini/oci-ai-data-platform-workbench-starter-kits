from copy import deepcopy
from dataclasses import asdict
from unittest.mock import MagicMock, Mock

import pytest

from app.ai_data_governance.catalog import CatalogScope
from app.ai_data_governance.lineage import LineageReader, LineageTransport


PLATFORM = "ocid1.aidataplatform.oc1.us-chicago-1.test"
SCOPE = CatalogScope("catalog", "catalog.schema")


def node(key, **changes):
    return {"id": key, "qualifiedName": f"aidp://catalogs@{PLATFORM}/o/catalog.schema.{key}",
            "properties": {"unverified": {"private": "not-for-context"}}, **changes}


def test_native_nodes_and_links_are_read_with_the_actual_column_filter():
    request = Mock(return_value=({"nodes": [node("a"), node("b")],
                                 "links": [{"fromNodeId": "a", "toNodeId": "b", "type": "DERIVED", "properties": {"opaque": "omitted"}}]}, {}))
    reader = LineageReader(request, PLATFORM, admit_node=lambda _: True)
    result = reader.read(SCOPE, "catalog.schema.a", column_name="ABC13")
    payload = request.call_args.kwargs["payload"]
    assert payload["pathFilters"] == {"predicates": {"anchorNodeColumnsIn": ["ABC13"]}}
    assert payload["level"] == "COLUMN" and payload["maxDepth"] == 2
    assert len(result.nodes) == 2 and len(result.links) == 1 and result.pagination_complete
    assert "not-for-context" not in str(asdict(result)) and "opaque" not in str(asdict(result))


def test_visibility_gate_removes_nodes_and_links_without_leaking_their_names():
    request = Mock(return_value=({"nodes": [node("allowed"), node("restricted")],
        "links": [{"fromNodeId": "allowed", "toNodeId": "restricted"},
                  {"fromNodeId": "allowed", "toNodeId": "unresolved"}]}, {}))
    result = LineageReader(request, PLATFORM, admit_node=lambda n: n["id"] == "allowed").read(SCOPE, "catalog.schema.allowed")
    assert [n["id"] for n in result.nodes] == ["allowed"] and not result.links
    assert result.omitted_nodes == 1 and result.unresolved_links == 2
    assert "restricted" not in str(asdict(result))


def test_pagination_budget_is_partial_not_a_claim_of_complete_lineage():
    request = Mock(return_value=({"nodes": [], "links": []}, {"Opc-Next-Page": "next"}))
    result = LineageReader(request, PLATFORM, admit_node=lambda _: True, max_pages=1).read(SCOPE, "catalog.schema.a")
    assert not result.pagination_complete
    assert result.query["maxDepth"] == 2


def test_empty_graph_does_not_invent_usage_or_execution():
    request = Mock(return_value=({"nodes": [], "links": []}, {}))
    result = LineageReader(request, PLATFORM, admit_node=lambda _: True).read(SCOPE, "catalog.schema.a")
    assert result.nodes == result.links == ()
    assert "executed" not in asdict(result)


def test_repeated_page_token_and_conflicting_node_revisions_fail():
    request = Mock(return_value=({"nodes": [], "links": []}, {"opc-next-page": "repeated"}))
    with pytest.raises(ValueError, match="repeated"):
        LineageReader(request, PLATFORM, admit_node=lambda _: True).read(SCOPE, "catalog.schema.a")
    request.side_effect = [({"nodes": [node("a")], "links": []}, {"opc-next-page": "next"}),
                          ({"nodes": [node("a", displayName="changed")], "links": []}, {})]
    with pytest.raises(ValueError, match="Conflicting"):
        LineageReader(request, PLATFORM, admit_node=lambda _: True).read(SCOPE, "catalog.schema.a")


@pytest.mark.parametrize("body", [{"nodes": [], "edges": []}, {"nodes": [{"displayName": "missing-id"}], "links": []},
    {"nodes": [], "links": [{"source": "wrong", "target": "wrong"}]},
    {"nodes": [node("a", qualifiedName=5)], "links": []}])
def test_unverified_response_shapes_are_not_silently_accepted(body):
    reader = LineageReader(Mock(return_value=(body, {})), PLATFORM, admit_node=lambda _: True)
    with pytest.raises(ValueError):
        reader.read(SCOPE, "catalog.schema.a")


def transport():
    session = MagicMock()
    response = session.request.return_value.__enter__.return_value
    response.status_code, response.headers = 200, {}
    response.iter_content.return_value = [b'{"nodes":[],"links":[]}']
    conf = {"region": "us-chicago-1", "platform_id": PLATFORM,
            "workspace_key": "d40e4c62-71d9-4837-8609-5479d950f6d5",
            "source_scopes": [{"catalog_key": "catalog", "schema_key": "catalog.schema"}]}
    return LineageTransport(conf, Mock(), session), session


def query():
    return {"anchorNode": f"aidp://catalogs@{PLATFORM}/o/catalog.schema.a", "direction": "BOTH",
            "maxDepth": 2, "level": "ENTITY", "shouldIncludeEdges": True}


def test_only_signed_bounded_lineage_read_action_is_admitted():
    request, session = transport()
    request("POST", "/actions/fetchLineage", payload=query(), params={"limit": "100"})
    assert session.request.call_args.args[0] == "POST"
    assert session.request.call_args.kwargs["json"] == query()
    assert session.request.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("change", [
    {"anchorNode": f"aidp://catalogs@{PLATFORM}/o/oci_medallion.oci_artifacts.ai_dg_work"},
    {"anchorNode": "aidp://catalogs@other/o/catalog.schema.a"}, {"maxDepth": 20}, {"maxDepth": True},
    {"nodeFilters": {}}, {"level": "COLUMN"}, {"shouldIncludeEdges": False},
    {"pathFilters": {"predicates": {"parentIdIn": ["unadmitted"]}}},
])
def test_lineage_transport_rejects_foreign_scopes_and_unadmitted_options_before_http(change):
    request, session = transport()
    with pytest.raises(ValueError):
        request("POST", "/actions/fetchLineage", payload={**query(), **change}, params={"limit": "100"})
    session.request.assert_not_called()
