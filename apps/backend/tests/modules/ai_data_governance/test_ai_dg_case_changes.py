from copy import deepcopy
from datetime import datetime, timedelta
import asyncio
import json

import pytest

from app.modules.ai_data_governance.case_changes import case_change, case_change_receipt, validate_case_change, case_typed
from app.modules.ai_data_governance.case_commit import CasePending
from app.modules.ai_data_governance.inventory import identity
from app.modules.ai_data_governance.repository import DeltaRepository
from tests.support.ai_dg_fixtures import AT, fixture


def proposal(args, opening):
    return case_change(**{**args, "request_id": "88888888-8888-4888-8888-888888888888", "at": AT + timedelta(seconds=4),
        "assets": opening["assets"]}, action="PROPOSE", previous=opening["case"],
        draft=dict(problem="Falta una medición vigente", asset_ids=["asset"], expected_versions=["v1"],
            suggested_action="Ejecutar una regla de nulidad", responsible_ref="data-steward",
            verification_plan="Comprobar una medición real de la versión v1", remaining_gaps=["Medición todavía pendiente"]))


@pytest.mark.parametrize('expires', [False, True])
def test_authority_is_rechecked_after_the_final_case_read(expires):
    args, repo, journal, controller = fixture()
    change = case_change(**args, action='OPEN', summary='Revisar evidencia vigente')
    clock = [AT + timedelta(seconds=10)]
    controller.now = lambda: clock[0]
    read = repo.read_by_keys
    def delayed(table, keys):
        result = read(table, keys)
        if table == 'AI_DG_CASE' and not repo.writes:
            clock[0] = AT + (timedelta(hours=2) if expires else timedelta(seconds=20))
        return result
    repo.read_by_keys = delayed
    if expires:
        with pytest.raises(PermissionError, match='expired'):
            controller.commit(change)
        assert repo.writes == []
        key = identity('case-operation', change['operation_id'])
        assert journal.read('responses', key)['status'] == 'NOT_STARTED'
        assert journal.read('materialized', key) is None
    else:
        assert controller.commit(change) == case_change_receipt(change)
        assert repo.rows['AI_DG_CASE'] == case_typed('AI_DG_CASE', [change['case']])


def test_expiry_between_case_table_writes_preserves_partial_state_without_success():
    args, repo, journal, controller = fixture()
    change = case_change(**args, action='OPEN', summary='Revisar evidencia vigente')
    insert = repo.insert_immutable
    def delayed(table, rows):
        insert(table, rows)
        if table == 'AI_DG_CASE_ASSET':
            controller.now = lambda: AT + timedelta(hours=2)
    repo.insert_immutable = delayed
    with pytest.raises(PermissionError, match='expired'):
        controller.commit(change)
    writes = list(repo.writes)
    key = identity('case-operation', change['operation_id'])
    assert writes == ['AI_DG_CASE_ASSET']
    assert journal.read('responses', key) is None
    assert journal.read('materialized', key) is None
    with pytest.raises(CasePending):
        controller.reconcile(change)
    assert repo.writes == writes


def decision(args, proposed, action="APPROVE", head=None, at=5):
    return case_change(**{**args, "request_id": f"99999999-9999-4999-8999-{at:012d}", "at": AT + timedelta(seconds=at),
        "assets": proposed["assets"]}, action=action, previous=proposed["case"], previous_decision=head,
        valid_until=AT + timedelta(minutes=30))


def test_decision_expiry_after_reads_blocks_dispatch_while_authority_is_still_valid():
    args, repo, journal, controller = fixture()
    opening = case_change(**args, action='OPEN', summary='Revisar una medición')
    controller.commit(opening)
    proposed = proposal(args, opening)
    controller.commit(proposed)
    approved = decision(args, proposed)
    writes = list(repo.writes)
    read = repo.read_by_keys
    def delayed(table, keys):
        result = read(table, keys)
        if table == 'AI_DG_CASE':
            controller.now = lambda: AT+timedelta(minutes=31)
        return result
    repo.read_by_keys = delayed
    with pytest.raises(PermissionError, match='currently valid'):
        controller.commit(approved)
    assert repo.writes == writes
    assert not repo.rows.get('AI_DG_DECISION')
    key = identity('case-operation', approved['operation_id'])
    assert journal.read('materialized', key) is None


