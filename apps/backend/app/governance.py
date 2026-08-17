"""Participant-scoped names and editable AIDP governance Agent source."""

from __future__ import annotations

import json
import re


PARTICIPANT_KEY = re.compile(r"u[1-9][0-9]*")

DAMA_SYSTEM_PROMPT = """You are a senior data governance specialist grounded in DAMA-DMBOK. Use only the provided participant-scoped tools and never invent SQL, identifiers, owners, metrics, lineage, or data. Match the user's language. For factual questions, call the tool that supplies the evidence before answering: catalog_inventory for scope, catalog_metrics for governed values, and catalog_lineage for ENTITY or COLUMN traceability. Explain the result in four concise parts: Evidence, Explanation, Governance implication, and Recommendation or limitation. Clearly separate observed facts from DAMA-based recommendations. Metric names prefixed contract. and lineage paths prefixed CONTRACT: are versioned laboratory contracts, not observed runtime results; always identify that limitation. If evidence is unavailable, say so and identify the metadata or control needed; do not guess. Refuse arbitrary SQL, mutations, and requests for another participant's information. When lineage is requested, name the source-to-target path and distinguish entity lineage from column lineage."""


def database_names(participant_key: str) -> tuple[str, str]:
    if PARTICIPANT_KEY.fullmatch(participant_key) is None or int(participant_key[1:]) < 101:
        raise ValueError("A participant key starting at u101 is required")
    stem = participant_key.upper()
    return f"{stem}_AGENT", f"{stem}_AGENT_RO"


def external_catalog_name(participant_key: str) -> str:
    database_names(participant_key)
    return f"{participant_key}_agent_autonomous"


def agent_source(
    *,
    model_id: str,
    region: str,
    compartment_id: str,
    external_catalog_key: str,
    database_schema: str,
) -> bytes:
    """Render a code Agent with only predefined, parameterized read queries."""
    if not all((model_id, region, compartment_id, external_catalog_key, database_schema)):
        raise ValueError("The Agent runtime contract is incomplete")
    encoded = json.dumps(
        {
            "model_id": model_id,
            "region": region,
            "compartment_id": compartment_id,
            "catalog_key": external_catalog_key,
            "schema": database_schema,
        },
        sort_keys=True,
    )
    source = f'''\
import logging

from aidputils.agents.toolkit.agent_helper import init_oci_llm, pre_invoke_setup
from aidputils.agents.toolkit.configs import AIDPToolConf, OCIAIConf
from aidputils.agents.toolkit.tool_helper import create_langgraph_tool
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent

CONFIG = {encoded}
logger = logging.getLogger("data_governance_agent")
checkpointer = globals().get("checkpointer")


def sql_tool(name, description, query, params=None):
    definition = AIDPToolConf(
        name=name,
        description=description,
        tool_class="SQLTool",
        conf={{"catalogKey": CONFIG["catalog_key"], "schemaKey": CONFIG["schema"], "query": query}},
        params=params or [],
    )
    return create_langgraph_tool(definition.model_dump())


TOOLS = [
    sql_tool(
        "catalog_inventory",
        "List only this participant's laboratories represented in the governed metric catalog. Use it to establish scope before cross-laboratory governance answers.",
        "SELECT LAB_ID FROM LAB_METRICS WHERE METRIC_NAME = 'contract.assignment_status' AND METRIC_VALUE = 'active' ORDER BY LAB_ID",
    ),
    sql_tool(
        "catalog_metrics",
        "Read predefined, participant-scoped governance evidence for one assigned laboratory. A contract. prefix means declared expected evidence, not an observed runtime result.",
        "SELECT METRIC_NAME, METRIC_VALUE FROM LAB_METRICS WHERE LAB_ID = {{{{lab_id}}}} AND EXISTS (SELECT 1 FROM LAB_METRICS S WHERE S.LAB_ID = LAB_METRICS.LAB_ID AND S.METRIC_NAME = 'contract.assignment_status' AND S.METRIC_VALUE = 'active') ORDER BY METRIC_NAME",
        [{{"name": "lab_id", "type": "string", "description": "Assigned laboratory identifier"}}],
    ),
    sql_tool(
        "catalog_lineage",
        "Read entity or column lineage evidence limited to the participant catalog. A CONTRACT: prefix means a declared versioned path, not an observed runtime path.",
        "SELECT RELATION_PATH FROM LINEAGE_RELATIONS WHERE LAB_ID = {{{{lab_id}}}} AND LINEAGE_LEVEL = {{{{lineage_level}}}} AND EXISTS (SELECT 1 FROM LAB_METRICS S WHERE S.LAB_ID = LINEAGE_RELATIONS.LAB_ID AND S.METRIC_NAME = 'contract.assignment_status' AND S.METRIC_VALUE = 'active') ORDER BY RELATION_PATH",
        [
            {{"name": "lab_id", "type": "string", "description": "Assigned laboratory identifier"}},
            {{"name": "lineage_level", "type": "string", "description": "ENTITY or COLUMN"}},
        ],
    ),
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
