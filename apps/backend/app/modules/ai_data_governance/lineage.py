"""Bounded native lineage evidence, separate from code references and execution.

The service returns nodes/links, not a complete execution history. Pagination
completion is recorded separately from the requested traversal depth. Properties
are intentionally omitted: their generic map has no verified secret-free schema.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from .catalog import CatalogScope, is_governance_control_table
from .service_transport import MetadataTransport


def _lineage_task_locator(node, platform, workspace):
    prefix = f"aidp://workflows@{platform}/{workspace}/o/"
    name = node.get("qualifiedName", "")
    if node.get("type") != "Task" or not isinstance(name, str) or not name.startswith(prefix):
        return None
    parts = name[len(prefix):].split("/")
    if len(parts) != 2 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,127}", parts[1]):
        return None
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", parts[0]):
        return None
    return tuple(parts)


class LineageTaskReader:
    """Admit native task nodes through existing, bounded workspace job reads."""

    def __init__(self, request, platform, workspace):
        self.request, self.platform, self.workspace = request, platform, workspace
        self.jobs = {}

    def visible(self, node):
        locator = _lineage_task_locator(node, self.platform, self.workspace)
        if locator is None:
            return False
        job_key, task_key = locator
        if job_key not in self.jobs:
            if len(self.jobs) >= 16:
                return False
            self.jobs[job_key] = ()
            try:
                job, _ = self.request("GET", f"/workspaces/{self.workspace}/jobs/{job_key}")
            except (PermissionError, RuntimeError, ValueError):
                return False
            if (not isinstance(job, dict) or job.get("key") != job_key
                    or job.get("workspaceKey", self.workspace) != self.workspace):
                return False
            tasks = job.get("tasks")
            if not isinstance(tasks, list) or len(tasks) > 64 or not all(isinstance(task, dict) for task in tasks):
                return False
            self.jobs[job_key] = tuple(task.get("taskKey") for task in tasks)
        return self.jobs[job_key].count(task_key) == 1


def _lineage_request(platform_key: str, scope: CatalogScope, table_key: str,
                     column_name: str | None, depth: int) -> dict:
    if (not isinstance(table_key, str) or not table_key.startswith(scope.schema_key + ".")
            or len(table_key) > 1024 or is_governance_control_table(table_key)):
        raise ValueError("The lineage anchor is outside its admitted schema")
    if type(depth) is not int or not 1 <= depth <= 4:
        raise ValueError("Lineage traversal exceeds its depth bound")
    if column_name is not None and (not isinstance(column_name, str) or not 1 <= len(column_name) <= 256):
        raise ValueError("An admitted native column is required")
    payload = {"anchorNode": f"aidp://catalogs@{platform_key}/o/{table_key}", "direction": "BOTH",
               "maxDepth": depth, "level": "COLUMN" if column_name else "ENTITY", "shouldIncludeEdges": True}
    if column_name:
        payload["pathFilters"] = {"predicates": {"anchorNodeColumnsIn": [column_name]}}
    return payload


class LineageTransport(MetadataTransport):
    """The additional POST is a bounded read action, never a resource mutation."""

    def __init__(self, config: dict, signer, session):
        super().__init__(config, signer, session)
        self.platform = config["platform_id"]
        self.scopes = tuple(CatalogScope(**scope) for scope in config["source_scopes"])
        if not 1 <= len(self.scopes) <= 32:
            raise ValueError("A bounded business metadata scope set is required")

    def __call__(self, method: str, path: str, *, params=None, payload=None):
        if method == "GET" and payload is None:
            return super().__call__(method, path, params=params)
        if method != "POST" or path != "/actions/fetchLineage":
            raise ValueError("Only the native lineage read action is admitted")
        _validate_lineage_call(payload, params, self.platform, self.scopes)
        return self._exchange("POST", path, params=params, payload=payload)


def _validate_lineage_call(payload: dict, params: dict, platform: str, scopes: tuple) -> None:
    if not isinstance(payload, dict) or not isinstance(params, dict) or set(params) - {"limit", "page"}:
        raise ValueError("Invalid lineage query")
    if params.get("limit") != "100" or ("page" in params and (not isinstance(params["page"], str) or not 1 <= len(params["page"]) <= 1024)):
        raise ValueError("Invalid lineage pagination")
    anchor = payload.get("anchorNode")
    prefix = f"aidp://catalogs@{platform}/o/"
    if not isinstance(anchor, str) or not anchor.startswith(prefix):
        raise ValueError("Lineage anchor belongs to a different platform")
    key = anchor[len(prefix):]
    scope = next((scope for scope in scopes if key.startswith(scope.schema_key + ".")), None)
    if scope is None:
        raise ValueError("Lineage anchor is outside configured source schemas")
    column = _lineage_column(payload)
    if payload != _lineage_request(platform, scope, key, column, payload.get("maxDepth")):
        raise ValueError("Lineage options exceed the admitted read contract")


def _lineage_column(payload: dict) -> str | None:
    filters = payload.get("pathFilters")
    if filters is None:
        return None
    if not isinstance(filters, dict) or set(filters) != {"predicates"}:
        raise ValueError("Invalid column lineage filter")
    predicates = filters["predicates"]
    if not isinstance(predicates, dict) or set(predicates) != {"anchorNodeColumnsIn"}:
        raise ValueError("Invalid column lineage predicate")
    values = predicates["anchorNodeColumnsIn"]
    if not isinstance(values, list) or len(values) != 1:
        raise ValueError("Column lineage requires exactly one admitted field")
    return values[0]


@dataclass(frozen=True)
class LineageSnapshot:
    query: dict
    nodes: tuple[dict, ...]
    links: tuple[dict, ...]
    pagination_complete: bool
    omitted_nodes: int
    unresolved_links: int


class LineageReader:
    def __init__(self, request, platform_key: str, *, admit_node, max_pages: int = 5):
        if not callable(admit_node) or type(max_pages) is not int or not 1 <= max_pages <= 10:
            raise ValueError("Lineage requires a trusted visibility gate and page budget")
        self.request, self.platform, self.admit_node = request, platform_key, admit_node
        self.max_pages = max_pages

    def read(self, scope: CatalogScope, table_key: str, *, column_name=None, depth=2) -> LineageSnapshot:
        query = _lineage_request(self.platform, scope, table_key, column_name, depth)
        nodes, links, tokens = {}, {}, set()
        page, complete = None, False
        for _ in range(self.max_pages):
            params = {"limit": "100", **({"page": page} if page else {})}
            body, headers = self.request("POST", "/actions/fetchLineage", payload=query, params=params)
            _merge_lineage_page(body, nodes, links)
            page = {str(key).lower(): value for key, value in headers.items()}.get("opc-next-page")
            if page is None:
                complete = True
                break
            if not isinstance(page, str) or not 1 <= len(page) <= 1024 or page in tokens:
                raise ValueError("Invalid or repeated lineage page token")
            tokens.add(page)
        allowed = {key: node for key, node in nodes.items() if self.admit_node(dict(node)) is True}
        visible = [link for link in links.values() if link["fromNodeId"] in allowed and link["toNodeId"] in allowed]
        return LineageSnapshot(query, tuple(allowed.values()), tuple(visible), complete,
                               len(nodes) - len(allowed), len(links) - len(visible))


def _merge_lineage_page(body: dict, nodes: dict, links: dict) -> None:
    if not isinstance(body, dict) or any(not isinstance(body.get(name), list) or len(body[name]) > 1000 for name in ("nodes", "links")):
        raise ValueError("Invalid native lineage page")
    for raw in body["nodes"]:
        node = _lineage_fields(raw, ("id", "qualifiedName", "displayName", "parentId", "type"), ("id",))
        key = node["id"]
        if key in nodes and nodes[key] != node:
            raise ValueError("Conflicting lineage node revisions")
        nodes[key] = node
    for raw in body["links"]:
        link = _lineage_fields(raw, ("fromNodeId", "toNodeId", "type", "providerType"), ("fromNodeId", "toNodeId"))
        key = tuple(link.get(name) for name in ("fromNodeId", "toNodeId", "type", "providerType"))
        links[key] = link
    if len(nodes) > 1000 or len(links) > 2000 or len(json.dumps([nodes, list(links.values())]).encode("utf-8")) > 262144:
        raise ValueError("Lineage evidence exceeds its total budget")


def _lineage_fields(raw: dict, fields: tuple, required: tuple) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Invalid lineage object")
    result = {}
    for name in fields:
        value = raw.get(name)
        if value is not None:
            if not isinstance(value, str) or not 1 <= len(value) <= 2048:
                raise ValueError("Invalid lineage text")
            result[name] = value
    if not set(required).issubset(result):
        raise ValueError("Lineage object has no native identity")
    return result
