import ast
import asyncio
from contextlib import contextmanager,nullcontext
from copy import deepcopy
import json
import sys
from types import ModuleType,SimpleNamespace
from unittest.mock import AsyncMock,Mock

import pytest

from app.ai_data_governance import native_activity,native_governance
from app.ai_data_governance.activity_commit import ActivityPending,activity_receipt
from app.ai_data_governance.governance_graph import build_governance_graph,READ_OPERATIONS
from app.ai_data_governance.governance_source import governance_agent_source
from test_ai_dg_activity import activity_fixture,MemoryJournal
from test_ai_dg_response_rendering import answer_state


def activity_harness(monkeypatch):
    binding,_,_=activity_fixture(); binding['storage_root']='oci://artifacts@namespace/oci_artifacts'
    objects=SimpleNamespace(base_client=SimpleNamespace(session=SimpleNamespace(close=Mock())))
    journal=MemoryJournal(); runner=Mock(side_effect=lambda mode,batch:activity_receipt(batch))
    monkeypatch.setattr(native_activity,'runtime_object_storage',lambda *a:objects)
    monkeypatch.setattr(native_activity,'GovernanceJournal',lambda *a,**kw:journal)
    monkeypatch.setattr(native_activity,'run_native_activity',lambda binding,signer,program,mode,batch,check,**kw:runner(mode,batch))
    transport=SimpleNamespace(access_mode='CENTRAL_METADATA',signer=object(),check_identity=Mock())
    return native_activity.NativeActivity(binding,transport,'fixed-program',Mock()),journal,runner,objects


def test_real_graph_records_phase_transitions_without_extra_model_or_catalog_calls(monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver
    activity,journal,runner,_=activity_harness(monkeypatch)
    authorize,read=AsyncMock(),AsyncMock()
    model=AsyncMock(return_value=json.dumps({'intent':'meaning','clarification':'¿Qué tabla quieres consultar?'}))
    graph=build_governance_graph(authorize=authorize,resolve_assets=AsyncMock(),read=read,call_model=model,
        capabilities=READ_OPERATIONS,checkpointer=InMemorySaver(),observer=activity.observe)
    async def run():
        await activity.begin('subject','session','Qué significa esa tabla?')
        result=await graph.ainvoke({'question':'Qué significa esa tabla?'},{'configurable':{'thread_id':'activity'}})
        receipt=await activity.finish(result)
        return receipt
    assert asyncio.run(run())['verified'] is True
    model.assert_awaited_once(); read.assert_not_awaited()
    assert [(e['phase'],e['transition']) for e in activity.events]==[('invocation','started'),
        *[(phase,transition) for phase in ('admission','understand','retrieve','assess','respond') for transition in ('started','completed')],
        ('invocation','completed')]
    assert runner.call_count==1
    batch=runner.call_args.args[1]
    assert batch['run']['run_kind']=='AGENT' and batch['run']['attempt_no']==1
    assert {e['event_type'] for e in batch['events']}=={'START','PROGRESS','END'}
    assert len(journal.values)==4


def test_unknown_write_is_pending_and_keeps_its_frozen_batch(monkeypatch):
    activity,journal,runner,_=activity_harness(monkeypatch)
    runner.side_effect=TimeoutError('uncertain')
    async def run():
        await activity.begin('subject','session','question')
        with pytest.raises(ActivityPending): await activity.finish({'response':{'result_state':'answered'}})
    asyncio.run(run())
    assert runner.call_count==1
    assert sum(kind=='outcomes' for kind,key in journal.values)==1
    assert not any(kind=='materialized' for kind,key in journal.values)


def test_cancellation_during_a_model_phase_freezes_a_cancelled_attempt_without_repeating_the_model(monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver
    activity,journal,runner,_=activity_harness(monkeypatch)
    async def run():
        entered=asyncio.Event()
        async def model(*args):
            entered.set(); await asyncio.Event().wait()
        counted=AsyncMock(side_effect=model)
        graph=build_governance_graph(authorize=AsyncMock(),resolve_assets=AsyncMock(),read=AsyncMock(),
            call_model=counted,capabilities=READ_OPERATIONS,checkpointer=InMemorySaver(),observer=activity.observe)
        await activity.begin('subject','session','question')
        task=asyncio.create_task(native_governance.invoke_consultation_graph(graph,'question',
            {'configurable':{'thread_id':'cancelled'}},activity))
        await entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        counted.assert_awaited_once()
    asyncio.run(run())
    batch=next(value for (kind,_),value in journal.values.items() if kind=='outcomes')
    assert batch['run']['status']=='CANCELLED'
    assert any(json.loads(row['metrics_json'])['phase']=='understand' and row['event_type']=='ERROR' for row in batch['events'])
    assert sum(kind=='outcomes' for kind,key in journal.values)==1
    runner.assert_not_called()


def test_cancellation_during_start_waits_for_the_bounded_worker_and_never_calls_the_model(monkeypatch):
    from threading import Event
    activity,journal,runner,_=activity_harness(monkeypatch)
    entered,release=Event(),Event()
    create=journal.create
    def delayed(kind,key,value):
        entered.set(); assert release.wait(2)
        return create(kind,key,value)
    journal.create=delayed
    async def run():
        task=asyncio.create_task(activity.begin('subject','session','question'))
        while not entered.is_set(): await asyncio.sleep(0)
        task.cancel();release.set()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())
    assert activity.cancelled.is_set() and not activity.events
    assert len(journal.values)==1 and next(iter(journal.values))[0]=='executions'
    runner.assert_not_called()


def test_cancellation_while_freezing_the_result_preserves_the_batch_without_starting_spark(monkeypatch):
    from threading import Event
    activity,journal,runner,_=activity_harness(monkeypatch)
    entered,release=Event(),Event();create=journal.create
    def delayed(kind,key,value):
        if kind=='outcomes':
            entered.set();assert release.wait(2)
        return create(kind,key,value)
    journal.create=delayed
    async def run():
        await activity.begin('subject','session','question')
        task=asyncio.create_task(activity.finish({'response':{'result_state':'answered'}}))
        while not entered.is_set(): await asyncio.sleep(0)
        task.cancel();release.set()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())
    assert sum(kind=='outcomes' for kind,key in journal.values)==1
    assert not any(kind in {'claims','materialized'} for kind,key in journal.values)
    runner.assert_not_called()


