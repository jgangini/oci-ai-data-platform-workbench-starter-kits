import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.modules.ai_data_governance import case_program as program, case_transport as transport
from app.modules.ai_data_governance.case_changes import case_change, case_change_receipt
from app.modules.ai_data_governance.case_activity import case_operation_run_id
from app.modules.ai_data_governance.case_commit import CasePending
from app.modules.ai_data_governance.case_source import case_program_source
from app.modules.ai_data_governance.contracts import TreatmentDraft
from app.modules.ai_data_governance.inventory import identity
from tests.support.ai_dg_fixtures import AT, fixture


def command_fixture():
    args, repo, journal, controller = fixture()
    binding = {**controller.binding, "storage_root": "oci://artifacts@namespace/oci_artifacts"}
    operation = identity("governance-case-request", identity("activity-subject", args["subject"]),
        identity("activity-session", args["session"]), args["request_id"])
    args["run_id"] = case_operation_run_id(binding, {"operation_id": operation})
    repo.rows["AI_DG_RUN"] = []
    change = case_change(**args, action="OPEN", summary="Revisar una medición real")
    caller = dict(actor_ref=change["actor_ref"], session_ref=change["session_ref"],
        operation_id=change["operation_id"], component_revision=binding["component_revision"])
    return binding, change, caller, controller.authorize(change), repo, journal, controller


def execute(monkeypatch, mode="COMMIT_CASE"):
    binding, change, caller, grant, repo, journal, controller = command_fixture()
    monkeypatch.setattr(program, "DeltaRepository", lambda *a, **k: repo)
    monkeypatch.setattr(program, "GovernanceJournal", lambda *a, **k: journal)
    spark = SimpleNamespace(conf=SimpleNamespace(set=Mock()))
    result = program.execute_case_program(spark, object(), binding, mode, change, caller,
        None if mode == "RECONCILE_CASE" else grant, check_active=lambda: None, now=controller.now)
    return result, spark, repo


def test_fixed_case_program_uses_utc_exact_schema_and_only_the_control_tables(monkeypatch):
    result, spark, repo = execute(monkeypatch)
    assert result["verified"] is True and result["execution_verified"] is False
    assert set(repo.writes) <= {"AI_DG_CASE", "AI_DG_CASE_ASSET", "AI_DG_DECISION", "AI_DG_RUN", "AI_DG_EVENT"}
    spark.conf.set.assert_called_once_with("spark.sql.session.timeZone", "UTC")
    assert repo.rows["AI_DG_CASE"][0]["status"] == "OPEN"


@pytest.mark.parametrize('assembled', [False, True])
def test_case_program_installs_the_temporal_guard_at_the_repository_dispatch_boundary(monkeypatch, assembled):
    binding, change, caller, grant, repo, journal, controller = command_fixture()
    clock = [AT+timedelta(seconds=10)]
    captured = {}
    def repository(*args, **kwargs):
        captured['guard'] = kwargs['before_write']
        return repo
    if assembled:
        namespace = {}
        exec(compile(case_program_source(), 'consultation_case.py', 'exec'), namespace)
        namespace.update(DeltaRepository=repository, GovernanceJournal=lambda *a, **k: journal)
        execute = namespace['execute_case_program']
    else:
        monkeypatch.setattr(program, 'DeltaRepository', repository)
        monkeypatch.setattr(program, 'GovernanceJournal', lambda *a, **k: journal)
        execute = program.execute_case_program
    spark = SimpleNamespace(conf=SimpleNamespace(set=Mock()))
    assert execute(spark, object(), binding, 'COMMIT_CASE', change, caller, grant,
        check_active=lambda: None, now=lambda: clock[0])['verified'] is True
    captured['guard']('AI_DG_CASE')
    clock[0] = AT+timedelta(hours=2)
    for table in ('AI_DG_CASE', 'AI_DG_CASE_ASSET', 'AI_DG_DECISION'):
        with pytest.raises(PermissionError, match='expired'):
            captured['guard'](table)
    # Monitoring can still record failure or reconcile a historical outcome.
    captured['guard']('AI_DG_RUN')
    captured['guard']('AI_DG_EVENT')


