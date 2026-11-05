"""Fixed, bounded read queries over the approved Governance Delta projections."""

from __future__ import annotations

import asyncio
import re
from threading import Event

from .central_sql import central_projection_result, run_central_projection, metadata_worker
from .inventory import canonical, identity
from .model_output import model_json
from .schema import NAMESPACE


def _query_text(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2000:
        raise ValueError("A bounded query value is required")
    return "decode(unhex('" + value.encode("utf-8").hex() + "'),'UTF-8')"


def metadata_json_options():
    """Keep the full Spark timestamp precision in exact case/policy snapshots."""
    return "map('ignoreNullFields','false','timestampFormat','yyyy-MM-dd HH:mm:ss.SSSSSSXXX')"


def _control_table(name):
    allowed = {"AI_DG_SCOPE_ASSET", "AI_DG_ASSET_VERSION", "AI_DG_ASSET", "AI_DG_SCOPE",
        "AI_DG_EVIDENCE", "AI_DG_ASSESSMENT", "AI_DG_RULE", "AI_DG_CASE", "AI_DG_CASE_ASSET",
        "AI_DG_DECISION", "AI_DG_RUN", "AI_DG_EVENT"}
    if name not in allowed:
        raise ValueError("Unsupported consultation table")
    return ".".join("`" + part + "`" for part in (*NAMESPACE.split("."), name))


def consultation_scope_ids(config):
    scopes = config["source_scopes"]
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 32:
        raise ValueError("A bounded consultation scope set is required")
    result = []
    for scope in scopes:
        if (not isinstance(scope, dict) or set(scope) != {"catalog_key", "schema_key"}
                or not isinstance(scope["catalog_key"], str) or not isinstance(scope["schema_key"], str)
                or not scope["schema_key"].startswith(scope["catalog_key"] + ".")):
            raise ValueError("Invalid consultation source scope")
        result.append(identity("AI_DG_SCOPE", config["platform_id"], config["workspace_key"],
            scope["catalog_key"], scope["schema_key"]))
    if len(set(result)) != len(result):
        raise ValueError("Duplicate consultation scope")
    return tuple(result)


def projection_search(config, mentions, *, asset_ids=None, tables_only=False):
    if not isinstance(mentions, list) or len(mentions) > 32:
        raise ValueError("Too many asset search terms")
    if type(tables_only) is not bool:
        raise ValueError("Table inventory selection requires a boolean")
    scope_ids = consultation_scope_ids(config)
    terms = "SELECT '' AS search_term"
    names = exact = "TRUE"
    if mentions and asset_ids is None:
        terms = "SELECT explode(array(" + ",".join(_query_text(term) for term in mentions) + ")) AS search_term"
        names = "instr(lower(p.qualified_name),lower(terms.search_term))>0"
        exact = "lower(p.qualified_name)=lower(terms.search_term) OR lower(substring_index(p.qualified_name,'.',-1))=lower(terms.search_term)"
    if asset_ids is not None:
        if not isinstance(asset_ids, list) or not 1 <= len(asset_ids) <= 32 or any(
                not isinstance(value, str) or not 1 <= len(value) <= 256 for value in asset_ids):
            raise ValueError("Invalid checkpoint asset identities")
        names = "p.asset_id IN (" + ",".join(_query_text(value) for value in sorted(set(asset_ids))) + ")"
    fields = {"scope_id": "p.scope_id", "asset_id": "p.asset_id", "version_id": "p.verified_version_id",
        "asset_kind": "p.asset_kind", "qualified_name": "p.qualified_name", "description": "p.description",
        "description_origin": "p.description_origin", "data_type": "p.data_type", "owner_ref": "p.owner_ref",
        "metadata_hash": "v.metadata_hash", "coverage": "p.coverage_state", "observed_at": "v.observed_at",
        "table_key": "get_json_object(v.metadata_json,'$.table_key')",
        "column_name": "get_json_object(v.metadata_json,'$.column_name')"}
    payload = ",".join("'" + name + "'," + expr for name, expr in fields.items())
    sql = (f"SELECT to_json(named_struct({payload}),map('ignoreNullFields','false')) AS payload,p.qualified_name AS sort_name,p.asset_id AS sort_id,"
        f"terms.search_term AS search_term,CASE WHEN ({exact}) THEN 0 ELSE 1 END AS priority FROM {_control_table('AI_DG_SCOPE_ASSET')} p "
        f"JOIN {_control_table('AI_DG_ASSET_VERSION')} v ON v.version_id=p.verified_version_id AND v.asset_id=p.asset_id "
        f"JOIN {_control_table('AI_DG_ASSET')} a ON a.asset_id=p.asset_id AND a.current_version_id=v.version_id "
        f"JOIN {_control_table('AI_DG_SCOPE')} s ON s.scope_id=p.scope_id "
        f"CROSS JOIN ({terms}) terms "
        "WHERE p.consistency_state='VERIFIED' AND p.projection_hash=v.metadata_hash AND s.enabled=TRUE "
        "AND s.platform_key=" + _query_text(config["platform_id"]) + " AND s.workspace_key=" + _query_text(config["workspace_key"])
        + " AND p.scope_id IN (" + ",".join(_query_text(scope) for scope in scope_ids) + ")"
        + " AND (" + names + ")"
        + (" AND p.asset_kind='TABLE'" if tables_only and not mentions and asset_ids is None else ""))
    sql = ("WITH matched AS (" + sql + "), ranked AS (SELECT *,min(priority) OVER (PARTITION BY search_term) AS best_priority FROM matched) "
        "SELECT payload FROM ranked WHERE priority=best_priority GROUP BY payload,sort_name,sort_id ORDER BY sort_name,sort_id LIMIT 33")
    return _bounded_statement(sql)


def _admitted_versions(rows, *, alias):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError("An admitted asset set is required")
    pairs = set()
    for row in rows:
        scope, version = row.get("scope_id"), row.get("version_id")
        if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in (scope, version)):
            raise ValueError("Invalid admitted asset revision")
        pairs.add((scope, version))
    return "(" + " OR ".join(f"({alias}.scope_id={_query_text(scope)} AND {alias}.asset_version_id={_query_text(version)})"
        for scope, version in sorted(pairs)) + ")"


