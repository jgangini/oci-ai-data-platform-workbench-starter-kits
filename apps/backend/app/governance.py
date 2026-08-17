"""Participant-scoped, Master Catalog backed governance Agent source."""

from __future__ import annotations

import json
import re


PARTICIPANT_KEY = re.compile(r"u[1-9][0-9]*")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")
GOVERNANCE_CREDENTIAL_NAME = "AidpGovernanceOperator"

DAMA_SYSTEM_PROMPT = """You are a senior data governance specialist grounded in DAMA-DMBOK. Use only the provided participant-scoped tools and never invent SQL, identifiers, owners, metrics, lineage, freshness, or data. Match the user's language. Call at most one tool per model turn and never emit parallel tool calls; sequential calls are allowed when one result is needed to resolve the next call.

Choose evidence precisely:
- For where to find a table or field, what information a table contains, column types, descriptions, format, lifecycle state, or catalog timestamps, call catalog_inventory. Pass search_term and include_columns=true for a focused table or field lookup. Use the smallest relevant medallion layer and use ALL only when the location is unknown or the complete live scope is required.
- For the flow to or from a table, the workflow tasks or notebook paths involved, call catalog_lineage at ENTITY level.
- For the origin, destination, derivation, or use of a field, call catalog_lineage at COLUMN level and pass column_name. If the table is unknown, locate it first with catalog_inventory, then call catalog_lineage on the uniquely observed table.

Treat timeUpdated as the last Master Catalog metadata update, not proof that the underlying data was refreshed at that time. Treat a task display name as a workflow task; call it a notebook only when the job metadata provides notebook_path. Explain a field's likely business use only as an interpretation of observed name, type, table, and lineage unless a catalog description explicitly defines it. Use match_count and counts_by_layer exactly as returned; never recalculate or alter those counts in prose. Reconcile enumerated records with those counts, and omit a count rather than estimate it.

Explain results in four concise parts: Evidence, Explanation, Governance implication, and Recommendation or limitation. Clearly distinguish observed Master Catalog metadata and lineage from DAMA-based recommendations. If evidence is unavailable, say so and identify the metadata or control needed; do not guess. Refuse arbitrary SQL, mutations, and requests for another participant's information. If a request names another participant, refuse immediately: do not call a tool, substitute the allowed participant, or disclose the allowed participant's records. When lineage is requested, present the observed source-to-target path in order, name the intervening tasks and notebook paths, and distinguish entity lineage from column lineage."""


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