def test_historical_reconciliation_needs_no_new_grant_or_delta_write():
    args, repo, journal, controller = fixture()
    change = case_change(**args, action="OPEN", summary="Revisar una medición real")
    def lose_ack(): raise TimeoutError("after all rows committed")
    repo.after_case = lose_ack
    with pytest.raises(TimeoutError): controller.commit(change)
    repo.after_case = None
    controller.now = lambda: AT + timedelta(hours=2)
    controller.authorize = Mock(side_effect=AssertionError("No fresh approval for historical readback"))
    repo.rows["AI_DG_SCOPE_ASSET"][0]["verified_version_id"] = "advanced"
    repo.rows["AI_DG_EVIDENCE"][0]["expires_at"] = AT
    writes = len(repo.writes)
    assert controller.reconcile(change) == case_change_receipt(change)
    assert len(repo.writes) == writes
    controller.authorize.assert_not_called()
    assert controller.reconcile(change) == case_change_receipt(change)


def pending_command_fixture(monkeypatch):
    binding,change,caller,grant,repo,journal,controller=command_fixture()
    monkeypatch.setattr(program,'DeltaRepository',lambda *a,**k:repo)
    monkeypatch.setattr(program,'GovernanceJournal',lambda *a,**k:journal)
    spark=SimpleNamespace(conf=SimpleNamespace(set=Mock()))
    insert=repo.insert_immutable
    def interrupted(table,rows):
        if table=='AI_DG_CASE_ASSET':raise KeyboardInterrupt('simulate remote process loss')
        return insert(table,rows)
    repo.insert_immutable=interrupted
    with pytest.raises(KeyboardInterrupt):
        program.execute_case_program(spark,object(),binding,'COMMIT_CASE',change,caller,grant,
            check_active=lambda:None,now=controller.now)
    repo.insert_immutable=insert
    return binding,change,caller,repo,journal,controller,spark


def test_native_reconcile_diagnoses_the_frozen_prewrite_checks_without_another_writer(monkeypatch):
    binding,change,caller,repo,journal,controller,spark=pending_command_fixture(monkeypatch)
    writes=len(repo.writes);objects=deepcopy(journal.rows)
    result=program.execute_case_program(spark,object(),binding,'RECONCILE_CASE',change,caller,None,
        check_active=lambda:None,now=controller.now)
    assert result['kind']=='GOVERNANCE_CASE_PENDING' and result['verified'] is False
    assert result['operation_id']==change['operation_id'] and result['input_digest']==identity('case-change',change)
    assert result['checks']==[
        {'phase':'FROZEN_START','state':'PASSED','error_kind':None},
        {'phase':'CURRENT_REFERENCES','state':'PASSED','error_kind':None},
        {'phase':'EXPECTED_PREVIOUS_STATE','state':'PASSED','error_kind':None}]
    assert len(repo.writes)==writes and journal.rows==objects


def test_pending_diagnostics_identify_the_rejected_check_without_exposing_exception_text(monkeypatch):
    binding,change,caller,repo,journal,controller,spark=pending_command_fixture(monkeypatch)
    monkeypatch.setattr(program.CaseCommitter,'_check_references',Mock(side_effect=TypeError('private SQL token')))
    writes=len(repo.writes);objects=deepcopy(journal.rows)
    result=program.execute_case_program(spark,object(),binding,'RECONCILE_CASE',change,caller,None,
        check_active=lambda:None,now=controller.now)
    assert result['checks'][1]==dict(phase='CURRENT_REFERENCES',state='REJECTED',error_kind='TYPE_ERROR')
    assert 'private' not in str(result) and len(repo.writes)==writes and journal.rows==objects


@pytest.mark.parametrize('kind',['outcomes','claims','start'])
def test_pending_diagnostics_require_original_admission_and_never_create_missing_records(monkeypatch,kind):
    binding,change,caller,repo,journal,controller,spark=pending_command_fixture(monkeypatch)
    if kind=='start':
        journal.rows.pop(('executions',identity('case-run-start',change['operation_id'])))
    else:
        journal.rows={key:value for key,value in journal.rows.items() if key[0]!=kind}
    writes=len(repo.writes);objects=deepcopy(journal.rows)
    with pytest.raises(CasePending):
        program.execute_case_program(spark,object(),binding,'RECONCILE_CASE',change,caller,None,
            check_active=lambda:None,now=controller.now)
    assert len(repo.writes)==writes and journal.rows==objects


