from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from governance_gateway.catalog import AidpCatalogClient, CatalogColumn, CatalogSyncError
from governance_gateway.store import _ExistingColumn, _resolve_object_ids


class Response:
    def __init__(self, payload: dict[str, Any], headers: dict[str, str] | None = None, status: int = 200) -> None:
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status

    def json(self) -> dict[str, Any]:
        return self._payload


class Session:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> Response:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _column(name: str = "email", ordinal: int = 1, kind: str = "string") -> CatalogColumn:
    return CatalogColumn(
        "aidp_lab", "gold", "customers", name, "aidp_lab", "catalog-guid", "aidp_lab.gold",
        "aidp_lab.gold.customers", "table-fingerprint", ordinal, kind, "", "STANDARD",
        "2026-01-01T00:00:00Z", "ocid1.user.oc1..owner",
    )


def test_catalog_snapshot_uses_official_pagination_and_skips_control_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOVERNANCE_CONTROL_CATALOG", "aidp_lab")
    session = Session([
        Response({"items": [{"key": "aidp_lab", "displayName": "aidp_lab", "catalogGuid": "guid"}]}),
        Response({"items": [
            {"key": "aidp_lab.gold", "displayName": "gold"},
            {"key": "aidp_lab.oci_control", "displayName": "oci_control"},
        ]}),
        Response({"items": [{"key": "aidp_lab.gold.customers", "displayName": "customers"}]}),
        Response({
            "key": "aidp_lab.gold.customers",
            "displayName": "customers",
            "entityType": "STANDARD",
            "createdBy": "ocid1.user.oc1..owner",
            "timeCreated": "2026-01-01T00:00:00Z",
            "tableFields": [{"fieldName": "email", "fieldType": "string"}],
        }),
        Response({
            "nodes": [
                {"id": "source", "qualifiedName": "aidp_lab.gold.customers.email"},
                {"id": "target", "qualifiedName": "aidp_lab.gold.customer_summary.email"},
            ],
            "links": [{"fromNodeId": "source", "toNodeId": "target"}],
        }),
    ])
    client = AidpCatalogClient(
        "https://aidp.us-chicago-1.oci.oraclecloud.com",
        "ocid1.aidataplatform.oc1.us-chicago-1.example",
        session,
    )
    snapshot = client.snapshot()
    assert [column.column_name for column in snapshot.columns] == ["email"]
    assert len(session.calls) == 5
    assert session.calls[0][1]["params"]["limit"] == 1000
    assert session.calls[0][1]["allow_redirects"] is False
    assert snapshot.lineage[0].source_qualified_name == "aidp_lab.gold.customers.email"
    assert session.calls[-1][1]["json"]["level"] == "COLUMN"


def test_catalog_pagination_rejects_repeated_page_token() -> None:
    session = Session([
        Response({"items": []}, {"opc-next-page": "same"}),
        Response({"items": []}, {"opc-next-page": "same"}),
    ])
    client = AidpCatalogClient(
        "https://aidp.us-chicago-1.oci.oraclecloud.com",
        "ocid1.aidataplatform.oc1.us-chicago-1.example",
        session,
    )
    with pytest.raises(CatalogSyncError, match="repeated"):
        client.snapshot()


def test_principal_directory_contains_only_aidp_visible_roles_and_assignments() -> None:
    session = Session([
        Response({"items": [{"key": "developer", "displayName": "AIDP_DEVELOPER"}]}),
        Response({"assignees": [
            {"type": "USER", "target": "ocid1.user.oc1..developer", "displayName": "developer@example.com"},
            {"type": "GROUP", "target": "ocid1.group.oc1..developers"},
            {"type": "SERVICE", "target": "ignored"},
        ]}),
    ])
    client = AidpCatalogClient(
        "https://aidp.us-chicago-1.oci.oraclecloud.com",
        "ocid1.aidataplatform.oc1.us-chicago-1.example",
        session,
    )
    assert client.principals() == [
        {"principal_type": "GROUP", "principal_name": "ocid1.group.oc1..developers", "display_name": "ocid1.group.oc1..developers"},
        {"principal_type": "ROLE", "principal_name": "AIDP_DEVELOPER", "display_name": "AIDP_DEVELOPER"},
        {"principal_type": "USER", "principal_name": "ocid1.user.oc1..developer", "display_name": "developer@example.com"},
    ]


def test_metadata_change_preserves_object_identity() -> None:
    existing = [_ExistingColumn("stable-id", "table-fingerprint", "old.table", "email", 1, "string", False)]
    changed = replace(_column(), column_description="updated", column_type="varchar")
    resolved, new_count, renamed_count = _resolve_object_ids((changed,), existing)
    assert resolved[0][1:] == ("stable-id", "EXACT")
    assert (new_count, renamed_count) == (0, 0)


def test_single_unambiguous_column_rename_preserves_policy_identity() -> None:
    existing = [_ExistingColumn("stable-id", "table-fingerprint", "old.table", "email", 1, "string", False)]
    renamed = _column("contact_email")
    resolved, new_count, renamed_count = _resolve_object_ids((renamed,), existing)
    assert resolved[0][1:] == ("stable-id", "INFERRED_RENAME")
    assert (new_count, renamed_count) == (0, 1)


def test_ambiguous_column_change_is_new_and_therefore_unreviewed() -> None:
    existing = [
        _ExistingColumn("id-1", "table-fingerprint", "old.table", "first", 0, "string", False),
        _ExistingColumn("id-2", "table-fingerprint", "old.table", "second", 1, "string", False),
    ]
    incoming = (_column("new_first", 0), _column("new_second", 1))
    resolved, new_count, renamed_count = _resolve_object_ids(incoming, existing)
    assert all(status == "NEW" for _, _, status in resolved)
    assert (new_count, renamed_count) == (2, 0)