def agent_source(
    *,
    model_id: str,
    region: str,
    compartment_id: str,
    platform_id: str,
    participant_key: str,
    catalog_name: str,
) -> bytes:
    """Render a code Agent that reads live AIDP metadata with a stored OCI signer."""
    required = (
        model_id,
        region,
        compartment_id,
        platform_id,
        participant_key,
        catalog_name,
    )
    if not all(required):
        raise ValueError("The Agent runtime contract is incomplete")
    database_names(participant_key)
    encoded = json.dumps(
        {
            "model_id": model_id,
            "region": region,
            "compartment_id": compartment_id,
            "platform_id": platform_id,
            "catalog_name": catalog_name,
            "credential_name": GOVERNANCE_CREDENTIAL_NAME,
            "participant_key": participant_key,
            "table_prefix": f"{participant_key}_",
        },
        sort_keys=True,
    )
    system_prompt = (
        DAMA_SYSTEM_PROMPT
        + f"\nThe only allowed participant is {participant_key}. A different participant "
        "identifier in the request requires an immediate refusal without a tool call."
    )
    source = f'''\
"""Read-only AIDP data-governance Agent generated for one participant.

Learning map:
1. CONFIG fixes the participant, region, platform, and private catalog boundary.
2. The HTTP helpers sign native AIDP REST calls with a shared OCI credential.
3. Catalog helpers discover current metadata; no table or lineage is hard-coded.
4. The two Agent tools expose focused inventory and lineage evidence.
5. DataGovernanceAgent connects those tools to the selected OCI Generative AI model.

The file is generated during lab assignment. Editing it is useful for experiments,
but an administrative Agent redeploy intentionally replaces local changes.
"""

import json
import logging
import re
from urllib.parse import quote

import oci
import requests
import aidputils
from aidputils.agents.toolkit.agent_helper import init_oci_llm, pre_invoke_setup
from aidputils.agents.toolkit.configs import OCIAIConf
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

# Generated runtime boundary. Secrets are resolved later from Credential Store and
# are never embedded in this file.
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


# ---- Native AIDP REST helpers -------------------------------------------------
def _request(session, signer, method, path, *, params=None, payload=None):
    """Call one read-only AIDP endpoint and return JSON plus response headers."""
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


def _list(session, signer, path, params):
    """Read every page because catalog and job lists can exceed one response."""
    result = []
    page = None
    while True:
        query = {{"limit": "25", **params}}
        if page:
            query["page"] = page
        body, headers = _request(session, signer, "GET", path, params=query)
        result.extend(_items(body))
        page = headers.get("opc-next-page") or headers.get("Opc-Next-Page")
        if not page:
            return result


def _name(value):
    return str(value.get("displayName") or value.get("name") or "")


# ---- Participant catalog discovery and metadata normalization ----------------
def _catalog_contract(session, signer):
    """Resolve the private catalog and its four medallion schemas by live name."""
    catalogs = [
        item
        for item in _list(session, signer, "/catalogs", {{}})
        if _name(item) == CONFIG["catalog_name"]
    ]
    if len(catalogs) != 1 or not catalogs[0].get("key"):
        raise RuntimeError("The participant Master Catalog was not found uniquely")
    catalog_key = str(catalogs[0]["key"])
    schemas = _list(session, signer, "/schemas", {{"catalogKey": catalog_key}})
    by_layer = {{}}
    for layer in LAYERS:
        matches = [item for item in schemas if _name(item) == f"oci_{{layer}}"]
        if len(matches) != 1 or not matches[0].get("key"):
            raise RuntimeError(f"The participant {{layer}} schema was not found uniquely")
        by_layer[layer] = matches[0]
    return catalog_key, by_layer


def _column_record(column):
    """Keep only explanatory, non-sensitive column metadata returned by AIDP."""
    return {{
        "name": str(
            column.get("fieldName")
            or column.get("displayName")
            or column.get("name")
            or ""
        ),
        "type": str(
            column.get("fieldType")
            or column.get("dataType")
            or column.get("type")
            or "unknown"
        ),
        "precision": column.get("fieldPrecision"),
        "scale": column.get("fieldScale"),
        "description": (
            column.get("fieldDescription")
            or column.get("description")
            or column.get("comment")
            or ""
        ),
    }}


def _table_record(item, detail, layer, schema, include_columns):
    """Normalize list and detail responses into one beginner-friendly record."""
    managed = detail.get("managedTableDefinition") or {{}}
    external = detail.get("externalTableDefinition") or {{}}
    columns = (
        detail.get("tableFields")
        or detail.get("columns")
        or detail.get("columnDefinitions")
        or []
    )
    return {{
        "key": str(item.get("key") or detail.get("key") or ""),
        "layer": layer,
        "schema": _name(schema),
        "table": _name(item) or _name(detail),
        "qualified_name": (
            f"aidp://catalogs@{{CONFIG['platform_id']}}/o/"
            f"{{CONFIG['catalog_name']}}.{{_name(schema)}}.{{_name(item) or _name(detail)}}"
        ),
        "description": str(detail.get("description") or ""),
        "table_type": str(detail.get("tableType") or item.get("tableType") or "unknown"),
        "lifecycle_state": str(
            detail.get("lifecycleState") or item.get("lifecycleState") or "unknown"
        ),
        "format": str(
            managed.get("managedTableDataFormat")
            or external.get("externalTableDataFormat")
            or detail.get("dataFormat")
            or detail.get("format")
            or "unknown"
        ),
        "time_created": detail.get("timeCreated") or item.get("timeCreated"),
        "time_updated": detail.get("timeUpdated") or item.get("timeUpdated"),
        "created_by": detail.get("createdBy") or item.get("createdBy"),
        "updated_by": detail.get("updatedBy") or item.get("updatedBy"),
        "columns": [
            _column_record(column)
            for column in columns
            if include_columns and isinstance(column, dict)
        ],
    }}


def _catalog_tables(
    session,
    signer,
    catalog_key,
    schemas,
    layer,
    include_columns=False,
    search_term="",
):
    """List participant tables and optionally fetch detail for focused search."""
    tables = []
    schema = schemas[layer]
    for item in _list(
        session,
        signer,
        "/tables",
        {{"catalogKey": catalog_key, "schemaKey": str(schema["key"])}},
    ):
        table_name = _name(item)
        if not TABLE_NAME.fullmatch(table_name):
            continue
        detail = item
        table_key = str(item.get("key") or "")
        if (include_columns or search_term) and table_key:
            candidate, _ = _request(
                session,
                signer,
                "GET",
                f"/tables/{{quote(table_key, safe='')}}",
            )
            if isinstance(candidate, dict):
                detail = candidate
        record = _table_record(item, detail, layer, schema, include_columns)
        if search_term:
            needle = search_term.casefold()
            searchable = [record["table"], record["description"]]
            searchable.extend(
                f"{{column['name']}} {{column['description']}}"
                for column in record["columns"]
            )
            if not any(needle in value.casefold() for value in searchable):
                continue
            record["matched_columns"] = [
                column["name"]
                for column in record["columns"]
                if needle in f"{{column['name']}} {{column['description']}}".casefold()
            ]
        tables.append(record)
    return tables


def _table_candidates(tables, table_name):
    """Resolve a full or short table name without leaving the participant scope."""
    value = table_name.strip().casefold().rsplit(".", 1)[-1]
    exact = [table for table in tables if table["table"].casefold() == value]
    if exact:
        return exact
    return [
        table
        for table in tables
        if table["table"].casefold().endswith(f"_{{value}}")
    ]


def _job_task_metadata(session, signer, task_nodes):
    """Map observed workflow tasks to their current notebook paths and dependencies."""
    jobs = {{}}
    details = {{}}
    for node in task_nodes:
        defaults = (node.get("properties") or {{}}).get("default") or {{}}
        workspace_key = str(defaults.get("workspaceKey") or "")
        job_key = str(defaults.get("jobKey") or "")
        if not workspace_key or not job_key:
            continue
        reference = (workspace_key, job_key)
        if reference not in jobs:
            job, _ = _request(
                session,
                signer,
                "GET",
                f"/workspaces/{{quote(workspace_key, safe='')}}/jobs/{{quote(job_key, safe='')}}",
            )
            if _contains_foreign_participant(job):
                raise RuntimeError("A lineage job crossed the participant boundary")
            jobs[reference] = job
        for task in jobs[reference].get("tasks") or []:
            if not isinstance(task, dict):
                continue
            task_key = str(task.get("taskKey") or "")
            details[(job_key, task_key)] = {{
                "notebook_path": task.get("notebookPath"),
                "task_type": task.get("type"),
                "depends_on": [
                    str(dependency.get("taskKey") or "")
                    for dependency in task.get("dependsOn") or []
                    if isinstance(dependency, dict)
                ],
            }}
    return details


def _column_lineage_component(graph, table_name, column_name):
    """Select the connected lineage component for one column from a table anchor."""
    nodes = [node for node in graph.get("nodes") or [] if isinstance(node, dict)]
    links = [link for link in graph.get("links") or [] if isinstance(link, dict)]
    seeds = {{
        str(node.get("id") or "")
        for node in nodes
        if str(node.get("displayName") or "").casefold() == column_name.casefold()
        and str(
            ((node.get("properties") or {{}}).get("default") or {{}}).get("tableName")
            or ""
        ).casefold() == table_name.casefold()
    }}
    if not seeds:
        return {{"nodes": [], "links": []}}
    adjacent = {{}}
    for link in links:
        source = str(link.get("fromNodeId") or "")
        target = str(link.get("toNodeId") or "")
        adjacent.setdefault(source, set()).add(target)
        adjacent.setdefault(target, set()).add(source)
    selected = set(seeds)
    pending = list(seeds)
    while pending:
        current = pending.pop()
        for neighbor in adjacent.get(current, set()):
            if neighbor not in selected:
                selected.add(neighbor)
                pending.append(neighbor)
    return {{
        "nodes": [node for node in nodes if str(node.get("id") or "") in selected],
        "links": [
            link
            for link in links
            if str(link.get("fromNodeId") or "") in selected
            and str(link.get("toNodeId") or "") in selected
        ],
    }}


def _lineage_summary(session, signer, graph, process_graph=None):
    """Turn AIDP's node/link graph into ordered evidence an Agent can explain."""
    graph_nodes = [node for node in graph.get("nodes") or [] if isinstance(node, dict)]
    process_nodes = [
        node
        for node in (process_graph or graph).get("nodes") or []
        if isinstance(node, dict)
    ]
    task_nodes = [node for node in process_nodes if str(node.get("type")) == "Task"]
    job_tasks = _job_task_metadata(session, signer, task_nodes)
    task_by_id = {{}}
    task_by_stage = {{}}
    tasks = {{}}
    for node in task_nodes:
        name = str(node.get("displayName") or "")
        defaults = (node.get("properties") or {{}}).get("default") or {{}}
        run = (node.get("properties") or {{}}).get("processRun") or {{}}
        job_key = str(defaults.get("jobKey") or "")
        task_by_id[str(node.get("id") or "")] = name
        if run.get("stageId"):
            task_by_stage[str(run["stageId"])] = name
        current = tasks.setdefault((job_key, name), {{
            "task": name,
            "job_name": defaults.get("jobName"),
            "direction": node.get("direction"),
            "depth": node.get("depth"),
            "process_run_status": run.get("processRunStatus"),
            "process_run_time": run.get("processRunEventTime"),
            **job_tasks.get((job_key, name), {{}}),
        }})
        if run.get("processRunEventTime") and not current.get("process_run_time"):
            current["process_run_time"] = run["processRunEventTime"]

    node_by_id = {{str(node.get("id") or ""): node for node in graph_nodes}}
    entities = {{}}
    for node in graph_nodes:
        if str(node.get("type")) == "Task":
            continue
        defaults = (node.get("properties") or {{}}).get("default") or {{}}
        record = {{
            "name": str(node.get("displayName") or ""),
            "type": str(node.get("type") or "unknown"),
            "qualified_name": node.get("qualifiedName"),
            "layer": defaults.get("databaseName"),
            "table": defaults.get("tableName"),
            "data_type": defaults.get("dataType"),
            "direction": node.get("direction"),
            "depth": node.get("depth"),
        }}
        entities[(record["qualified_name"], record["name"])] = record

    relations = []
    seen_relations = set()
    used_tasks = set()
    for link in graph.get("links") or []:
        if not isinstance(link, dict):
            continue
        source = node_by_id.get(str(link.get("fromNodeId") or ""), {{}})
        target = node_by_id.get(str(link.get("toNodeId") or ""), {{}})
        properties = link.get("properties") or {{}}
        defaults = properties.get("default") or {{}}
        run = properties.get("processRun") or {{}}
        process_task = (
            task_by_id.get(str(defaults.get("processNodeId") or ""))
            or task_by_stage.get(str(run.get("stageId") or ""))
        )
        if process_task:
            used_tasks.add(process_task)
        record = {{
            "from": str(source.get("displayName") or link.get("fromNodeId") or ""),
            "from_type": str(source.get("type") or "unknown"),
            "to": str(target.get("displayName") or link.get("toNodeId") or ""),
            "to_type": str(target.get("type") or "unknown"),
            "process_task": process_task,
            "transformation": defaults.get("transformation"),
        }}
        identity = tuple(record.values())
        if identity not in seen_relations:
            seen_relations.add(identity)
            relations.append(record)

    task_records = list(tasks.values())
    if process_graph is not None:
        task_records = [task for task in task_records if task["task"] in used_tasks]
    timestamps = [
        task["process_run_time"] for task in task_records if task.get("process_run_time")
    ]
    return {{
        "node_count": len(graph_nodes),
        "relation_count": len(relations),
        "entities": sorted(
            entities.values(),
            key=lambda item: (str(item["depth"]), item["type"], item["name"]),
        ),
        "tasks": sorted(task_records, key=lambda item: (str(item["depth"]), item["task"])),
        "relations": relations,
        "latest_observed_process_time": max(timestamps) if timestamps else None,
    }}


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
    return {{"stage": stage, "type": type(exc).__name__, "status": status, "code": code}}


def _contains_foreign_participant(value):
    if isinstance(value, str):
        matches = FOREIGN_TABLE.findall(value)
        return any(
            not match.casefold().startswith(CONFIG["table_prefix"].casefold())
            for match in matches
        )
    if isinstance(value, dict):
        return any(_contains_foreign_participant(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_foreign_participant(item) for item in value)
    return False


def _credential_signer():
    """Build an OCI request signer from the deployment-owned shared credential."""
    try:
        values = {{
            key: aidputils.secrets.get(name=CONFIG["credential_name"], key=key)
            for key in ("tenancy", "user", "fingerprint", "region", "private_key")
        }}
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ValueError("The shared OCI credential is incomplete")
        if values["region"] != CONFIG["region"]:
            raise ValueError("The shared OCI credential region does not match this deployment")
        return oci.signer.Signer(
            tenancy=values["tenancy"],
            user=values["user"],
            fingerprint=values["fingerprint"],
            private_key_file_location=None,
            private_key_content=values["private_key"],
        )
    except Exception as exc:
        raise RuntimeError("The shared OCI credential is unavailable or invalid") from exc


def _tools():
    """Create read-only tools bound to the participant catalog in CONFIG."""
    signer = _credential_signer()
    session = requests.Session()

    @tool
    def catalog_inventory(
        layer: str = "ALL",
        include_columns: bool = False,
        search_term: str = "",
    ) -> str:
        """Find or describe this participant's current Master Catalog tables and fields.

        Use search_term for a table or column name. Set include_columns=true for
        field names, types, precision, scale, and descriptions. Records also show
        format, lifecycle state, and catalog creation/update timestamps.
        """
        selected = layer.strip().lower()
        if selected != "all" and selected not in LAYERS:
            return json.dumps({{"error": "layer must be ALL, LANDING, BRONZE, SILVER, or GOLD"}})
        query = search_term.strip()
        if len(query) > 100:
            return json.dumps({{"error": "search_term must contain at most 100 characters"}})
        if _contains_foreign_participant(query):
            return json.dumps({{"error": "search is outside this participant catalog"}})
        try:
            catalog_key, schemas = _catalog_contract(session, signer)
            layers = LAYERS if selected == "all" else (selected,)
            with_columns = include_columns or bool(query)
            tables = [
                table
                for item in layers
                for table in _catalog_tables(
                    session,
                    signer,
                    catalog_key,
                    schemas,
                    item,
                    with_columns,
                    query,
                )
            ]
            return json.dumps({{
                "participant_key": CONFIG["participant_key"],
                "catalog": CONFIG["catalog_name"],
                "evidence_type": "observed_master_catalog",
                "search_term": query or None,
                "match_count": len(tables),
                "counts_by_layer": {{
                    item: sum(table["layer"] == item for table in tables)
                    for item in layers
                }},
                "tables": sorted(tables, key=lambda item: (item["layer"], item["table"])),
            }}, sort_keys=True)
        except Exception as exc:
            return json.dumps({{"error": _safe_error("catalog_inventory", exc)}}, sort_keys=True)

    @tool
    def catalog_lineage(
        table_name: str,
        lineage_level: str = "ENTITY",
        column_name: str = "",
    ) -> str:
        """Trace one participant table or field through observed AIDP lineage.

        ENTITY returns tables, workflow tasks, notebook paths, dependencies, and
        runs. COLUMN plus column_name returns field derivations and the exact
        processing tasks/notebooks recorded for those relations.
        """
        normalized_name = table_name.strip().lower()
        level = lineage_level.strip().upper()
        requested_column = column_name.strip()
        if (
            not normalized_name
            or re.fullmatch(r"[a-z][a-z0-9_.]*", normalized_name) is None
            or _contains_foreign_participant(normalized_name)
        ):
            return json.dumps({{"error": "table is outside this participant catalog"}})
        if level not in {{"ENTITY", "COLUMN"}}:
            return json.dumps({{"error": "lineage_level must be ENTITY or COLUMN"}})
        if requested_column:
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", requested_column) is None:
                return json.dumps({{"error": "column_name is invalid"}})
            level = "COLUMN"
        try:
            catalog_key, schemas = _catalog_contract(session, signer)
            tables = [
                table
                for layer in LAYERS
                for table in _catalog_tables(
                    session,
                    signer,
                    catalog_key,
                    schemas,
                    layer,
                )
            ]
            matches = _table_candidates(tables, normalized_name)
            if len(matches) != 1:
                return json.dumps({{
                    "error": "table was not found uniquely in this participant catalog",
                    "candidates": sorted(table["table"] for table in matches),
                }})
            table = matches[0]
            anchor = table["qualified_name"]
            resolved_column = None
            if requested_column:
                detail, _ = _request(
                    session,
                    signer,
                    "GET",
                    f"/tables/{{quote(table['key'], safe='')}}",
                )
                columns = [
                    _column_record(column)
                    for column in (
                        detail.get("tableFields")
                        or detail.get("columns")
                        or detail.get("columnDefinitions")
                        or []
                    )
                    if isinstance(column, dict)
                ]
                column_matches = [
                    column
                    for column in columns
                    if column["name"].casefold() == requested_column.casefold()
                ]
                if len(column_matches) != 1:
                    return json.dumps({{
                        "error": "column was not found uniquely in the selected table",
                        "available_columns": sorted(column["name"] for column in columns),
                    }})
                resolved_column = column_matches[0]
            graph, _ = _request(
                session,
                signer,
                "POST",
                "/actions/fetchLineage",
                params={{"limit": "400" if level == "COLUMN" else "100"}},
                payload={{
                    "anchorNode": anchor,
                    "direction": "BOTH",
                    "maxDepth": 8,
                    "level": level,
                    "shouldIncludeEdges": True,
                }},
            )
            if _contains_foreign_participant(graph):
                return json.dumps({{"error": "lineage response crossed the participant boundary"}})
            if resolved_column:
                graph = _column_lineage_component(
                    graph,
                    table["table"],
                    resolved_column["name"],
                )
                if not graph["nodes"]:
                    return json.dumps({{
                        "error": "column exists but no observed column lineage was found"
                    }})
            process_graph = None
            if level == "COLUMN":
                process_graph, _ = _request(
                    session,
                    signer,
                    "POST",
                    "/actions/fetchLineage",
                    params={{"limit": "100"}},
                    payload={{
                        "anchorNode": table["qualified_name"],
                        "direction": "BOTH",
                        "maxDepth": 8,
                        "level": "ENTITY",
                        "shouldIncludeEdges": True,
                    }},
                )
                if _contains_foreign_participant(process_graph):
                    return json.dumps({{"error": "lineage response crossed the participant boundary"}})
            return json.dumps({{
                "participant_key": CONFIG["participant_key"],
                "catalog": CONFIG["catalog_name"],
                "anchor": anchor,
                "table": table["table"],
                "column": resolved_column,
                "level": level,
                "direction": "BOTH",
                "max_depth": 8,
                "evidence_type": "observed_master_catalog_lineage",
                "lineage": _lineage_summary(session, signer, graph, process_graph),
            }}, sort_keys=True)
        except Exception as exc:
            return json.dumps({{"error": _safe_error("catalog_lineage", exc)}}, sort_keys=True)

    return [catalog_inventory, catalog_lineage]


def _error_response(error):
    return {{"messages": [{{"role": "ai", "content": json.dumps({{"agent_error": error}})}}]}}


class DataGovernanceAgent:
    def __init__(self):
        self.llm = None
        self.setup_error = None

    def setup(self):
        try:
            self.llm = init_oci_llm(OCIAIConf(
                model_provider="generic",
                compartment_id=CONFIG["compartment_id"],
                endpoint=f"https://inference.generativeai.{{CONFIG['region']}}.oci.oraclecloud.com",
                model_id=CONFIG["model_id"],
                model_args={{}},
                guardrails_config={{"name": "Data governance", "description": "Participant isolation", "policies": []}},
            ))
        except Exception as exc:
            self.setup_error = _safe_error("setup", exc)
            logger.exception("Governance Agent setup failed")

    async def invoke(self, user_query, **kwargs):
        config = pre_invoke_setup(**kwargs)
        if self.setup_error:
            return _error_response(self.setup_error)
        try:
            agent_args = {{
                "model": self.llm,
                "tools": _tools(),
                "prompt": {system_prompt!r},
                "debug": False,
            }}
            if checkpointer:
                try:
                    agent = create_react_agent(checkpointer=checkpointer, **agent_args)
                except Exception:
                    logger.warning("Checkpointer initialization failed; using a stateless graph", exc_info=True)
                    agent = create_react_agent(**agent_args)
            else:
                agent = create_react_agent(**agent_args)
            message = {{"messages": [dict(HumanMessage(content=user_query))]}}
            return await agent.ainvoke(
                input=message,
                config=config,
            )
        except Exception as exc:
            logger.exception("Governance Agent invocation failed")
            return _error_response(_safe_error("invoke", exc))
'''
    return source.encode("utf-8")