def test_pending_diagnostics_preserve_the_historical_run_revision(monkeypatch):
    binding,change,caller,repo,journal,controller,spark=pending_command_fixture(monkeypatch)
    binding['component_revision']='e'*64
    caller['component_revision']=binding['component_revision']
    result=program.execute_case_program(spark,object(),binding,'RECONCILE_CASE',change,caller,None,
        check_active=lambda:None,now=controller.now)
    assert result['component_revision']=='e'*64
    assert result['checks'][1]==dict(phase='CURRENT_REFERENCES',state='PASSED',error_kind=None)


def test_assembled_program_executes_the_pending_branch_with_no_new_effects(monkeypatch):
    binding,change,caller,repo,journal,controller,spark=pending_command_fixture(monkeypatch)
    namespace={}
    exec(compile(case_program_source(),'consultation_case.py','exec'),namespace)
    namespace['DeltaRepository']=lambda *a,**k:repo
    namespace['GovernanceJournal']=lambda *a,**k:journal
    writes=len(repo.writes);objects=deepcopy(journal.rows)
    result=namespace['execute_case_program'](spark,object(),binding,'RECONCILE_CASE',change,caller,None,
        check_active=lambda:None,now=controller.now)
    assert transport.checked_case_pending_report(binding,change,result)==result
    assert len(repo.writes)==writes and journal.rows==objects


@pytest.mark.parametrize('kind',['outcomes','claims','start'])
def test_diagnostics_reject_corrupted_frozen_controls_without_repair(monkeypatch,kind):
    binding,change,caller,repo,journal,controller,spark=pending_command_fixture(monkeypatch)
    if kind=='outcomes':
        journal.rows['outcomes',identity('case-operation',change['operation_id'])]['case']['summary']='different'
    elif kind=='claims':
        for key in journal.rows:
            if key[0]=='claims' and journal.rows[key].get('operation_id')==change['operation_id']:
                journal.rows[key]['input_digest']='f'*64
    else:
        journal.rows['executions',identity('case-run-start',change['operation_id'])]['run']['scope_id']='foreign'
    writes=len(repo.writes);objects=deepcopy(journal.rows)
    with pytest.raises((CasePending,ValueError)):
        program.execute_case_program(spark,object(),binding,'RECONCILE_CASE',change,caller,None,
            check_active=lambda:None,now=controller.now)
    assert len(repo.writes)==writes and journal.rows==objects


def test_diagnostic_check_does_not_swallow_cancellation_or_changed_identity():
    import asyncio
    with pytest.raises(asyncio.CancelledError):
        program.case_pending_check('CURRENT_REFERENCES',Mock(side_effect=asyncio.CancelledError()),lambda:None)
    changed=[False]
    def caller():
        if changed[0]:raise PermissionError('caller changed')
    with pytest.raises(PermissionError):
        program.case_pending_check('CURRENT_REFERENCES',lambda:changed.__setitem__(0,True),caller)


@pytest.mark.parametrize('path,value',[
    (('operation_id',),'f'*64), (('input_digest',),'f'*64), (('component_revision',),'f'*64),
    (('verified',),0), (('verified',),True), (('message',),'SQL and credentials'),
    (('checks',0,'phase'),'ARBITRARY_CHECK'), (('checks',0,'state'),'REJECTED'),
    (('checks',1,'error_kind'),'private SQL'), (('checks',),[]),
    (('checks',1,'phase'),'FROZEN_START')])
def test_pending_report_rejects_foreign_or_open_ended_metadata(path,value):
    binding,change,*_=command_fixture()
    report=dict(kind='GOVERNANCE_CASE_PENDING',verified=False,operation_id=change['operation_id'],
        input_digest=identity('case-change',change),component_revision=binding['component_revision'],
        checks=[dict(phase=phase,state='PASSED',error_kind=None) for phase in
            ('FROZEN_START','CURRENT_REFERENCES','EXPECTED_PREVIOUS_STATE')])
    target=report
    for key in path[:-1]:target=target[key]
    target[path[-1]]=value
    with pytest.raises(ValueError):transport.checked_case_pending_report(binding,change,report)