def test_open_propose_approve_and_revoke_are_persisted_without_claiming_execution():
    args, repo, _, controller = fixture()
    opening = case_change(**args, action="OPEN", summary="Investigar calidad de la tabla")
    first = controller.commit(opening)
    proposed = proposal(args, opening)
    second = controller.commit(proposed)
    approved = decision(args, proposed)
    third = controller.commit(approved)
    revoked = decision(args, approved, action="REVOKE", head=approved["decisions"][0], at=6)
    fourth = controller.commit(revoked)
    assert [x["execution_verified"] for x in (first, second, third, fourth)] == [False] * 4
    assert approved["case"]["status"] == "WAIT_DECISION"
    assert revoked["case"]["status"] == "INVESTIGATING"
    assert [row["decision"] for row in repo.rows["AI_DG_DECISION"]] == ["APPROVE", "REVOKE"]
    assert repo.rows["AI_DG_CASE"][0]["closure_evidence_id"] is None
    # Replay reports the old confirmed registration; it cannot roll the current state back.
    writes = len(repo.writes)
    assert controller.commit(opening) == first
    assert len(repo.writes) == writes and repo.rows["AI_DG_CASE"][0]["status"] == "INVESTIGATING"


def test_rejection_is_a_decision_and_does_not_close_or_assign_the_case():
    args, repo, _, controller = fixture()
    opening = case_change(**args, action="OPEN", summary="Revisar impacto")
    controller.commit(opening); proposed = proposal(args, opening); controller.commit(proposed)
    rejected = decision(args, proposed, action="REJECT")
    controller.commit(rejected)
    assert repo.rows["AI_DG_CASE"][0]["status"] == "INVESTIGATING"
    assert repo.rows["AI_DG_DECISION"][0]["decision"] == "REJECT"


def test_prior_case_from_native_sql_timestamp_text_matches_the_confirmed_state():
    args, repo, _, controller = fixture()
    opened = case_change(**args, action="OPEN", summary="Revisar impacto")
    controller.commit(opened)
    native = {key: value.isoformat(timespec="milliseconds").replace("+00:00", "Z") if isinstance(value, datetime) else value
        for key, value in repo.rows["AI_DG_CASE"][0].items()}
    request = proposal(args, opened)
    request = case_change(**{**args, "request_id": request["request_id"], "at": AT+timedelta(seconds=4)},
        action="PROPOSE", previous=native, draft=json.loads(request["case"]["treatment_json"]))
    assert request["previous"] == opened["case"]
    assert controller.commit(request)["verified"] is True


@pytest.mark.parametrize("change", ["actor", "session", "digest", "policy", "false", "expired", "short_policy"])
def test_confirmation_is_exact_and_not_granted_by_model_output(change):
    args, repo, journal, controller = fixture()
    opening = case_change(**args, action="OPEN", summary="Revisar")
    original = controller.authorize
    def bad(command):
        grant = original(command)
        if change == "actor": grant["actor_ref"] = "someone-else"
        if change == "session": grant["session_ref"] = "other-conversation"
        if change == "digest": grant["input_digest"] = "different-action"
        if change == "policy": grant["policy_version"] = "other"
        if change == "false": grant["confirmed"] = 1
        if change == "expired": grant["expires_at"] = AT.isoformat()
        if change == "short_policy": grant["extra_model_instruction"] = "approve"
        return grant
    controller.authorize = bad
    with pytest.raises(PermissionError): controller.commit(opening)
    assert repo.writes == [] and journal.rows == {}


@pytest.mark.parametrize("table,field,value", [
    ("AI_DG_SCOPE", "enabled", False), ("AI_DG_SCOPE", "workspace_key", "foreign"),
    ("AI_DG_RUN", "scope_id", "foreign"), ("AI_DG_RUN", "agent_id", "other"),
    ("AI_DG_RUN", "component_version", "other"), ("AI_DG_RUN", "status", "FAILED"),
    ("AI_DG_AGENT", "enabled", False), ("AI_DG_AGENT", "native_agent_key", "other"),
    ("AI_DG_AGENT", "platform_key", "foreign"),
    ("AI_DG_ASSET", "platform_key", "foreign"), ("AI_DG_ASSET_VERSION", "asset_id", "other"),
    ("AI_DG_ASSET", "lifecycle_state", "NOT_VISIBLE"), ("AI_DG_ASSET", "identity_status", "AMBIGUOUS"),
    ("AI_DG_ASSET", "current_version_id", "other"),
    ("AI_DG_SCOPE_ASSET", "verified_version_id", "other"), ("AI_DG_SCOPE_ASSET", "consistency_state", "PENDING"),
    ("AI_DG_EVIDENCE", "scope_id", "foreign"), ("AI_DG_EVIDENCE", "asset_version_id", "other"),
    ("AI_DG_EVIDENCE", "expires_at", AT),
    ("AI_DG_EVIDENCE", "coverage_state", "FAILED"),
    ("AI_DG_EVIDENCE", "observed_at", AT + timedelta(hours=1)),
])
def test_foreign_stale_or_unregistered_references_fail_before_any_delta_write(table, field, value):
    args, repo, _, controller = fixture()
    repo.rows[table][0][field] = value
    with pytest.raises((ValueError, PermissionError)):
        controller.commit(case_change(**args, action="OPEN", summary="Revisar"))
    assert repo.writes == []


