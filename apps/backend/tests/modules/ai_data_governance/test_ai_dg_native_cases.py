import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.ai_data_governance import native_cases
from app.modules.ai_data_governance.case_changes import case_change_receipt, case_time
from app.modules.ai_data_governance.case_policy import case_authority_rule_id, case_authority_installation_receipt
from app.modules.ai_data_governance.inventory import canonical, identity
from tests.support.ai_dg_fixtures import CaseJournal
from tests.modules.ai_data_governance.test_ai_dg_case_confirmation import journal_call, FIRST_REQUEST, NEXT_REQUEST
from tests.modules.ai_data_governance.test_ai_dg_case_readmission import readmission_fixture, AT


def fixture(monkeypatch):
    reader, spec, proofs, projections, caller, query = readmission_fixture()
    binding = {**reader.binding, 'agent_key':'11111111-1111-4111-8111-111111111111',
        'component_revision':'a'*64, 'deployment_stage':'TEST'}
    context = SimpleNamespace(dh_user_principal='opaque', subject_id='operator', session_id='session')
    caller.access_mode = 'CENTRAL_METADATA'
    actor = identity('activity-subject', context.subject_id)
    rule = dict(rule_id=case_authority_rule_id(binding, spec['scope_id'], actor), rule_version=1,
        scope_id=spec['scope_id'], target_asset_id=None, rule_kind='ACCESS_DECLARATION',
        criteria_json=canonical(dict(schema_version=1,purpose='CASE_REGISTRATION',actor_ref=actor,
            actions=['OPEN','PROPOSE','APPROVE','REJECT','REVOKE'])),owner_ref='operator',
        valid_from=case_time(AT-timedelta(hours=1)), valid_to=case_time(AT+timedelta(hours=1)))
    policy = dict(rule=rule,scope=dict(scope_id=spec['scope_id'],platform_key=binding['platform_id'],
        workspace_key=binding['workspace_key'],catalog_key='catalog',schema_key='catalog.schema',enabled=True))
    journal = CaseJournal()
    journal.create('materialized',identity('case-authority-installed',rule['rule_id'],1),
        case_authority_installation_receipt(binding,rule))
    def rows(sql, **_):
        if 'AI_DG_RULE' in sql: return deepcopy([policy])
        return deepcopy(proofs if 'AI_DG_EVIDENCE' in sql else projections)
    query.side_effect = rows
    activity = SimpleNamespace(journal=journal,_worker=journal_call,check_active=Mock())
    actions = native_cases.NativeCaseActions(binding,caller,activity,'fixed-program',query,reader.hooks_factory,
        get_context=lambda:context,now=lambda:AT)
    executor = AsyncMock(side_effect=lambda binding,caller,program,mode,change,grant,**_:case_change_receipt(change))
    monkeypatch.setattr(native_cases,'native_case_operation',executor)
    return actions,spec,policy,proofs,context,executor,query


def test_composed_policy_readmission_consent_and_executor_use_only_the_fixed_case_command(monkeypatch):
    actions,spec,_,_,_,executor,query = fixture(monkeypatch)
    preview = asyncio.run(actions.offer(spec))
    result = asyncio.run(actions.confirm(preview['command'],request_id=FIRST_REQUEST))
    assert result['case_pending'] is False and result['result_state']=='answered'
    assert result['action_receipts'][0]['verified'] is True and result['action_receipts'][0]['execution_verified'] is False
    call = executor.call_args
    assert call.args[2:4]==('fixed-program','COMMIT_CASE')
    assert call.args[5]['input_digest']==identity('case-change',call.args[4])
    assert [call.kwargs['limit'] for call in query.call_args_list]==[2,32,16,2,32,16]
    query.side_effect=AssertionError('A replay must not acquire a new policy or assessment')
    assert asyncio.run(actions.confirm(preview['command'],request_id=NEXT_REQUEST))==result
    assert executor.call_args.args[3]=='RECONCILE_CASE' and executor.call_args.args[5] is None


def test_ordinary_questions_cannot_reach_policy_or_case_execution(monkeypatch):
    actions,_,_,_,_,executor,query = fixture(monkeypatch)
    assert asyncio.run(actions.confirm('¿Cuántas tablas hay?',request_id=FIRST_REQUEST)) is None
    query.assert_not_awaited(); executor.assert_not_awaited()


