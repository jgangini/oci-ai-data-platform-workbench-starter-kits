"""Fixed case transitions. Model text is a draft, never a decision or a write grant."""

from copy import deepcopy
from datetime import datetime, timezone
import re
from uuid import UUID

from .contracts import TreatmentDraft
from .inventory import canonical, identity
from .repository import checked_rows
from .schema import load_model


def case_time(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Case operations require a timezone-aware time")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def case_digest(value):
    return identity("governance-case-state", value)


def case_versions(scope_id, assets):
    return identity("governance-case-versions", scope_id,
        sorted((row["asset_id"], row["assessed_version_id"]) for row in assets))


def case_typed(table_name, rows):
    table = next(t for t in load_model()["tables"] if t["name"] == table_name)
    columns = [c["name"] for c in table["columns"] if c["type"] == "TIMESTAMP"]
    typed = [{**row, **{name: datetime.fromisoformat(row[name]) if row[name] is not None else None
        for name in columns}} for row in rows]
    return checked_rows(table_name, typed, limit=16)


def case_record_snapshot(table_name, row):
    if table_name not in {"AI_DG_CASE", "AI_DG_DECISION"}:
        raise ValueError("Only case and decision state can become an assessed snapshot")
    table = next(t for t in load_model()["tables"] if t["name"] == table_name)
    result = deepcopy(row)
    for column in table["columns"]:
        if column["type"] == "TIMESTAMP" and result[column["name"]] is not None:
            value = result[column["name"]]
            result[column["name"]] = case_time(value if isinstance(value, datetime) else datetime.fromisoformat(value))
    case_typed(table_name, [result])
    return result


def case_change(*, subject, session, **arguments):
    if any(not isinstance(v, str) or not v.strip() or len(v) > 4096 for v in (subject, session)):
        raise ValueError("Case operations require a trusted caller and conversation")
    return _case_change(actor_ref=identity("activity-subject", subject),
        session_ref=identity("activity-session", session), **arguments)


def _case_change(*, actor_ref, session_ref, request_id, scope_id, run_id, policy_version, at,
                action, assets, summary=None, previous=None, draft=None, previous_decision=None, valid_until=None):
    """Construct only OPEN/PROPOSE/APPROVE/REJECT/REVOKE; no assignment or closure."""
    if any(not isinstance(v, str) or not v.strip() or len(v) > 4096
            for v in (scope_id, run_id, policy_version)):
        raise ValueError("Case operations require a trusted caller and scope")
    if any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in (actor_ref, session_ref)):
        raise ValueError("Case operations require bound caller digests")
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("Case operations require a canonical request UUID")
    if action not in {"OPEN", "PROPOSE", "APPROVE", "REJECT", "REVOKE"}:
        raise ValueError("Unknown case transition")
    previous = case_record_snapshot("AI_DG_CASE", previous) if previous is not None else None
    previous_decision = case_record_snapshot("AI_DG_DECISION", previous_decision) if previous_decision is not None else None
    observed = case_time(at)
    operation = identity("governance-case-request", actor_ref, session_ref, request_id)
    case_id = identity("AI_DG_CASE", scope_id, operation) if previous is None else previous["case_id"]
    links = case_asset_links(case_id, assets)
    after = _initial_case_state(action, previous, summary, scope_id, case_id, run_id, observed,
        draft, previous_decision, valid_until)
    decisions = []
    if action == "PROPOSE":
        after = _proposed_case(after, links, draft, previous_decision, valid_until)
    elif action in {"APPROVE", "REJECT", "REVOKE"}:
        if draft is not None:
            raise ValueError("Only a proposal accepts model-authored treatment text")
        decisions = [_case_decision(action, previous, links, previous_decision,
            valid_until, operation, run_id, actor_ref, policy_version, observed)]
        if action != "APPROVE":
            after["status"] = "INVESTIGATING"
    case_typed("AI_DG_CASE", [after])
    case_typed("AI_DG_DECISION", decisions)
    return dict(schema_version=1, kind="GOVERNANCE_CASE_CHANGE", operation_id=operation,
        request_id=request_id,
        actor_ref=actor_ref, session_ref=session_ref, scope_id=scope_id, run_id=run_id,
        policy_version=policy_version, action=action, observed_at=observed,
        previous=deepcopy(previous), case=after, assets=links, decisions=decisions,
        previous_decision=deepcopy(previous_decision))


def case_asset_links(case_id, assets):
    """The assessed set stays fixed for this case; reopening does not rebase it."""
    if not isinstance(case_id, str) or not re.fullmatch(r"[0-9a-f]{64}", case_id):
        raise ValueError("An exact case reference is required")
    if not isinstance(assets, list) or not 1 <= len(assets) <= 16:
        raise ValueError("A case requires a bounded assessed asset set")
    links = sorted([{**deepcopy(row), "case_id": row.get("case_id", case_id)} for row in assets], key=lambda row: row["asset_id"])
    case_typed("AI_DG_CASE_ASSET", links)
    if any(row["case_id"] != case_id or row["impact_role"] not in {"SUBJECT", "UPSTREAM", "DOWNSTREAM", "EXECUTOR"}
            for row in links):
        raise ValueError("Case links escaped their case or assessed role")
    return links


