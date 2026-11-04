"""Bounded catalog discovery and immutable per-run scope reuse."""
from __future__ import annotations

from .inventory import identity
from .workflow_receipts import execution_receipt_key, load_execution_receipt


def _discovery_items(request, path, params, check_active):
    seen, token = set(), None
    for _ in range(10):
        check_active()
        query = {**params, "limit": "100"}
        if token is not None:
            query["page"] = token
        body, headers = request("GET", path, params=query)
        rows = body.get("items") if isinstance(body, dict) else body
        if not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(r, dict) for r in rows):
            raise ValueError("Invalid catalog discovery page")
        yield from rows
        token = next((v for k, v in headers.items() if k.lower() == "opc-next-page"), None)
        if token is None:
            return
        if not isinstance(token, str) or not token or len(token) > 8192 or token in seen:
            raise ValueError("Invalid or repeated catalog discovery cursor")
        seen.add(token)
    raise ValueError("Catalog discovery exceeded its page budget")


def discover_source_scopes(request, *, check_active):
    scopes, catalogs = [], set()
    for catalog in _discovery_items(request, "/catalogs", {}, check_active):
        key = catalog.get("key")
        if not isinstance(key, str) or not key or len(key) > 512 or key in catalogs:
            raise ValueError("Catalog discovery requires distinct native identities")
        catalogs.add(key)
        for schema in _discovery_items(request, "/schemas", {"catalogKey": key}, check_active):
            schema_key = schema.get("key")
            if (not isinstance(schema_key, str) or not schema_key.startswith(key + ".")
                    or len(schema_key) > 1024 or schema.get("catalogKey", key) != key):
                raise ValueError("Discovered schema belongs to another catalog")
            row = {"catalog_key": key, "schema_key": schema_key}
            if row in scopes or len(scopes) >= 32:
                raise ValueError("Discovered scopes are duplicated or exceed the run budget")
            scopes.append(row)
    if not scopes:
        raise ValueError("No complete catalog scope is available")
    return sorted(scopes, key=lambda r: (r["catalog_key"], r["schema_key"]))


def pipeline_source_scopes(repository, journal, request, binding, arguments, *, check_active):
    previous = journal.read("executions", execution_receipt_key(arguments.job_run_id))
    if arguments.task_key == "sync_inventory" and previous is None:
        return discover_source_scopes(request, check_active=check_active)
    receipt = load_execution_receipt(journal, binding, arguments.job_run_id)
    scopes = []
    for row in receipt["scopes"]:
        check_active()
        found = repository.read_by_keys("AI_DG_SCOPE", [{"scope_id": row["scope_id"]}])
        if len(found) != 1:
            raise ValueError("Frozen workflow scope is unavailable")
        scope = found[0]
        expected = identity("AI_DG_SCOPE", binding["platform_id"], binding["workspace_key"],
                            scope["catalog_key"], scope["schema_key"])
        if (scope["scope_id"] != expected or not scope["enabled"]
                or scope["platform_key"] != binding["platform_id"] or scope["workspace_key"] != binding["workspace_key"]):
            raise ValueError("Frozen workflow scope binding changed")
        scopes.append({"catalog_key": scope["catalog_key"], "schema_key": scope["schema_key"]})
    return scopes
