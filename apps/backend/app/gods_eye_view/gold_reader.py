"""Versioned Gold reads through the native AIDP Spark SQL tool."""
import json
import math
import re


def statement(sql, binds, catalog):
    if not isinstance(catalog, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
        raise ValueError("Invalid Gold catalog")
    if not sql.lstrip().upper().startswith("SELECT ") or ";" in sql:
        raise ValueError("Gold queries must be fixed SELECT statements")
    if set(re.findall(r":([a-z_][a-z_0-9]*)", sql)) != set(binds):
        raise ValueError("Gold query parameters do not match")

    def literal(match):
        value = binds[match[1]]
        if value is None:
            return "NULL"
        if isinstance(value, str) and len(value) <= 200:
            # ponytail: hex literals avoid SQLTool's Oracle bind heuristics and Spark string escapes.
            return "decode(unhex('" + value.encode("utf-8").hex() + "'),'UTF-8')"
        if type(value) in (int, float) and math.isfinite(value):
            return str(value)
        raise ValueError("Invalid Gold query parameter")

    # Retain the installed Gold views; only the agent's logical query names changed.
    sql = re.sub(r"\bgods_eye_view_(incidents|evidence|sensors|event_posts)\b",
                 lambda match: f"`{catalog}`.`oci_gold`.`territorial_{match[1]}`", sql)
    return re.sub(r":([a-z_][a-z_0-9]*)", literal, sql)


def query(config, sql, binds):
    from aidputils.agents.toolkit.configs import AIDPToolConf
    from aidputils.agents.toolkit.tool_helper import create_langgraph_tool

    compute = config.get("gold_query_compute_id")
    if not isinstance(compute, str) or not compute or len(compute) > 200:
        raise RuntimeError("Gold query compute is not configured")
    native = create_langgraph_tool(AIDPToolConf(name="gods_eye_view_gold", description="Read the requested Gold publication",
        tool_class="SQLTool", conf={"catalogType": "STANDARD", "clusterKey": compute,
            "catalogKey": config["catalog"], "schemaKey": "oci_gold",
            "query": statement(sql, binds, config.get("catalog")), "isRowLimitEnabled": True, "maxRows": 101}, params=[]).model_dump())
    response = native.invoke({})
    if isinstance(response, str):
        response = json.loads(response)
    if not isinstance(response, dict) or response.get("isError") or response.get("error") or (response.get("metadata") or {}).get("error"):
        raise RuntimeError("Gold Spark query failed")
    result = response.get("result", response)
    if isinstance(result, dict):
        result = result.get("structuredContent", result)
    if not isinstance(result, dict) or result.get("error") or (result.get("metadata") or {}).get("error"):
        raise RuntimeError("Gold Spark query failed")
    rows = result.get("rows", result.get("result"))
    if not isinstance(rows, list) or len(rows) > 100:
        raise RuntimeError("Gold Spark query returned an invalid result")
    decoded = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Gold Spark query returned an invalid row")
        payload = row.get("payload", row.get("PAYLOAD"))
        item = json.loads(payload) if isinstance(payload, str) else None
        if not isinstance(item, dict):
            raise RuntimeError("Gold Spark query returned an invalid payload")
        decoded.append(item)
    return decoded
