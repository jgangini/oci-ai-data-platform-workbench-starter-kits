from copy import deepcopy
from datetime import timedelta

import pytest

from app.ai_data_governance.case_activity import CaseActivityRun, case_run_start
from app.ai_data_governance.case_changes import case_change_receipt
from app.ai_data_governance.case_commit import CasePending
from app.ai_data_governance.inventory import identity
from app.ai_data_governance.model_output import model_json
from test_ai_dg_case_changes import AT
from test_ai_dg_case_program import command_fixture


def setup_case():
    binding, change, _, _, repo, journal, controller = command_fixture()
    return binding, change, repo, journal, controller, CaseActivityRun(
        repo, journal, binding, controller, lambda: None, controller.now)


def test_case_registration_has_its_own_scope_run_and_verified_start_before_case_writer():
    binding, change, repo, journal, controller, activity = setup_case()
    # Gold cases must not inherit the artifacts monitoring scope or its parent RUN.
    scope = dict(catalog_key="oci_medallion", schema_key="oci_medallion.oci_gold")
    scope_id = identity("AI_DG_SCOPE", binding["platform_id"], binding["workspace_key"], *scope.values())
    binding["source_scopes"].append(scope)
    change["scope_id"] = scope_id; change["case"]["scope_id"] = scope_id
    repo.rows["AI_DG_SCOPE"][0]["scope_id"] = scope_id
    repo.rows["AI_DG_SCOPE_ASSET"][0]["scope_id"] = scope_id
    repo.rows["AI_DG_EVIDENCE"][0]["scope_id"] = scope_id
    # Rebuild: the case identity and links depend on scope, not a mutable display label.
    from app.ai_data_governance.case_changes import _case_change
    change = _case_change(actor_ref=change["actor_ref"], session_ref=change["session_ref"],
        request_id=change["request_id"], scope_id=scope_id, run_id=change["run_id"], policy_version=change["policy_version"],
        at=AT + timedelta(seconds=3), action="OPEN", assets=[{k: v for k, v in change["assets"][0].items() if k != "case_id"}], summary="Caso Gold")
    before = controller.commit
    def observed(candidate):
        run = repo.rows["AI_DG_RUN"][0]
        assert run["scope_id"] == scope_id and run["parent_run_id"] is None
        assert run["status"] == "RUNNING" and run["finished_at"] is None
        assert repo.rows["AI_DG_EVENT"][0]["event_type"] == "START"
        assert run["started_at"] <= AT + timedelta(seconds=3)
        return before(candidate)
    controller.commit = observed
    receipt = activity.commit(change)
    assert receipt == case_change_receipt(change) and receipt["execution_verified"] is False
    run = repo.rows["AI_DG_RUN"][0]
    assert run["status"] == "SUCCEEDED" and run["finished_at"] == controller.now()
    assert model_json(run["manifest_json"])["input_digest"] == identity("case-change", change)
    assert [row["event_type"] for row in repo.rows["AI_DG_EVENT"]] == ["START", "END"]
    assert "Caso Gold" not in run["manifest_json"]


@pytest.mark.parametrize("boundary", ["producer", "scope", "run_id", "future", "confirmation"])
def test_invalid_case_run_admission_does_not_write(boundary):
    binding, change, repo, journal, controller, activity = setup_case()
    if boundary == "producer": repo.rows["AI_DG_AGENT"] = []
    elif boundary == "scope": repo.rows["AI_DG_SCOPE"][0]["workspace_key"] = "foreign"
    elif boundary == "run_id": change["run_id"] = "not-this-operation"
    elif boundary == "future": activity.now = lambda: AT
    else: controller.authorize = lambda _: None
    with pytest.raises((PermissionError, ValueError)):
        activity.commit(change)
    assert repo.writes == [] and journal.rows == {}


def test_duplicate_operation_and_lost_start_ack_do_not_start_another_case_writer():
    _, change, repo, journal, controller, activity = setup_case()
    receipt = activity.commit(change)
    before = len(repo.writes)
    with pytest.raises(CasePending): activity.commit(change)
    assert activity.reconcile(change) == receipt and len(repo.writes) == before
    _, change, repo, journal, controller, activity = setup_case()
    create = journal.create
    def lost(kind, key, value):
        result = create(kind, key, value)
        if kind == "claims": raise TimeoutError("claim acknowledged remotely only")
        return result
    journal.create = lost
    with pytest.raises(TimeoutError): activity.commit(change)
    journal.create = create
    with pytest.raises(CasePending): activity.commit(change)
    with pytest.raises(CasePending): activity.reconcile(change)
    assert repo.writes == []


@pytest.mark.parametrize("table", ["AI_DG_RUN", "AI_DG_EVENT"])
def test_uncertain_monitoring_start_cannot_be_used_as_a_case_registration(table):
    _, change, repo, _, _, activity = setup_case()
    insert = repo.insert_immutable
    def uncertain(name, rows):
        insert(name, rows)
        if name == table: raise TimeoutError("lost metadata write ack")
    repo.insert_immutable = uncertain
    with pytest.raises(TimeoutError): activity.commit(change)
    repo.insert_immutable = insert
    before = len(repo.writes)
    with pytest.raises(CasePending): activity.commit(change)
    with pytest.raises(CasePending): activity.reconcile(change)
    assert len(repo.writes) == before and not repo.rows.get("AI_DG_CASE")


