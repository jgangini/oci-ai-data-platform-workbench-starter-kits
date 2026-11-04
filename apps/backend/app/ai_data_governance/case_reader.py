"""Bounded case-history reads, independent of the asset's current catalog revision."""

import re
from datetime import datetime

from .inventory import canonical, identity
from .model_output import model_json
from .projection_query import _bounded_statement, _control_table, _query_text, consultation_scope_ids, metadata_json_options
from .contracts import TreatmentDraft


def case_columns():
    return {
        "CASE": ("case_id", "scope_id", "opened_run_id", "summary", "owner_ref", "status", "change_digest",
            "treatment_json", "closure_evidence_id", "created_at", "updated_at"),
        "ASSET": ("asset_id", "assessed_version_id", "impact_role", "evidence_id", "qualified_name", "admitted"),
        "DECISION": ("decision_id", "case_id", "run_id", "actor_ref", "decision", "change_digest",
            "versions_digest", "policy_version", "valid_until", "recorded_at"),
        "RUN": ("run_id", "scope_id", "parent_run_id", "agent_id", "run_kind", "execution_key", "attempt_no",
            "component_version", "native_run_ref", "status", "started_at", "finished_at"),
        "EVENT": ("event_id", "run_id", "event_key", "parent_event_id", "event_type", "asset_id",
            "occurred_at", "ingested_at", "summary"),
    }