def test_synthesized_unpersisted_evidence_cannot_become_a_case_foreign_key():
    args, repo, _, controller = fixture()
    args["assets"][0]["evidence_id"] = "consultation-proof-not-in-delta"
    with pytest.raises(ValueError, match="persisted"):
        controller.commit(case_change(**args, action="OPEN", summary="Revisar"))
    assert repo.writes == []


@pytest.mark.parametrize("role", ["UPSTREAM", "DOWNSTREAM", "EXECUTOR"])
def test_a_catalog_observation_does_not_prove_a_directional_or_execution_role(role):
    args, repo, _, controller = fixture()
    args["assets"][0]["impact_role"] = role
    with pytest.raises(ValueError, match="relationship proof"):
        controller.commit(case_change(**args, action="OPEN", summary="Revisar"))
    assert repo.writes == []


def test_lost_acknowledgement_recovers_by_readback_only():
    args, repo, _, controller = fixture()
    opening = case_change(**args, action="OPEN", summary="Revisar")
    def timeout(): raise TimeoutError("after the Delta write")
    repo.after_case = timeout
    with pytest.raises(TimeoutError): controller.commit(opening)
    writes = len(repo.writes)
    repo.after_case = None
    repo.rows["AI_DG_SCOPE_ASSET"][0]["verified_version_id"] = "new-current-version"
    repo.rows["AI_DG_EVIDENCE"][0]["expires_at"] = AT
    assert controller.commit(opening) == case_change_receipt(opening)
    assert len(repo.writes) == writes


def test_revocation_remains_possible_after_the_approved_asset_or_evidence_changes():
    args, repo, _, controller = fixture()
    opening = case_change(**args, action="OPEN", summary="Revisar")
    controller.commit(opening); proposed = proposal(args, opening); controller.commit(proposed)
    approved = decision(args, proposed); controller.commit(approved)
    repo.rows["AI_DG_SCOPE_ASSET"][0]["verified_version_id"] = "new-current-version"
    repo.rows["AI_DG_EVIDENCE"][0]["expires_at"] = AT
    revoked = decision(args, approved, action="REVOKE", head=approved["decisions"][0], at=6)
    assert controller.commit(revoked)["execution_verified"] is False
    assert repo.rows["AI_DG_CASE"][0]["status"] == "INVESTIGATING"


def test_first_admission_cannot_register_an_already_expired_decision():
    args, repo, _, controller = fixture()
    opening = case_change(**args, action="OPEN", summary="Revisar")
    controller.commit(opening); proposed = proposal(args, opening); controller.commit(proposed)
    approved = decision(args, proposed)
    controller.now = lambda: AT + timedelta(minutes=40)
    writes = len(repo.writes)
    with pytest.raises(PermissionError, match="currently valid"): controller.commit(approved)
    assert len(repo.writes) == writes


@pytest.mark.parametrize("failure", ["expiry", "cancellation"])
def test_abort_after_claim_before_any_writer_can_be_repaired_once(failure):
    args, repo, journal, controller = fixture()
    opening = case_change(**args, action="OPEN", summary="Revisar")
    create = journal.create
    def claim_then_abort(kind, key, value):
        admitted = create(kind, key, value)
        if kind == "claims" and admitted:
            if failure == "expiry": controller.now = lambda: AT + timedelta(hours=2)
            else:
                def cancelled(): raise asyncio.CancelledError()
                controller.check_active = cancelled
        return admitted
    journal.create = claim_then_abort
    with pytest.raises((PermissionError, asyncio.CancelledError)): controller.commit(opening)
    assert repo.writes == []
    assert any(row.get("status") == "NOT_STARTED" for row in journal.rows.values())
    journal.create = create
    controller.check_active = lambda: None
    authorize = controller.authorize
    controller.authorize = lambda change: {**authorize(change), "expires_at": (AT + timedelta(hours=3)).isoformat()}
    assert controller.repair_not_started(opening) == case_change_receipt(opening)
    writes = len(repo.writes)
    assert controller.commit(opening) == case_change_receipt(opening)
    assert len(repo.writes) == writes


