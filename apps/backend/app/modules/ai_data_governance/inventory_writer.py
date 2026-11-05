"""Persist admitted inventory in recoverable steps under one workflow writer.

Each table commit is independent. Current pointers follow immutable snapshots;
the caller may retry after an uncertain response using the same observations.
This module neither acquires a lock nor establishes native caller permissions.
"""

from __future__ import annotations

from .inventory import InventoryRecords
from .repository import checked_rows


def persist_inventory(repository, scope_id: str, run_id: str, records: InventoryRecords, *, check_active=None) -> dict:
    groups = {"AI_DG_ASSET": records.assets, "AI_DG_ASSET_VERSION": records.versions,
              "AI_DG_SCOPE_ASSET": records.projections, "AI_DG_EVIDENCE": records.evidence,
              "AI_DG_WORK": records.work}
    for name, values in groups.items():
        checked_rows(name, values, limit=repository.max_batch_rows)
    scope = _one(repository, "AI_DG_SCOPE", {"scope_id": scope_id})
    run = _one(repository, "AI_DG_RUN", {"run_id": run_id})
    if not scope["enabled"] or run["scope_id"] != scope_id or run["status"] != "RUNNING":
        raise ValueError("Inventory requires an enabled scope and its active run")
    _validate_records(scope, run_id, records)
    new_assets, previous, desired = _asset_transitions(repository, records.assets)
    versions = _retain_original(repository, "AI_DG_ASSET_VERSION", records.versions,
                                {"observed_at", "observed_run_id"})
    evidence = _retain_original(repository, "AI_DG_EVIDENCE", records.evidence,
                                {"observed_at", "producer_run_id"})
    old_projections = _existing(repository, "AI_DG_SCOPE_ASSET", records.projections)
    old_work = _existing(repository, "AI_DG_WORK", records.work)
    def commit(method, *args, **kwargs):
        if check_active is not None:
            check_active()
        return method(*args, **kwargs)

    commit(repository.insert_immutable, "AI_DG_ASSET", new_assets)
    commit(repository.insert_immutable, "AI_DG_ASSET_VERSION", versions)
    commit(repository.insert_immutable, "AI_DG_EVIDENCE", evidence)
    commit(repository.update_current, "AI_DG_ASSET", desired, expected=previous)
    projections = _current_projections(records.projections, old_projections)
    _save_current(repository, "AI_DG_SCOPE_ASSET", projections, old_projections, commit)

    # Never reset publication/reconciliation work during a metadata scan.
    work = _work_to_reopen(records.work, old_work, old_projections)
    _save_current(repository, "AI_DG_WORK", work, old_work, commit)
    return {"assets_confirmed": len(desired), "versions_confirmed": len(versions),
            "work_saved": len(work), "work_retained": len(records.work) - len(work)}


def _asset_transitions(repository, assets: list[dict]) -> tuple[list, list, list]:
    old_assets = _existing(repository, "AI_DG_ASSET", assets)
    previous, desired, new = [], [], []
    fixed = ("platform_key", "asset_kind", "native_key", "parent_asset_id")
    for asset in assets:
        before = old_assets.get(asset["asset_id"])
        if before is not None and any(before[name] != asset[name] for name in fixed):
            raise ValueError("Inventory identity conflicts with a persisted asset")
        if before is None:
            before = {**asset, "current_version_id": None}
            new.append(before)
        previous.append(before)
        desired.append({**asset, "first_seen_at": before["first_seen_at"]})
    return new, previous, desired


def _current_projections(records: list[dict], previous: dict) -> list[dict]:
    result = []
    for projection in records:
        old = previous.get((projection["scope_id"], projection["asset_id"]))
        if old and old["consistency_state"] in {"UPDATE_PENDING", "REPLICA_PENDING"}:
            projection = {**old, "last_seen_at": projection["last_seen_at"],
                          "last_seen_run_id": projection["last_seen_run_id"]}
        result.append(projection)
    return result


def _work_to_reopen(records: list[dict], old_work: dict, old_projections: dict) -> list[dict]:
    result = []
    for item in records:
        old = old_work.get(item["work_id"])
        projection = old_projections.get((item["scope_id"], item["asset_id"]))
        pending = projection and projection["consistency_state"] in {"UPDATE_PENDING", "REPLICA_PENDING"}
        if old is None or (not pending and _reopenable_work(item, old, projection)):
            result.append(item)
    return result


def _reopenable_work(item: dict, previous: dict, projection: dict | None) -> bool:
    changed = previous["expected_version_id"] != item["expected_version_id"]
    if previous["state"] in {"DONE", "PRESERVED", "UNSUPPORTED"}:
        formerly_documented = bool(projection and projection["description"] and projection["description"].strip())
        return changed or formerly_documented or previous["reason_code"] == "DESCRIPTION_CLEARED_EXTERNALLY"
    return (changed and previous["phase"] == "DOCUMENT" and previous["state"] in {"WAIT_CONTEXT", "READY", "RETRY"}
            and previous["selected_document_id"] is None)


