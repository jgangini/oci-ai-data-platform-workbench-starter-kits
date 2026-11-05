"""Bounded catalog synchronization; no model calls or catalog modifications."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .catalog import CatalogScope
from .inventory import InventoryRecords, canonical, table_records
from .inventory_writer import persist_inventory


def synchronize_scope(repository, reader, scope_id: str, run_id: str, *, check_active) -> dict:
    """Called only after native task admission and exclusive writer admission.

    Partial enumeration never marks unseen assets as deleted. A failed Delta
    commit propagates so the task can reconcile/retry the same phase.
    """
    check_active()
    scope, run, manifest = _sync_context(repository, scope_id, run_id)
    if run["status"] in {"SUCCEEDED", "PARTIAL"} and isinstance(manifest.get("inventory"), dict):
        return manifest["inventory"]
    if run["status"] != "RUNNING":
        raise ValueError("The synchronization run is no longer active")
    limits = json.loads(scope["limits_json"])
    max_tables = limits.get("max_tables", 1000) if isinstance(limits, dict) else None
    if type(max_tables) is not int or not 1 <= max_tables <= 10000:
        raise ValueError("Invalid inventory table budget")
    stats = _collect_inventory(repository, reader, scope, run_id, check_active, max_tables)
    return _close_inventory_run(repository, run_id, stats, check_active)


def _sync_context(repository, scope_id: str, run_id: str) -> tuple[dict, dict, dict]:
    scopes = repository.read_by_keys("AI_DG_SCOPE", [{"scope_id": scope_id}])
    runs = repository.read_by_keys("AI_DG_RUN", [{"run_id": run_id}])
    if len(scopes) != 1 or len(runs) != 1:
        raise ValueError("A persisted synchronization scope/run is required")
    scope, run = scopes[0], runs[0]
    if run["scope_id"] != scope_id or not scope["enabled"]:
        raise ValueError("The synchronization scope is not admitted")
    manifest = json.loads(run["manifest_json"])
    if not isinstance(manifest, dict):
        raise ValueError("Invalid synchronization manifest")
    return scope, run, manifest


def _collect_inventory(repository, reader, scope: dict, run_id: str,
                       check_active, max_tables: int) -> dict:
    scope_id = scope["scope_id"]
    selected = CatalogScope(scope["catalog_key"], scope["schema_key"])
    stats = {"tables_observed": 0, "tables_failed": 0, "tables_partial": 0,
             "assets_confirmed": 0, "enumeration_complete": False,
             "catalog_writes": 0, "model_calls": 0}
    batch = InventoryRecords()
    seen = set()
    for page in _catalog_pages(reader, selected, stats, check_active):
        for item in page.items:
            key = item["key"]
            if key in seen or len(seen) >= max_tables:
                stats["enumeration_error"] = "CATALOG_ENUMERATION_INVALID_OR_BOUNDED"
                break
            seen.add(key)
            observed = _table_observation(reader, selected, key, scope, run_id, stats)
            if observed is None:
                continue
            detail, records = observed
            batch = _enqueue_inventory(repository, scope_id, run_id, batch, records, check_active)
            stats["assets_confirmed"] += len(records.assets)
            stats["tables_observed"] += 1
            if detail["columns"] is None:
                stats["tables_partial"] += 1
        if "enumeration_error" in stats:
            break
    _flush_inventory(repository, scope_id, run_id, batch, check_active)
    return stats


def _catalog_pages(reader, selected, stats: dict, check_active):
    pages = iter(reader.table_pages(selected))
    while True:
        check_active()
        try:
            yield next(pages)
        except StopIteration:
            stats["enumeration_complete"] = True
            return
        except Exception:
            stats["enumeration_error"] = "CATALOG_ENUMERATION_FAILED"
            return


def _table_observation(reader, selected, key: str, scope: dict, run_id: str, stats: dict):
    try:
        detail = reader.read_table(selected, key)
        records = table_records(scope["platform_key"], scope["scope_id"], run_id, detail,
            datetime.now(timezone.utc))
        return detail, records
    except Exception:
        stats["tables_failed"] += 1
        return None


def _enqueue_inventory(repository, scope_id, run_id, batch, records, check_active):
    for chunk in _inventory_chunks(records, repository.max_batch_rows):
        if len(batch.assets) + len(chunk.assets) > repository.max_batch_rows:
            _flush_inventory(repository, scope_id, run_id, batch, check_active)
            batch = InventoryRecords()
        for name in ("assets", "versions", "projections", "evidence", "work"):
            getattr(batch, name).extend(getattr(chunk, name))
    return batch


def _close_inventory_run(repository, run_id: str, stats: dict, check_active) -> dict:
    complete = stats["enumeration_complete"] and not stats["tables_failed"] and not stats["tables_partial"]
    stats["status"] = "SUCCEEDED" if complete else "PARTIAL"
    current = repository.read_by_keys("AI_DG_RUN", [{"run_id": run_id}])
    if len(current) != 1 or current[0]["status"] != "RUNNING":
        raise ValueError("Synchronization completion requires the same active run")
    before = current[0]
    # Preserve other producer checkpoints; this task owns only the inventory key.
    latest = json.loads(before["manifest_json"])
    if not isinstance(latest, dict):
        raise ValueError("Invalid synchronization completion manifest")
    after = {**before, "status": stats["status"], "manifest_json": canonical({**latest, "inventory": stats}),
             "finished_at": datetime.now(timezone.utc)}
    check_active()
    repository.update_current("AI_DG_RUN", [after], expected=[before])
    return stats


def _flush_inventory(repository, scope_id: str, run_id: str, batch: InventoryRecords, check_active) -> None:
    if batch.assets:
        check_active()
        persist_inventory(repository, scope_id, run_id, batch, check_active=check_active)


def _inventory_chunks(records: InventoryRecords, limit: int):
    if len(records.assets) <= limit:
        yield records
        return
    if limit < 2:
        raise ValueError("A wide table requires room for its parent and one column")
    parent = records.assets[0]
    if parent["asset_kind"] != "TABLE" or parent["parent_asset_id"] is not None:
        raise ValueError("Wide-table chunks require the observed table parent")
    for start in range(1, len(records.assets), limit - 1):
        assets = [parent, *records.assets[start:start + limit - 1]]
        identifiers = {row["asset_id"] for row in assets}
        versions = [row for row in records.versions if row["asset_id"] in identifiers]
        version_ids = {row["version_id"] for row in versions}
        yield InventoryRecords(assets=assets, versions=versions,
            projections=[row for row in records.projections if row["asset_id"] in identifiers],
            evidence=[row for row in records.evidence if row["asset_version_id"] in version_ids],
            work=[row for row in records.work if row["asset_id"] in identifiers])
