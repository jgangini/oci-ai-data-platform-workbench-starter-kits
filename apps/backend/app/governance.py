"""Generated sources for the global, native AIDP governance extension."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any


GOVERNANCE_MODULE_ID = "ai_data_governance_vsc_extension"
GOVERNANCE_DISPLAY_NAME = "AI Data Governance for VSC Extension"
GOVERNANCE_CREDENTIAL_NAME = "AidpDataGovernanceExtension"
GOVERNANCE_AGENT_NAME = "ai_data_governance_vsc_extension"
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


DAMA_SYSTEM_PROMPT = """You are a senior data governance specialist grounded in DAMA-DMBOK.
Use only catalog_inventory and catalog_lineage. They read every ACTIVE Master Catalog catalog,
except the oci_medallion.oci_artifacts control schema. Never invent metadata, lineage, owners,
freshness, policies, SQL, identifiers, or access decisions. Treat timeUpdated as a catalog
metadata timestamp, not proof of data freshness. Clearly distinguish observed evidence from a
DAMA-based recommendation. Refuse mutations, arbitrary SQL, requests for the control schema,
and claims that cannot be certified from the returned evidence. Match the user's language and
answer as Evidence, Explanation, Governance implication, and Recommendation or limitation."""


_AGENT_TEMPLATE = r'''\
"""Read-only global AIDP Master Catalog governance Agent."""

import json
import logging
import re
from urllib.parse import quote

import aidputils
import oci
import requests
from aidputils.agents.toolkit.agent_helper import init_oci_llm, pre_invoke_setup
from aidputils.agents.toolkit.configs import OCIAIConf
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

CONFIG = __CONFIG_JSON__
SYSTEM_PROMPT = __SYSTEM_PROMPT_JSON__
API_BASE = (
    f"https://datalake.{CONFIG['region']}.oci.oraclecloud.com/20260430/"
    f"aiDataPlatforms/{CONFIG['platform_id']}"
)
IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
logger = logging.getLogger("ai_data_governance_vsc_extension")
checkpointer = globals().get("checkpointer")


def _request(session, signer, method, path, *, params=None, payload=None):
    response = session.request(
        method,
        API_BASE + path,
        auth=signer,
        params=params,
        json=payload,
        headers={"Accept": "application/json"},
        timeout=(10, 60),
    )
    response.raise_for_status()
    return (response.json() if response.content else {}), response.headers


def _items(body):
    if isinstance(body, list):
        if not all(isinstance(item, dict) for item in body):
            raise RuntimeError("AIDP returned invalid list items")
        return body
    if isinstance(body, dict):
        if "items" not in body and "Items" not in body:
            raise RuntimeError("AIDP returned an invalid paginated response")
        values = body.get("items") if "items" in body else body.get("Items")
        if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
            raise RuntimeError("AIDP returned invalid list items")
        return values
    raise RuntimeError("AIDP returned an invalid paginated response")


def _list(session, signer, path, params=None):
    result = []
    page = None
    seen_pages = set()
    for _ in range(1000):
        query = {"limit": "100", **(params or {})}
        if page:
            query["page"] = page
        body, headers = _request(session, signer, "GET", path, params=query)
        result.extend(_items(body))
        page = headers.get("opc-next-page") or headers.get("Opc-Next-Page")
        if not page:
            return result
        if page in seen_pages:
            raise RuntimeError("AIDP returned a repeated pagination token")
        seen_pages.add(page)
    raise RuntimeError("AIDP pagination exceeded the safety limit")


def _name(item):
    return str(item.get("displayName") or item.get("name") or "")


def _active_catalogs(session, signer):
    return [
        item for item in _list(session, signer, "/catalogs")
        if str(item.get("lifecycleState") or item.get("state") or "").upper() == "ACTIVE"
        and item.get("key")
    ]


def _schemas(session, signer, catalog):
    catalog_name = _name(catalog)
    return [
        schema
        for schema in _list(session, signer, "/schemas", {"catalogKey": str(catalog["key"])})
        if schema.get("key")
        and not (catalog_name == "oci_medallion" and _name(schema) == "oci_artifacts")
    ]


def _columns(detail):
    values = detail.get("tableFields") or detail.get("columns") or detail.get("columnDefinitions") or []
    return [
        {
            "name": str(value.get("fieldName") or value.get("displayName") or value.get("name") or ""),
            "ordinal": value.get("fieldPosition") or value.get("ordinalPosition"),
            "data_type": str(value.get("fieldType") or value.get("dataType") or value.get("type") or "unknown"),
            "description": str(value.get("fieldDescription") or value.get("description") or value.get("comment") or ""),
        }
        for value in values
        if isinstance(value, dict)
    ]


def _table_records(session, signer, include_columns, search_term="", catalog_filter="", schema_filter=""):
    records = []
    needle = search_term.casefold()
    for catalog in _active_catalogs(session, signer):
        catalog_name = _name(catalog)
        if catalog_filter and catalog_name.casefold() != catalog_filter.casefold():
            continue
        for schema in _schemas(session, signer, catalog):
            schema_name = _name(schema)
            if schema_filter and schema_name.casefold() != schema_filter.casefold():
                continue
            for table in _list(session, signer, "/tables", {"catalogKey": str(catalog["key"]), "schemaKey": str(schema["key"])}):
                table_key = str(table.get("key") or "")
                detail = table
                if table_key and (include_columns or needle):
                    value, _ = _request(session, signer, "GET", f"/tables/{quote(table_key, safe='')}")
                    if isinstance(value, dict):
                        detail = value
                columns = _columns(detail) if include_columns or needle else []
                table_name = _name(table) or _name(detail)
                record = {
                    "catalog": catalog_name,
                    "schema": schema_name,
                    "table": table_name,
                    "table_key": table_key,
                    "description": str(detail.get("description") or ""),
                    "table_type": str(detail.get("tableType") or table.get("tableType") or "unknown"),
                    "lifecycle_state": str(detail.get("lifecycleState") or table.get("lifecycleState") or "unknown"),
                    "time_created": detail.get("timeCreated") or table.get("timeCreated"),
                    "time_updated": detail.get("timeUpdated") or table.get("timeUpdated"),
                    "qualified_name": f"aidp://catalogs@{CONFIG['platform_id']}/o/{catalog_name}.{schema_name}.{table_name}",
                    "columns": columns if include_columns else [],
                }
                searchable = [record["catalog"], record["schema"], record["table"], record["description"]]
                searchable.extend(f"{column['name']} {column['description']}" for column in columns)
                if not needle or any(needle in value.casefold() for value in searchable):
                    records.append(record)
    return records


def _is_control_qualified_name(value):
    normalized = str(value or "").casefold().replace("/", ".")
    token = "oci_medallion.oci_artifacts"
    offset = normalized.find(token)
    while offset >= 0:
        end = offset + len(token)
        before = normalized[offset - 1] if offset else ""
        after = normalized[end] if end < len(normalized) else ""
        if (not before or not (before.isalnum() or before == "_")) and (
            not after or not (after.isalnum() or after == "_")
        ):
            return True
        offset = normalized.find(token, offset + 1)
    return False


def _is_control_lineage_node(node):
    if not isinstance(node, dict):
        raise RuntimeError("AIDP returned an invalid lineage node")
    if any(_is_control_qualified_name(node.get(name)) for name in ("qualifiedName", "id")):
        return True
    pending = [node]
    while pending:
        value = pending.pop()
        if not isinstance(value, dict):
            continue
        normalized = {str(key).replace("_", "").casefold(): item for key, item in value.items()}
        if (
            str(normalized.get("catalogname") or normalized.get("catalog") or "").casefold()
            == "oci_medallion"
            and str(normalized.get("schemaname") or normalized.get("schema") or "").casefold()
            == "oci_artifacts"
        ):
            return True
        if any(
            _is_control_qualified_name(item)
            for key, item in normalized.items()
            if key in {"qualifiedname", "fullyqualifiedname"}
        ):
            return True
        pending.extend(item for item in value.values() if isinstance(item, dict))
    return False


def _filter_control_lineage(graph):
    if (
        not isinstance(graph, dict)
        or not isinstance(graph.get("nodes"), list)
        or not isinstance(graph.get("links"), list)
        or not all(isinstance(node, dict) for node in graph["nodes"])
        or not all(isinstance(link, dict) for link in graph["links"])
    ):
        raise RuntimeError("AIDP returned an invalid lineage response")
    excluded = {
        str(node["id"])
        for node in graph["nodes"]
        if _is_control_lineage_node(node) and node.get("id")
    }
    while True:
        descendants = {
            str(node["id"])
            for node in graph["nodes"]
            if node.get("id") and str(node.get("parentId") or "") in excluded
        }
        expanded = excluded | descendants
        if expanded == excluded:
            break
        excluded = expanded
    filtered = dict(graph)
    filtered["nodes"] = [
        node for node in graph["nodes"]
        if not _is_control_lineage_node(node)
        and str(node.get("id") or "") not in excluded
    ]
    filtered["links"] = [
        link for link in graph["links"]
        if str(link.get("fromNodeId") or "") not in excluded
        and str(link.get("toNodeId") or "") not in excluded
    ]
    return filtered


def _safe_error(stage, exc):
    response = getattr(exc, "response", None)
    return {
        "stage": stage,
        "type": type(exc).__name__,
        "status": getattr(exc, "status", None) or getattr(response, "status_code", None),
        "code": getattr(exc, "code", None),
    }


def _credential_signer():
    try:
        values = {
            key: aidputils.secrets.get(name=CONFIG["credential_name"], key=key)
            for key in ("tenancy", "user", "fingerprint", "region", "private_key")
        }
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ValueError("incomplete credential")
        if values["region"] != CONFIG["region"]:
            raise ValueError("credential region mismatch")
        return oci.signer.Signer(
            tenancy=values["tenancy"],
            user=values["user"],
            fingerprint=values["fingerprint"],
            private_key_file_location=None,
            private_key_content=values["private_key"],
        )
    except Exception as exc:
        raise RuntimeError("The governance OCI credential is unavailable or invalid") from exc


def _tools():
    signer = _credential_signer()
    session = requests.Session()

    @tool
    def catalog_inventory(search_term: str = "", include_columns: bool = True, catalog_name: str = "", schema_name: str = "") -> str:
        """Search tables and columns across every ACTIVE Master Catalog catalog."""
        values = (search_term.strip(), catalog_name.strip(), schema_name.strip())
        if any(len(value) > 256 for value in values):
            return json.dumps({"error": "catalog search inputs must contain at most 256 characters"})
        if catalog_name == "oci_medallion" and schema_name == "oci_artifacts":
            return json.dumps({"error": "the governance control schema is excluded"})
        try:
            records = _table_records(session, signer, include_columns, search_term, catalog_name, schema_name)
            return json.dumps({
                "evidence_type": "observed_master_catalog",
                "match_count": len(records),
                "tables": sorted(records, key=lambda item: (item["catalog"], item["schema"], item["table"])),
            }, sort_keys=True)
        except Exception as exc:
            return json.dumps({"error": _safe_error("catalog_inventory", exc)}, sort_keys=True)

    @tool
    def catalog_lineage(table_name: str, catalog_name: str = "", schema_name: str = "", lineage_level: str = "ENTITY", column_name: str = "") -> str:
        """Trace entity or column lineage for one uniquely resolved Master Catalog table."""
        requested = table_name.strip()
        level = "COLUMN" if column_name.strip() else lineage_level.strip().upper()
        if not requested or not IDENTIFIER.fullmatch(requested) or level not in {"ENTITY", "COLUMN"}:
            return json.dumps({"error": "table_name or lineage_level is invalid"})
        if catalog_name == "oci_medallion" and schema_name == "oci_artifacts":
            return json.dumps({"error": "the governance control schema is excluded"})
        try:
            records = _table_records(session, signer, True, requested, catalog_name, schema_name)
            matches = [item for item in records if requested.casefold() in {
                item["table"].casefold(),
                f"{item['schema']}.{item['table']}".casefold(),
                f"{item['catalog']}.{item['schema']}.{item['table']}".casefold(),
            }]
            if len(matches) != 1:
                return json.dumps({
                    "error": "table was not found uniquely",
                    "candidates": sorted(f"{item['catalog']}.{item['schema']}.{item['table']}" for item in matches),
                })
            table = matches[0]
            if column_name and sum(column["name"].casefold() == column_name.casefold() for column in table["columns"]) != 1:
                return json.dumps({"error": "column was not found uniquely in the selected table"})
            graph, _ = _request(
                session,
                signer,
                "POST",
                "/actions/fetchLineage",
                params={"limit": "400" if level == "COLUMN" else "100"},
                payload={"anchorNode": table["qualified_name"], "direction": "BOTH", "maxDepth": 8, "level": level, "shouldIncludeEdges": True},
            )
            return json.dumps({
                "evidence_type": "observed_master_catalog_lineage",
                "catalog": table["catalog"],
                "schema": table["schema"],
                "table": table["table"],
                "column": column_name or None,
                "level": level,
                "lineage": _filter_control_lineage(graph),
            }, sort_keys=True)
        except Exception as exc:
            return json.dumps({"error": _safe_error("catalog_lineage", exc)}, sort_keys=True)

    return [catalog_inventory, catalog_lineage]


def _error_response(error):
    return {"messages": [{"role": "ai", "content": json.dumps({"agent_error": error})}]}


class DataGovernanceAgent:
    def __init__(self):
        self.llm = None
        self.setup_error = None

    def setup(self):
        try:
            self.llm = init_oci_llm(OCIAIConf(
                model_provider="generic",
                compartment_id=CONFIG["compartment_id"],
                endpoint=f"https://inference.generativeai.{CONFIG['region']}.oci.oraclecloud.com",
                model_id=CONFIG["model_id"],
                model_args={},
                guardrails_config={"name": "Data governance", "description": "Read-only Master Catalog", "policies": []},
            ))
        except Exception as exc:
            self.setup_error = _safe_error("setup", exc)
            logger.exception("Governance Agent setup failed")

    async def invoke(self, user_query, **kwargs):
        config = pre_invoke_setup(**kwargs)
        if self.setup_error:
            return _error_response(self.setup_error)
        try:
            args = {"model": self.llm, "tools": _tools(), "prompt": SYSTEM_PROMPT, "debug": False}
            if checkpointer:
                try:
                    agent = create_react_agent(checkpointer=checkpointer, **args)
                except Exception:
                    logger.warning("Checkpointer initialization failed; using a stateless graph", exc_info=True)
                    agent = create_react_agent(**args)
            else:
                agent = create_react_agent(**args)
            return await agent.ainvoke(input={"messages": [dict(HumanMessage(content=user_query))]}, config=config)
        except Exception as exc:
            logger.exception("Governance Agent invocation failed")
            return _error_response(_safe_error("invoke", exc))
'''


def agent_source(*, model_id: str, region: str, compartment_id: str, platform_id: str) -> bytes:
    """Render the global two-tool Agent without user tokens or gateway dependencies."""
    if not all((model_id, region, compartment_id, platform_id)):
        raise ValueError("The Agent runtime contract is incomplete")
    config = json.dumps(
        {
            "model_id": model_id,
            "region": region,
            "compartment_id": compartment_id,
            "platform_id": platform_id,
            "credential_name": GOVERNANCE_CREDENTIAL_NAME,
        },
        sort_keys=True,
    )
    return (
        _AGENT_TEMPLATE.replace("__CONFIG_JSON__", config)
        .replace("__SYSTEM_PROMPT_JSON__", json.dumps(DAMA_SYSTEM_PROMPT))
        .encode("utf-8")
    )


_SYNC_TEMPLATE = r'''\
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import aidputils
import oci
import requests
from delta.tables import DeltaTable
from pyspark.sql import Row
from pyspark.sql.functions import col, current_timestamp, lit

CONFIG = __SYNC_CONFIG_JSON__
MODULE_ID = CONFIG["module_id"]
CONTROL_SCHEMA = "oci_medallion.oci_artifacts"
API_BASE = (
    f"https://datalake.{CONFIG['region']}.oci.oraclecloud.com/20260430/"
    f"aiDataPlatforms/{CONFIG['platform_id']}"
)


def _location(table):
    return f"oci://oci_artifacts@{CONFIG['namespace']}/oci_artifacts/{table}"


spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CONTROL_SCHEMA}")
spark.sql(f"""CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.data_governance_config (
 module_id STRING, schema_version INT, enabled INT, updated_at TIMESTAMP, updated_by STRING
) USING DELTA LOCATION '{_location("data_governance_config")}'""")
spark.sql(f"""CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.data_governance_metadata (
 object_id STRING, catalog_key STRING, catalog_guid STRING, catalog_name STRING, schema_key STRING, schema_name STRING,
 table_key STRING, table_fingerprint STRING, table_name STRING, table_created_at STRING,
 table_created_by STRING, entity_type STRING, column_key STRING, column_name STRING, column_ordinal INT,
 data_type STRING, description STRING, fingerprint STRING, source_version STRING,
 identity_status STRING, is_deleted INT, created_at TIMESTAMP, updated_at TIMESTAMP, deleted_at TIMESTAMP
) USING DELTA LOCATION '{_location("data_governance_metadata")}'""")
spark.sql(f"""CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.data_governance_access_policy (
 permission_id STRING, object_id STRING, group_ocid STRING, group_name STRING, has_access INT,
 updated_at TIMESTAMP, updated_by STRING
) USING DELTA LOCATION '{_location("data_governance_access_policy")}'""")
spark.sql(f"""CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.data_governance_sync_state (
 source STRING, snapshot_version STRING, snapshot_hash STRING, status STRING, observed_count BIGINT,
 inserted_count BIGINT, updated_count BIGINT, deleted_count BIGINT, started_at TIMESTAMP,
 last_success_at TIMESTAMP, error_code STRING
) USING DELTA LOCATION '{_location("data_governance_sync_state")}'""")
spark.sql(f"""MERGE INTO {CONTROL_SCHEMA}.data_governance_config t
USING (SELECT '{MODULE_ID}' module_id, 1 schema_version, 0 enabled,
current_timestamp() updated_at, 'installer' updated_by) s ON t.module_id=s.module_id
WHEN NOT MATCHED THEN INSERT *""")

def _validated_enabled(rows):
    if len(rows) != 1 or rows[0]["enabled"] not in (0, 1):
        raise ValueError("The governance config singleton is invalid")
    return int(rows[0]["enabled"])


def _sync_state_frame(**values):
    return spark.createDataFrame([Row(**values)], schema="""
        source STRING, snapshot_version STRING, snapshot_hash STRING, status STRING,
        observed_count BIGINT, inserted_count BIGINT, updated_count BIGINT, deleted_count BIGINT,
        started_at TIMESTAMP, last_success_at TIMESTAMP, error_code STRING
    """)


started = datetime.now(timezone.utc)
config_rows = spark.table(f"{CONTROL_SCHEMA}.data_governance_config").where(
    f"module_id='{MODULE_ID}'"
).limit(2).collect()
try:
    config_enabled = _validated_enabled(config_rows)
except Exception as exc:
    error_code = hashlib.sha256(type(exc).__name__.encode()).hexdigest()[:16]
    failed = _sync_state_frame(
        source="master_catalog", snapshot_version="", snapshot_hash="", status="ERROR",
        observed_count=0, inserted_count=0, updated_count=0, deleted_count=0,
        started_at=started, last_success_at=None, error_code=error_code,
    )
    DeltaTable.forName(spark, f"{CONTROL_SCHEMA}.data_governance_sync_state").alias("t").merge(
        failed.alias("s"), "t.source=s.source"
    ).whenMatchedUpdate(set={"status": "s.status", "started_at": "s.started_at", "error_code": "s.error_code"}).whenNotMatchedInsertAll().execute()
    raise
SHOULD_DISABLE = CONFIG["desired_enabled"] is False or (
    config_enabled == 0
    and CONFIG["desired_enabled"] is None
    and not CONFIG["bootstrap_snapshot"]
)


def _credential_signer():
    values = {
        key: aidputils.secrets.get(name=CONFIG["credential_name"], key=key)
        for key in ("tenancy", "user", "fingerprint", "region", "private_key")
    }
    if any(not value for value in values.values()) or values["region"] != CONFIG["region"]:
        raise RuntimeError("The governance OCI credential is invalid")
    return oci.signer.Signer(
        tenancy=values["tenancy"], user=values["user"], fingerprint=values["fingerprint"],
        private_key_file_location=None, private_key_content=values["private_key"],
    )


def _request(session, signer, method, path, *, params=None, payload=None):
    response = session.request(
        method, API_BASE + path, auth=signer, params=params,
        json=payload,
        headers={"Accept": "application/json"}, timeout=(10, 60),
    )
    response.raise_for_status()
    return (response.json() if response.content else {}), response.headers


def _list(session, signer, path, params=None):
    result, page = [], None
    seen_pages = set()
    for _ in range(1000):
        query = {"limit": "100", **(params or {})}
        if page:
            query["page"] = page
        body, headers = _request(session, signer, "GET", path, params=query)
        if isinstance(body, list):
            values = body
        elif isinstance(body, dict) and ("items" in body or "Items" in body):
            values = body.get("items") if "items" in body else body.get("Items")
        else:
            raise RuntimeError("AIDP returned an invalid paginated response")
        if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
            raise RuntimeError("AIDP returned invalid list items")
        result.extend(values)
        page = headers.get("opc-next-page") or headers.get("Opc-Next-Page")
        if not page:
            return result
        if page in seen_pages:
            raise RuntimeError("AIDP returned a repeated pagination token")
        seen_pages.add(page)
    raise RuntimeError("AIDP pagination exceeded the safety limit")


def _name(item):
    return str(item.get("displayName") or item.get("name") or "")


def _pause_workflow():
    if not CONFIG["workspace_key"] or not CONFIG["job_key"]:
        raise RuntimeError("The disabled governance workflow cannot resolve its own job")
    session, signer = requests.Session(), _credential_signer()
    path = f"/workspaces/{quote(CONFIG['workspace_key'], safe='')}/jobs/{quote(CONFIG['job_key'], safe='')}"
    details, _ = _request(session, signer, "GET", path)
    payload = {
        name: details[name]
        for name in ("name", "path", "description", "maxConcurrentRuns", "jobClusters", "tasks")
        if name in details
    }
    payload["continuous"] = {"pauseStatus": "PAUSED"}
    _request(session, signer, "PUT", path, payload=payload)


def _identity_indexes(existing):
    by_key, by_exact, by_table = {}, {}, {}
    for item in existing:
        fingerprint = str(item.get("table_fingerprint") or "")
        column_key = str(item.get("column_key") or "")
        if column_key:
            by_key.setdefault((fingerprint, column_key), []).append(item)
        if int(item.get("is_deleted") or 0) == 0:
            by_exact.setdefault((fingerprint, str(item.get("column_name") or "").casefold()), []).append(item)
        by_table.setdefault(fingerprint, []).append(item)
    return by_key, by_exact, by_table


def _unique_existing_id(candidates, used):
    if len(candidates) > 1:
        raise RuntimeError("The control metadata contains ambiguous column identities")
    if not candidates:
        return None
    object_id = candidates[0]["object_id"]
    if object_id in used:
        raise RuntimeError("The source snapshot contains duplicate column identities")
    return object_id


def _existing_column_id(column, by_key, by_exact, used):
    fingerprint = column["table_fingerprint"]
    column_key = str(column.get("column_key") or "")
    object_id = _unique_existing_id(
        by_key.get((fingerprint, column_key), []) if column_key else [], used
    )
    if object_id is not None:
        return object_id
    return _unique_existing_id(
        by_exact.get((fingerprint, column["column_name"].casefold()), []), used
    )


def _has_retired_name(column, table_history):
    if column.get("column_key"):
        return False
    column_name = str(column["column_name"]).casefold()
    return any(
        int(item.get("is_deleted") or 0) == 1
        and str(item.get("column_name") or "").casefold() == column_name
        for item in table_history
    )


def _rename_column_id(columns, candidates, table_history):
    if len(columns) != 1:
        return None
    if len(candidates) != 1:
        return None
    column, candidate = columns[0], candidates[0]
    if int(column["column_ordinal"]) != int(candidate["column_ordinal"]):
        return None
    if column["data_type"].casefold() != candidate["data_type"].casefold():
        return None
    if _has_retired_name(column, table_history):
        return None
    return candidate["object_id"]


def _new_column_id(column, fingerprint, table_history):
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


def _resolve_identities(incoming, existing):
    by_key, by_exact, by_table = _identity_indexes(existing)
    resolved, unmatched, used = [], {}, set()
    for column in incoming:
        fingerprint = column["table_fingerprint"]
        object_id = _existing_column_id(column, by_key, by_exact, used)
        if object_id is None:
            unmatched.setdefault(fingerprint, []).append(column)
            continue
        resolved.append((column, object_id, "EXACT"))
        used.add(object_id)
    for fingerprint, columns in unmatched.items():
        table_history = by_table.get(fingerprint, [])
        candidates = [
            item for item in table_history
            if item["object_id"] not in used and int(item["is_deleted"] or 0) == 0
        ]
        object_id = _rename_column_id(columns, candidates, table_history)
        if object_id is not None:
            resolved.append((columns[0], object_id, "INFERRED_RENAME"))
            used.add(object_id)
            continue
        for column in columns:
            resolved.append((column, _new_column_id(column, fingerprint, table_history), "NEW"))
    return resolved


def _metadata_fingerprint(*values):
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def _change_counts(incoming, existing, unchanged=False):
    def value(item, name, default=None):
        return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)

    existing_by_id = {str(value(row, "object_id", "")): row for row in existing}
    incoming_by_id = {str(value(row, "object_id", "")): row for row in incoming}
    if unchanged:
        return 0, 0, 0
    inserted = sum(object_id not in existing_by_id for object_id in incoming_by_id)
    updated = sum(
        object_id in existing_by_id
        and (
            str(value(existing_by_id[object_id], "fingerprint", "") or "")
            != str(value(row, "fingerprint", "") or "")
            or int(value(existing_by_id[object_id], "is_deleted", 0) or 0) != 0
        )
        for object_id, row in incoming_by_id.items()
    )
    deleted = sum(
        int(value(row, "is_deleted", 0) or 0) == 0 and object_id not in incoming_by_id
        for object_id, row in existing_by_id.items()
    )
    return inserted, updated, deleted


def _snapshot():
    session, signer = requests.Session(), _credential_signer()
    existing = [row.asDict(recursive=True) for row in spark.table(f"{CONTROL_SCHEMA}.data_governance_metadata").collect()]
    incoming = []
    seen_table_fingerprints = {}
    for catalog in _list(session, signer, "/catalogs"):
        state = str(catalog.get("lifecycleState") or catalog.get("state") or "").upper()
        if state != "ACTIVE" or not catalog.get("key"):
            continue
        catalog_guid = str(catalog.get("catalogGuid") or catalog["key"])
        for schema in _list(session, signer, "/schemas", {"catalogKey": str(catalog["key"])}):
            if not schema.get("key") or (_name(catalog) == "oci_medallion" and _name(schema) == "oci_artifacts"):
                continue
            for table in _list(session, signer, "/tables", {"catalogKey": str(catalog["key"]), "schemaKey": str(schema["key"])}):
                table_key = str(table.get("key") or "")
                if not table_key:
                    continue
                detail, _ = _request(session, signer, "GET", f"/tables/{quote(table_key, safe='')}")
                created_at = str(detail.get("timeCreated") or "")
                created_by = str(detail.get("createdBy") or "")
                entity_type = str(detail.get("entityType") or "")
                if not entity_type:
                    raise RuntimeError("AIDP returned a table without entityType")
                identity = "\0".join((catalog_guid, created_at, created_by, entity_type))
                if not created_at or not created_by:
                    identity = "\0".join((catalog_guid, table_key))
                table_fingerprint = hashlib.sha256(identity.encode()).hexdigest()
                prior_table = seen_table_fingerprints.setdefault(table_fingerprint, table_key)
                if prior_table != table_key:
                    raise RuntimeError("AIDP returned ambiguous table creation identities")
                columns = detail.get("tableFields") or detail.get("columns") or detail.get("columnDefinitions")
                if not isinstance(columns, list) or not all(isinstance(column, dict) for column in columns):
                    raise RuntimeError("AIDP returned invalid table columns")
                for fallback_ordinal, column in enumerate(columns, start=1):
                    name = str(column.get("fieldName") or column.get("displayName") or column.get("name") or "")
                    data_type = str(column.get("fieldType") or column.get("dataType") or column.get("type") or "")
                    if not name or not data_type:
                        raise RuntimeError("AIDP returned an incomplete column")
                    source_ordinal = column.get("fieldPosition")
                    if source_ordinal is None:
                        source_ordinal = column.get("ordinalPosition")
                    try:
                        ordinal = fallback_ordinal if source_ordinal is None else int(source_ordinal)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError("AIDP returned an invalid column ordinal") from exc
                    description = str(column.get("fieldDescription") or column.get("description") or column.get("comment") or "")
                    catalog_name = _name(catalog)
                    schema_name = _name(schema)
                    table_name = _name(detail) or _name(table)
                    catalog_key = str(catalog["key"])
                    schema_key = str(schema["key"])
                    column_key = str(column.get("key") or column.get("id") or "")
                    source_version = str(detail.get("timeUpdated") or table.get("timeUpdated") or "")
                    fingerprint = _metadata_fingerprint(
                        catalog_key, catalog_guid, catalog_name, schema_key, schema_name,
                        table_key, table_fingerprint, table_name, created_at, created_by,
                        entity_type, column_key, name, ordinal, data_type, description,
                        source_version,
                    )
                    if not source_version:
                        source_version = fingerprint
                    incoming.append({
                        "catalog_key": catalog_key, "catalog_guid": catalog_guid,
                        "catalog_name": catalog_name, "schema_key": schema_key,
                        "schema_name": schema_name, "table_key": table_key,
                        "table_fingerprint": table_fingerprint, "table_name": table_name,
                        "table_created_at": created_at, "table_created_by": created_by, "entity_type": entity_type,
                        "column_key": column_key,
                        "column_name": name, "column_ordinal": ordinal, "data_type": data_type,
                        "description": description, "fingerprint": fingerprint,
                        "source_version": source_version,
                    })
    return [
        Row(**column, object_id=object_id, identity_status=status, is_deleted=0)
        for column, object_id, status in _resolve_identities(incoming, existing)
    ], existing


if SHOULD_DISABLE:
    if CONFIG["desired_enabled"] is False:
        spark.sql(f"UPDATE {CONTROL_SCHEMA}.data_governance_config SET enabled=0, updated_at=current_timestamp(), updated_by='installer' WHERE module_id='{MODULE_ID}'")
    spark.sql(f"""MERGE INTO {CONTROL_SCHEMA}.data_governance_sync_state t
    USING (SELECT 'master_catalog' source, 'DISABLED' status, current_timestamp() started_at) s
    ON t.source=s.source
    WHEN MATCHED THEN UPDATE SET t.status=s.status, t.started_at=s.started_at, t.error_code=NULL
    WHEN NOT MATCHED THEN INSERT (source, snapshot_version, snapshot_hash, status, observed_count,
      inserted_count, updated_count, deleted_count, started_at, last_success_at, error_code)
      VALUES (s.source, '', '', s.status, 0, 0, 0, 0, s.started_at, NULL, NULL)""")
    if CONFIG["desired_enabled"] is None:
        _pause_workflow()
    dbutils.notebook.exit("DISABLED")

try:
    records, existing_metadata = _snapshot()
    snapshot_hash = hashlib.sha256("".join(sorted(row.fingerprint for row in records)).encode()).hexdigest()
    previous = spark.table(f"{CONTROL_SCHEMA}.data_governance_sync_state").where("source='master_catalog'").limit(1).collect()
    unchanged = bool(previous and previous[0]["snapshot_hash"] == snapshot_hash and previous[0]["status"] == "SUCCESS")
    inserted_count, updated_count, deleted_count = _change_counts(
        records, existing_metadata, unchanged
    )
    if not unchanged:
        target = DeltaTable.forName(spark, f"{CONTROL_SCHEMA}.data_governance_metadata")
        if records:
            incoming = (
                spark.createDataFrame(records)
                .withColumn("created_at", current_timestamp())
                .withColumn("updated_at", current_timestamp())
                .withColumn("deleted_at", lit(None).cast("timestamp"))
            )
            target.alias("t").merge(incoming.alias("s"), "t.object_id=s.object_id").whenMatchedUpdate(set={
                "catalog_key": "s.catalog_key", "catalog_guid": "s.catalog_guid", "catalog_name": "s.catalog_name", "schema_key": "s.schema_key",
                "schema_name": "s.schema_name", "table_key": "s.table_key", "table_name": "s.table_name",
                "table_fingerprint": "s.table_fingerprint", "table_created_at": "s.table_created_at",
                "table_created_by": "s.table_created_by", "entity_type": "s.entity_type",
                "column_key": "s.column_key", "column_name": "s.column_name", "column_ordinal": "s.column_ordinal",
                "data_type": "s.data_type", "description": "s.description", "fingerprint": "s.fingerprint",
                "source_version": "s.source_version", "identity_status": "s.identity_status", "is_deleted": "0",
                "updated_at": "current_timestamp()", "deleted_at": "NULL",
            }).whenNotMatchedInsertAll().execute()
        observed = [row.object_id for row in records]
        deletion_condition = col("is_deleted") == 0
        if observed:
            deletion_condition = deletion_condition & (~col("object_id").isin(observed))
        target.update(
            condition=deletion_condition,
            set={"is_deleted": lit(1), "deleted_at": current_timestamp(), "updated_at": current_timestamp()},
        )
    now = datetime.now(timezone.utc)
    state = _sync_state_frame(
        source="master_catalog", snapshot_version=now.isoformat(), snapshot_hash=snapshot_hash,
        status="SUCCESS", observed_count=len(records), inserted_count=inserted_count,
        updated_count=updated_count, deleted_count=deleted_count,
        started_at=started, last_success_at=now, error_code=None,
    )
    DeltaTable.forName(spark, f"{CONTROL_SCHEMA}.data_governance_sync_state").alias("t").merge(
        state.alias("s"), "t.source=s.source"
    ).whenMatchedUpdateAll().whenNotMatchedInsertAll().execute()
    if CONFIG["desired_enabled"] is not None:
        enabled = 1 if CONFIG["desired_enabled"] else 0
        spark.sql(f"UPDATE {CONTROL_SCHEMA}.data_governance_config SET enabled={enabled}, updated_at=current_timestamp(), updated_by='installer' WHERE module_id='{MODULE_ID}'")
except Exception as exc:
    error_code = hashlib.sha256(type(exc).__name__.encode()).hexdigest()[:16]
    failed = _sync_state_frame(
        source="master_catalog", snapshot_version="", snapshot_hash="", status="ERROR",
        observed_count=0, inserted_count=0, updated_count=0, deleted_count=0,
        started_at=started, last_success_at=None, error_code=error_code,
    )
    DeltaTable.forName(spark, f"{CONTROL_SCHEMA}.data_governance_sync_state").alias("t").merge(
        failed.alias("s"), "t.source=s.source"
    ).whenMatchedUpdate(set={"status": "s.status", "started_at": "s.started_at", "error_code": "s.error_code"}).whenNotMatchedInsertAll().execute()
    raise
finally:
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    time.sleep(max(0, 30 - elapsed))
'''


def governance_sync_notebook(
    *,
    namespace: str,
    platform_id: str,
    region: str,
    desired_enabled: bool | None,
    bootstrap_snapshot: bool = False,
    workspace_key: str = "",
    job_key: str = "",
) -> dict[str, Any]:
    """Return the protected Spark notebook used by the single continuous workflow."""
    if not all((namespace, platform_id, region)):
        raise ValueError("The governance synchronization runtime contract is incomplete")
    config = json.dumps(
        {
            "module_id": GOVERNANCE_MODULE_ID,
            "credential_name": GOVERNANCE_CREDENTIAL_NAME,
            "namespace": namespace,
            "platform_id": platform_id,
            "region": region,
            "desired_enabled": desired_enabled,
            "bootstrap_snapshot": bootstrap_snapshot,
            "workspace_key": workspace_key,
            "job_key": job_key,
        },
        sort_keys=True,
    )
    source = _SYNC_TEMPLATE.replace("__SYNC_CONFIG_JSON__", config)
    return {
        "cells": [
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": source.splitlines(keepends=True),
            }
        ],
        "metadata": {"language_info": {"name": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
