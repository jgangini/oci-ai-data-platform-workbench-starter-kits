from copy import deepcopy
import asyncio
from datetime import timedelta

import pytest

from app.ai_data_governance.case_policy import (case_authority_rule_id, case_authority_query,
    checked_case_authority_record, case_authority_installation_receipt, case_registration_policy, native_case_policy)
from app.ai_data_governance.inventory import canonical, identity
from test_ai_dg_case_changes import AT
from test_ai_dg_case_program import command_fixture


def fixture():
    binding, change, _, _, _, _, _ = command_fixture()
    scope_id, actor = change["scope_id"], change["actor_ref"]
    scope = binding["source_scopes"][0]
    rule = {"rule_id": case_authority_rule_id(binding, scope_id, actor), "rule_version": 1, "scope_id": scope_id,
        "target_asset_id": None, "rule_kind": "ACCESS_DECLARATION", "owner_ref": "declared-steward",
        "criteria_json": canonical({"schema_version": 1, "purpose": "CASE_REGISTRATION", "actor_ref": actor, "actions": ["OPEN", "PROPOSE"]}),
        "valid_from": (AT-timedelta(minutes=1)).isoformat(), "valid_to": (AT+timedelta(hours=1)).isoformat()}
    row = {"scope": {"scope_id": scope_id, "platform_key": binding["platform_id"], "workspace_key": binding["workspace_key"],
        "enabled": True, **scope}, "rule": rule}
    normalized = checked_case_authority_record(binding, row, scope_id, actor, "OPEN", now=AT)
    receipt = case_authority_installation_receipt(binding, normalized)
    return binding, row, receipt, scope_id, actor


def test_case_authority_is_a_specific_installed_declaration_not_a_business_permission():
    binding, row, receipt, scope, actor = fixture()
    policy = case_registration_policy(binding, [row], receipt, scope, actor, "OPEN", now=AT)
    assert policy["policy_version"].endswith(receipt["rule_digest"])
    assert len(policy["policy_version"]) <= 256
    assert policy["expires_at"] == (AT+timedelta(hours=1)).isoformat(timespec="microseconds")
    assert case_registration_policy(binding, [row], receipt, scope, actor, "PROPOSE", now=AT) == policy


@pytest.mark.parametrize("section,field,value", [
    ("scope", "schema_key", "another.schema"),
    ("scope", "platform_key", "another-platform"),
    ("scope", "workspace_key", "another-workspace"),
    ("scope", "enabled", 1),
    ("rule", "target_asset_id", "one-asset"),
    ("rule", "rule_kind", "QUALITY"),
    ("rule", "rule_version", True),
    ("rule", "criteria_json", lambda actor: canonical({"group": actor, "has_access": True})),
    ("rule", "criteria_json", lambda actor: canonical({"schema_version": 1, "purpose": "CASE_REGISTRATION", "actor_ref": actor, "actions": ["*"]})),
    ("rule", "criteria_json", lambda actor: canonical({"schema_version": 1, "purpose": "CASE_REGISTRATION", "actor_ref": actor, "actions": ["OPEN", "OPEN"]})),
    ("rule", "criteria_json", lambda actor: canonical({"schema_version": 1, "purpose": "CASE_REGISTRATION", "actor_ref": actor, "actions": ["OPEN"], "confirmed": True})),
    ("rule", "criteria_json", lambda actor: canonical({"purpose": "CASE_REGISTRATION", "actor_ref": actor, "actions": ["OPEN"]})),
    ("rule", "valid_from", (AT+timedelta(seconds=1)).isoformat()),
    ("rule", "valid_to", AT.isoformat()),
    ("rule", "valid_to", None),
])
def test_case_authority_rejects_legacy_owner_only_foreign_and_expired_declarations(section, field, value):
    binding, row, receipt, scope, actor = fixture()
    row[section][field] = value(actor) if callable(value) else value
    with pytest.raises((PermissionError, TypeError, ValueError)):
        checked_case_authority_record(binding, row, scope, actor, "OPEN", now=AT)
    with pytest.raises((PermissionError, TypeError, ValueError)):
        case_registration_policy(binding, [row], receipt, scope, actor, "OPEN", now=AT)


def test_case_authority_does_not_transfer_to_another_native_actor_or_action():
    binding, row, receipt, scope, actor = fixture()
    with pytest.raises(PermissionError):
        case_registration_policy(binding, [row], receipt, scope, "f" * 64, "OPEN", now=AT)
    with pytest.raises(PermissionError):
        case_registration_policy(binding, [row], receipt, scope, actor, "APPROVE", now=AT)