def test_completed_case_is_recovered_without_rewriting_a_failed_run_as_success():
    _, change, repo, journal, _, activity = setup_case()
    repo.after_case = lambda: (_ for _ in ()).throw(TimeoutError("case committed, ack lost"))
    with pytest.raises(TimeoutError): activity.commit(change)
    assert repo.rows["AI_DG_RUN"][0]["status"] == "PARTIAL"
    assert model_json(repo.rows["AI_DG_EVENT"][-1]["metrics_json"])["registration_verified"] is False
    repo.after_case = None
    before = list(repo.writes)
    receipt = activity.reconcile(change)
    assert receipt == case_change_receipt(change)
    assert repo.writes[len(before):] == ["AI_DG_EVENT"]
    assert repo.rows["AI_DG_RUN"][0]["status"] == "PARTIAL"
    assert repo.rows["AI_DG_EVENT"][-1]["event_type"] == "PROGRESS"
    before = list(repo.writes)
    assert activity.reconcile(change) == receipt and repo.writes == before


def test_uncertain_case_rows_remain_pending_without_another_case_write():
    _, change, repo, _, _, activity = setup_case()
    insert = repo.insert_immutable
    def partial(name, rows):
        if name == "AI_DG_CASE": raise TimeoutError("case did not acknowledge")
        insert(name, rows)
    repo.insert_immutable = partial
    with pytest.raises(TimeoutError): activity.commit(change)
    repo.insert_immutable = insert
    before = list(repo.writes)
    with pytest.raises(CasePending): activity.reconcile(change)
    assert repo.writes == before and repo.rows["AI_DG_RUN"][0]["status"] == "PARTIAL"


@pytest.mark.parametrize("boundary", ["run_update", "end_insert"])
def test_bookkeeping_ack_loss_never_repeats_case_write(boundary):
    _, change, repo, _, _, activity = setup_case()
    update, insert = repo.update_current, repo.insert_immutable
    def lose_update(name, rows, *, expected):
        update(name, rows, expected=expected)
        if name == "AI_DG_RUN": raise TimeoutError("terminal RUN ack lost")
    def lose_end(name, rows):
        insert(name, rows)
        if name == "AI_DG_EVENT" and rows[0]["event_type"] == "END": raise TimeoutError("END ack lost")
    if boundary == "run_update": repo.update_current = lose_update
    else: repo.insert_immutable = lose_end
    with pytest.raises(TimeoutError): activity.commit(change)
    repo.update_current, repo.insert_immutable = update, insert
    before = list(repo.writes)
    if boundary == "run_update":
        with pytest.raises(CasePending): activity.reconcile(change)
    else:
        assert activity.reconcile(change) == case_change_receipt(change)
    assert repo.writes == before


def test_historical_readback_retains_the_served_revision_after_code_upgrade():
    binding, change, repo, _, _, activity = setup_case()
    receipt = activity.commit(change)
    original = deepcopy(repo.rows["AI_DG_RUN"])
    binding["component_revision"] = "f" * 64
    assert activity.reconcile(change) == receipt
    assert repo.rows["AI_DG_RUN"] == original
    with pytest.raises(ValueError, match="frozen case run"):
        activity.commit(change)


def test_corrupted_start_record_is_rejected_without_claiming_or_writing():
    binding, change, repo, journal, _, activity = setup_case()
    record = case_run_start(binding, change)
    record["run"]["parent_run_id"] = "foreign"
    journal.create("executions", activity._key(change, "start"), record)
    with pytest.raises(ValueError, match="frozen case run"):
        activity.commit(change)
    assert repo.writes == []


def test_sealed_pre_writer_abort_can_be_repaired_without_changing_historical_run_result():
    _, change, repo, _, controller, activity = setup_case()
    before = controller._check_before
    def stop(_): raise InterruptedError("stopped before Delta CASE writer")
    controller._check_before = stop
    with pytest.raises(InterruptedError): activity.commit(change)
    assert repo.rows["AI_DG_RUN"][0]["status"] == "PARTIAL"
    assert not repo.rows.get("AI_DG_CASE")
    controller._check_before = before
    assert activity.repair_not_started(change) == case_change_receipt(change)
    assert repo.rows["AI_DG_RUN"][0]["status"] == "PARTIAL"
    assert repo.rows["AI_DG_EVENT"][-1]["event_type"] == "PROGRESS"
    writes = list(repo.writes)
    assert activity.reconcile(change) == case_change_receipt(change)
    assert repo.writes == writes


def test_partial_run_does_not_authorize_repair_without_the_exact_pre_writer_abort():
    _, change, repo, _, _, activity = setup_case()
    insert = repo.insert_immutable
    def partial(name, rows):
        if name == "AI_DG_CASE": raise TimeoutError("writer is uncertain")
        insert(name, rows)
    repo.insert_immutable = partial
    with pytest.raises(TimeoutError): activity.commit(change)
    repo.insert_immutable = insert
    writes = list(repo.writes)
    with pytest.raises(CasePending, match="sealed pre-writer abort"):
        activity.repair_not_started(change)
    assert repo.writes == writes


def test_missing_start_event_prevents_a_complete_monitoring_receipt():
    _, change, repo, _, _, activity = setup_case()
    activity.commit(change)
    repo.rows["AI_DG_EVENT"] = [row for row in repo.rows["AI_DG_EVENT"] if row["event_type"] != "START"]
    before = list(repo.writes)
    with pytest.raises(CasePending, match="start event"):
        activity.reconcile(change)
    assert repo.writes == before
