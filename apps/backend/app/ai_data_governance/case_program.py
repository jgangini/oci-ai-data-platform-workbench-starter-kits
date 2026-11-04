"""Fixed case operations under the existing central credential, not model tools."""

from datetime import datetime, timezone

from .activity_records import activity_agent
from .case_changes import validate_case_change, case_confirmation, case_digest, case_change_receipt, case_effect_admission
from .case_commit import CaseCommitter, CasePending, _same
from .case_activity import CaseActivityRun
from .inventory import canonical, identity
from .journal import GovernanceJournal
from .projection_query import consultation_scope_ids
from .repository import DeltaRepository
from .service_transport import runtime_signer, runtime_object_storage


CASE_MODES = frozenset({"COMMIT_CASE", "RECONCILE_CASE", "REPAIR_CASE_NOT_STARTED"})


def checked_case_command(binding, mode, change, caller_binding, grant, *, now=None):
    if mode not in CASE_MODES:
        raise ValueError("Unknown fixed case operation")
    validate_case_change(change)
    activity_agent(binding)
    if change["scope_id"] not in consultation_scope_ids(binding):
        raise PermissionError("Case operation escaped its configured coverage")
    expected = {"actor_ref": change["actor_ref"], "session_ref": change["session_ref"],
        "operation_id": change["operation_id"], "component_revision": binding["component_revision"]}
    if canonical(caller_binding) != canonical(expected):
        raise PermissionError("Case operation differs from the bound native caller")
    if mode == "RECONCILE_CASE":
        if grant is not None:
            raise ValueError("Reconciliation accepts no new decision confirmation")
    else:
        case_confirmation(change, grant, datetime.now(timezone.utc) if now is None else now)


def execute_case_program(spark, objects, binding, mode, change, caller_binding, grant, *, check_active, now):
    """Objects and Spark are supplied by trusted runtime code, never serialized tools."""
    check_active()
    checked_case_command(binding, mode, change, caller_binding, grant, now=now())
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    def before_write(table_name):
        check_active()
        if table_name in {"AI_DG_CASE", "AI_DG_CASE_ASSET", "AI_DG_DECISION"}:
            if mode == "RECONCILE_CASE":
                raise PermissionError("Historical reconciliation cannot dispatch a new case write")
            case_effect_admission(change, grant, now())
    repository = DeltaRepository(spark, binding["storage_root"], max_batch_rows=16, before_write=before_write)
    journal = GovernanceJournal(objects, platform_key=binding["platform_id"],
        workspace_key=binding["workspace_key"], storage_root=binding["storage_root"])
    controller = CaseCommitter(repository, journal, binding, lambda candidate: grant, check_active, now=now)
    activity = CaseActivityRun(repository, journal, binding, controller, check_active, now)
    if mode == "RECONCILE_CASE":
        try:
            return activity.reconcile(change)
        except CasePending:
            return case_pending_diagnostics(activity, change)
    if mode == "REPAIR_CASE_NOT_STARTED":
        return activity.repair_not_started(change)
    return activity.commit(change)


def case_pending_diagnostics(activity, change):
    """Describe current read-only checks; never infer the cause of the original failure."""
    activity.check_active()
    journal = activity.journal
    key = identity('case-operation', change['operation_id'])
    claim_key = identity('case-state-writer', change['case']['case_id'], case_digest(change['previous']))
    claim = dict(operation_id=change['operation_id'], input_digest=identity('case-change', change))
    if not _same(journal.read('outcomes', key), change) or not _same(journal.read('claims', claim_key), claim):
        raise CasePending('Diagnostics require the original admitted operation')
    start = activity._start_record(change, historical=True)
    historical = {**activity.binding, 'component_revision':start['run']['component_version']}
    reader = CaseCommitter(activity.repository, journal, historical, lambda _:None, activity.check_active, now=activity.now)
    checks = [dict(phase='FROZEN_START', state='PASSED', error_kind=None)]
    for phase, callback in (('CURRENT_REFERENCES', lambda:reader._check_references(change)),
            ('EXPECTED_PREVIOUS_STATE', lambda:reader._check_before(change))):
        checks.append(case_pending_check(phase, callback, activity.check_active))
    activity.check_active()
    return dict(kind='GOVERNANCE_CASE_PENDING', verified=False, operation_id=change['operation_id'],
        input_digest=identity('case-change', change), component_revision=activity.binding['component_revision'], checks=checks)


def case_pending_check(phase, callback, check_active):
    check_active()
    state, kind = 'PASSED', None
    try:
        callback()
    except Exception as error:
        state = 'REJECTED'
        kind = {CasePending:'PENDING', PermissionError:'PERMISSION_ERROR', ValueError:'VALUE_ERROR',
            TypeError:'TYPE_ERROR', RuntimeError:'RUNTIME_ERROR'}.get(type(error), 'UNAVAILABLE')
    check_active()
    return dict(phase=phase, state=state, error_kind=kind)


def run_case_program(spark, binding, mode, change, caller_binding, grant, *, secret_get):
    """The complete journal/Delta operation runs in one bounded Spark command.

    Only existing named credentials are resolved in native memory. No token or
    private key is a command argument, output or frozen case record.
    """
    checked_case_command(binding, mode, change, caller_binding, grant)
    signer = runtime_signer(binding, secret_get)
    objects = runtime_object_storage(binding, signer, secret_get)
    try:
        return execute_case_program(spark, objects, binding, mode, change, caller_binding, grant,
            check_active=lambda: None, now=lambda: datetime.now(timezone.utc))
    finally:
        objects.base_client.session.close()