def test_pending_transport_report_is_bound_to_reconcile_and_never_becomes_a_receipt(monkeypatch):
    binding,change,grant,context,client,output=transport_fixture()
    report=dict(kind='GOVERNANCE_CASE_PENDING',verified=False,operation_id=change['operation_id'],
        input_digest=identity('case-change',change),component_revision=binding['component_revision'],
        checks=[dict(phase='FROZEN_START',state='PASSED',error_kind=None),
            dict(phase='CURRENT_REFERENCES',state='REJECTED',error_kind='TYPE_ERROR'),
            dict(phase='EXPECTED_PREVIOUS_STATE',state='PASSED',error_kind=None)])
    output['outputs'][0]['value']=json.dumps(report)
    monkeypatch.setattr(transport,'_run_central_command',lambda *a,**k:output)
    with pytest.raises(transport.CaseReconciliationPending) as caught:
        transport.run_native_case(binding,client,case_program_source(),'RECONCILE_CASE',change,None,
            get_context=lambda:context)
    assert caught.value.diagnostic==report
    with pytest.raises(ValueError):
        transport.run_native_case(binding,client,case_program_source(),'COMMIT_CASE',change,grant,
            get_context=lambda:context)


def test_reconciliation_cannot_create_an_intent_claim_or_repair_partial_rows():
    args, repo, journal, controller = fixture()
    change = case_change(**args, action="OPEN", summary="Revisar una medición real")
    with pytest.raises(CasePending): controller.reconcile(change)
    assert journal.rows == {} and repo.writes == []
    insert = repo.insert_immutable
    def partial(name, rows):
        if name == "AI_DG_CASE": raise TimeoutError("still uncertain")
        insert(name, rows)
    repo.insert_immutable = partial
    with pytest.raises(TimeoutError): controller.commit(change)
    writes, objects = len(repo.writes), deepcopy(journal.rows)
    with pytest.raises(CasePending): controller.reconcile(change)
    assert len(repo.writes) == writes and journal.rows == objects


@pytest.mark.parametrize("field", ["actor_ref", "session_ref", "operation_id", "component_revision"])
def test_program_binds_the_actual_caller_and_revision_before_factories(monkeypatch, field):
    binding, change, caller, grant, _, _, _ = command_fixture()
    caller[field] = "foreign"
    repository, journal = Mock(), Mock()
    monkeypatch.setattr(program, "DeltaRepository", repository)
    monkeypatch.setattr(program, "GovernanceJournal", journal)
    with pytest.raises(PermissionError):
        program.execute_case_program(object(), object(), binding, "COMMIT_CASE", change, caller, grant,
            check_active=lambda: None, now=lambda: AT)
    repository.assert_not_called(); journal.assert_not_called()


@pytest.mark.parametrize("mode,grant", [("DELETE_CASE", None), ("RECONCILE_CASE", {}), ("COMMIT_CASE", None)])
def test_unknown_operations_and_mixed_reconcile_write_payloads_are_rejected(mode, grant):
    binding, change, caller, _, _, _, _ = command_fixture()
    with pytest.raises((ValueError, PermissionError)):
        program.checked_case_command(binding, mode, change, caller, grant)


def test_assembled_program_is_readable_and_executes_the_real_controller(monkeypatch):
    source = case_program_source()
    assert source == case_program_source()
    assert "base64" not in source and "ObjectStorageClient" in source
    tree = ast.parse(source)
    assert not any(isinstance(n, ast.ImportFrom) and n.level for n in ast.walk(tree))
    namespace = {}
    exec(compile(source, "consultation_case.py", "exec"), namespace)
    assert namespace["TreatmentDraft"].model_json_schema() == TreatmentDraft.model_json_schema()
    binding, change, caller, grant, repo, journal, controller = command_fixture()
    namespace["DeltaRepository"] = lambda *a, **k: repo
    namespace["GovernanceJournal"] = lambda *a, **k: journal
    spark = SimpleNamespace(conf=SimpleNamespace(set=Mock()))
    assert namespace["execute_case_program"](spark, object(), binding, "COMMIT_CASE", change, caller, grant,
        check_active=lambda: None, now=controller.now) == case_change_receipt(change)
    writes = len(repo.writes)
    assert namespace["execute_case_program"](spark, object(), binding, "RECONCILE_CASE", change, caller, None,
        check_active=lambda: None, now=controller.now) == case_change_receipt(change)
    assert len(repo.writes) == writes


def test_old_production_revision_cannot_start_another_case_write_after_upgrade():
    args, repo, _, controller = fixture()
    change = case_change(**args, action="OPEN", summary="Revisar una medición real")
    controller.binding["deployment_stage"] = "PROD"
    repo.rows["AI_DG_AGENT"][0]["deployment_version"] = "e" * 64
    with pytest.raises(PermissionError, match="activity run"):
        controller.commit(change)
    assert repo.writes == []