def projection_evidence(rows, kinds):
    allowed = {"STATIC_CODE", "EXECUTION", "RUNTIME_LINEAGE", "ACCESS_OBSERVATION"}
    if not isinstance(kinds, tuple) or not kinds or not set(kinds).issubset(allowed):
        raise ValueError("Unsupported consultation evidence kind")
    payload = "'evidence_id',e.evidence_id,'scope_id',e.scope_id,'version_id',e.asset_version_id,'kind',e.evidence_kind," \
        "'facts_json',e.facts_json,'coverage',e.coverage_state,'observed_at',e.observed_at,'expires_at',e.expires_at"
    sql = f"SELECT to_json(named_struct({payload}),map('ignoreNullFields','false')) AS payload FROM {_control_table('AI_DG_EVIDENCE')} e WHERE "
    sql += _admitted_versions(rows, alias="e") + " AND e.evidence_kind IN (" + ",".join(_query_text(kind) for kind in kinds) + ")"
    sql += " AND (e.expires_at IS NULL OR e.expires_at>current_timestamp()) ORDER BY e.observed_at DESC,e.evidence_id LIMIT 129"
    return _bounded_statement(sql)


def projection_quality(rows):
    # The scope comes from the versioned rule; assessment alone has no scope_id.
    revisions = _admitted_versions(rows, alias="e")
    payload = "'assessment_id',q.assessment_id,'rule_id',q.rule_id,'rule_version',q.rule_version," \
        "'version_id',q.asset_version_id,'scope_id',r.scope_id,'status',q.status,'result_json',q.result_json," \
        "'evidence_id',e.evidence_id,'rule_kind',r.rule_kind,'evidence_kind',e.evidence_kind," \
        "'coverage',e.coverage_state,'measured_at',q.measured_at,'expires_at',q.expires_at"
    sql = (f"SELECT to_json(named_struct({payload}),map('ignoreNullFields','false')) AS payload FROM {_control_table('AI_DG_ASSESSMENT')} q "
        f"JOIN {_control_table('AI_DG_RULE')} r ON r.rule_id=q.rule_id AND r.rule_version=q.rule_version "
        f"JOIN {_control_table('AI_DG_EVIDENCE')} e ON e.evidence_id=q.evidence_id AND e.asset_version_id=q.asset_version_id AND e.scope_id=r.scope_id "
        f"JOIN {_control_table('AI_DG_ASSET_VERSION')} v ON v.version_id=q.asset_version_id "
        "WHERE " + revisions + " AND q.expires_at>current_timestamp() AND q.measured_at<=current_timestamp() "
        "AND r.rule_kind='QUALITY' AND e.evidence_kind='QUALITY' "
        "AND (r.target_asset_id IS NULL OR r.target_asset_id=v.asset_id) AND r.valid_from<=q.measured_at "
        "AND (r.valid_to IS NULL OR r.valid_to>current_timestamp()) "
        "AND (e.expires_at IS NULL OR e.expires_at>current_timestamp()) "
        "ORDER BY q.measured_at DESC,q.assessment_id LIMIT 129")
    return _bounded_statement(sql)