@pytest.mark.parametrize('repeat_count', [2, 3])
@pytest.mark.parametrize('worker_fails', [False, True])
def test_repeated_cancellation_drains_the_worker_before_clients_close(monkeypatch, repeat_count, worker_fails):
    from threading import Event
    activity, _, _, objects = activity_harness(monkeypatch)
    entered, release, finished = Event(), Event(), Event()
    closed_after_finish = []
    objects.base_client.session.close.side_effect = lambda: closed_after_finish.append(finished.is_set())

    def writer():
        entered.set()
        try:
            assert release.wait(2)
            if worker_fails:
                raise RuntimeError('The admitted worker failed')
        finally:
            finished.set()

    async def run():
        async def invocation():
            try:
                await activity._worker(writer)
            finally:
                activity.close()
        task = asyncio.create_task(invocation())
        while not entered.is_set():
            await asyncio.sleep(0)
        try:
            task.cancel()
            while not activity.cancelled.is_set():
                await asyncio.sleep(0)
            for _ in range(repeat_count - 1):
                task.cancel()
                for _ in range(3):
                    await asyncio.sleep(0)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert finished.is_set()
        assert closed_after_finish == [True]
    asyncio.run(run())


def test_served_assembly_contains_the_fixed_activity_program_and_all_required_helpers(monkeypatch):
    config,_,_=activity_fixture(); config.pop('component_revision')
    config['storage_root']='oci://artifacts@namespace/oci_artifacts'
    source=governance_agent_source(config)
    module=ModuleType('governance_activity_runtime');monkeypatch.setitem(sys.modules,module.__name__,module)
    exec(compile(source,'governance_runtime.py','exec'),module.__dict__)
    assert module.NativeActivity and module.ActivityCommitter and module.GovernanceJournal
    assert module.CONFIG['deployment_stage']=='TEST'
    assert module.ACTIVITY_PROGRAM.startswith('\n"""Fixed AI_DG')
    assert 'base64' not in module.ACTIVITY_PROGRAM and 'exec(' not in module.ACTIVITY_PROGRAM
    assert 'ACTIVITY_PROGRAM = r\'\'\'\n' in source.decode()
    assert 'REGISTER_AGENT' not in module.READ_OPERATIONS and 'APPEND_ACTIVITY' not in module.READ_OPERATIONS
    monkeypatch.setattr('app.ai_data_governance.governance_source.activity_program_source',lambda:module.ACTIVITY_PROGRAM+'\n# changed')
    changed=governance_agent_source(config)
    config_node=lambda src:next(ast.literal_eval(n.value) for n in ast.parse(src).body if isinstance(n,ast.Assign)
        and any(isinstance(t,ast.Name) and t.id=='CONFIG' for t in n.targets))
    assert config_node(source)['component_revision']!=config_node(changed)['component_revision']


@pytest.mark.parametrize('omit',['storage_root','deployment_stage','agent_code_path'])
def test_partial_activity_binding_is_rejected(omit):
    config,_,_=activity_fixture();config.pop('component_revision')
    config['storage_root']='oci://artifacts@namespace/oci_artifacts';config.pop(omit)
    with pytest.raises(ValueError): governance_agent_source(config)