def test_expired_preview_explains_recovery_without_renewing_or_dispatching(monkeypatch):
    actions,spec,_,_,_,executor,query=fixture(monkeypatch)
    offered=asyncio.run(actions.offer(spec))
    actions.confirmations.now=lambda:AT+timedelta(minutes=5)
    query.reset_mock()
    result=asyncio.run(actions.confirm(offered['command'],request_id=FIRST_REQUEST))
    assert result['confirmation_expired'] is True and result['result_state']=='not_evaluable'
    assert result['case_pending'] is False and result['action_receipts']==[]
    assert 'venció' in result['answer_markdown'] and 'nueva vista previa' in result['answer_markdown']
    assert 'identidad' not in result['answer_markdown'] and 'CONFIRMAR' not in result['answer_markdown']
    executor.assert_not_awaited();query.assert_not_awaited()
    assert len([key for kind,key in actions.activity.journal.rows if kind=='batches'])==1
    assert not any(kind=='responses' for kind,_ in actions.activity.journal.rows)


@pytest.mark.parametrize('field',['subject_id','session_id'])
def test_expired_preview_still_rejects_a_different_native_caller(monkeypatch,field):
    actions,spec,_,_,context,executor,_=fixture(monkeypatch)
    offered=asyncio.run(actions.offer(spec));actions.confirmations.now=lambda:AT+timedelta(minutes=5)
    setattr(context,field,'other')
    with pytest.raises(PermissionError):asyncio.run(actions.confirm(offered['command'],request_id=FIRST_REQUEST))
    executor.assert_not_awaited()


def test_missing_installed_policy_cannot_offer_or_dispatch_a_case(monkeypatch):
    actions,spec,_,_,_,executor,query=fixture(monkeypatch)
    query.return_value=[]; query.side_effect=None
    with pytest.raises(PermissionError): asyncio.run(actions.offer(spec))
    assert not any(kind=='batches' for kind,_ in actions.activity.journal.rows)
    executor.assert_not_awaited()


def test_changed_evidence_after_preview_stops_the_real_composed_confirmation(monkeypatch):
    actions,spec,_,proofs,_,executor,_=fixture(monkeypatch)
    preview=asyncio.run(actions.offer(spec))
    proofs[0]['evidence']['facts_json']='{}'
    with pytest.raises(PermissionError): asyncio.run(actions.confirm(preview['command'],request_id=FIRST_REQUEST))
    executor.assert_not_awaited()
    assert not any(kind=='responses' for kind,_ in actions.activity.journal.rows)


@pytest.mark.parametrize('failure',[TimeoutError(),ValueError(),PermissionError()])
def test_uncertain_or_rejected_dispatch_stays_pending_and_replay_is_read_only(monkeypatch,failure):
    actions,spec,_,_,_,executor,_=fixture(monkeypatch)
    preview=asyncio.run(actions.offer(spec)); executor.side_effect=failure
    pending=asyncio.run(actions.confirm(preview['command'],request_id=FIRST_REQUEST))
    assert pending['case_pending'] is True and pending['action_receipts']==[]
    executor.side_effect=lambda b,c,p,m,change,grant,**_:case_change_receipt(change)
    recovered=asyncio.run(actions.confirm(preview['command'],request_id=NEXT_REQUEST))
    assert recovered['case_ref']==pending['case_ref'] and recovered['case_pending'] is False
    assert [call.args[3] for call in executor.call_args_list]==['COMMIT_CASE','RECONCILE_CASE']


@pytest.mark.parametrize('status,expected', [(504,True), ('504',False), (True,False), (500,False)])
def test_pending_command_reports_only_a_typed_timeout_without_leaking_exception(monkeypatch,status,expected):
    actions,spec,_,_,_,executor,_=fixture(monkeypatch)
    preview=asyncio.run(actions.offer(spec))
    failure=RuntimeError('private SQL and credentials must not appear')
    failure.status_code=status
    executor.side_effect=failure
    result=asyncio.run(actions.confirm(preview['command'],request_id=FIRST_REQUEST))
    assert result['case_pending'] and result['action_receipts']==[]
    assert ('tiempo de comprobación' in result['answer_markdown']) is expected
    assert 'private' not in result['answer_markdown'] and 'credentials' not in str(result)
    assert executor.call_args.args[3]=='COMMIT_CASE'


