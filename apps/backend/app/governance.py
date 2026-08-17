"""Participant-scoped, Master Catalog backed governance Agent source."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence


PARTICIPANT_KEY = re.compile(r"u[1-9][0-9]*")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")

DAMA_SYSTEM_PROMPT = """You are a senior data governance specialist grounded in DAMA-DMBOK. Use only the provided participant-scoped tools and never invent SQL, identifiers, owners, metrics, lineage, or data. Match the user's language. Before factual answers, call the relevant catalog_inventory_landing, catalog_inventory_bronze, catalog_inventory_silver, and catalog_inventory_gold tools to establish the live Master Catalog scope. Use catalog_lineage for the canonical ENTITY or COLUMN lineage contract and the lab-specific catalog_metrics_* tool for observed table row counts. Explain results in four concise parts: Evidence, Explanation, Governance implication, and Recommendation or limitation. Clearly distinguish live SQL observations, canonical package contracts, and DAMA-based recommendations. If evidence is unavailable, say so and identify the metadata or control needed; do not guess. Refuse arbitrary SQL, mutations, and requests for another participant's information. When lineage is requested, name the source-to-target path, distinguish entity lineage from column lineage, and state that the canonical contract must be corroborated by the laboratory's validation notebook or Master Catalog lineage view before it is described as observed."""


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


