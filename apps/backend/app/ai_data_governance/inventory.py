"""Turn admitted catalog observations into versioned metadata, never business rows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identity(*parts: object) -> str:
    return hashlib.sha256(canonical(parts).encode("utf-8")).hexdigest()


def description_origin(text: str | None) -> str:
    if text is None or not text.strip():
        return "UNKNOWN"
    # A marker alone cannot verify authorship; only a verified publication can.
    return "EXTERNAL_UNVERIFIED"


@dataclass
class InventoryRecords:
    assets: list[dict] = field(default_factory=list)
    versions: list[dict] = field(default_factory=list)
    projections: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    work: list[dict] = field(default_factory=list)


def table_records(platform_key: str, scope_id: str, run_id: str, table: dict,
                  observed_at: datetime, *, legacy_ids: dict[str, str] | None = None) -> InventoryRecords:
    """The caller admits scope/table identity before this pure transformation.

    Qualified-path keys are not proof of physical identity continuity after a
    rename/recreation. No similarity matching, tombstones or quality claims.
    """
    if not all(isinstance(value, str) and value for value in (platform_key, scope_id, run_id, table.get("table_key"))):
        raise ValueError("Admitted inventory identities are required")
    if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
        raise ValueError("An aware observation timestamp is required")
    columns = table.get("columns")
    if columns is not None and (not isinstance(columns, (tuple, list)) or len(columns) > 10000):
        raise ValueError("Invalid or unbounded column observation")
    _check_columns(columns)
    result = InventoryRecords()
    table_key = table["table_key"]
    table_id = _asset_id(platform_key, "TABLE", table_key, legacy_ids)
    table_facts = {name: table.get(name) for name in ("table_key", "name", "description", "native_version")}
    table_facts["columns"] = columns
    _add_observation(result, platform_key, scope_id, run_id, observed_at, table_id,
                     "TABLE", table_key, None, table_facts, columns is not None)
    for column in columns or ():
        native_key = column.get("native_key") or canonical([table_key, column["name"]])
        asset_id = _asset_id(platform_key, "COLUMN", native_key, legacy_ids)
        facts = {**column, "table_key": table_key, "column_name": column["name"],
                 "native_version": table.get("native_version")}
        _add_observation(result, platform_key, scope_id, run_id, observed_at, asset_id,
                         "COLUMN", native_key, table_id, facts, True)
    return result


def _check_columns(columns: tuple | list | None) -> None:
    names = set()
    native_keys = set()
    for column in columns or ():
        if not isinstance(column, dict) or not isinstance(column.get("name"), str) or not column["name"]:
            raise ValueError("A native column name is required")
        if set(column) - {"name", "native_key", "ordinal", "data_type", "description"}:
            raise ValueError("Inventory accepts metadata fields only")
        name = column["name"]
        if name in names:
            raise ValueError("Duplicate column identity in inventory observation")
        names.add(name)
        key = column.get("native_key")
        if key is not None:
            if not isinstance(key, str) or not key or key in native_keys:
                raise ValueError("Invalid or duplicate native column key")
            native_keys.add(key)


def _asset_id(platform_key: str, kind: str, key: str, legacy_ids: dict | None) -> str:
    lookup = canonical([kind, key])
    previous = (legacy_ids or {}).get(lookup)
    if previous is not None and (not isinstance(previous, str) or not previous):
        raise ValueError("Invalid migrated inventory identity")
    return previous or identity("AI_DG_ASSET", platform_key, kind, key)


def _add_observation(result: InventoryRecords, platform: str, scope: str, run: str,
                     at: datetime, asset: str, kind: str, native_key: str,
                     parent: str | None, facts: dict, complete: bool) -> None:
    description = facts.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("Invalid observed description")
    data_type = facts.get("data_type")
    metadata = canonical(facts)
    metadata_hash = identity("metadata", facts)
    version = identity("AI_DG_ASSET_VERSION", asset, metadata_hash)
    qualified_name = facts["table_key"] + ("." + facts["column_name"] if kind == "COLUMN" else "")
    coverage = "COMPLETE" if complete else "PARTIAL"
    origin = description_origin(description)
    result.assets.append({"asset_id": asset, "platform_key": platform, "asset_kind": kind,
        "native_key": native_key, "parent_asset_id": parent, "current_version_id": version,
        "identity_status": "RESOLVED" if kind == "TABLE" or facts.get("native_key") else "FALLBACK",
        "lifecycle_state": "ACTIVE", "first_seen_at": at, "updated_at": at})
    result.versions.append({"version_id": version, "asset_id": asset,
        "native_version": facts.get("native_version"), "qualified_name": qualified_name,
        "description": description, "description_origin": origin, "data_type": data_type,
        "ordinal_position": facts.get("ordinal"), "owner_ref": None, "metadata_json": metadata,
        "metadata_hash": metadata_hash, "observed_at": at, "observed_run_id": run})
    result.projections.append({"scope_id": scope, "asset_id": asset, "last_seen_run_id": run,
        "last_seen_at": at, "coverage_state": coverage, "consistency_state": "VERIFIED",
        "verified_version_id": version, "asset_kind": kind, "qualified_name": qualified_name,
        "description": description, "description_origin": origin, "data_type": data_type,
        "owner_ref": None, "projection_hash": metadata_hash, "projected_at": at})
    result.evidence.append({"evidence_id": identity("AI_DG_EVIDENCE", scope, version, metadata_hash),
        "scope_id": scope, "asset_version_id": version, "producer_run_id": run,
        "evidence_kind": "CATALOG", "source_ref": qualified_name,
        "evidence_hash": metadata_hash, "facts_json": metadata, "coverage_state": coverage,
        "observed_at": at, "expires_at": None})
    if description is None or not description.strip():
        result.work.append({"work_id": identity("AI_DG_WORK", scope, asset, "description"),
            "scope_id": scope, "asset_id": asset, "field_path": "description",
            "expected_version_id": version, "context_hash": None, "phase": "DOCUMENT",
            "state": "WAIT_CONTEXT", "selected_document_id": None,
            "reason_code": "USAGE_CONTEXT_NOT_COLLECTED", "next_attempt_at": None,
            "last_run_id": run, "updated_at": at})