def test_native_recovery_after_a_revision_change_only_dispatches_the_frozen_readback(monkeypatch):
    actions,spec,_,_,_,executor,query=fixture(monkeypatch)
    preview=asyncio.run(actions.offer(spec))
    executor.side_effect=TimeoutError()
    pending=asyncio.run(actions.confirm(preview['command'],request_id=FIRST_REQUEST))
    original=deepcopy(executor.call_args.args[4])
    actions.binding['component_revision']='e'*64
    actions.confirmations.binding['component_revision']='e'*64
    query.side_effect=AssertionError('No policy or evidence renewal during recovery')
    executor.side_effect=lambda b,c,p,m,change,grant,**_:case_change_receipt(change)
    recovered=asyncio.run(actions.confirm(preview['command'],request_id=NEXT_REQUEST))
    assert executor.call_args.args[3:]==('RECONCILE_CASE',original,None)
    assert recovered['case_ref']==pending['case_ref'] and recovered['case_pending'] is False


def test_native_pending_diagnostic_is_partial_not_a_receipt_or_an_instruction_to_repeat(monkeypatch):
    actions,spec,_,_,_,executor,_=fixture(monkeypatch)
    offered=asyncio.run(actions.offer(spec))
    first=asyncio.run(actions.confirm(offered['command'],request_id=FIRST_REQUEST))
    change=executor.call_args.args[4]
    report=dict(kind='GOVERNANCE_CASE_PENDING',verified=False,operation_id=change['operation_id'],
        input_digest=identity('case-change',change),component_revision=actions.binding['component_revision'],
        checks=[dict(phase='FROZEN_START',state='PASSED',error_kind=None),
            dict(phase='CURRENT_REFERENCES',state='REJECTED',error_kind='TYPE_ERROR'),
            dict(phase='EXPECTED_PREVIOUS_STATE',state='PASSED',error_kind=None)])
    executor.side_effect=native_cases.CaseReconciliationPending(report)
    result=asyncio.run(actions.confirm(offered['command'],request_id=NEXT_REQUEST))
    assert result['case_pending'] and result['result_state']=='partial' and result['action_receipts']==[]
    assert result['case_ref']==first['case_ref'] and result['case_diagnostic']==report
    assert 'Repite' not in result['answer_markdown'] and executor.call_args.args[3]=='RECONCILE_CASE'
    assert 'Referencias actuales del registro: error técnico de tipo.' in result['answer_markdown']
    assert 'Estado anterior del caso: comprobado.' in result['answer_markdown']
    assert 'TYPE_ERROR' not in result['answer_markdown'] and 'causa del fallo original' in result['answer_markdown']


@pytest.mark.parametrize('field,value',[('execution_verified',True),('verified',1),('case_id','f'*64)])
def test_a_mismatched_or_coerced_receipt_cannot_be_reported_as_confirmed(monkeypatch,field,value):
    actions,spec,_,_,_,executor,_=fixture(monkeypatch)
    preview=asyncio.run(actions.offer(spec))
    executor.side_effect=lambda b,c,p,m,change,grant,**_:{**case_change_receipt(change),field:value}
    result=asyncio.run(actions.confirm(preview['command'],request_id=FIRST_REQUEST))
    assert result['case_pending'] and result['action_receipts']==[]


@pytest.mark.parametrize('failure',[None,TimeoutError()])
def test_changed_native_user_during_dispatch_cannot_receive_another_users_receipt_or_pending_reference(monkeypatch,failure):
    actions,spec,_,_,context,executor,_=fixture(monkeypatch)
    preview=asyncio.run(actions.offer(spec))
    def changed(b,c,p,m,change,grant,**_):
        context.subject_id='other-user'
        if failure: raise failure
        return case_change_receipt(change)
    executor.side_effect=changed
    with pytest.raises(PermissionError): asyncio.run(actions.confirm(preview['command'],request_id=FIRST_REQUEST))


def test_case_dispatch_cancellation_propagates_without_a_pending_or_success_response(monkeypatch):
    actions,spec,_,_,_,executor,_=fixture(monkeypatch)
    preview=asyncio.run(actions.offer(spec)); executor.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): asyncio.run(actions.confirm(preview['command'],request_id=FIRST_REQUEST))