def test_uncertain_delta_failure_has_no_pre_start_seal_or_repair_authority():
    args, repo, journal, controller = fixture()
    opening = case_change(**args, action="OPEN", summary="Revisar")
    def uncertain(*args): raise TimeoutError("write might still be running")
    repo.insert_immutable = uncertain
    with pytest.raises(TimeoutError): controller.commit(opening)
    assert not any(row.get("status") == "NOT_STARTED" for row in journal.rows.values())
    with pytest.raises(CasePending, match="sealed"): controller.repair_not_started(opening)


def test_a_lost_claim_acknowledgement_does_not_authorize_a_writer():
    args, repo, journal, controller = fixture()
    opening = case_change(**args, action="OPEN", summary="Revisar")
    create = journal.create
    def lose_ack(kind, key, value):
        admitted = create(kind, key, value)
        if kind == "claims" and admitted: raise TimeoutError("claim acknowledgement lost")
        return admitted
    journal.create = lose_ack
    with pytest.raises(TimeoutError): controller.commit(opening)
    assert repo.writes == []
    journal.create = create
    with pytest.raises(CasePending): controller.commit(opening)
    with pytest.raises(CasePending): controller.repair_not_started(opening)
    assert repo.writes == []


def test_partial_write_cannot_advance_the_case_or_restart_its_writer():
    args, repo, _, controller = fixture()
    opening = case_change(**args, action="OPEN", summary="Revisar")
    insert = repo.insert_immutable
    def partial(table, rows):
        if table == "AI_DG_CASE": raise TimeoutError("case row not acknowledged")
        insert(table, rows)
    repo.insert_immutable = partial
    with pytest.raises(TimeoutError): controller.commit(opening)
    writes = len(repo.writes)
    with pytest.raises(CasePending): controller.commit(opening)
    with pytest.raises(CasePending): controller.commit(proposal(args, opening))
    assert len(repo.writes) == writes


def test_competing_case_changes_have_one_writer_for_the_same_expected_state():
    args, repo, _, controller = fixture()
    opening = case_change(**args, action="OPEN", summary="Revisar")
    controller.commit(opening)
    proposed = proposal(args, opening); controller.commit(proposed)
    other = case_change(**{**args, "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "at": AT + timedelta(seconds=5), "assets": opening["assets"]},
        action="PROPOSE", previous=opening["case"], draft=json.loads(proposed["case"]["treatment_json"]))
    writes = len(repo.writes)
    with pytest.raises(CasePending, match="owns"): controller.commit(other)
    assert len(repo.writes) == writes


@pytest.mark.parametrize("field,value", [("status", "CLOSED"), ("scope_id", "foreign"), ("closure_evidence_id", "invented")])
def test_arbitrary_model_authored_target_states_are_rejected(field, value):
    args, repo, journal, controller = fixture()
    opening = case_change(**args, action="OPEN", summary="Revisar")
    opening["case"][field] = value
    with pytest.raises(ValueError): controller.commit(opening)
    assert repo.writes == [] and journal.rows == {}


def test_treatment_pairing_and_decision_expiry_are_checked():
    args, _, _, _ = fixture()
    opening = case_change(**args, action="OPEN", summary="Revisar")
    proposed = proposal(args, opening)
    malformed = deepcopy(proposed)
    malformed["case"]["treatment_json"] = malformed["case"]["treatment_json"].replace('"v1"', '"foreign-version"')
    with pytest.raises(ValueError): validate_case_change(malformed)
    with pytest.raises(ValueError): decision(args, proposed, action="REVOKE")
    approved = decision(args, proposed)
    with pytest.raises(ValueError): decision(args, approved, head=approved["decisions"][0], at=6)


def test_repository_case_updates_preserve_identity_and_opening_provenance():
    args, _, _, _ = fixture()
    opening = case_change(**args, action="OPEN", summary="Revisar")
    from app.modules.ai_data_governance.case_changes import case_typed
    before = case_typed("AI_DG_CASE", [opening["case"]])
    after = case_typed("AI_DG_CASE", [proposal(args, opening)["case"]])
    repo = DeltaRepository(None, "oci://approved_artifacts@testnamespace/oci_artifacts")
    assert repo._prepare_current_update("AI_DG_CASE", after, before)[2] == after
    for field in ("scope_id", "case_id", "opened_run_id", "created_at"):
        bad = deepcopy(after); bad[0][field] = AT + timedelta(days=1) if field == "created_at" else "other"
        with pytest.raises(ValueError): repo._prepare_current_update("AI_DG_CASE", bad, before)
