"""Installation-admitted declarations govern case registration, never data access."""

from datetime import datetime
import re

from .case_changes import case_time
from .contracts import CaseAuthorityDeclaration
from .inventory import canonical, identity
from .model_output import model_json
from .projection_query import _bounded_statement, _control_table, _query_text, consultation_scope_ids, metadata_json_options


def case_authority_rule_id(binding, scope_id, actor_ref):
    if scope_id not in consultation_scope_ids(binding) or not isinstance(actor_ref, str) or not re.fullmatch(r"[0-9a-f]{64}", actor_ref):
        raise PermissionError("Case authority requires a configured scope and native actor digest")
    return identity("AI_DG_RULE", "CASE_REGISTRATION", binding["platform_id"], binding["workspace_key"], scope_id, actor_ref)


def case_authority_query(binding, scope_id, actor_ref):
    key = case_authority_rule_id(binding, scope_id, actor_ref)
    rule_fields = ("rule_id", "rule_version", "scope_id", "target_asset_id", "rule_kind", "criteria_json", "owner_ref", "valid_from", "valid_to")
    scope_fields = ("scope_id", "platform_key", "workspace_key", "catalog_key", "schema_key", "enabled")
    rule = ",".join(f"'{name}',r.{name}" for name in rule_fields)
    scope = ",".join(f"'{name}',s.{name}" for name in scope_fields)
    sql = (f"SELECT to_json(named_struct('scope',named_struct({scope}),'rule',named_struct({rule})),{metadata_json_options()}) AS payload "
        f"FROM {_control_table('AI_DG_RULE')} r JOIN {_control_table('AI_DG_SCOPE')} s ON s.scope_id=r.scope_id "
        f"WHERE r.rule_id={_query_text(key)} AND r.scope_id={_query_text(scope_id)} "
        f"AND s.platform_key={_query_text(binding['platform_id'])} AND s.workspace_key={_query_text(binding['workspace_key'])} "
        "AND s.enabled=TRUE AND (r.valid_from IS NULL OR r.valid_from<=current_timestamp()) "
        "AND (r.valid_to IS NULL OR r.valid_to>current_timestamp()) "
        "ORDER BY r.rule_version LIMIT 3")
    return _bounded_statement(sql)


def checked_case_authority_record(binding, row, scope_id, actor_ref, action, *, now):
    key = case_authority_rule_id(binding, scope_id, actor_ref)
    scope_fields = {"scope_id", "platform_key", "workspace_key", "catalog_key", "schema_key", "enabled"}
    rule_fields = {"rule_id", "rule_version", "scope_id", "target_asset_id", "rule_kind", "criteria_json", "owner_ref", "valid_from", "valid_to"}
    if not isinstance(row, dict) or set(row) != {"scope", "rule"} or set(row["scope"]) != scope_fields or set(row["rule"]) != rule_fields:
        raise PermissionError("Case authority result has an unsupported shape")
    scope, rule = row["scope"], row["rule"]
    expected = {"scope_id": scope_id, "platform_key": binding["platform_id"], "workspace_key": binding["workspace_key"], "enabled": True}
    if (any(scope[name] != value for name, value in expected.items()) or scope["enabled"] is not True
            or {"catalog_key": scope["catalog_key"], "schema_key": scope["schema_key"]} not in binding["source_scopes"]
            or identity("AI_DG_SCOPE", binding["platform_id"], binding["workspace_key"], scope["catalog_key"], scope["schema_key"]) != scope_id):
        raise PermissionError("Case authority belongs to another installation or scope")
    if (rule["rule_id"] != key or rule["scope_id"] != scope_id or rule["target_asset_id"] is not None
            or rule["rule_kind"] != "ACCESS_DECLARATION" or type(rule["rule_version"]) is not int or not 1 <= rule["rule_version"] < 2**31):
        raise PermissionError("Case authority declaration is not a scope-level versioned rule")
    return checked_case_authority_criteria(rule, actor_ref, action, now=now)


def checked_case_authority_criteria(rule, actor_ref, action, *, now):
    criteria = CaseAuthorityDeclaration.model_validate(model_json(rule["criteria_json"], max_bytes=8192))
    if criteria.actor_ref != actor_ref or action not in criteria.actions:
        raise PermissionError("The declaration does not permit this actor and case action")
    start, end = datetime.fromisoformat(rule["valid_from"]), datetime.fromisoformat(rule["valid_to"])
    if now.utcoffset() is None or start.utcoffset() is None or end.utcoffset() is None or not start <= now < end:
        raise PermissionError("Case authority declaration is not currently valid")
    return {**rule, "valid_from": case_time(start), "valid_to": case_time(end)}


def case_authority_installation_receipt(binding, rule):
    """The operator persists this only after an explicitly admitted rule installation."""
    return {"schema_version": 1, "kind": "GOVERNANCE_CASE_AUTHORITY_INSTALLATION",
        "platform_key": binding["platform_id"], "workspace_key": binding["workspace_key"],
        "scope_id": rule["scope_id"], "rule_id": rule["rule_id"], "rule_version": rule["rule_version"],
        "rule_digest": identity("case-authority-rule", rule)}


def case_registration_policy(binding, rows, receipt, scope_id, actor_ref, action, *, now):
    if not isinstance(rows, list) or len(rows) != 1:
        raise PermissionError("Exactly one active case authority version is required")
    rule = checked_case_authority_record(binding, rows[0], scope_id, actor_ref, action, now=now)
    expected = case_authority_installation_receipt(binding, rule)
    if not isinstance(receipt, dict) or canonical(receipt) != canonical(expected):
        raise PermissionError("Case authority has no exact admitted installation receipt")
    return {"policy_version": "CASE_AUTHORITY:" + rule["rule_id"] + ":" + str(rule["rule_version"]) + ":" + expected["rule_digest"],
        "expires_at": rule["valid_to"]}


async def native_case_policy(binding, actor_ref, scope_id, action, *, query, journal, journal_call, now):
    """Resolve fresh metadata and installation proof, without changing declarations."""
    rows = await query(case_authority_query(binding, scope_id, actor_ref), limit=2)
    if not isinstance(rows, list) or len(rows) != 1:
        raise PermissionError("Exactly one active case authority version is required")
    rule = checked_case_authority_record(binding, rows[0], scope_id, actor_ref, action, now=now())
    key = identity("case-authority-installed", rule["rule_id"], rule["rule_version"])
    receipt = await journal_call(journal.read, "materialized", key)
    return case_registration_policy(binding, rows, receipt, scope_id, actor_ref, action, now=now())
