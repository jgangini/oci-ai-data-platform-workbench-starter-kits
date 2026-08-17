"""Participant-scoped, Master Catalog backed governance Agent source."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence


PARTICIPANT_KEY = re.compile(r"u[1-9][0-9]*")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")

DAMA_SYSTEM_PROMPT = """You are a senior data governance specialist grounded in DAMA-DMBOK. Use only the provided participant-scoped tools and never invent SQL, identifiers, owners, metrics, lineage, or data. Match the user's language. Before factual answers, call catalog_inventory to establish the live Master Catalog scope. Use catalog_lineage for observed ENTITY or COLUMN traceability and the lab-specific catalog_metrics_* tool for observed table row counts. Explain results in four concise parts: Evidence, Explanation, Governance implication, and Recommendation or limitation. Clearly separate observed facts from DAMA-based recommendations. If evidence is unavailable, say so and identify the metadata or control needed; do not guess. Refuse arbitrary SQL, mutations, and requests for another participant's information. When lineage is requested, name the observed source-to-target path and distinguish entity lineage from column lineage."""


def database_names(participant_key: str) -> tuple[str, str]:
    """Return legacy Autonomous names so old deployments can be cleaned safely."""
    if PARTICIPANT_KEY.fullmatch(participant_key) is None or int(participant_key[1:]) < 101:
        raise ValueError("A participant key starting at u101 is required")
    stem = participant_key.upper()
    return f"{stem}_AGENT", f"{stem}_AGENT_RO"


def external_catalog_name(participant_key: str) -> str:
    """Return the legacy mirrored-catalog name used only during cleanup."""
    database_names(participant_key)
    return f"{participant_key}_agent_autonomous"


def governed_metric_queries(
    participant_key: str,
    catalog_name: str,
    lab_tables: Mapping[str, Mapping[str, Sequence[str]]],
) -> dict[str, str]:
    """Build allowlisted row-count queries against real participant Delta tables."""
    database_names(participant_key)
    if IDENTIFIER.fullmatch(catalog_name) is None:
        raise ValueError("A safe participant catalog name is required")
    queries: dict[str, str] = {}
    for lab_id, layers in sorted(lab_tables.items()):
        if IDENTIFIER.fullmatch(lab_id) is None:
            raise ValueError("A safe lab identifier is required")
        selects: list[str] = []
        for layer, table_names in sorted(layers.items()):
            if IDENTIFIER.fullmatch(layer) is None:
                raise ValueError("A safe medallion layer is required")
            for logical_name in table_names:
                if IDENTIFIER.fullmatch(logical_name) is None:
                    raise ValueError("A safe table identifier is required")
                table_name = f"{participant_key}_{lab_id}_{logical_name}"
                selects.append(
                    "SELECT "
                    f"'{lab_id}' AS lab_id, '{layer}' AS layer, "
                    f"'{table_name}' AS table_name, COUNT(*) AS row_count "
                    f"FROM `{catalog_name}`.`oci_{layer}`.`{table_name}`"
                )
        if selects:
            queries[lab_id] = " UNION ALL ".join(selects) + " ORDER BY layer, table_name"
    return queries


def agent_source(
    *,
    model_id: str,
    region: str,
    compartment_id: str,
    platform_id: str,
    participant_key: str,
    catalog_key: str,
    catalog_name: str,
    schema_keys: Mapping[str, str],
    spark_compute_key: str,
    metric_queries: Mapping[str, str],
) -> bytes:
    """Render a code Agent that reads live AIDP metadata and governed Delta tables."""
    required = (
        model_id,
        region,
        compartment_id,
        platform_id,
        participant_key,
        catalog_key,
        catalog_name,
        spark_compute_key,
    )
    if not all(required) or set(schema_keys) != {"landing", "bronze", "silver", "gold"}:
        raise ValueError("The Agent runtime contract is incomplete")
    database_names(participant_key)
    if any(not value for value in schema_keys.values()) or not metric_queries:
        raise ValueError("The Agent catalog contract is incomplete")
    encoded = json.dumps(
        {
            "model_id": model_id,
            "region": region,
            "compartment_id": compartment_id,
            "platform_id": platform_id,
            "catalog_key": catalog_key,
            "catalog_name": catalog_name,
            "schema_keys": dict(schema_keys),
            "spark_compute_key": spark_compute_key,
            "participant_key": participant_key,
            "table_prefix": f"{participant_key}_",
            "metric_queries": dict(metric_queries),
        },
        sort_keys=True,
    )
    source = f'''\
import json
import logging
import re
from urllib.parse import quote

import oci
import requests
from aidputils.agents.toolkit.agent_helper import init_oci_llm, pre_invoke_setup
from aidputils.agents.toolkit.configs import AIDPToolConf, OCIAIConf
from aidputils.agents.toolkit.tool_helper import create_langgraph_tool
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

CONFIG = {encoded}
API_BASE = (
    f"https://datalake.{{CONFIG['region']}}.oci.oraclecloud.com/20260430/"
    f"aiDataPlatforms/{{CONFIG['platform_id']}}"
)
LAYERS = ("landing", "bronze", "silver", "gold")
TABLE_NAME = re.compile(rf"^{{re.escape(CONFIG['table_prefix'])}}[a-z0-9_]+$")
FOREIGN_TABLE = re.compile(r"\\bu[1-9][0-9]*_[a-z0-9_]+", re.IGNORECASE)
logger = logging.getLogger("data_governance_agent")
checkpointer = globals().get("checkpointer")
_session = None
_signer = None


def _client():
    global _session, _signer
    if _session is None:
        _session = requests.Session()
        _signer = oci.auth.signers.get_resource_principals_signer()
    return _session, _signer


def _request(method, path, *, params=None, payload=None):
    session, signer = _client()
    response = session.request(
        method,
        API_BASE + path,
        auth=signer,
        params=params,
        json=payload,
        headers={{"Accept": "application/json"}},
        timeout=(10, 60),
    )
    response.raise_for_status()
    return response.json() if response.content else {{}}, response.headers


def _items(body):
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if isinstance(body, dict):
        values = body.get("items") or body.get("Items") or []
        return [item for item in values if isinstance(item, dict)]
    return []


def _list(path, params):
    result = []
    page = None
    while True:
        query = {{"limit": "100", **params}}
        if page:
            query["page"] = page
        body, headers = _request("GET", path, params=query)
        result.extend(_items(body))
        page = headers.get("opc-next-page") or headers.get("Opc-Next-Page")
        if not page:
            return result


def _name(value):
    return str(value.get("displayName") or value.get("name") or "")


def _catalog_tables(layer, include_columns=False):
    tables = []
    for item in _list(
        "/tables",
        {{"catalogKey": CONFIG["catalog_key"], "schemaKey": CONFIG["schema_keys"][layer]}},
    ):
        table_name = _name(item)
        if not TABLE_NAME.fullmatch(table_name):
            continue
        detail = item
        table_key = str(item.get("key") or "")
        if include_columns and table_key:
            candidate, _ = _request("GET", f"/tables/{{quote(table_key, safe='')}}")
            if isinstance(candidate, dict):
                detail = candidate
        columns = detail.get("columns") or detail.get("columnDefinitions") or []
        tables.append({{
            "layer": layer,
            "table": table_name,
            "qualified_name": str(
                detail.get("qualifiedName")
                or f"{{CONFIG['catalog_name']}}.oci_{{layer}}.{{table_name}}"
            ),
            "format": detail.get("dataFormat") or detail.get("format") or "DELTA",
            "columns": [
                str(column.get("displayName") or column.get("name") or "")
                for column in columns
                if isinstance(column, dict)
            ],
        }})
    return tables


@tool
def catalog_inventory(layer: str = "ALL", include_columns: bool = False) -> str:
    """List live participant tables from Master Catalog by medallion layer."""
    selected = layer.strip().lower()
    if selected != "all" and selected not in LAYERS:
        return json.dumps({{"error": "layer must be ALL, LANDING, BRONZE, SILVER, or GOLD"}})
    layers = LAYERS if selected == "all" else (selected,)
    tables = [table for item in layers for table in _catalog_tables(item, include_columns)]
    return json.dumps({{
        "participant_key": CONFIG["participant_key"],
        "catalog": CONFIG["catalog_name"],
        "tables": sorted(tables, key=lambda item: (item["layer"], item["table"])),
    }}, sort_keys=True)


def _contains_foreign_participant(value):
    if isinstance(value, str):
        matches = FOREIGN_TABLE.findall(value)
        return any(not match.casefold().startswith(CONFIG["table_prefix"].casefold()) for match in matches)
    if isinstance(value, dict):
        return any(_contains_foreign_participant(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_foreign_participant(item) for item in value)
    return False


@tool
def catalog_lineage(table_name: str, lineage_level: str = "ENTITY") -> str:
    """Fetch observed AIDP lineage for one participant table at ENTITY or COLUMN level."""
    normalized_name = table_name.strip().lower()
    level = lineage_level.strip().upper()
    if not TABLE_NAME.fullmatch(normalized_name):
        return json.dumps({{"error": "table is outside this participant catalog"}})
    if level not in {{"ENTITY", "COLUMN"}}:
        return json.dumps({{"error": "lineage_level must be ENTITY or COLUMN"}})
    matches = [
        table
        for layer in LAYERS
        for table in _catalog_tables(layer)
        if table["table"].casefold() == normalized_name.casefold()
    ]
    if len(matches) != 1:
        return json.dumps({{"error": "table was not found uniquely in this participant catalog"}})
    graph, _ = _request(
        "POST",
        "/actions/fetchLineage",
        params={{"limit": "400" if level == "COLUMN" else "100"}},
        payload={{
            "anchorNode": matches[0]["qualified_name"],
            "direction": "BOTH",
            "maxDepth": 8,
            "level": level,
            "shouldIncludeEdges": True,
        }},
    )
    if _contains_foreign_participant(graph):
        return json.dumps({{"error": "lineage response crossed the participant boundary"}})
    return json.dumps({{
        "participant_key": CONFIG["participant_key"],
        "catalog": CONFIG["catalog_name"],
        "anchor": matches[0]["qualified_name"],
        "level": level,
        "direction": "BOTH",
        "max_depth": 8,
        "graph": graph,
    }}, sort_keys=True)


def _metric_tool(lab_id, query):
    definition = AIDPToolConf(
        name=f"catalog_metrics_{{lab_id}}",
        description=(
            f"Return observed row counts from this participant's {{lab_id}} Delta tables. "
            "Call catalog_inventory first and use this tool only when that lab is present."
        ),
        tool_class="SQLTool",
        conf={{
            "queryType": "SPARK",
            "sparkComputeKey": CONFIG["spark_compute_key"],
            "catalogKey": CONFIG["catalog_key"],
            "schemaKey": CONFIG["schema_keys"]["gold"],
            "query": query,
            "isRowLimitEnabled": True,
            "maxRows": 200,
        }},
        params=[],
    )
    return create_langgraph_tool(definition.model_dump())


TOOLS = [catalog_inventory, catalog_lineage] + [
    _metric_tool(lab_id, query)
    for lab_id, query in sorted(CONFIG["metric_queries"].items())
]


class DataGovernanceAgent:
    def __init__(self):
        self.agent = None

    def setup(self):
        llm = init_oci_llm(OCIAIConf(
            model_provider="generic",
            compartment_id=CONFIG["compartment_id"],
            endpoint=f"https://inference.generativeai.{{CONFIG['region']}}.oci.oraclecloud.com",
            model_id=CONFIG["model_id"],
            model_args={{}},
            guardrails_config={{"name": "Data governance", "description": "Participant isolation", "policies": []}},
        ))
        prompt = {DAMA_SYSTEM_PROMPT!r}
        kwargs = {{"model": llm, "tools": TOOLS, "prompt": prompt, "debug": False}}
        if checkpointer:
            kwargs["checkpointer"] = checkpointer
        self.agent = create_react_agent(**kwargs)

    async def invoke(self, user_query, **kwargs):
        return await self.agent.ainvoke(
            {{"messages": [dict(HumanMessage(content=user_query))]}},
            config=pre_invoke_setup(**kwargs),
        )
'''
    return source.encode("utf-8")