@pytest.mark.parametrize('mode,activity,program',[('DELEGATED',object(),'fixed'),('CENTRAL_METADATA',None,'fixed'),
    ('CENTRAL_METADATA',object(),'')])
def test_missing_central_identity_journal_or_fixed_program_cannot_enable_cases(mode,activity,program):
    with pytest.raises(PermissionError):
        native_cases.NativeCaseActions({},SimpleNamespace(access_mode=mode),activity,program,None,None,get_context=None)


def test_the_readable_served_assembly_executes_the_same_confirmation_and_policy_flow(monkeypatch):
    import sys
    from types import ModuleType
    from app.modules.ai_data_governance.governance_source import governance_agent_source
    from tests.support.ai_dg_fixtures import activity_fixture

    config,_,_=activity_fixture(); config.pop('component_revision')
    config['storage_root']='oci://artifacts@namespace/oci_artifacts'
    module=ModuleType('native_case_assembly'); monkeypatch.setitem(sys.modules,module.__name__,module)
    exec(compile(governance_agent_source(config),'governance_runtime.py','exec'),module.__dict__)
    original,spec,_,_,_,executor,query=fixture(monkeypatch)
    monkeypatch.setattr(module,'native_case_operation',executor)
    actions=module.NativeCaseActions(original.binding,original.caller,original.activity,module.CASE_PROGRAM,
        query,original.readmission.hooks_factory,get_context=original.get_context,now=lambda:AT)
    preview=asyncio.run(actions.offer(spec))
    result=asyncio.run(actions.confirm(preview['command'],request_id=FIRST_REQUEST))
    assert result['action_receipts'][0]['verified'] is True and result['case_pending'] is False
    assert executor.call_args.args[2]==module.CASE_PROGRAM
    assert module.CASE_PROGRAM.startswith('\n"""Fixed AI_DG')
    assert 'COMMIT_CASE' not in module.READ_OPERATIONS and 'native_case_operation' not in module.READ_OPERATIONS


def preview_state(proofs):
    assets=[dict(asset_id=r['asset_id'],version_id=r['version_id'],name=r['qualified_name']) for r in proofs]
    return dict(intent=dict(intent='treatment',case_reference=None),assets=assets,treatment=dict(problem='Falta medición de calidad',
        asset_ids=[r['asset_id'] for r in assets],expected_versions=[r['version_id'] for r in assets],
        suggested_action='Medir nulidad',responsible_ref=None,verification_plan='Leer la medición vigente',remaining_gaps=['Medición pendiente']))


def test_native_opening_preview_uses_persisted_catalog_proofs_without_a_grant_or_case_write(monkeypatch):
    actions,_,_,proofs,_,executor,_=fixture(monkeypatch)
    result=asyncio.run(actions.preview(preview_state(proofs),{}))
    assert result['available'] is True and result['action']=='OPEN' and result['case_ref'] is None
    assert result['command'].startswith('CONFIRMAR ABRIR ')
    assert result['summary']=='Falta medición de calidad\n\nAlcance y pendientes: Medición pendiente'
    assert {(r['asset_id'],r['version_id']) for r in result['assessed_assets']}=={(r['asset_id'],r['version_id']) for r in proofs}
    executor.assert_not_awaited()
    assert not any(kind=='responses' for kind,_ in actions.activity.journal.rows)


def test_opening_keeps_evidence_limits_in_visible_and_frozen_case_summary(monkeypatch):
    from app.modules.ai_data_governance.case_previews import render_case_preview

    actions,_,_,proofs,_,executor,_=fixture(monkeypatch)
    state=preview_state(proofs)
    state['treatment']['problem']='No hay mediciones de calidad pertinentes de la revisión confirmada.'
    limits=['En la evidencia recibida no hay mediciones; no se evaluó todo el catálogo.',
        'El linaje parcial sin relaciones no demuestra aislamiento.']
    state['treatment']['remaining_gaps']=limits
    preview=asyncio.run(actions.preview(state,{}))
    assert preview['available'] is True and preview['action']=='OPEN' and preview['treatment'] is None
    executor.assert_not_awaited()
    for limit in limits:
        assert limit in preview['summary']
        assert limit in render_case_preview(preview)
    result=asyncio.run(actions.confirm(preview['command'],request_id=FIRST_REQUEST))
    change=executor.call_args.args[4]
    assert change['action']=='OPEN' and change['case']['summary']==preview['summary']
    assert change['case']['treatment_json'] is None and not change['decisions']
    assert result['action_receipts']==[case_change_receipt(change)]


