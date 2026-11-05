import asyncio
import ast
from copy import deepcopy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.modules.ai_data_governance.consultation import ConsultationHooks
from app.modules.ai_data_governance.consultation import _consultation_proof
from app.modules.ai_data_governance.code_usage import CallerCodeVerifier
from app.modules.ai_data_governance.contracts import GovIntent
from app.modules.ai_data_governance.inventory import canonical
from app.modules.ai_data_governance.governance_graph import GovernanceNodes
from tests.modules.ai_data_governance.test_ai_dg_case_readmission import readmission_fixture, history_fixture, AT


def fixture():
    reader,_,_,projections,caller,query=readmission_fixture()
    _,spec,_,history=history_fixture()
    query.side_effect=lambda sql,**_:deepcopy(history if '`AI_DG_CASE`' in sql else projections)
    hooks=ConsultationHooks(reader.binding,caller,query)
    intent=GovIntent(intent='treatment',case_reference=spec['previous']['case_id'])
    return hooks,intent,projections,history,caller,query


def test_treatment_reads_the_case_and_only_its_exact_current_assessed_business_versions():
    hooks,intent,_,_,caller,_=fixture()
    assets=asyncio.run(hooks.resolve_assets(intent,{}))
    assert len(assets)==3 and assets[0]['asset_id'].startswith('case:')
    case=asyncio.run(hooks.read('read_case',assets,{}))
    catalog=asyncio.run(hooks.read('read_asset_snapshot',assets,{}))
    assert len(case)==1 and len(catalog)==2
    asyncio.run(hooks.reauthorize_evidence({},assets=assets,evidence=[*case,*catalog]))
    assert {p['kind'] for p in [*case,*catalog]}=={'CASE_RECORD','CATALOG'}
    assert all(call.args==('GET','/tables/catalog.schema.orders') for call in caller.call_args_list)


def test_case_context_cannot_admit_an_unrelated_asset_by_mixing_it_with_the_case_resource():
    hooks,intent,_,_,caller,_=fixture()
    assets=asyncio.run(hooks.resolve_assets(intent,{}));caller.reset_mock()
    assets.append(dict(asset_id='foreign',version_id='f'*64,name='catalog.schema.secret'))
    with pytest.raises(PermissionError,match='unrelated asset'):
        asyncio.run(hooks.authorize({},assets=assets))
    caller.assert_not_called()


def test_treatment_context_does_not_rebase_the_frozen_case_to_a_new_catalog_version():
    hooks,intent,projections,_,caller,_=fixture()
    projections[0]['version_id']='f'*64
    with pytest.raises(ValueError,match='version does not match'):
        asyncio.run(hooks.resolve_assets(intent,{}))
    caller.assert_not_called()


def test_changed_case_history_between_phases_invalidates_the_mixed_context():
    hooks,intent,_,history,caller,_=fixture()
    assets=asyncio.run(hooks.resolve_assets(intent,{}));caller.reset_mock()
    body=json.loads(history[0]['data']);body['summary']='Cambió el caso';history[0]['data']=canonical(body)
    with pytest.raises(RuntimeError,match='history changed'):
        asyncio.run(hooks.authorize({},assets=assets))
    caller.assert_not_called()


def test_partial_case_links_keep_readable_history_without_generating_a_treatment_scope():
    hooks,intent,_,history,caller,query=fixture()
    target=next(row for row in history if row['record_kind']=='ASSET')
    body={key:None for key in json.loads(target['data'])};body['admitted']=False;target['data']=canonical(body)
    assets=asyncio.run(hooks.resolve_assets(intent,{}))
    assert len(assets)==1 and assets[0]['asset_id'].startswith('case:')
    proof=asyncio.run(hooks.read('read_case',assets,{}))[0]
    assert proof['coverage']=='partial' and proof['facts']['omitted_asset_links']==1
    caller.assert_not_called()
    assert all(call.args[0].startswith('WITH matched') for call in query.call_args_list)


def test_old_event_history_can_be_truncated_without_losing_a_complete_assessed_asset_set():
    hooks,intent,_,history,_,_=fixture()
    case=json.loads(history[0]['data'])
    for i in range(17):
        event=dict(event_id='event'+str(i),run_id=case['opened_run_id'],event_key='key'+str(i),parent_event_id=None,
            event_type='PROGRESS',asset_id=None,occurred_at=AT.isoformat(),ingested_at=AT.isoformat(),summary='Actividad')
        history.append(dict(record_kind='EVENT',case_id=case['case_id'],data=canonical(event)))
    assets=asyncio.run(hooks.resolve_assets(intent,{}))
    assert len(assets)==3
    proof=asyncio.run(hooks.read('read_case',assets,{}))[0]
    assert proof['facts']['history_truncated'] is True and proof['facts']['asset_links_truncated'] is False