def checked_case_reference(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("An exact Governance case reference is required")
    return value


def case_history_query(binding, case_id):
    checked_case_reference(case_id)
    scopes = ",".join(_query_text(scope) for scope in consultation_scope_ids(binding))
    matched = (f"SELECT c.* FROM {_control_table('AI_DG_CASE')} c JOIN {_control_table('AI_DG_SCOPE')} s ON s.scope_id=c.scope_id "
        f"WHERE c.case_id={_query_text(case_id)} AND s.scope_id IN ({scopes}) "
        f"AND s.platform_key={_query_text(binding['platform_id'])} AND s.workspace_key={_query_text(binding['workspace_key'])} LIMIT 2")
    admitted = "a.asset_id IS NOT NULL AND p.asset_id IS NOT NULL AND v.version_id IS NOT NULL"
    links = ("SELECT c.case_id," + ",".join(f"CASE WHEN {admitted} THEN {expr} ELSE NULL END AS {name}" for name,expr in (
        ("asset_id","ca.asset_id"), ("assessed_version_id","ca.assessed_version_id"), ("impact_role","ca.impact_role"),
        ("evidence_id","ca.evidence_id"), ("qualified_name","v.qualified_name")))
        + f",({admitted}) AS admitted FROM matched c JOIN {_control_table('AI_DG_CASE_ASSET')} ca ON ca.case_id=c.case_id "
        + f"LEFT JOIN {_control_table('AI_DG_ASSET')} a ON a.asset_id=ca.asset_id AND a.platform_key={_query_text(binding['platform_id'])} "
        + f"LEFT JOIN {_control_table('AI_DG_SCOPE_ASSET')} p ON p.asset_id=a.asset_id AND p.scope_id=c.scope_id "
        + f"LEFT JOIN {_control_table('AI_DG_ASSET_VERSION')} v ON v.asset_id=a.asset_id AND v.version_id=ca.assessed_version_id "
        + "ORDER BY ca.asset_id LIMIT 17")
    decisions = (f"SELECT d.* FROM {_control_table('AI_DG_DECISION')} d JOIN matched c ON d.case_id=c.case_id "
        "ORDER BY d.recorded_at DESC,d.decision_id LIMIT 9")
    runs = (f"SELECT r.*,c.case_id FROM {_control_table('AI_DG_RUN')} r JOIN matched c "
        "ON r.run_id=c.opened_run_id AND r.scope_id=c.scope_id LIMIT 2")
    events = (f"SELECT e.*,r.case_id FROM {_control_table('AI_DG_EVENT')} e JOIN opening_runs r ON e.run_id=r.run_id "
        "ORDER BY e.occurred_at DESC,e.event_id LIMIT 17")
    sources = {"CASE":"matched", "ASSET":"case_assets", "DECISION":"case_decisions", "RUN":"opening_runs", "EVENT":"case_events"}
    projections = []
    for kind, fields in case_columns().items():
        data = ",".join(f"'{field}',{field}" for field in fields)
        projections.append(f"SELECT to_json(named_struct('record_kind','{kind}','case_id',case_id,'data',"
            f"to_json(named_struct({data}),{metadata_json_options()})),map('ignoreNullFields','false')) AS payload FROM {sources[kind]}")
    return _bounded_statement(f"WITH matched AS ({matched}),case_assets AS ({links}),case_decisions AS ({decisions}),"
        f"opening_runs AS ({runs}),case_events AS ({events}) SELECT payload FROM (" + " UNION ALL ".join(projections) + ") records LIMIT 49")


def case_history_groups(binding, case_id, rows):
    checked_case_reference(case_id)
    if not isinstance(rows, list) or len(rows) > 48 or len(canonical(rows).encode()) > 131072:
        raise ValueError("Case history exceeds its budget")
    columns = case_columns()
    grouped = {kind:[] for kind in columns}
    for item in rows:
        kind, record = case_history_item(case_id, item)
        grouped[kind].append(record)
    if not grouped["CASE"]:
        if rows:
            raise ValueError("Case history has no parent record")
        return None
    if len(grouped["CASE"]) != 1 or len(grouped["RUN"]) > 1:
        raise ValueError("Case history contains duplicate parent records")
    case = grouped["CASE"][0]
    if case["case_id"] != case_id or case["scope_id"] not in consultation_scope_ids(binding):
        raise ValueError("Case escaped its admitted scope")
    case_history_children(case, grouped)
    return grouped


def case_history_item(case_id, item):
    columns = case_columns()
    if (not isinstance(item, dict) or set(item) != {"record_kind", "case_id", "data"}
            or item["record_kind"] not in columns or item["case_id"] != case_id):
        raise ValueError("Case history returned a mismatched identity")
    record = model_json(item["data"], max_bytes=32768)
    if set(record) != set(columns[item["record_kind"]]):
        raise ValueError("Case history fields differ from the admitted projection")
    checked_case_record(item["record_kind"], record)
    return item["record_kind"], record


def case_history_snapshot(binding, case_id, rows):
    grouped = case_history_groups(binding, case_id, rows)
    if grouped is None:
        return None
    case = grouped["CASE"][0]
    treatment, treatment_valid = case_treatment(case["treatment_json"])
    truncated = len(grouped["ASSET"]) > 16 or len(grouped["DECISION"]) > 8 or len(grouped["EVENT"]) > 16
    links = [row for row in sorted(grouped["ASSET"],key=lambda row:row["asset_id"] or "")[:16] if row["admitted"] is True]
    omitted = sum(row["admitted"] is not True for row in grouped["ASSET"])
    facts = {"case":{key:value for key,value in case.items() if key != "treatment_json"}, "treatment":treatment,
        "asset_links":links, "decisions":case_recent_records(grouped["DECISION"],"recorded_at","decision_id",8),
        "opening_run":grouped["RUN"][0] if grouped["RUN"] else None,
        "events":case_recent_records(grouped["EVENT"],"occurred_at","event_id",16),
        "history_truncated":truncated, "asset_links_truncated":len(grouped["ASSET"]) > 16, "omitted_asset_links":omitted,
        "treatment_format_supported":treatment_valid,
        "basis":"STORED_CASE_HISTORY", "execution_verified":False, "decision_execution_verified":False}
    resource = {"asset_id":"case:" + case_id, "version_id":identity("case-history-snapshot", facts), "name":"Caso " + case_id}
    proof = {"evidence_id":identity("case-history-proof", resource), "asset_id":resource["asset_id"],
        "version_id":resource["version_id"], "kind":"CASE_RECORD", "facts":facts,
        "coverage":"partial" if truncated or omitted or not grouped["RUN"] or not treatment_valid else "complete"}
    return {"resource":resource, "proof":proof}


def checked_case_record(kind, record):
    nullable = {"CASE":{"owner_ref","change_digest","treatment_json","closure_evidence_id"},
        "ASSET":{"evidence_id"}, "DECISION":set(), "RUN":{"parent_run_id","agent_id","native_run_ref","finished_at"},
        "EVENT":{"parent_event_id","asset_id"}}[kind]
    if kind == "ASSET":
        if type(record["admitted"]) is not bool:
            raise ValueError("Invalid case asset admission")
        if not record["admitted"]:
            if any(value is not None for key,value in record.items() if key != "admitted"):
                raise ValueError("Unadmitted case assets must not expose their metadata")
            return
    for key,value in record.items():
        if key == "admitted" or value is None and key in nullable:
            continue
        checked_case_value(key, value)


def checked_case_value(key, value):
    if key == "attempt_no":
        if type(value) is not int or not 0 <= value < 2147483648:
            raise ValueError("Invalid case run attempt")
    elif not isinstance(value,str) or not value.strip() or len(value) > (16384 if key == "treatment_json" else 2000):
        raise ValueError("Invalid case metadata value")
    elif key.endswith("_at") or key == "valid_until":
        if datetime.fromisoformat(value.replace("Z","+00:00")).utcoffset() is None:
            raise ValueError("Case history timestamps require a timezone")


def case_recent_records(rows, at_key, identity_key, limit):
    return sorted(rows,key=lambda row:(-datetime.fromisoformat(row[at_key].replace("Z","+00:00")).timestamp(),row[identity_key]))[:limit]


def case_treatment(value):
    if value is None:
        return None, True
    try:
        draft = TreatmentDraft.model_validate_json(value)
    except ValueError:
        return None, False
    return draft.model_dump(mode="json"), True


def case_history_children(case, grouped):
    for kind, limit, key in (("ASSET",17,"asset_id"), ("DECISION",9,"decision_id"), ("EVENT",17,"event_id")):
        values = grouped[kind]
        if len(values) > limit:
            raise ValueError("Case history group exceeds its bound")
        admitted = [row for row in values if kind != "ASSET" or row["admitted"] is True]
        ids = [row[key] for row in admitted]
        if any(not isinstance(value,str) or not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("Case history contains duplicate or invalid child identities")
    if any(row["case_id"] != case["case_id"] for row in grouped["DECISION"]):
        raise ValueError("Decision belongs to another case")
    if grouped["RUN"]:
        run = grouped["RUN"][0]
        if run["run_id"] != case["opened_run_id"] or run["scope_id"] != case["scope_id"]:
            raise ValueError("Opening run belongs to another case scope")
    if any(row["run_id"] != case["opened_run_id"] for row in grouped["EVENT"]):
        raise ValueError("Event belongs to another case run")