@pytest.mark.parametrize('pending',[False,True])
def test_native_answer_preserves_sources_and_distinguishes_recording_from_materialization(monkeypatch,pending):
    state=answer_state()
    helper=SimpleNamespace(pre_invoke_setup=lambda **kw:{},flow_setup=lambda **kw:nullcontext())
    monkeypatch.setitem(sys.modules,'aidputils.agents.toolkit.agent_helper',helper)
    monkeypatch.setitem(sys.modules,'aidputils.agents.auth.util',SimpleNamespace(auth_utils=SimpleNamespace(get_auth_context=Mock())))
    monkeypatch.setitem(sys.modules,'aidputils',SimpleNamespace(secrets=SimpleNamespace(get=Mock())))
    monkeypatch.setattr(native_governance,'consultation_config',lambda *a:{'metadata':{}})
    monkeypatch.setattr(native_governance,'_caller_identity',lambda *a:('principal','subject','session'))
    caller=SimpleNamespace(session=SimpleNamespace(close=Mock()))
    monkeypatch.setattr(native_governance,'native_central_transport',lambda *a:caller)
    hooks=SimpleNamespace(authorize=AsyncMock(),resolve_assets=AsyncMock(),read=AsyncMock(),_read_admitted=AsyncMock(),capabilities=(),reauthorize_evidence=AsyncMock(),_validate_admitted_evidence=AsyncMock(), _reobserve_admitted_evidence=AsyncMock())
    monkeypatch.setattr(native_governance,'ConsultationHooks',lambda *a,**kw:hooks)
    def build(**kwargs):
        assert kwargs['reauthorize_evidence'] is hooks._validate_admitted_evidence
        assert kwargs['reobserve_evidence'] is hooks._reobserve_admitted_evidence
        assert kwargs['authorize'] is hooks.authorize
        assert kwargs['read'] is hooks._read_admitted
        return SimpleNamespace(ainvoke=AsyncMock(return_value=state))
    monkeypatch.setattr(native_governance,'build_governance_graph',build)
    activity=SimpleNamespace(begin=AsyncMock(),observe=Mock(),finish=AsyncMock(return_value={'recorded':True,'materialized':False}),close=Mock())
    if pending: activity.finish.side_effect=ActivityPending('uncertain')
    monkeypatch.setattr(native_governance,'NativeActivity',lambda *a:activity)
    agent=native_governance.DataGovernanceAgent();agent.binding={'storage_root':'bound','component_revision':'d'*64};agent.memory=object()
    response=asyncio.run(agent.invoke('question'))['messages'][0]
    assert '[1] Catálogo:' in response.content
    assert response.additional_kwargs.get('activity_pending',False) is pending
    assert ('activity_record' in response.additional_kwargs) is not pending
    assert 'activity_receipt' not in response.additional_kwargs
    activity.finish.assert_awaited_once_with(state,failed=False,cancelled=False,materialize=False)
    activity.close.assert_called_once();caller.session.close.assert_called_once()


@pytest.mark.parametrize('pending',[False,True])
def test_raw_native_case_confirmation_returns_the_trusted_result_without_building_a_model_graph(monkeypatch,pending):
    helper=SimpleNamespace(pre_invoke_setup=lambda **kw:{},flow_setup=lambda **kw:nullcontext())
    monkeypatch.setitem(sys.modules,'aidputils.agents.toolkit.agent_helper',helper)
    monkeypatch.setitem(sys.modules,'aidputils.agents.auth.util',SimpleNamespace(auth_utils=SimpleNamespace(get_auth_context=Mock())))
    monkeypatch.setitem(sys.modules,'aidputils',SimpleNamespace(secrets=SimpleNamespace(get=Mock())))
    monkeypatch.setattr(native_governance,'consultation_config',lambda *a:{'metadata':{}})
    monkeypatch.setattr(native_governance,'_caller_identity',lambda *a:('principal','subject','session'))
    caller=SimpleNamespace(session=SimpleNamespace(close=Mock()))
    monkeypatch.setattr(native_governance,'native_central_transport',lambda *a:caller)
    monkeypatch.setattr(native_governance,'ConsultationHooks',lambda *a,**kw:object())
    graph=Mock(side_effect=AssertionError('A confirmation must bypass the model graph'))
    monkeypatch.setattr(native_governance,'build_governance_graph',graph)
    activity=SimpleNamespace(begin=AsyncMock(),intent={'request_id':'11111111-1111-4111-8111-111111111111'},
        finish=AsyncMock(return_value={'verified':True}),close=Mock())
    result={'case_ref':'a'*64,'result_state':'partial' if pending else 'answered','action_receipts':[],
        'case_pending':pending,'answer_markdown':'Registro pendiente' if pending else 'Registro comprobado'}
    cases=SimpleNamespace(confirm=AsyncMock(return_value=result))
    monkeypatch.setattr(native_governance,'NativeCaseActions',lambda *a,**kw:cases)
    monkeypatch.setattr(native_governance,'NativeActivity',lambda *a:activity)
    agent=native_governance.DataGovernanceAgent();agent.binding={'storage_root':'bound','component_revision':'d'*64};agent.memory=object()
    command='CONFIRMAR ABRIR '+'b'*32
    response=asyncio.run(agent.invoke(command))['messages'][0]
    assert response.content==result['answer_markdown'] and response.additional_kwargs['governance']==result
    graph.assert_not_called();cases.confirm.assert_awaited_once_with(command,request_id=activity.intent['request_id'])
    activity.finish.assert_awaited_once_with({'response':result},failed=False,cancelled=False,materialize=False)
    activity.close.assert_called_once();caller.session.close.assert_called_once()