def _initial_case_state(action, previous, summary, scope_id, case_id, run_id, observed,
                        draft, previous_decision, valid_until):
    if action == "OPEN":
        if previous is not None or draft is not None or previous_decision is not None or valid_until is not None:
            raise ValueError("Opening a case accepts no prior state or decision")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
            raise ValueError("An explicit case summary is required")
        return dict(case_id=case_id, scope_id=scope_id, opened_run_id=run_id, summary=summary,
            owner_ref=None, status="OPEN", change_digest=None, treatment_json=None,
            closure_evidence_id=None, created_at=observed, updated_at=observed)
    if previous is None or previous["scope_id"] != scope_id or summary is not None:
        raise ValueError("A transition requires the exact existing case")
    case_typed("AI_DG_CASE", [previous])
    if datetime.fromisoformat(previous["updated_at"]) >= datetime.fromisoformat(observed):
        raise ValueError("Case transitions must advance their observation time")
    if previous["status"] not in {"OPEN", "INVESTIGATING", "WAIT_DECISION"}:
        raise ValueError("This case state requires a different executor or verification contract")
    return {**previous, "updated_at": observed}


def _proposed_case(after, links, draft, previous_decision, valid_until):
    if valid_until is not None or previous_decision is not None and previous_decision["decision"] == "APPROVE":
        raise ValueError("A proposal cannot supersede an active approval")
    treatment = TreatmentDraft.model_validate(draft)
    pairs = list(zip(treatment.asset_ids, treatment.expected_versions))
    if (len(treatment.asset_ids) != len(treatment.expected_versions) or len(pairs) != len(links)
            or set(pairs) != {(row["asset_id"], row["assessed_version_id"]) for row in links}):
        raise ValueError("Treatment assets must match their exact assessed versions")
    text = canonical(treatment.model_dump(mode="json"))
    return {**after, "status": "WAIT_DECISION", "treatment_json": text,
        "change_digest": identity("governance-case-treatment", after["scope_id"], case_versions(after["scope_id"], links), text)}


def _case_decision(action, previous, links, previous_decision, valid_until, operation, run_id, actor_ref, policy_version, observed):
    scope_id, case_id = previous["scope_id"], previous["case_id"]
    if previous["status"] != "WAIT_DECISION" or not previous["change_digest"]:
        raise ValueError("A decision requires an outstanding exact treatment")
    treatment = TreatmentDraft.model_validate_json(previous["treatment_json"])
    text = canonical(treatment.model_dump(mode="json"))
    if previous["change_digest"] != identity("governance-case-treatment", scope_id, case_versions(scope_id, links), text):
        raise ValueError("The treatment or assessed versions changed")
    if action == "REVOKE":
        if (previous_decision is None or previous_decision["decision"] != "APPROVE"
                or previous_decision["case_id"] != case_id
                or previous_decision["change_digest"] != previous["change_digest"]
                or previous_decision["versions_digest"] != case_versions(scope_id, links)):
            raise ValueError("Revocation requires the current exact approval")
    elif previous_decision is not None:
        raise ValueError("An already decided treatment must not be decided again")
    expiry = case_time(valid_until)
    if datetime.fromisoformat(expiry) <= datetime.fromisoformat(observed):
        raise ValueError("A decision requires a future policy validity limit")
    return dict(decision_id=identity("AI_DG_DECISION", case_id, operation), case_id=case_id,
        run_id=run_id, actor_ref=actor_ref, decision=action, change_digest=previous["change_digest"],
        versions_digest=case_versions(scope_id, links), policy_version=policy_version,
        valid_until=expiry, recorded_at=observed)


def validate_case_change(change):
    if not isinstance(change, dict) or len(canonical(change).encode()) > 131072:
        raise ValueError("The case change exceeds its bound")
    rebuilt = _case_change(actor_ref=change["actor_ref"], session_ref=change["session_ref"],
        request_id=change["request_id"], scope_id=change["scope_id"], run_id=change["run_id"],
        policy_version=change["policy_version"], at=datetime.fromisoformat(change["observed_at"]),
        action=change["action"], assets=change["assets"], previous=change["previous"],
        previous_decision=change["previous_decision"],
        summary=change["case"]["summary"] if change["action"] == "OPEN" else None,
        draft=TreatmentDraft.model_validate_json(change["case"]["treatment_json"]).model_dump(mode="json")
            if change["action"] == "PROPOSE" else None,
        valid_until=datetime.fromisoformat(change["decisions"][0]["valid_until"]) if change["decisions"] else None)
    if canonical(rebuilt) != canonical(change):
        raise ValueError("Case change differs from its fixed transition")
    return change


def case_change_receipt(change):
    head = change["decisions"][-1] if change["decisions"] else None
    return dict(schema_version=1, kind="GOVERNANCE_CASE_CHANGE", operation_id=change["operation_id"],
        input_digest=identity("case-change", change), case_id=change["case"]["case_id"],
        state_digest=case_digest(change["case"]), assets_digest=identity("case-assets", change["assets"]),
        decision_head=head, verified=True, execution_verified=False)


def case_confirmation(change, grant, now):
    fields = {"confirmed", "actor_ref", "session_ref", "input_digest", "policy_version", "expires_at"}
    if not isinstance(grant, dict) or set(grant) != fields or grant["confirmed"] is not True:
        raise PermissionError("A trusted explicit case confirmation is required")
    if (any(grant[name] != change[name] for name in ("actor_ref", "session_ref", "policy_version"))
            or grant["input_digest"] != identity("case-change", change)):
        raise PermissionError("Confirmation does not match the exact case change")
    expiry = datetime.fromisoformat(grant["expires_at"])
    if now.utcoffset() is None or expiry.utcoffset() is None or expiry <= now:
        raise PermissionError("Case confirmation expired")
    if any(datetime.fromisoformat(row["valid_until"]) > expiry for row in change["decisions"]):
        raise PermissionError("A decision cannot outlive its confirmed policy")
    return grant


def case_effect_admission(change, grant, now):
    """Check the frozen authority at dispatch, after potentially slow reads."""
    case_confirmation(change, grant, now)
    if (datetime.fromisoformat(change["observed_at"]) > now
            or any(datetime.fromisoformat(row["valid_until"]) <= now for row in change["decisions"])):
        raise PermissionError("A new case decision must be currently valid")