def _one(repository, table: str, key: dict) -> dict:
    result = repository.read_by_keys(table, [key])
    if len(result) != 1:
        raise ValueError("Inventory context is missing or ambiguous")
    return result[0]


def _existing(repository, table: str, records: list[dict]) -> dict:
    primary = repository.tables[table]["pk"]
    rows = repository.read_by_keys(table, [{key: row[key] for key in primary} for row in records])
    key = lambda row: row[primary[0]] if len(primary) == 1 else tuple(row[k] for k in primary)
    return {key(row): row for row in rows}


def _retain_original(repository, table: str, records: list[dict], provenance: set[str]) -> list[dict]:
    previous = _existing(repository, table, records)
    primary = repository.tables[table]["pk"][0]
    result = []
    for record in records:
        old = previous.get(record[primary])
        if old is not None and any(old[key] != value for key, value in record.items() if key not in provenance):
            raise ValueError("An inventory snapshot has conflicting immutable content")
        result.append(old if old is not None else record)
    return result


def _save_current(repository, table: str, records: list[dict], previous: dict, commit) -> None:
    primary = repository.tables[table]["pk"]
    key = lambda row: row[primary[0]] if len(primary) == 1 else tuple(row[k] for k in primary)
    new = [row for row in records if key(row) not in previous]
    changed = [row for row in records if key(row) in previous and row != previous[key(row)]]
    commit(repository.insert_immutable, table, new)
    commit(repository.update_current, table, changed, expected=[previous[key(row)] for row in changed])


def _validate_records(scope: dict, run_id: str, records: InventoryRecords) -> None:
    assets = {row["asset_id"]: row for row in records.assets}
    versions = {row["version_id"]: row for row in records.versions}
    projections = {(row["scope_id"], row["asset_id"]): row for row in records.projections}
    if {asset["current_version_id"] for asset in assets.values()} != set(versions) or len(versions) != len(assets):
        raise ValueError("Inventory requires one current version per observed asset")
    if {asset for _, asset in projections} != set(assets):
        raise ValueError("Inventory requires a projection for every observed asset")
    _validate_asset_versions(scope, run_id, assets, versions)
    _validate_projections(scope, run_id, records.projections, versions)
    _validate_evidence(scope, run_id, records.evidence, versions)
    _validate_work(scope, run_id, records.work, versions, projections)


def _validate_asset_versions(scope: dict, run_id: str, assets: dict, versions: dict) -> None:
    for asset in assets.values():
        version = versions.get(asset["current_version_id"])
        if asset["platform_key"] != scope["platform_key"] or version is None or version["asset_id"] != asset["asset_id"]:
            raise ValueError("Inventory asset/version is outside the admitted platform")
        if asset["parent_asset_id"] is not None and asset["parent_asset_id"] not in assets:
            raise ValueError("Inventory requires the observed parent asset")
    for version in versions.values():
        if version["observed_run_id"] != run_id or version["asset_id"] not in assets:
            raise ValueError("Inventory version belongs to another run or asset")
        if not scope["schema_key"].startswith(scope["catalog_key"] + ".") or not version["qualified_name"].startswith(scope["schema_key"] + "."):
            raise ValueError("Inventory version is outside the configured catalog/schema")


def _validate_projections(scope: dict, run_id: str, projections: list[dict], versions: dict) -> None:
    for projection in projections:
        version = versions.get(projection["verified_version_id"])
        if projection["scope_id"] != scope["scope_id"] or projection["last_seen_run_id"] != run_id or version is None or version["asset_id"] != projection["asset_id"]:
            raise ValueError("Inventory projection belongs to another scope or version")
        if any(projection[name] != version[name] for name in (
            "qualified_name", "description", "description_origin", "data_type", "owner_ref")) or projection["projection_hash"] != version["metadata_hash"]:
            raise ValueError("Inventory projection differs from its immutable version")


def _validate_evidence(scope: dict, run_id: str, evidence_rows: list[dict], versions: dict) -> None:
    for evidence in evidence_rows:
        if evidence["scope_id"] != scope["scope_id"] or evidence["producer_run_id"] != run_id or evidence["asset_version_id"] not in versions:
            raise ValueError("Inventory evidence belongs to another scope or version")
        version = versions[evidence["asset_version_id"]]
        if evidence["evidence_hash"] != version["metadata_hash"] or evidence["facts_json"] != version["metadata_json"]:
            raise ValueError("Catalog evidence differs from its immutable version")


def _validate_work(scope: dict, run_id: str, work_rows: list[dict], versions: dict, projections: dict) -> None:
    for work in work_rows:
        version = versions.get(work["expected_version_id"])
        if work["scope_id"] != scope["scope_id"] or work["last_run_id"] != run_id or version is None or version["asset_id"] != work["asset_id"]:
            raise ValueError("Inventory work belongs to another scope or version")
        if (work["scope_id"], work["asset_id"]) not in projections or (version["description"] and version["description"].strip()):
            raise ValueError("Inventory work requires a projected empty description")