def test_native_case_cancellation_records_the_attempt_and_propagates_without_manufacturing_a_response():
    cases=SimpleNamespace(confirm=AsyncMock(side_effect=asyncio.CancelledError()))
    activity=SimpleNamespace(finish=AsyncMock(return_value={'verified':True}))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(native_governance.invoke_case_confirmation(cases,'command','request',activity))
    activity.finish.assert_awaited_once_with({},failed=False,cancelled=True,materialize=False)


@pytest.mark.parametrize('raw_confirmation',[False,True])
@pytest.mark.parametrize('expired',[False,True])
def test_failed_invocation_freezes_activity_before_native_auth_context_exits(monkeypatch,raw_confirmation,expired):
    active=[]
    @contextmanager
    def flow(**kwargs):
        active.append(True)
        try:
            yield
        finally:
            active.clear()
    helper=SimpleNamespace(pre_invoke_setup=lambda **kw:{},flow_setup=flow)
    monkeypatch.setitem(sys.modules,'aidputils.agents.toolkit.agent_helper',helper)
    monkeypatch.setitem(sys.modules,'aidputils.agents.auth.util',SimpleNamespace(auth_utils=SimpleNamespace(get_auth_context=Mock())))
    monkeypatch.setitem(sys.modules,'aidputils',SimpleNamespace(secrets=SimpleNamespace(get=Mock())))
    monkeypatch.setattr(native_governance,'consultation_config',lambda *a:{'metadata':{}})
    monkeypatch.setattr(native_governance,'_caller_identity',lambda *a:('principal','subject','session'))
    caller=SimpleNamespace(session=SimpleNamespace(close=Mock()))
    monkeypatch.setattr(native_governance,'native_central_transport',lambda *a:caller)
    hooks=SimpleNamespace(authorize=AsyncMock(),resolve_assets=AsyncMock(),read=AsyncMock(),_read_admitted=AsyncMock(),capabilities=(),reauthorize_evidence=AsyncMock(),_validate_admitted_evidence=AsyncMock(), _reobserve_admitted_evidence=AsyncMock())
    monkeypatch.setattr(native_governance,'ConsultationHooks',lambda *a,**kw:hooks)
    error=native_governance.SourceObservationExpired if expired else PermissionError
    graph=SimpleNamespace(ainvoke=AsyncMock(side_effect=error('private diagnostic')))
    monkeypatch.setattr(native_governance,'build_governance_graph',Mock(return_value=graph))
    cases=SimpleNamespace(confirm=AsyncMock(side_effect=error('private diagnostic')))
    monkeypatch.setattr(native_governance,'NativeCaseActions',lambda *a,**kw:cases)
    frozen=[]
    async def finish(*args,**kwargs):
        if not active:
            raise PermissionError('Native caller context already left')
        frozen.append(kwargs)
        return {'verified':True}
    activity=SimpleNamespace(begin=AsyncMock(),intent={'request_id':'11111111-1111-4111-8111-111111111111'},
        events=[{'phase':'invocation'}],observe=Mock(),finish=AsyncMock(side_effect=finish),close=Mock())
    monkeypatch.setattr(native_governance,'NativeActivity',lambda *a:activity)
    agent=native_governance.DataGovernanceAgent();agent.binding={'storage_root':'bound','component_revision':'d'*64};agent.memory=object()
    question='CONFIRMAR ABRIR '+'0'*32 if raw_confirmation else 'question'
    response=asyncio.run(agent.invoke(question))['messages'][0]
    assert response.additional_kwargs['error_kind']==('evidence_expired' if expired else 'authorization')
    assert 'private diagnostic' not in response.content
    if expired:
        assert 'evidencia de código venció' in response.content
        assert 'identidad' not in response.content
    assert frozen==[{'failed':True,'cancelled':False,'materialize':False}]
    activity.finish.assert_awaited_once();activity.close.assert_called_once();caller.session.close.assert_called_once()
    assert not active
    if raw_confirmation:
        native_governance.build_governance_graph.assert_not_called()