def governed_lineage_contracts(
    participant_key: str,
    catalog_name: str,
    lab_contracts: Mapping[str, Mapping[str, Sequence[str]]],
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Qualify canonical lab lineage paths for one participant catalog."""
    database_names(participant_key)
    if IDENTIFIER.fullmatch(catalog_name) is None:
        raise ValueError("A safe participant catalog name is required")
    node = re.compile(
        r"(landing|bronze|silver|gold)\.([a-z][a-z0-9_]*)(?:\.([a-z][a-z0-9_]*))?"
    )
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for lab_id, levels in sorted(lab_contracts.items()):
        if IDENTIFIER.fullmatch(lab_id) is None or set(levels) != {"ENTITY", "COLUMN"}:
            raise ValueError("A safe lineage lab contract is required")
        rendered: dict[str, tuple[str, ...]] = {}
        for level, paths in levels.items():
            qualified: list[str] = []
            for path in paths:
                segments: list[str] = []
                for raw in str(path).split("->"):
                    match = node.fullmatch(raw.strip())
                    if match is None:
                        raise ValueError("A safe canonical lineage path is required")
                    layer, table, column = match.groups()
                    segment = (
                        f"{catalog_name}.oci_{layer}."
                        f"{participant_key}_{lab_id}_{table}"
                    )
                    segments.append(f"{segment}.{column}" if column else segment)
                qualified.append("->".join(segments))
            rendered[level] = tuple(qualified)
        result[lab_id] = rendered
    return result


def agent_source(
    *,
    model_id: str,
    region: str,
    compartment_id: str,
    participant_key: str,
    catalog_key: str,
    catalog_name: str,
    schema_keys: Mapping[str, str],
    spark_compute_key: str,
    metric_queries: Mapping[str, str],
    lineage_contracts: Mapping[str, Mapping[str, Sequence[str]]],
) -> bytes:
    """Render a code Agent that reads live AIDP metadata and governed Delta tables."""
    required = (
        model_id,
        region,
        compartment_id,
        participant_key,
        catalog_key,
        catalog_name,
        spark_compute_key,
    )
    if not all(required) or set(schema_keys) != {"landing", "bronze", "silver", "gold"}:
        raise ValueError("The Agent runtime contract is incomplete")
    database_names(participant_key)
    if any(not value for value in schema_keys.values()) or not lineage_contracts:
        raise ValueError("The Agent catalog contract is incomplete")
    encoded = json.dumps(
        {
            "model_id": model_id,
            "region": region,
            "compartment_id": compartment_id,
            "catalog_key": catalog_key,
            "catalog_name": catalog_name,
            "schema_keys": dict(schema_keys),
            "spark_compute_key": spark_compute_key,
            "participant_key": participant_key,
            "table_prefix": f"{participant_key}_",
            "metric_queries": dict(metric_queries),
            "lineage_contracts": {
                lab_id: {level: list(paths) for level, paths in levels.items()}
                for lab_id, levels in lineage_contracts.items()
            },
        },
        sort_keys=True,
    )
    source = f'''\
import json
import logging
import re

from aidputils.agents.toolkit.agent_helper import init_oci_llm, pre_invoke_setup
from aidputils.agents.toolkit.configs import AIDPToolConf, OCIAIConf
from aidputils.agents.toolkit.tool_helper import create_langgraph_tool
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

CONFIG = {encoded}
LAYERS = ("landing", "bronze", "silver", "gold")
LAB_ID = re.compile(r"[a-z][a-z0-9_]*")
logger = logging.getLogger("data_governance_agent")
checkpointer = globals().get("checkpointer")


def _inventory_tool(layer):
    definition = AIDPToolConf(
        name=f"catalog_inventory_{{layer}}",
        description=(
            f"List this participant's live {{layer}} tables from Master Catalog. "
            "Use this before factual answers about that medallion layer."
        ),
        tool_class="SQLTool",
        conf={{
            "queryType": "SPARK",
            "sparkComputeKey": CONFIG["spark_compute_key"],
            "catalogKey": CONFIG["catalog_key"],
            "schemaKey": CONFIG["schema_keys"][layer],
            "query": "SHOW TABLES LIKE '{participant_key}_*'",
            "isRowLimitEnabled": True,
            "maxRows": 500,
        }},
        params=[],
    )
    return create_langgraph_tool(definition.model_dump())


@tool
def catalog_lineage(
    lab_id: str,
    lineage_level: str = "ENTITY",
    contains: str = "",
) -> str:
    """Return the canonical participant-qualified lineage contract for one lab."""
    normalized_lab = lab_id.strip().lower()
    level = lineage_level.strip().upper()
    if not LAB_ID.fullmatch(normalized_lab):
        return json.dumps({{"error": "lab_id is invalid"}})
    if level not in {{"ENTITY", "COLUMN"}}:
        return json.dumps({{"error": "lineage_level must be ENTITY or COLUMN"}})
    contract = CONFIG["lineage_contracts"].get(normalized_lab)
    if not isinstance(contract, dict):
        return json.dumps({{"error": "no canonical lineage contract exists for this lab"}})
    paths = list(contract.get(level) or [])
    needle = contains.strip().casefold()
    if needle:
        paths = [path for path in paths if needle in path.casefold()]
    return json.dumps({{
        "participant_key": CONFIG["participant_key"],
        "catalog": CONFIG["catalog_name"],
        "lab_id": normalized_lab,
        "level": level,
        "evidence_type": "canonical_package_contract",
        "paths": paths,
        "observation_requirement": (
            "Corroborate with the lab validation notebook or Master Catalog lineage view."
        ),
    }}, sort_keys=True)


def _metric_tool(lab_id, query):
    definition = AIDPToolConf(
        name=f"catalog_metrics_{{lab_id}}",
        description=(
            f"Return observed row counts from this participant's {{lab_id}} Delta tables. "
            "Call the relevant catalog_inventory_* tools first and use this tool only "
            "when that lab is present."
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


def _base_tools():
    return [_inventory_tool(layer) for layer in LAYERS] + [catalog_lineage]


def _safe_error(stage, exc):
    response = getattr(exc, "response", None)
    status = getattr(exc, "status", None) or getattr(response, "status_code", None)
    code = getattr(exc, "code", None)
    if response is not None and not code:
        try:
            body = response.json()
            code = body.get("code") if isinstance(body, dict) else None
        except Exception:
            code = None
    return {{
        "stage": stage,
        "type": type(exc).__name__,
        "status": status,
        "code": code,
    }}


def _error_response(error):
    return {{"messages": [{{"role": "ai", "content": json.dumps({{"agent_error": error}})}}]}}


class DataGovernanceAgent:
    def __init__(self):
        self.agent = None
        self.setup_error = None
        self.metric_tools_error = None

    def setup(self):
        try:
            tools = _base_tools()
            try:
                tools.extend(
                    _metric_tool(lab_id, query)
                    for lab_id, query in sorted(CONFIG["metric_queries"].items())
                )
            except Exception as exc:
                self.metric_tools_error = _safe_error("metric_tools", exc)
                logger.warning("Metric tools are unavailable; catalog tools remain active", exc_info=True)
            llm = init_oci_llm(OCIAIConf(
                model_provider="generic",
                compartment_id=CONFIG["compartment_id"],
                endpoint=f"https://inference.generativeai.{{CONFIG['region']}}.oci.oraclecloud.com",
                model_id=CONFIG["model_id"],
                model_args={{}},
                guardrails_config={{"name": "Data governance", "description": "Participant isolation", "policies": []}},
            ))
            prompt = {DAMA_SYSTEM_PROMPT!r}
            kwargs = {{"model": llm, "tools": tools, "prompt": prompt, "debug": False}}
            if checkpointer:
                try:
                    self.agent = create_react_agent(checkpointer=checkpointer, **kwargs)
                    return
                except Exception:
                    logger.warning("Checkpointer initialization failed; using a stateless graph", exc_info=True)
            self.agent = create_react_agent(**kwargs)
        except Exception as exc:
            self.setup_error = _safe_error("setup", exc)
            logger.exception("Governance Agent setup failed")

    async def invoke(self, user_query, **kwargs):
        if self.setup_error:
            return _error_response(self.setup_error)
        try:
            message = {{"messages": [dict(HumanMessage(content=user_query))]}}
            return await self.agent.ainvoke(
                input=message,
                config=pre_invoke_setup(**kwargs),
            )
        except Exception as exc:
            logger.exception("Governance Agent invocation failed")
            return _error_response(_safe_error("invoke", exc))
'''
    return source.encode("utf-8")