def _bounded_statement(sql):
    if not sql.startswith(("SELECT ", "WITH matched AS (SELECT ")) or ";" in sql or len(sql.encode("utf-8")) > 131072:
        raise ValueError("Consultation query exceeds its read-only bound")
    return sql


def decode_projection_rows(response, limit):
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError("Invalid consultation row bound")
    result = _projection_result(response)
    rows = result.get("rows", result.get("result"))
    if not isinstance(rows, list) or len(rows) > limit:
        raise ValueError("Governance query is incomplete or exceeds its row bound")
    decoded = []
    for row in rows:
        if not isinstance(row, dict) or {"payload", "PAYLOAD"}.issubset(row):
            raise ValueError("Invalid Governance projection row")
        payload = row.get("payload", row.get("PAYLOAD"))
        if not isinstance(payload, str):
            raise ValueError("Invalid Governance projection row")
        decoded.append(model_json(payload, max_bytes=262144))
    return decoded


def _projection_result(response):
    """Validate the SQLTool envelope before decoding individual projected rows."""
    if isinstance(response, str):
        response = model_json(response, max_bytes=2097152)
    if not isinstance(response, dict) or len(canonical(response).encode()) > 2097152:
        raise ValueError("Invalid consultation query response")
    result = response
    counts = []
    for name in ("result", "structuredContent"):
        _check_projection_error(result)
        counts.extend(_projection_counts(result))
        if isinstance(result.get(name), dict):
            result = result[name]
    _check_projection_error(result)
    counts.extend(_projection_counts(result))
    rows = result.get("rows", result.get("result"))
    if counts and (not isinstance(rows, list) or any(count != len(rows) for count in counts)):
        raise ValueError("Governance query row count is inconsistent")
    return result


def _projection_counts(result):
    counts = []
    for record in (result, result.get("metadata") or {}):
        if "results_truncated" in record and record["results_truncated"] is not False:
            raise ValueError("Governance query is truncated or has invalid completeness metadata")
        if "rows_fetched" in record:
            count = record["rows_fetched"]
            if type(count) is not int or count < 0:
                raise ValueError("Invalid Governance query row count")
            counts.append(count)
    return counts


def _check_projection_error(result):
    metadata = result.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("Invalid Governance query metadata")
    if result.get("error") or result.get("isError") or (metadata or {}).get("error"):
        raise RuntimeError("Governance projection query failed")


async def native_projection_query(config, sql, *, limit, transport):
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError("Invalid consultation row bound")
    _bounded_statement(sql)
    if not sql.endswith(" LIMIT " + str(limit + 1)):
        raise ValueError("A bounded SQL overflow sentinel is required")
    transport.check_identity()
    if getattr(transport, "access_mode", None) != "CENTRAL_METADATA":
        raise PermissionError("The installed central metadata identity is required")
    cancelled = Event()
    def check_active():
        if cancelled.is_set():
            raise InterruptedError("Governance SQL query was cancelled")
        transport.check_identity()
    result = await metadata_worker(run_central_projection, config, transport.signer, sql, check_active, cancelled=cancelled)
    check_active()
    return decode_projection_rows(central_projection_result(result), limit)