def mixed_case_evidence(monkeypatch):
    hooks,intent,_,history,caller,query=fixture()
    caller.code_reads_enabled=True
    hooks=ConsultationHooks(hooks.binding,caller,query,code_usage_enabled=True)
    assets=asyncio.run(hooks.resolve_assets(intent,{}))
    cases=asyncio.run(hooks.read('read_case',assets,{}))
    row=hooks.rows[assets[1]['asset_id']]
    now=datetime.now(timezone.utc)
    facts=dict(target=row['qualified_name'],references=[{'source_path':'/Workspace/jobs/orders.py'}],
        execution_evidence=False,analysis_complete=False,observed_at=now.isoformat(),
        expires_at=(now+timedelta(minutes=10)).isoformat(),source_evidence_id='a'*64)
    static=_consultation_proof(row,'STATIC_CODE',facts,'partial',discriminator=facts['source_evidence_id'])
    verifier=Mock(return_value=True)
    monkeypatch.setattr(CallerCodeVerifier,'verify',verifier)
    query.reset_mock()
    return hooks,assets,[*cases,static],history,query,verifier


def expire_static_observation(hooks,evidence):
    old=evidence[-1]
    facts={**old['facts'], 'observed_at':(datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat(),
        'expires_at':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()}
    evidence[-1]=_consultation_proof(hooks.rows[old['asset_id']],'STATIC_CODE',facts,'partial',
        discriminator=facts['source_evidence_id'])


def test_mixed_case_and_code_evidence_reads_current_case_once_per_readmission(monkeypatch):
    hooks,assets,evidence,_,query,verifier=mixed_case_evidence(monkeypatch)
    for attempt in range(2):
        asyncio.run(hooks.reauthorize_evidence({},assets=assets,evidence=evidence))
        assert query.await_count==attempt+1
        assert verifier.call_count==attempt+1


@pytest.mark.parametrize('changed',['case','source','expired_source','missing_capability','proof'])
def test_single_readmission_still_rejects_changed_mixed_case_or_code(monkeypatch,changed):
    hooks,assets,evidence,history,query,verifier=mixed_case_evidence(monkeypatch)
    if changed=='case':
        body=json.loads(history[0]['data']);body['summary']='Cambió el caso';history[0]['data']=canonical(body)
    elif changed=='source':
        verifier.return_value=False
    elif changed=='expired_source':
        expire_static_observation(hooks,evidence)
    elif changed=='missing_capability':
        hooks.capabilities=hooks.capabilities-{'find_code_usage'}
    else:
        evidence[0]['coverage']='insufficient'
    with pytest.raises((PermissionError,RuntimeError)):
        asyncio.run(hooks.reauthorize_evidence({},assets=assets,evidence=evidence))
    assert query.await_count==1
    if changed!='source':
        verifier.assert_not_called()


def admitted_nodes(hooks):
    from app.modules.ai_data_governance.governance_source import governance_agent_source
    from tests.support.ai_dg_fixtures import binding
    calls = [node for node in ast.walk(ast.parse(governance_agent_source(binding())))
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='build_governance_graph']
    assert len(calls)==1
    read = next(keyword.value for keyword in calls[0].keywords if keyword.arg=='read')
    native_read = eval(compile(ast.Expression(read),'<native graph read binding>','eval'),{'hooks':hooks})
    return GovernanceNodes(authorize=hooks.authorize, resolve_assets=hooks.resolve_assets, read=native_read,
        call_model=Mock(), capabilities=hooks.capabilities,
        reauthorize_evidence=hooks._validate_admitted_evidence)


def test_native_retrieval_admits_case_and_catalog_once_per_operation():
    from unittest.mock import AsyncMock
    hooks,intent,_,_,caller,query=fixture()
    caller.code_reads_enabled=True
    hooks=ConsultationHooks(hooks.binding,caller,query,code_usage_enabled=True)
    assets=asyncio.run(hooks.resolve_assets(intent,{}))
    hooks.resolve_assets=AsyncMock(return_value=assets)
    hooks._lineage=lambda row:_consultation_proof(row,'RUNTIME_LINEAGE',{'links':[]},'partial')
    hooks._quality=AsyncMock(return_value=[])
    hooks._code_usage=AsyncMock(return_value=[])
    query.reset_mock();caller.reset_mock()
    result=asyncio.run(admitted_nodes(hooks).retrieve({'intent':intent.model_dump(mode='json')},{}))
    assert result['assets']==assets
    assert len([call for call in query.call_args_list if '`AI_DG_CASE`' in call.args[0]])==5
    assert caller.call_count==5
    assert hooks._quality.await_count==1 and hooks._code_usage.await_count==1


@pytest.mark.parametrize('change',['catalog','identity'])
def test_native_retrieval_rechecks_changes_before_the_next_operation(change):
    from unittest.mock import AsyncMock
    hooks,intent,_,_,caller,query=fixture()
    caller.code_reads_enabled=True
    hooks=ConsultationHooks(hooks.binding,caller,query,code_usage_enabled=True)
    hooks._lineage=lambda row:_consultation_proof(row,'RUNTIME_LINEAGE',{'links':[]},'partial')
    async def changed_after_quality(rows):
        if change=='catalog': caller.side_effect=RuntimeError('Catalog changed')
        else: caller.check_identity.side_effect=PermissionError('Identity changed')
        return []
    hooks._quality=changed_after_quality
    hooks._code_usage=AsyncMock()
    with pytest.raises((RuntimeError,PermissionError)):
        asyncio.run(admitted_nodes(hooks).retrieve({'intent':intent.model_dump(mode='json')},{}))
    hooks._code_usage.assert_not_awaited()


def test_phase_boundary_reads_case_once_and_rechecks_sources_every_time(monkeypatch):
    hooks,assets,evidence,_,query,verifier=mixed_case_evidence(monkeypatch)
    nodes=admitted_nodes(hooks)
    for attempt in range(2):
        asyncio.run(nodes.check_evidence(dict(assets=assets,evidence=evidence),{}))
        assert query.await_count==attempt+1
        assert verifier.call_count==attempt+1


@pytest.mark.parametrize('changed',['case','source','catalog','identity','expired_during_admission','new_hooks'])
def test_phase_boundary_rejects_changes_without_an_admission_cache(monkeypatch,changed):
    hooks,assets,evidence,history,query,verifier=mixed_case_evidence(monkeypatch)
    nodes=admitted_nodes(hooks)
    state=dict(assets=assets,evidence=evidence)
    asyncio.run(nodes.check_evidence(state,{}))
    if changed=='case':
        body=json.loads(history[0]['data']);body['summary']='Caso cambiado';history[0]['data']=canonical(body)
    elif changed=='source':
        verifier.return_value=False
    elif changed=='catalog':
        hooks.caller.side_effect=PermissionError('Catalog changed')
    elif changed=='identity':
        hooks.caller.check_identity.side_effect=PermissionError('Identity changed')
    elif changed=='new_hooks':
        hooks=ConsultationHooks(hooks.binding,hooks.caller,query,code_usage_enabled=True)
        nodes=admitted_nodes(hooks)
        body=json.loads(history[0]['data']);body['summary']='Cambio antes de reanudar';history[0]['data']=canonical(body)
    else:
        authorize=nodes.authorize
        async def expire_after_admission(*args,**kwargs):
            await authorize(*args,**kwargs)
            expire_static_observation(hooks,evidence)
        nodes.authorize=expire_after_admission
    with pytest.raises((PermissionError,RuntimeError)):
        asyncio.run(nodes.check_evidence(state,{}))


@pytest.mark.parametrize('callback',[None,'noop'])
def test_phase_boundary_requires_authorization_even_without_an_evidence_validator(callback):
    from unittest.mock import AsyncMock
    authorize=AsyncMock(side_effect=PermissionError('Caller unavailable'))
    validator=AsyncMock() if callback else None
    nodes=GovernanceNodes(authorize=authorize,resolve_assets=Mock(),read=Mock(),call_model=Mock(),
        capabilities=(),reauthorize_evidence=validator)
    with pytest.raises(PermissionError,match='Caller unavailable'):
        asyncio.run(nodes.check_evidence(dict(assets=[],evidence=[]),{}))
    authorize.assert_awaited_once_with({},assets=[])
    if validator is not None:
        validator.assert_not_awaited()


def test_treatment_collects_short_lived_code_after_slow_quality_read_without_renewing_it(monkeypatch):
    import app.modules.ai_data_governance.consultation as consultation
    hooks,_,_,_,query,verifier=mixed_case_evidence(monkeypatch)
    clock=[datetime.now(timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return clock[0]
    monkeypatch.setattr(consultation,'datetime',Clock)
    original_query=query.side_effect
    async def query_with_quality_delay(sql,**kwargs):
        if '`AI_DG_ASSESSMENT`' in sql:
            clock[0]+=timedelta(seconds=120)
            return []
        return original_query(sql,**kwargs)
    query.side_effect=query_with_quality_delay
    hooks._lineage=lambda row:_consultation_proof(row,'RUNTIME_LINEAGE',dict(links=[]),'partial')
    captured=[]
    async def observe_code(rows):
        row=rows[0]
        facts=dict(target=row['qualified_name'],references=[{'source_path':'/Workspace/jobs/orders.py'}],
            execution_evidence=False,analysis_complete=False,observed_at=clock[0].isoformat(),
            expires_at=(clock[0]+timedelta(minutes=10)).isoformat(),source_evidence_id='b'*64)
        proof=_consultation_proof(row,'STATIC_CODE',facts,'partial',discriminator=facts['source_evidence_id'])
        captured.append(deepcopy(proof))
        return [proof]
    hooks._code_usage=observe_code
    _,intent,_,_,_,_=fixture()
    nodes=admitted_nodes(hooks)
    state=asyncio.run(nodes.retrieve(dict(intent=intent.model_dump(mode='json'),assets=[]),{}))
    assert len(captured)==1
    clock[0]+=timedelta(seconds=550)
    asyncio.run(nodes.check_evidence(state,{}))
    assert next(p for p in state['evidence'] if p['kind']=='STATIC_CODE')==captured[0]
    assert (_usage_expiry(captured[0])-datetime.fromisoformat(captured[0]['facts']['observed_at'])).total_seconds()==600
    clock[0]+=timedelta(seconds=51)
    with pytest.raises(consultation.SourceObservationExpired):
        asyncio.run(nodes.check_evidence(state,{}))
    assert verifier.call_count==1


def _usage_expiry(proof):
    return datetime.fromisoformat(proof['facts']['expires_at'])


def test_case_change_during_quality_read_prevents_the_following_code_observation(monkeypatch):
    from unittest.mock import AsyncMock
    hooks,_,_,history,query,_=mixed_case_evidence(monkeypatch)
    original_query=query.side_effect
    async def change_during_quality(sql,**kwargs):
        if '`AI_DG_ASSESSMENT`' in sql:
            body=json.loads(history[0]['data']);body['summary']='Caso cambiado durante calidad'
            history[0]['data']=canonical(body)
            return []
        return original_query(sql,**kwargs)
    query.side_effect=change_during_quality
    hooks._lineage=lambda row:_consultation_proof(row,'RUNTIME_LINEAGE',dict(links=[]),'partial')
    hooks._code_usage=AsyncMock()
    _,intent,_,_,_,_=fixture()
    with pytest.raises(RuntimeError,match='history changed'):
        asyncio.run(admitted_nodes(hooks).retrieve(dict(intent=intent.model_dump(mode='json'),assets=[]),{}))
    hooks._code_usage.assert_not_awaited()


@pytest.mark.parametrize('intent',['treatment','quality'])
@pytest.mark.parametrize('window',['expired','future','naive','missing'])
def test_quality_window_is_rechecked_before_models_for_every_consultation_intent(intent,window):
    from unittest.mock import AsyncMock
    now=datetime.now(timezone.utc)
    facts=dict(measured_at=(now-timedelta(minutes=1)).isoformat(),expires_at=(now+timedelta(minutes=1)).isoformat())
    if window=='expired': facts['expires_at']=(now-timedelta(seconds=1)).isoformat()
    elif window=='future': facts['measured_at']=(now+timedelta(seconds=30)).isoformat()
    elif window=='naive': facts['measured_at']=now.replace(tzinfo=None).isoformat()
    else: facts.pop('expires_at')
    model=AsyncMock()
    nodes=GovernanceNodes(authorize=AsyncMock(),resolve_assets=Mock(),read=Mock(),call_model=model,capabilities=())
    state=dict(assets=[],evidence=[dict(kind='QUALITY_ASSESSMENT',facts=facts)],intent=dict(intent=intent))
    with pytest.raises(ValueError,match='current measurement window'):
        asyncio.run(nodes.assess(state,{}))
    model.assert_not_awaited()


def test_current_quality_window_is_not_reused_after_it_expires(monkeypatch):
    import app.modules.ai_data_governance.governance_graph as graph
    from unittest.mock import AsyncMock
    clock=[datetime.now(timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return clock[0]
    monkeypatch.setattr(graph,'datetime',Clock)
    facts=dict(measured_at=clock[0].isoformat(),expires_at=(clock[0]+timedelta(seconds=30)).isoformat())
    state=dict(assets=[],evidence=[dict(kind='QUALITY_ASSESSMENT',facts=facts)])
    nodes=GovernanceNodes(authorize=AsyncMock(),resolve_assets=Mock(),read=Mock(),call_model=Mock(),capabilities=())
    asyncio.run(nodes.check_evidence(state,{}))
    clock[0]+=timedelta(seconds=31)
    with pytest.raises(ValueError,match='current measurement window'):
        asyncio.run(nodes.check_evidence(state,{}))


def test_quality_expiring_during_source_readmission_never_reaches_the_model(monkeypatch):
    import app.modules.ai_data_governance.governance_graph as graph
    from unittest.mock import AsyncMock
    clock=[datetime.now(timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return clock[0]
    monkeypatch.setattr(graph,'datetime',Clock)
    facts=dict(measured_at=clock[0].isoformat(),expires_at=(clock[0]+timedelta(seconds=30)).isoformat())
    async def delayed_readmission(*args,**kwargs): clock[0]+=timedelta(seconds=31)
    model=AsyncMock(side_effect=AssertionError('Expired measurement reached the model'))
    nodes=GovernanceNodes(authorize=AsyncMock(),resolve_assets=Mock(),read=Mock(),call_model=model,
        capabilities=(),reauthorize_evidence=delayed_readmission)
    state=dict(question='Prepara un tratamiento',gaps=[],assets=[],
        evidence=[dict(kind='QUALITY_ASSESSMENT',facts=facts)],intent=dict(intent='treatment'))
    with pytest.raises(ValueError,match='current measurement window'):
        asyncio.run(nodes.assess(state,{}))
    model.assert_not_awaited()


def test_static_observation_expiring_during_file_verification_is_not_admitted(monkeypatch):
    import app.modules.ai_data_governance.consultation as consultation
    hooks,assets,evidence,_,_,verifier=mixed_case_evidence(monkeypatch)
    clock=[datetime.now(timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return clock[0]
    monkeypatch.setattr(consultation,'datetime',Clock)
    def delayed_file_check(*args):
        clock[0]+=timedelta(seconds=601)
        return True
    verifier.side_effect=delayed_file_check
    with pytest.raises(consultation.SourceObservationExpired):
        asyncio.run(hooks.reauthorize_evidence({},assets=assets,evidence=evidence))


def test_earlier_source_is_rechecked_after_all_later_file_verifications(monkeypatch):
    import app.modules.ai_data_governance.consultation as consultation
    hooks,assets,evidence,_,_,verifier=mixed_case_evidence(monkeypatch)
    clock=[datetime.now(timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return clock[0]
    monkeypatch.setattr(consultation,'datetime',Clock)
    row=hooks.rows[evidence[-1]['asset_id']]
    facts={**evidence[-1]['facts'],'observed_at':clock[0].isoformat(),
        'expires_at':(clock[0]+timedelta(seconds=30)).isoformat()}
    evidence[-1]=_consultation_proof(row,'STATIC_CODE',facts,'partial',discriminator=facts['source_evidence_id'])
    later={**facts,'expires_at':(clock[0]+timedelta(minutes=10)).isoformat(),'source_evidence_id':'c'*64}
    evidence.append(_consultation_proof(row,'STATIC_CODE',later,'partial',discriminator=later['source_evidence_id']))
    def file_check(*args):
        clock[0]+=timedelta(seconds=20)
        return True
    verifier.side_effect=file_check
    with pytest.raises(consultation.SourceObservationExpired):
        asyncio.run(hooks.reauthorize_evidence({},assets=assets,evidence=evidence))
    assert verifier.call_count==2