def test_oversized_opening_summary_cannot_drop_limits_or_offer_a_confirmation(monkeypatch):
    actions,_,_,proofs,_,executor,_=fixture(monkeypatch)
    state=preview_state(proofs)
    state['treatment']['remaining_gaps']=['x'*2000]
    offer=AsyncMock(wraps=actions.confirmations.offer)
    monkeypatch.setattr(actions.confirmations,'offer',offer)
    result=asyncio.run(actions.preview(state,{}))
    assert result['available'] is False and '2000 caracteres' in result['reason']
    assert 'conserve esos límites' in result['reason'] and 'command' not in result
    offer.assert_not_awaited(); executor.assert_not_awaited()
    assert not any(kind=='batches' for kind,_ in actions.activity.journal.rows)


@pytest.mark.parametrize('failure',['policy','version','draft'])
def test_changed_opening_assessment_or_missing_policy_cannot_offer_a_confirmation(monkeypatch,failure):
    actions,_,policy,proofs,_,executor,_=fixture(monkeypatch)
    state=preview_state(proofs)
    if failure=='policy':policy['rule']['criteria_json']='{}'
    if failure=='version':state['assets'][0]['version_id']='f'*64
    if failure=='draft':state['treatment']['asset_ids']=['another']
    result=asyncio.run(actions.preview(state,{}))
    assert result['available'] is False and 'command' not in result
    executor.assert_not_awaited()


def test_revocation_preview_retains_the_real_case_head_and_does_not_require_its_old_assets_to_be_current(monkeypatch):
    from app.modules.ai_data_governance.case_reader import case_history_snapshot
    from tests.modules.ai_data_governance.test_ai_dg_case_readmission import history_fixture

    actions,_,_,stored,_,executor,query=fixture(monkeypatch)
    _,spec,proofs,history=history_fixture()
    for proof in stored:
        proof.update(current_version_id='later',verified_version_id='later',lifecycle_state='RETIRED')
        proof['evidence']['expires_at']=(AT-timedelta(days=1)).isoformat()
    actions.caller.side_effect=AssertionError('Historical revocation must not query the current table')
    actions.confirmations.now=lambda:AT+timedelta(seconds=10)
    ordinary=query.side_effect
    query.side_effect=lambda sql,**kw:deepcopy(history) if '`AI_DG_CASE`' in sql else ordinary(sql,**kw)
    state=dict(intent=dict(intent='decision',case_reference=spec['previous']['case_id'],decision_action='revoke'),
        assets=[case_history_snapshot(actions.binding,spec['previous']['case_id'],history)['resource']])
    result=asyncio.run(actions.preview(state,{}))
    assert result['available'] is True and result['action']=='REVOKE'
    executor.assert_not_awaited()
    actions.caller.assert_not_called()


def test_a_changed_case_after_the_model_review_is_not_adopted_by_a_fresh_preview(monkeypatch):
    from app.modules.ai_data_governance.case_reader import case_history_snapshot
    from tests.modules.ai_data_governance.test_ai_dg_case_readmission import history_fixture

    actions,_,_,_,_,executor,query=fixture(monkeypatch)
    _,spec,_,history=history_fixture()
    state=dict(intent=dict(intent='decision',case_reference=spec['previous']['case_id'],decision_action='revoke'),
        assets=[case_history_snapshot(actions.binding,spec['previous']['case_id'],history)['resource']])
    body=json.loads(history[0]['data']);body['summary']='Cambio posterior';history[0]['data']=canonical(body)
    ordinary=query.side_effect
    query.side_effect=lambda sql,**kw:deepcopy(history) if '`AI_DG_CASE`' in sql else ordinary(sql,**kw)
    result=asyncio.run(actions.preview(state,{}))
    assert result['available'] is False and 'command' not in result
    executor.assert_not_awaited()


