import hashlib
"""Read-only global AIDP Master Catalog governance Agent."""

import json
import logging
import re
from urllib.parse import quote

import aidputils
import oci
import requests
from aidputils.agents.toolkit.agent_helper import GenAIChatInvoker, GenerativeAiInferenceV2Client, pre_invoke_setup
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

CONFIG = {}
SYSTEM_PROMPT = "You are a senior data governance specialist grounded in DAMA-DMBOK.\nUse only catalog_inventory and catalog_lineage. They read every ACTIVE Master Catalog catalog,\nexcept the oci_medallion.oci_artifacts control schema. Never invent metadata, lineage, owners,\nfreshness, policies, SQL, identifiers, or access decisions. Treat timeUpdated as a catalog\nmetadata timestamp, not proof of data freshness. Clearly distinguish observed evidence from a\nDAMA-based recommendation. Refuse mutations, arbitrary SQL, requests for the control schema,\nand claims that cannot be certified from the returned evidence. Match the user's language and\nanswer as Evidence, Explanation, Governance implication, and Recommendation or limitation."
API_BASE = (
    f"https://datalake.{CONFIG['region']}.oci.oraclecloud.com/20260430/"
    f"aiDataPlatforms/{CONFIG['platform_id']}"
)
IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
logger = logging.getLogger("ai_data_governance")
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
            for key in ("tenancy", "user", "fingerprint", "region")
        }
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ValueError("incomplete credential")
        if values["region"] != CONFIG["region"]:
            raise ValueError("credential region mismatch")
        identity = hashlib.sha256(json.dumps([values[key] for key in ("tenancy", "user", "fingerprint")], separators=(",", ":")).encode()).hexdigest()
        if CONFIG.get("identity_sha256") and identity != CONFIG["identity_sha256"]:
            raise ValueError("credential identity mismatch")
        values["private_key"] = aidputils.secrets.get(name=CONFIG["credential_name"], key="private_key")
        if not isinstance(values["private_key"], str) or not values["private_key"]:
            raise ValueError("incomplete credential")
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
            endpoint = f"https://inference.generativeai.{CONFIG['region']}.oci.oraclecloud.com"
            client = GenerativeAiInferenceV2Client(endpoint=endpoint, signer=_credential_signer())
            self.llm = GenAIChatInvoker(
                provider="generic", auth_type="API_KEY", client=client, is_stream=True,
                compartment_id=CONFIG["compartment_id"],
                service_endpoint=endpoint,
                model_id=CONFIG["model_id"],
                model_kwargs={},
                guardrails_config={"name": "Data governance", "description": "Read-only Master Catalog", "policies": []},
            )
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