def transport_fixture():
    binding, change, _, grant, _, _, _ = command_fixture()
    grant["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    context = SimpleNamespace(dh_user_principal="opaque-fixture", subject_id="subject-a", session_id="session-a")
    client = SimpleNamespace(access_mode="CENTRAL_METADATA", signer=object(), check_identity=Mock())
    output = dict(status="SUCCEEDED", results_truncated=False,
        outputs=[dict(type="TEXT_PLAIN", value=json.dumps(case_change_receipt(change)))])
    return binding, change, grant, context, client, output


def test_transport_uses_the_central_signer_and_validates_the_exact_receipt(monkeypatch):
    binding, change, grant, context, client, output = transport_fixture()
    def run(config, signer, command, check_active, *, language,max_poll_attempts):
        assert config is binding and signer is client.signer and language == "PYTHON"
        assert max_poll_attempts==120
        assert 'case_input = json.loads(' in command
        check_active()
        return output
    monkeypatch.setattr(transport, "_run_central_command", run)
    assert transport.run_native_case(binding, client, case_program_source(), "COMMIT_CASE", change, grant,
        get_context=lambda: context) == case_change_receipt(change)


@pytest.mark.parametrize('terminal_after',[10,50])
def test_case_transport_observes_one_multiphase_command_until_its_receipt(monkeypatch,terminal_after):
    binding,change,grant,context,client,output=transport_fixture()
    dispatches=[]
    def run(config,signer,command,check_active,*,language,max_poll_attempts=40):
        dispatches.append(command)
        for attempt in range(max_poll_attempts):
            check_active()
            if attempt==terminal_after:return output
        raise TimeoutError('The simulated fixed command is still running; no receipt')
    monkeypatch.setattr(transport,'_run_central_command',run)
    assert transport.run_native_case(binding,client,case_program_source(),'COMMIT_CASE',change,grant,
        get_context=lambda:context)==case_change_receipt(change)
    assert len(dispatches)==1


def test_case_observation_has_a_wall_clock_bound_and_never_confirms_a_late_receipt(monkeypatch):
    binding,change,grant,context,client,output=transport_fixture()
    clock=[0]
    monkeypatch.setattr(transport.time,'monotonic',lambda:clock[0])
    def run(config,signer,command,check_active,*,language,max_poll_attempts):
        assert max_poll_attempts==120
        clock[0]=360
        check_active()
        return output
    runner=Mock(side_effect=run)
    monkeypatch.setattr(transport,'_run_central_command',runner)
    with pytest.raises(transport.CaseObservationExpired):
        transport.run_native_case(binding,client,case_program_source(),'COMMIT_CASE',change,grant,
            get_context=lambda:context)
    assert runner.call_count==1 and transport.CaseObservationExpired.status_code==504


@pytest.mark.parametrize("mutation", ["actor", "session", "truncated", "failed", "foreign_receipt", "extra_output", "encoded"])
def test_transport_rejects_changed_context_or_incomplete_results(monkeypatch, mutation):
    binding, change, grant, context, client, output = transport_fixture()
    def run(config, signer, command, check_active, *, language,max_poll_attempts):
        if mutation == "actor": context.subject_id = "foreign"
        elif mutation == "session": context.session_id = "other"
        elif mutation == "truncated": output["results_truncated"] = True
        elif mutation == "failed": output["status"] = "FAILED"
        elif mutation == "foreign_receipt": output["outputs"][0]["value"] = '{"verified":true}'
        elif mutation == "extra_output": output["outputs"].append(output["outputs"][0])
        elif mutation == "encoded": output["outputs"][0]["is_base64"] = True
        return output
    monkeypatch.setattr(transport, "_run_central_command", run)
    with pytest.raises((ValueError, PermissionError, RuntimeError)):
        transport.run_native_case(binding, client, case_program_source(), "COMMIT_CASE", change, grant,
            get_context=lambda: context)


def test_treatment_python_punctuation_remains_json_data():
    binding, change, caller, grant, _, _, _ = command_fixture()
    text = "'); __import__('os').system('never-run'); #"
    change["case"]["summary"] = text
    # Rebuild a valid operation containing that literal text.
    args, _, _, _ = fixture()
    change = case_change(**args, action="OPEN", summary=text)
    grant = {**grant, "input_digest": identity("case-change",change),
        "expires_at":(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}
    source = transport.case_command(binding, "", "COMMIT_CASE", change, caller, grant)
    assert len(ast.parse(source).body) == 2
    captured = []
    getter = Mock(name="native_secret_get")
    exec(compile(source, "command.py", "exec"), dict(json=json, spark=None,
        aidputils=SimpleNamespace(secrets=SimpleNamespace(get=getter)),
        run_case_program=lambda spark, **kw: captured.append(kw) or {}, print=lambda *a: None))
    assert captured[0]["change"]["case"]["summary"] == text
    assert captured[0]["secret_get"] is getter


def test_spark_entry_uses_injected_secret_getter_without_importing_ai_compute_package(monkeypatch):
    import builtins

    namespace = {}
    exec(compile(case_program_source(), "consultation_case.py", "exec"), namespace)
    native_import = builtins.__import__
    def spark_import(name, *args, **kwargs):
        if name == "aidputils":
            raise ModuleNotFoundError("AI Compute package is not a Spark module")
        return native_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", spark_import)
    binding, change, caller, grant, _, _, _ = command_fixture()
    grant["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    getter, signer = Mock(), object()
    objects = SimpleNamespace(base_client=SimpleNamespace(session=SimpleNamespace(close=Mock())))
    namespace["runtime_signer"] = Mock(return_value=signer)
    namespace["runtime_object_storage"] = Mock(return_value=objects)
    namespace["execute_case_program"] = Mock(return_value=case_change_receipt(change))
    result = namespace["run_case_program"](None, binding, "COMMIT_CASE", change, caller, grant, secret_get=getter)
    assert result == case_change_receipt(change)
    namespace["runtime_signer"].assert_called_once_with(binding, getter)
    namespace["runtime_object_storage"].assert_called_once_with(binding, signer, getter)
    objects.base_client.session.close.assert_called_once()
    namespace["execute_case_program"].side_effect = RuntimeError("controller failed")
    with pytest.raises(RuntimeError, match="controller failed"):
        namespace["run_case_program"](None, binding, "COMMIT_CASE", change, caller, grant, secret_get=getter)
    assert objects.base_client.session.close.call_count == 2
    caller["actor_ref"] = "foreign"
    with pytest.raises(PermissionError):
        namespace["run_case_program"](None, binding, "COMMIT_CASE", change, caller, grant, secret_get=getter)
    assert namespace["runtime_signer"].call_count == 2


def test_cancelled_case_transport_does_not_submit_a_command(monkeypatch):
    from threading import Event

    binding, change, grant, context, client, _ = transport_fixture()
    command = Mock()
    monkeypatch.setattr(transport, "_run_central_command", command)
    cancelled = Event(); cancelled.set()
    with pytest.raises(InterruptedError, match="cancelled"):
        transport.run_native_case(binding, client, case_program_source(), "COMMIT_CASE", change, grant,
            get_context=lambda: context, cancelled=cancelled)
    command.assert_not_called()


def test_async_case_transport_returns_the_exact_receipt(monkeypatch):
    import asyncio

    binding, change, grant, context, client, output = transport_fixture()
    command = Mock(return_value=output)
    monkeypatch.setattr(transport, "_run_central_command", command)
    result = asyncio.run(transport.native_case_operation(binding, client, case_program_source(),
        "COMMIT_CASE", change, grant, get_context=lambda: context))
    assert result == case_change_receipt(change)
    assert command.call_count == 1


@pytest.mark.parametrize("completed_remotely", [False, True])
def test_repeated_cancellation_drains_the_case_worker_without_reporting_success(monkeypatch, completed_remotely):
    import asyncio
    from threading import Event

    binding, change, grant, context, client, output = transport_fixture()
    started, release, finished = Event(), Event(), Event()
    def command(config, signer, source, check_active, *, language,max_poll_attempts):
        started.set()
        try:
            assert release.wait(3)
            if not completed_remotely: check_active()
            return output
        finally:
            finished.set()
    runner = Mock(side_effect=command)
    monkeypatch.setattr(transport, "_run_central_command", runner)
    async def exercise():
        pending = asyncio.create_task(transport.native_case_operation(binding, client, case_program_source(),
            "COMMIT_CASE", change, grant, get_context=lambda: context))
        assert await asyncio.to_thread(started.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError): await pending
        assert finished.is_set()
    asyncio.run(exercise())
    assert runner.call_count == 1
