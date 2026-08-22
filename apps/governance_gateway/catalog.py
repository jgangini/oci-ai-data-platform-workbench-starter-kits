from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Iterable, Protocol
from urllib.parse import quote


_SOURCE = "AIDP_MASTER_CATALOG"
_MAX_PAGES = 1_000


class CatalogSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogColumn:
    catalog_name: str
    schema_name: str
    table_name: str
    column_name: str
    catalog_key: str
    catalog_guid: str
    schema_key: str
    table_key: str
    table_fingerprint: str
    column_ordinal: int
    column_type: str
    column_description: str
    entity_type: str
    table_created_at: str
    table_created_by: str


@dataclass(frozen=True)
class CatalogSnapshot:
    version: str
    content_hash: str
    columns: tuple[CatalogColumn, ...]
    lineage: tuple["LineageEdge", ...] = ()
    source: str = _SOURCE


@dataclass(frozen=True)
class LineageEdge:
    source_qualified_name: str
    target_qualified_name: str


class HttpResponse(Protocol):
    status_code: int
    headers: Any

    def json(self) -> Any: ...


class HttpSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> HttpResponse: ...
    def post(self, url: str, **kwargs: Any) -> HttpResponse: ...


class AidpCatalogClient:
    """Read an AIDP Master Catalog snapshot with OKE Workload Identity."""

    def __init__(self, endpoint: str, platform_id: str, session: HttpSession) -> None:
        endpoint = endpoint.rstrip("/")
        if not endpoint.startswith("https://") or not platform_id.startswith("ocid1.aidataplatform."):
            raise ValueError("A valid HTTPS AIDP endpoint and platform OCID are required.")
        self._base = f"{endpoint}/20260430/aiDataPlatforms/{quote(platform_id, safe='')}"
        self._session = session

    @classmethod
    def from_environment(cls) -> "AidpCatalogClient":
        region = os.environ.get("GOVERNANCE_OCI_REGION", "").strip()
        platform_id = os.environ.get("GOVERNANCE_AIDP_PLATFORM_ID", "").strip()
        if not region or not platform_id:
            raise CatalogSyncError("GOVERNANCE_OCI_REGION and GOVERNANCE_AIDP_PLATFORM_ID are required.")
        return cls(f"https://aidp.{region}.oci.oraclecloud.com", platform_id, _OkeSession())

    def snapshot(self) -> CatalogSnapshot:
        columns: list[CatalogColumn] = []
        lineage: set[LineageEdge] = set()
        catalogs = self._list("catalogs", {"catalogState": "ACTIVE"})
        for catalog in catalogs:
            catalog_key = _required_string(catalog, "key")
            catalog_name = _required_string(catalog, "displayName")
            catalog_guid = _string(catalog.get("catalogGuid")) or catalog_key
            schemas = self._list("schemas", {"catalogKey": catalog_key})
            for schema in schemas:
                schema_key = _required_string(schema, "key")
                schema_name = _required_string(schema, "displayName")
                if catalog_key == os.environ.get("GOVERNANCE_CONTROL_CATALOG", "aidp_lab") and schema_name == "oci_control":
                    continue
                tables = self._list("tables", {"catalogKey": catalog_key, "schemaKey": schema_key})
                for summary in tables:
                    table_key = _required_string(summary, "key")
                    detail = self._get(f"tables/{quote(table_key, safe='')}")
                    columns.extend(_table_columns(catalog_key, catalog_name, catalog_guid, schema_key, schema_name, detail))
                    lineage.update(self._lineage(table_key))

        ordered = tuple(sorted(columns, key=_column_sort_key))
        _reject_ambiguous_table_fingerprints(ordered)
        ordered_lineage = tuple(sorted(lineage, key=lambda edge: (
            edge.source_qualified_name.casefold(), edge.target_qualified_name.casefold(),
        )))
        canonical = json.dumps(
            {"columns": [column.__dict__ for column in ordered], "lineage": [edge.__dict__ for edge in ordered_lineage]},
            sort_keys=True, separators=(",", ":"),
        )
        return CatalogSnapshot(
            version=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            columns=ordered,
            lineage=ordered_lineage,
        )

    def principals(self) -> list[dict[str, str]]:
        """List only principals already visible through AIDP role assignments."""
        principals: dict[tuple[str, str], str] = {}
        for role in self._list("roles", {}):
            role_key = _required_string(role, "key")
            role_name = _required_string(role, "displayName")
            principals[("ROLE", role_name)] = role_name
            detail = self._get(f"roles/{quote(role_key, safe='')}")
            assignees = detail.get("assignees") or []
            if not isinstance(assignees, list) or not all(isinstance(item, dict) for item in assignees):
                raise CatalogSyncError("AIDP returned an invalid role assignment collection.")
            for assignee in assignees:
                principal_type = _string(assignee.get("type")).upper()
                if principal_type not in {"USER", "GROUP"}:
                    continue
                target = _required_string(assignee, "target")
                display_name = (
                    _string(assignee.get("displayName")) or _string(assignee.get("targetName"))
                    or _string(assignee.get("name")) or target
                )
                principals[(principal_type, target)] = display_name
        return [
            {"principal_type": principal_type, "principal_name": name, "display_name": principals[(principal_type, name)]}
            for principal_type, name in sorted(principals)
        ]

    def _list(self, resource: str, parameters: dict[str, str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page: str | None = None
        seen_pages: set[str] = set()
        for _ in range(_MAX_PAGES):
            query: dict[str, str | int] = {**parameters, "limit": 1000}
            if page:
                query["page"] = page
            response = self._request("GET", resource, params=query)
            payload = _object(response.json(), "AIDP list response")
            batch = payload.get("items")
            if not isinstance(batch, list) or not all(isinstance(item, dict) for item in batch):
                raise CatalogSyncError("AIDP returned an invalid catalog collection.")
            items.extend(batch)
            next_page = _string(response.headers.get("opc-next-page"))
            if not next_page:
                return items
            if len(next_page) > 1024 or next_page in seen_pages:
                raise CatalogSyncError("AIDP returned an invalid or repeated catalog page token.")
            seen_pages.add(next_page)
            page = next_page
        raise CatalogSyncError("AIDP catalog pagination exceeded the safety limit.")

    def _get(self, resource: str) -> dict[str, Any]:
        return _object(self._request("GET", resource).json(), "AIDP resource response")

    def _lineage(self, table_key: str) -> set[LineageEdge]:
        nodes: dict[str, str] = {}
        links: list[dict[str, Any]] = []
        page: str | None = None
        seen_pages: set[str] = set()
        max_depth = _lineage_depth(os.environ.get("GOVERNANCE_LINEAGE_MAX_DEPTH", "20"))
        body = {
            "anchorNode": table_key,
            "direction": "DOWNSTREAM",
            "level": "COLUMN",
            "maxDepth": max_depth,
            "shouldIncludeEdges": True,
        }
        retry_token = str(uuid.uuid4())
        for _ in range(_MAX_PAGES):
            params: dict[str, str | int] = {"limit": 1000}
            if page:
                params["page"] = page
            response = self._request(
                "POST", "actions/fetchLineage", params=params, json=body,
                extra_headers={"opc-retry-token": retry_token},
            )
            payload = _object(response.json(), "AIDP lineage response")
            _merge_lineage_page(payload, nodes, links)
            next_page = _string(response.headers.get("opc-next-page"))
            if not next_page:
                break
            if len(next_page) > 1024 or next_page in seen_pages:
                raise CatalogSyncError("AIDP returned an invalid or repeated lineage page token.")
            seen_pages.add(next_page)
            page = next_page
        else:
            raise CatalogSyncError("AIDP lineage pagination exceeded the safety limit.")
        return _lineage_edges(nodes, links)

    def _request(
        self, method: str, resource: str, extra_headers: dict[str, str] | None = None, **kwargs: Any
    ) -> HttpResponse:
        request = self._session.get if method == "GET" else self._session.post
        response = request(
            f"{self._base}/{resource}",
            timeout=(5, 30),
            allow_redirects=False,
            headers={"accept": "application/json", **(extra_headers or {})},
            **kwargs,
        )
        if response.status_code != 200:
            request_id = _string(response.headers.get("opc-request-id"))
            suffix = f" (opc-request-id: {request_id})" if request_id else ""
            raise CatalogSyncError(f"AIDP catalog request failed with HTTP {response.status_code}{suffix}.")
        return response


class CatalogSynchronizer:
    def __init__(self, client: AidpCatalogClient, store: Any) -> None:
        self._client = client
        self._store = store
        self._lock = Lock()

    def synchronize(self) -> dict[str, Any]:
        with self._lock:
            try:
                snapshot = self._client.snapshot()
                result = self._store.apply_catalog_snapshot(snapshot)
            except Exception as exc:
                try:
                    self._store.record_sync_failure(_SOURCE)
                except Exception:
                    pass
                raise CatalogSyncError("Master Catalog synchronization failed closed.") from exc
            return {"source": snapshot.source, "version": snapshot.version, "content_hash": snapshot.content_hash, **result}


class _OkeSession:
    """Delay Workload Identity discovery so the unauthenticated health probe always starts."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._session: Any | None = None

    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> HttpResponse:
        return self._request("POST", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        with self._lock:
            if self._session is None:
                import oci
                import requests

                session = requests.Session()
                session.auth = oci.auth.signers.get_oke_workload_identity_resource_principal_signer()
                self._session = session
            session = self._session
        return session.request(method, url, **kwargs)


def _table_columns(
    catalog_key: str,
    catalog_name: str,
    catalog_guid: str,
    schema_key: str,
    schema_name: str,
    detail: dict[str, Any],
) -> Iterable[CatalogColumn]:
    table_key = _required_string(detail, "key")
    table_name = _required_string(detail, "displayName")
    created_at = _string(detail.get("timeCreated"))
    created_by = _string(detail.get("createdBy"))
    entity_type = _required_string(detail, "entityType")
    fingerprint_source = "\0".join((catalog_guid, created_at, created_by, entity_type))
    if not created_at or not created_by:
        # ponytail: public AIDP schema/table keys are names; without creation identity, rename inference is unsafe.
        fingerprint_source = "\0".join((catalog_guid, table_key))
    table_fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
    fields = detail.get("tableFields") or []
    if not isinstance(fields, list) or not all(isinstance(field, dict) for field in fields):
        raise CatalogSyncError(f"AIDP returned invalid columns for table {table_key}.")
    for ordinal, field in enumerate(fields):
        yield CatalogColumn(
            catalog_name=catalog_name,
            schema_name=schema_name,
            table_name=table_name,
            column_name=_required_string(field, "fieldName"),
            catalog_key=catalog_key,
            catalog_guid=catalog_guid,
            schema_key=schema_key,
            table_key=table_key,
            table_fingerprint=table_fingerprint,
            column_ordinal=ordinal,
            column_type=_required_string(field, "fieldType"),
            column_description=_string(field.get("fieldDescription")),
            entity_type=entity_type,
            table_created_at=created_at,
            table_created_by=created_by,
        )


def _merge_lineage_page(
    payload: dict[str, Any], nodes: dict[str, str], links: list[dict[str, Any]]
) -> None:
    batch_nodes = payload.get("nodes")
    batch_links = payload.get("links")
    if (
        not isinstance(batch_nodes, list)
        or not all(isinstance(item, dict) for item in batch_nodes)
        or not isinstance(batch_links, list)
        or not all(isinstance(item, dict) for item in batch_links)
    ):
        raise CatalogSyncError("AIDP returned an invalid lineage graph.")
    for node in batch_nodes:
        node_id = _string(node.get("id"))
        qualified_name = _string(node.get("qualifiedName"))
        if node_id and qualified_name:
            nodes[node_id] = qualified_name
    links.extend(batch_links)


def _lineage_edges(nodes: dict[str, str], links: list[dict[str, Any]]) -> set[LineageEdge]:
    result: set[LineageEdge] = set()
    for link in links:
        source = nodes.get(_string(link.get("fromNodeId")))
        target = nodes.get(_string(link.get("toNodeId")))
        if source and target and source.casefold() != target.casefold():
            result.add(LineageEdge(source, target))
    return result


def _required_string(value: dict[str, Any], key: str) -> str:
    result = _string(value.get(key))
    if not result:
        raise CatalogSyncError(f"AIDP catalog metadata is missing {key}.")
    return result


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogSyncError(f"{label} must be a JSON object.")
    return value


def _column_sort_key(column: CatalogColumn) -> tuple[str, str, str, int, str]:
    return (
        column.catalog_key.casefold(), column.schema_key.casefold(), column.table_key.casefold(),
        column.column_ordinal, column.column_name.casefold(),
    )


def _reject_ambiguous_table_fingerprints(columns: tuple[CatalogColumn, ...]) -> None:
    tables: dict[str, str] = {}
    for column in columns:
        existing = tables.setdefault(column.table_fingerprint, column.table_key)
        if existing != column.table_key:
            raise CatalogSyncError(
                "AIDP returned two tables with indistinguishable creation identities; synchronization was blocked."
            )


def new_object_id(column: CatalogColumn) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_SOURCE}:{column.table_fingerprint}:{column.column_name.casefold()}"))


def _lineage_depth(value: str) -> int:
    try:
        depth = int(value)
    except ValueError as exc:
        raise CatalogSyncError("GOVERNANCE_LINEAGE_MAX_DEPTH must be an integer.") from exc
    if not 1 <= depth <= 100:
        raise CatalogSyncError("GOVERNANCE_LINEAGE_MAX_DEPTH must be between 1 and 100.")
    return depth
