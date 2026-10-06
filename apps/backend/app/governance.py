"""Configure the checked-in global AIDP Governance runtime files."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .config import _artifacts_bucket_name
from .gods_eye_view.runtime_secrets import SHARED_OCI_CREDENTIAL_NAME
from .lab_packs import module_runtime_source


GOVERNANCE_MODULE_ID = "ai_data_governance"
# Adopt the exact previous module/agent identity without creating a second installation.
LEGACY_GOVERNANCE_MODULE_ID = "ai_data_governance_vsc_extension"
GOVERNANCE_DISPLAY_NAME = "AI Data Governance"
GOVERNANCE_CREDENTIAL_NAME = SHARED_OCI_CREDENTIAL_NAME
GOVERNANCE_AGENT_NAME = "ai_data_governance"
GOVERNANCE_AGENT_COMPUTE_NAME = "aidp_data_governance_agent_compute"
GOVERNANCE_JOB_NAME = "wf_ai_data_governance_metadata_sync"
GOVERNANCE_BUCKET_NAME = "oci_artifacts"
GOVERNANCE_SCHEMA = "oci_artifacts"
GOVERNANCE_TABLES = (
    "data_governance_config",
    "data_governance_metadata",
    "data_governance_access_policy",
    "data_governance_sync_state",
)

PARTICIPANT_KEY = re.compile(r"u[1-9][0-9]*")


def database_names(participant_key: str) -> tuple[str, str]:
    """Return the existing Autonomous DB schemas used by Agent checkpointers."""
    if PARTICIPANT_KEY.fullmatch(participant_key) is None or int(participant_key[1:]) < 101:
        raise ValueError("A participant key starting at u101 is required")
    stem = participant_key.upper()
    return f"{stem}_AGENT", f"{stem}_AGENT_RO"


def _identity_indexes(existing: list[dict[str, Any]]) -> tuple[dict, dict, dict]:
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_exact: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_table: dict[str, list[dict[str, Any]]] = {}
    for item in existing:
        fingerprint = str(item.get("table_fingerprint") or "")
        column_key = str(item.get("column_key") or "")
        if column_key:
            by_key.setdefault((fingerprint, column_key), []).append(item)
        if int(item.get("is_deleted") or 0) == 0:
            by_exact.setdefault((fingerprint, str(item.get("column_name") or "").casefold()), []).append(item)
        by_table.setdefault(fingerprint, []).append(item)
    return by_key, by_exact, by_table


def _unique_existing_id(candidates: list[dict[str, Any]], used: set[str]) -> str | None:
    if len(candidates) > 1:
        raise ValueError("The control metadata contains ambiguous column identities")
    if not candidates:
        return None
    object_id = str(candidates[0]["object_id"])
    if object_id in used:
        raise ValueError("The source snapshot contains duplicate column identities")
    return object_id


def _existing_column_id(column: dict[str, Any], by_key: dict, by_exact: dict, used: set[str]) -> str | None:
    fingerprint = str(column["table_fingerprint"])
    column_key = str(column.get("column_key") or "")
    object_id = _unique_existing_id(
        by_key.get((fingerprint, column_key), []) if column_key else [], used
    )
    if object_id is not None:
        return object_id
    return _unique_existing_id(
        by_exact.get((fingerprint, str(column["column_name"]).casefold()), []), used
    )


def _has_retired_name(column: dict[str, Any], table_history: list[dict[str, Any]]) -> bool:
    if column.get("column_key"):
        return False
    column_name = str(column["column_name"]).casefold()
    return any(
        int(item.get("is_deleted") or 0) == 1
        and str(item.get("column_name") or "").casefold() == column_name
        for item in table_history
    )


def _rename_column_id(
    columns: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    table_history: list[dict[str, Any]],
) -> str | None:
    if len(columns) != 1:
        return None
    if len(candidates) != 1:
        return None
    column, candidate = columns[0], candidates[0]
    if int(column["column_ordinal"]) != int(candidate["column_ordinal"]):
        return None
    if str(column["data_type"]).casefold() != str(candidate["data_type"]).casefold():
        return None
    if _has_retired_name(column, table_history):
        return None
    return str(candidate["object_id"])


def _new_column_id(
    column: dict[str, Any], fingerprint: str, table_history: list[dict[str, Any]]
) -> str:
    stable_identity = str(column.get("column_key") or "")
    if not stable_identity:
        column_name = str(column["column_name"]).casefold()
        generation = 1 + sum(
            str(item.get("column_name") or "").casefold() == column_name
            and int(item.get("is_deleted") or 0) == 1
            for item in table_history
        )
        stable_identity = (
            f"{column_name}:source_version={column.get('source_version') or ''}:"
            f"fingerprint={column.get('fingerprint') or ''}:generation={generation}"
        )
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"AIDP_MASTER_CATALOG:{fingerprint}:{stable_identity}",
    ))


def resolve_column_identities(
    incoming: list[dict[str, Any]], existing: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], str, str]]:
    """Preserve IDs only for exact identity or an unambiguous one-for-one rename."""
    by_key, by_exact, by_table = _identity_indexes(existing)
    resolved: list[tuple[dict[str, Any], str, str]] = []
    unmatched: dict[str, list[dict[str, Any]]] = {}
    used: set[str] = set()
    for column in incoming:
        fingerprint = str(column["table_fingerprint"])
        object_id = _existing_column_id(column, by_key, by_exact, used)
        if object_id is None:
            unmatched.setdefault(fingerprint, []).append(column)
            continue
        resolved.append((column, object_id, "EXACT"))
        used.add(object_id)
    for fingerprint, columns in unmatched.items():
        table_history = by_table.get(fingerprint, [])
        candidates = [
            item
            for item in table_history
            if str(item.get("object_id") or "") not in used and int(item.get("is_deleted") or 0) == 0
        ]
        object_id = _rename_column_id(columns, candidates, table_history)
        if object_id is not None:
            resolved.append((columns[0], object_id, "INFERRED_RENAME"))
            used.add(object_id)
            continue
        for column in columns:
            resolved.append((column, _new_column_id(column, fingerprint, table_history), "NEW"))
    return resolved


def _configured_source(source: str, config: dict[str, Any]) -> str:
    if source.count("CONFIG = {}\n") != 1:
        raise ValueError("The Governance source must declare exactly one configuration block")
    # Python literals preserve None and booleans; bare JSON would emit invalid null/true names.
    return source.replace("CONFIG = {}\n", "CONFIG = " + repr(config) + "\n", 1)


def agent_source(*, model_id: str, region: str, compartment_id: str, platform_id: str,
                 credential_name: str = GOVERNANCE_CREDENTIAL_NAME, identity_sha256: str = "") -> bytes:
    """Render the global two-tool Agent without user tokens or gateway dependencies."""
    if not all((model_id, region, compartment_id, platform_id)):
        raise ValueError("The Agent runtime contract is incomplete")
    config = {
        "model_id": model_id,
        "region": region,
        "compartment_id": compartment_id,
        "platform_id": platform_id,
        "credential_name": credential_name,
        "identity_sha256": identity_sha256,
    }
    source = module_runtime_source(GOVERNANCE_MODULE_ID, "notebooks/agent/governance_agent.py").decode("utf-8")
    return _configured_source(source, config).encode("utf-8")


def governance_sync_notebook(
    *,
    namespace: str,
    platform_id: str,
    region: str,
    desired_enabled: bool | None,
    bootstrap_snapshot: bool = False,
    workspace_key: str = "",
    job_key: str = "",
    credential_name: str = GOVERNANCE_CREDENTIAL_NAME,
    identity_sha256: str = "",
    control_module_id: str = GOVERNANCE_MODULE_ID,
    artifacts_bucket_name: str = GOVERNANCE_BUCKET_NAME,
) -> dict[str, Any]:
    """Return the protected Spark notebook used by the single continuous workflow."""
    if not all((namespace, platform_id, region)):
        raise ValueError("The governance synchronization runtime contract is incomplete")
    if control_module_id not in {GOVERNANCE_MODULE_ID, LEGACY_GOVERNANCE_MODULE_ID}:
        raise ValueError("Unknown governance control identity")
    config = {
        "module_id": control_module_id,
        "credential_name": credential_name,
        "identity_sha256": identity_sha256,
        "namespace": namespace,
        "artifacts_bucket_name": _artifacts_bucket_name(artifacts_bucket_name),
        "platform_id": platform_id,
        "region": region,
        "desired_enabled": desired_enabled,
        "bootstrap_snapshot": bootstrap_snapshot,
        "workspace_key": workspace_key,
        "job_key": job_key,
    }
    notebook = json.loads(module_runtime_source(GOVERNANCE_MODULE_ID, "notebooks/data_governance_sync.ipynb"))
    cell = notebook["cells"][0]
    cell["source"] = _configured_source("".join(cell["source"]), config).splitlines(keepends=True)
    return notebook