@pytest.mark.parametrize('action',['approve','reject'])
def test_native_decision_preview_shows_the_frozen_treatment_to_be_decided(monkeypatch,action):
    from app.modules.ai_data_governance.case_reader import case_history_snapshot
    from app.modules.ai_data_governance.case_previews import render_case_preview
    from tests.modules.ai_data_governance.test_ai_dg_case_readmission import history_fixture

    actions,_,_,_,_,executor,query=fixture(monkeypatch)
    _,spec,_,history=history_fixture();history[:]=[r for r in history if r['record_kind']!='DECISION']
    actions.confirmations.now=lambda:AT+timedelta(seconds=10)
    ordinary=query.side_effect
    query.side_effect=lambda sql,**kw:deepcopy(history) if '`AI_DG_CASE`' in sql else ordinary(sql,**kw)
    state=dict(intent=dict(intent='decision',case_reference=spec['previous']['case_id'],decision_action=action),
        assets=[case_history_snapshot(actions.binding,spec['previous']['case_id'],history)['resource']])
    result=asyncio.run(actions.preview(state,{}))
    assert result['available'] is True and result['action']==action.upper()
    assert result['treatment']==json.loads(spec['previous']['treatment_json'])
    text=render_case_preview(result)
    assert 'Medir nulidad' in text and 'Leer medición vigente' in text and 'catalog.schema.orders' in text
    executor.assert_not_awaited()


def test_real_graph_and_native_preview_propose_for_the_same_case_and_assessed_catalog_assets(monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver
    from app.modules.ai_data_governance.consultation import ConsultationHooks
    from app.modules.ai_data_governance.governance_graph import build_governance_graph
    from app.modules.ai_data_governance.native_governance import render_governance_response
    from tests.modules.ai_data_governance.test_ai_dg_case_readmission import history_fixture

    actions,_,_,proofs,_,executor,query=fixture(monkeypatch)
    _,spec,_,history=history_fixture()
    history[:]=[row for row in history if row['record_kind']!='DECISION']
    prior=json.loads(history[0]['data']);prior.update(status='OPEN',change_digest=None,treatment_json=None)
    history[0]['data']=canonical(prior)
    ordinary=query.side_effect
    query.side_effect=lambda sql,**kw:deepcopy(history) if '`AI_DG_CASE`' in sql else ordinary(sql,**kw)
    actions.confirmations.now=lambda:AT+timedelta(seconds=10)
    hooks=ConsultationHooks(actions.binding,actions.caller,query)
    draft=preview_state(proofs)['treatment']
    sources=[]
    async def model(phase,payload,*_):
        if phase=='G1':return canonical(dict(intent='treatment',case_reference=prior['case_id']))
        if phase=='G3':
            sources[:]=[row['evidence_id'] for row in payload['evidence'] if row['kind']=='CATALOG']
            return canonical(dict(evidence_ids=sources,supported_claims=[dict(text='Contexto del catálogo observado',
                evidence_ids=sources,kind='observed')],conflicts=[],gaps=[],coverage='partial',next_action='answer'))
        if phase=='G4':return canonical(dict(findings=[dict(text='Requiere identificar el responsable',
            evidence_ids=sources,kind='recommendation')],potential_impacts=[],limitations=[],treatment_needed=True))
        if phase=='G5':return canonical(draft)
        return canonical(dict(answer_markdown='Preparé una propuesta sobre los activos del caso [1].',
            cited_evidence_ids=sources,limitations=[],case_ref=None,result_state='partial'))
    graph=build_governance_graph(authorize=hooks.authorize,resolve_assets=hooks.resolve_assets,read=hooks.read,
        call_model=model,capabilities={'read_case','read_asset_snapshot'},checkpointer=InMemorySaver(),
        reauthorize_evidence=hooks.reauthorize_evidence,prepare_case=actions.preview)
    result=asyncio.run(graph.ainvoke({'question':'Propón tratamiento para el caso '+prior['case_id']},
        {'configurable':{'thread_id':'native-proposal-preview'}}))
    preview=result['case_preview']
    assert preview['available'] is True and preview['action']=='PROPOSE' and preview['case_ref']==prior['case_id']
    assert preview['treatment']==result['treatment']
    text=render_governance_response(result)
    assert 'Medir nulidad' in text and 'Leer la medición vigente' in text and 'CONFIRMAR PROPONER' in text
    executor.assert_not_awaited()
    frozen=next(value for (kind,_),value in actions.activity.journal.rows.items() if kind=='batches')
    assert frozen['specification']['previous']['case_id']==prior['case_id']
    assert frozen['specification']['draft']==result['treatment']