@pytest.mark.parametrize("mutation", ["absent", "different", "scope", "version", "boolean_version", "boolean_schema"])
def test_a_declared_rule_without_its_exact_admitted_installation_does_not_grant(mutation):
    binding, row, receipt, scope, actor = fixture()
    if mutation == "absent": receipt = None
    elif mutation == "different": receipt["rule_digest"] = "f" * 64
    elif mutation == "scope": receipt["scope_id"] = "foreign"
    elif mutation == "version": receipt["rule_version"] += 1
    elif mutation == "boolean_version": receipt["rule_version"] = True
    else: receipt["schema_version"] = True
    with pytest.raises(PermissionError): case_registration_policy(binding, [row], receipt, scope, actor, "OPEN", now=AT)


def test_overlapping_active_rule_versions_are_not_resolved_by_picking_the_largest():
    binding, row, receipt, scope, actor = fixture()
    newer = deepcopy(row); newer["rule"]["rule_version"] = 2
    with pytest.raises(PermissionError, match="Exactly one"):
        case_registration_policy(binding, [row, newer], receipt, scope, actor, "OPEN", now=AT)
    with pytest.raises(PermissionError): case_registration_policy(binding, [], receipt, scope, actor, "OPEN", now=AT)


def test_case_authority_query_is_fixed_scope_bound_and_retains_the_overflow_sentinel():
    binding, _, _, scope, actor = fixture()
    sql = case_authority_query(binding, scope, actor)
    assert "`AI_DG_RULE`" in sql and "`AI_DG_SCOPE`" in sql and sql.endswith(" LIMIT 3")
    assert "timestampFormat','yyyy-MM-dd HH:mm:ss.SSSSSSXXX" in sql
    assert "MAX(" not in sql and "row_number" not in sql and ";" not in sql
    assert "r.valid_from<=current_timestamp()" in sql and "r.valid_to>current_timestamp()" in sql
    assert "has_access" not in sql and "owner_ref=" not in sql and "limits_json" not in sql
    assert case_authority_rule_id(binding, scope, actor) == identity("AI_DG_RULE", "CASE_REGISTRATION", binding["platform_id"], binding["workspace_key"], scope, actor)


def test_native_policy_reader_rechecks_fresh_expiry_after_journal_read():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    binding, row, receipt, scope, actor = fixture()
    query = AsyncMock(return_value=[row])
    journal = SimpleNamespace(read=Mock(return_value=receipt))
    time = [AT]
    async def call(function, *args): return function(*args)
    def read():
        return asyncio.run(native_case_policy(binding, actor, scope, "OPEN", query=query,
            journal=journal, journal_call=call, now=lambda: time[0]))
    assert read() == case_registration_policy(binding, [row], receipt, scope, actor, "OPEN", now=AT)
    query.assert_awaited_once_with(case_authority_query(binding, scope, actor), limit=2)
    journal.read.assert_called_once_with("materialized", identity("case-authority-installed", row["rule"]["rule_id"], 1))
    def expire(*_):
        time[0] = AT+timedelta(hours=2)
        return receipt
    journal.read.side_effect = expire
    with pytest.raises(PermissionError): read()
    query.return_value = [row, deepcopy(row)]
    journal.read.reset_mock()
    with pytest.raises(PermissionError): read()
    journal.read.assert_not_called()


@pytest.mark.parametrize("kind,target,start,end", [
    ("QUALITY", None, "2000-01-01", "9999-01-01"),
    ("ACCESS_DECLARATION", "one-asset", "2000-01-01", "9999-01-01"),
    ("ACCESS_DECLARATION", None, "2000-01-01", None),
    ("ACCESS_DECLARATION", None, None, "9999-01-01"),
])
def test_query_does_not_hide_malformed_competing_versions_before_uniqueness_check(kind, target, start, end):
    import sqlite3

    binding, row, _, scope, actor = fixture()
    query = case_authority_query(binding, scope, actor)
    predicate = query.split(" WHERE ", 1)[1].rsplit(" ORDER BY ", 1)[0].replace("current_timestamp()", "CURRENT_TIMESTAMP")
    # Execute the actual WHERE predicate; only Spark's time-call syntax differs here.
    with sqlite3.connect(":memory:") as db:
        db.create_function("unhex", 1, bytes.fromhex)
        db.create_function("decode", 2, lambda value, encoding: value.decode(encoding))
        db.execute("CREATE TABLE candidates(rule_id TEXT,scope_id TEXT,rule_kind TEXT,target_asset_id TEXT,valid_from TEXT,valid_to TEXT)")
        db.execute("CREATE TABLE scopes(scope_id TEXT,platform_key TEXT,workspace_key TEXT,enabled BOOLEAN)")
        db.execute("INSERT INTO scopes VALUES(?,?,?,1)", (scope, binding["platform_id"], binding["workspace_key"]))
        db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?)", (row["rule"]["rule_id"], scope, "ACCESS_DECLARATION", None, "2000-01-01", "9999-01-01"))
        db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?)", (row["rule"]["rule_id"], scope, kind, target, start, end))
        count = db.execute("SELECT count(*) FROM candidates r JOIN scopes s ON s.scope_id=r.scope_id WHERE " + predicate).fetchone()[0]
        assert count == 2
