"""Bounded catalog reads using the established signed AIDP request adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, Mapping
from urllib.parse import quote


def is_governance_control_table(table_key):
    key = table_key.casefold() if isinstance(table_key, str) else ""
    prefix = "oci_medallion.oci_artifacts."
    return key.startswith(prefix) and key[len(prefix):].startswith(("ai_dg_", "data_governance_"))


def is_redacted_metadata(value):
    return isinstance(value, str) and value.strip() == "*********(redacted)"


@dataclass(frozen=True)
class CatalogScope:
    """Resolved by authorization/configuration; constructing this is not an ACL check."""

    catalog_key: str
    schema_key: str

    def __post_init__(self):
        if not all(isinstance(value, str) and value for value in (self.catalog_key, self.schema_key)):
            raise ValueError("An explicit catalog and schema are required")


@dataclass(frozen=True)
class CatalogPage:
    items: tuple[dict, ...]
    next_page: str | None


def _check_scope(item: dict, scope: CatalogScope) -> None:
    for name, expected in (("catalogKey", scope.catalog_key), ("schemaKey", scope.schema_key)):
        if name in item and item[name] != expected:
            raise ValueError("AIDP returned metadata outside the requested scope")


def _text(item: dict, names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in item and item[name] is not None:
            if not isinstance(item[name], str):
                raise ValueError("AIDP returned invalid metadata text")
            if is_redacted_metadata(item[name]):
                raise ValueError("AIDP metadata text is redacted")
            return item[name]
    return None


def normalize_columns(detail: dict) -> tuple[dict, ...] | None:
    """None means unavailable coverage; an explicit empty array means no fields."""
    name = next((name for name in ("tableFields", "columns", "columnDefinitions") if name in detail), None)
    if name is None or detail[name] is None:
        return None
    fields = detail[name]
    if not isinstance(fields, list) or not all(isinstance(field, dict) for field in fields):
        raise ValueError("AIDP returned invalid table fields")
    result = []
    for field in fields:
        if not any(name in field for name in ("fieldDescription", "description", "comment")):
            raise ValueError("Native column description coverage is unavailable")
        ordinal = field.get("fieldPosition", field.get("ordinalPosition"))
        if ordinal is not None and type(ordinal) is not int:
            raise ValueError("AIDP returned an invalid field position")
        result.append({
            "name": _text(field, ("fieldName", "displayName", "name")),
            "native_key": _text(field, ("key", "fieldKey")),
            "ordinal": ordinal,
            "data_type": _text(field, ("fieldType", "dataType", "type")),
            "description": _text(field, ("fieldDescription", "description", "comment")),
        })
    return tuple(result)


class CatalogReader:
    """The injected adapter owns auth, base URL, timeout and safe HTTP errors."""

    def __init__(self, request: Callable, *, page_size: int = 100, max_pages: int = 1000):
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("Catalog page size is outside the bounded contract")
        if type(max_pages) is not int or not 1 <= max_pages <= 1000:
            raise ValueError("Catalog page budget is outside the bounded contract")
        self.request = request
        self.page_size = page_size
        self.max_pages = max_pages

    def table_pages(self, scope: CatalogScope) -> Iterator[CatalogPage]:
        token = None
        seen = set()
        for _ in range(self.max_pages):
            query = {"catalogKey": scope.catalog_key, "schemaKey": scope.schema_key,
                     "limit": str(self.page_size)}
            if token:
                query["page"] = token
            body, headers = self.request("GET", "/tables", params=query)
            page = self._page(body, headers, scope)
            if page.next_page in seen:
                raise ValueError("AIDP returned a repeated pagination token")
            yield CatalogPage(tuple(item for item in page.items if not is_governance_control_table(item["key"])), page.next_page)
            if page.next_page is None:
                return
            seen.add(page.next_page)
            token = page.next_page
        raise ValueError("Catalog pagination budget exhausted; inventory remains incomplete")

    @staticmethod
    def _page(body: object, headers: Mapping, scope: CatalogScope) -> CatalogPage:
        if isinstance(body, list):
            items = body
        elif isinstance(body, dict):
            items = body.get("items", body.get("Items"))
        else:
            items = None
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError("AIDP returned an invalid catalog page")
        for item in items:
            _check_scope(item, scope)
            if not isinstance(item.get("key"), str) or not item["key"]:
                raise ValueError("Catalog entries require native table identities")
        lowered = {key.lower(): value for key, value in headers.items()}
        token = lowered.get("opc-next-page")
        if token is not None and (not isinstance(token, str) or not token or len(token) > 8192):
            raise ValueError("AIDP returned an invalid pagination token")
        return CatalogPage(tuple(dict(item) for item in items), token)

    def read_table(self, scope: CatalogScope, table_key: str) -> dict:
        if not isinstance(table_key, str) or not table_key:
            raise ValueError("A native table identity is required")
        if is_governance_control_table(table_key):
            raise ValueError("Governance control tables cannot document themselves")
        detail, headers = self.request("GET", f"/tables/{quote(table_key, safe='')}")
        if not isinstance(detail, dict) or detail.get("key") != table_key:
            raise ValueError("AIDP returned a mismatched table identity")
        _check_scope(detail, scope)
        if "description" not in detail:
            raise ValueError("Native table description coverage is unavailable")
        native_version = {key.lower(): value for key, value in headers.items()}.get("etag")
        return {"table_key": table_key, "native_version": native_version,
                "name": _text(detail, ("displayName", "name")),
                "description": _text(detail, ("description",)), "columns": normalize_columns(detail)}
