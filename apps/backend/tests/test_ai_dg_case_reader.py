import asyncio
from copy import deepcopy
import json
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.ai_data_governance.case_reader import case_columns, case_history_query, case_history_snapshot
from app.ai_data_governance.consultation import ConsultationHooks
from app.ai_data_governance.contracts import GovIntent
from app.ai_data_governance.governance_graph import build_governance_graph, consultation_operations
from app.ai_data_governance.governance_source import governance_agent_source
from app.ai_data_governance.inventory import identity
from app.ai_data_governance.native_governance import render_governance_response
from app.ai_data_governance.schema import load_model
from test_ai_dg_governance_source import binding


CASE = 'a' * 64
AT = '2026-11-01T00:00:00+00:00'


def case_fixture():
    config = binding()
    scope = identity('AI_DG_SCOPE',config['platform_id'],config['workspace_key'],'catalog','catalog.schema')
    data = {
        'CASE': dict(case_id=CASE,scope_id=scope,opened_run_id='run',summary='Revisar una medición pendiente',
            owner_ref='steward',status='VERIFIED',change_digest=None,treatment_json=None,closure_evidence_id=None,
            created_at=AT,updated_at=AT),
        'ASSET': dict(asset_id='table',assessed_version_id='historical-version',impact_role='subject',evidence_id=None,
            qualified_name='catalog.schema.old_table',admitted=True),
        'RUN': dict(run_id='run',scope_id=scope,parent_run_id=None,agent_id='agent',run_kind='CONSULTATION',
            execution_key='op',attempt_no=0,component_version='revision',native_run_ref=None,status='SUCCESS',
            started_at=AT,finished_at=AT),
        'DECISION': dict(decision_id='decision',case_id=CASE,run_id='decision-run',actor_ref='steward',decision='APPROVE',
            change_digest='change',versions_digest='versions',policy_version='policy1',valid_until=AT,recorded_at=AT),
        'EVENT': dict(event_id='event',run_id='run',event_key='opened',parent_event_id=None,event_type='CASE_OPENED',
            asset_id='table',occurred_at=AT,ingested_at=AT,summary='Caso registrado'),
    }
    rows = [{'record_kind':kind,'case_id':CASE,'data':json.dumps(row)} for kind,row in data.items()]
    return config,data,rows


def rows_for(data):
    return [{'record_kind':kind,'case_id':CASE,'data':json.dumps(row)} for kind,row in data.items()]


def test_case_read_columns_are_a_subset_of_the_one_approved_delta_model():
    tables = {t['name']:{c['name'] for c in t['columns']} for t in load_model()['tables']}
    for kind, fields in case_columns().items():
        table = 'AI_DG_CASE_ASSET' if kind == 'ASSET' else 'AI_DG_' + kind
        assert set(fields) - {'qualified_name','admitted'} <= tables[table]


def test_fixed_case_query_binds_scope_platform_workspace_and_uses_sentinels():
    config,_,_ = case_fixture()
    sql = case_history_query(config,CASE)
    assert sql.startswith('WITH matched AS (SELECT ') and sql.endswith(' LIMIT 49')
    assert CASE not in sql and CASE.encode().hex() in sql
    assert 's.enabled' not in sql and 's.platform_key=' in sql and 's.workspace_key=' in sql
    assert 'p.scope_id=c.scope_id' in sql and 'v.version_id=ca.assessed_version_id' in sql
    assert 'v.version_id IS NOT NULL' in sql
    assert all(word not in sql for word in ('DELETE ','UPDATE ','INSERT ','DROP ','manifest_json','metrics_json',';'))
    assert consultation_operations('case_followup') == ('read_case',)


@pytest.mark.parametrize('value', [None,'','uuid','a'*63,'A'*64,"'; DROP TABLE x --"])
def test_invalid_case_reference_cannot_reach_sql(value):
    with pytest.raises(ValueError):
        case_history_query(binding(),value)


def test_history_keeps_assessed_version_and_attributes_status_without_claiming_execution():
    config,_,rows = case_fixture()
    snapshot = case_history_snapshot(config,CASE,rows)
    proof = snapshot['proof']
    assert snapshot['resource']['name'] == 'Caso ' + CASE
    assert proof['kind'] == 'CASE_RECORD'
    assert proof['facts']['asset_links'][0]['assessed_version_id'] == 'historical-version'
    assert proof['facts']['case']['status'] == 'VERIFIED'
    assert proof['facts']['decisions'][0]['decision'] == 'APPROVE'
    assert proof['facts']['execution_verified'] is False and proof['facts']['decision_execution_verified'] is False
    assert 'manifest_json' not in proof['facts']['opening_run']
    assert case_history_snapshot(config,CASE,list(reversed(rows))) == snapshot
    assert "timestampFormat','yyyy-MM-dd HH:mm:ss.SSSSSSXXX" in case_history_query(config, CASE)


@pytest.mark.parametrize('failure', ['scope','envelope','duplicate_case','duplicate_decision','run','event','date','attempt','admission'])
def test_wrong_scope_identity_types_or_duplicate_rows_fail_closed(failure):
    config,data,rows = case_fixture()
    if failure == 'scope': data['CASE']['scope_id'] = 'foreign'
    if failure == 'run': data['RUN']['scope_id'] = 'foreign'
    if failure == 'event': data['EVENT']['run_id'] = 'another-run'
    if failure == 'date': data['CASE']['updated_at'] = '2026-11-01'
    if failure == 'attempt': data['RUN']['attempt_no'] = True
    if failure == 'admission': data['ASSET']['admitted'] = 'true'
    rows = rows_for(data)
    if failure == 'envelope': rows[1]['case_id'] = 'b'*64
    if failure == 'duplicate_case': rows.append(deepcopy(rows[0]))
    if failure == 'duplicate_decision': rows.append(deepcopy(next(r for r in rows if r['record_kind']=='DECISION')))
    with pytest.raises(ValueError): case_history_snapshot(config,CASE,rows)


def test_missing_parent_is_not_a_successful_empty_history():
    config,_,rows = case_fixture()
    assert case_history_snapshot(config,CASE,[]) is None
    with pytest.raises(ValueError): case_history_snapshot(config,CASE,rows[1:])


def test_unadmitted_links_are_counted_without_exposing_their_metadata():
    config,data,_ = case_fixture()
    data['ASSET'] = {key:None for key in data['ASSET']}
    data['ASSET']['admitted'] = False
    proof = case_history_snapshot(config,CASE,rows_for(data))['proof']
    assert proof['coverage'] == 'partial'
    assert proof['facts']['asset_links'] == [] and proof['facts']['omitted_asset_links'] == 1
    data['ASSET']['qualified_name'] = 'foreign.hidden_asset'
    with pytest.raises(ValueError): case_history_snapshot(config,CASE,rows_for(data))


def test_unknown_treatment_json_is_not_forwarded_as_tool_configuration():
    config,data,_ = case_fixture()
    data['CASE']['treatment_json'] = json.dumps({'private_key':'DO_NOT_FORWARD','command':'bad'})
    proof = case_history_snapshot(config,CASE,rows_for(data))['proof']
    assert 'DO_NOT_FORWARD' not in json.dumps(proof)
    assert proof['coverage'] == 'partial' and proof['facts']['treatment_format_supported'] is False


def test_child_sentinels_make_history_partial_instead_of_claiming_exhaustiveness():
    config,_,rows = case_fixture()
    event = next(row for row in rows if row['record_kind']=='EVENT')
    for n in range(16):
        extra=deepcopy(event); body=json.loads(extra['data']); body['event_id']='event'+str(n)
        extra['data']=json.dumps(body); rows.append(extra)
    proof = case_history_snapshot(config,CASE,rows)['proof']
    assert proof['coverage']=='partial' and proof['facts']['history_truncated']
    assert len(proof['facts']['events'])==16


def test_decision_sentinel_drops_oldest_record_not_smallest_identifier():
    config,_,rows=case_fixture()
    decision=next(row for row in rows if row['record_kind']=='DECISION')
    base=json.loads(decision['data']); base['decision_id']='z-oldest'
    decision['data']=json.dumps(base)
    for n in range(1,9):
        extra=deepcopy(decision); value=json.loads(extra['data'])
        value.update(decision_id='d'+str(n),recorded_at=f'2026-11-01T00:{n:02d}:00+00:00')
        extra['data']=json.dumps(value); rows.append(extra)
    facts=case_history_snapshot(config,CASE,list(reversed(rows)))['proof']['facts']
    assert facts['history_truncated']
    assert [r['decision_id'] for r in facts['decisions']]==['d'+str(n) for n in range(8,0,-1)]


def hooks_for(rows):
    config,_,_=case_fixture()
    caller=Mock(side_effect=AssertionError('Case history must not call the current catalog'))
    caller.check_identity=Mock()
    query=AsyncMock(side_effect=lambda *args,**kw:deepcopy(rows))
    return ConsultationHooks(config,caller,query),query,caller


def test_case_followup_rechecks_its_snapshot_without_requiring_current_business_tables():
    _,_,rows=case_fixture(); hooks,query,caller=hooks_for(rows)
    assets=asyncio.run(hooks.resolve_assets(GovIntent(intent='case_followup',case_reference=CASE),{}))
    proofs=asyncio.run(hooks.read('read_case',assets,{}))
    asyncio.run(hooks.reauthorize_evidence({},assets=assets,evidence=proofs))
    assert proofs[0]['facts']['asset_links'][0]['assessed_version_id']=='historical-version'
    caller.assert_not_called()
    assert all(call.kwargs['limit']==48 for call in query.call_args_list)
    poisoned=deepcopy(proofs); poisoned[0]['facts']['case']['summary']='changed'
    with pytest.raises(PermissionError):
        asyncio.run(hooks.reauthorize_evidence({},assets=assets,evidence=poisoned))


def test_changed_or_removed_case_stops_resume_before_model_use():
    _,data,rows=case_fixture(); hooks,query,_=hooks_for(rows)
    assets=asyncio.run(hooks.resolve_assets(GovIntent(intent='case_followup',case_reference=CASE),{}))
    data['CASE']['summary']='Later revision'
    query.side_effect=lambda *args,**kw:rows_for(data)
    with pytest.raises(RuntimeError): asyncio.run(hooks.authorize({},assets=assets))
    query.side_effect=lambda *args,**kw:[]
    with pytest.raises(RuntimeError): asyncio.run(hooks.authorize({},assets=assets))


def test_real_stategraph_asks_for_missing_case_without_any_tool_query():
    from langgraph.checkpoint.memory import InMemorySaver
    hooks,query,_=hooks_for([])
    model=AsyncMock(return_value=json.dumps({'intent':'case_followup'}))
    graph=build_governance_graph(authorize=hooks.authorize,resolve_assets=hooks.resolve_assets,read=hooks.read,
        call_model=model,capabilities=hooks.capabilities,checkpointer=InMemorySaver(),reauthorize_evidence=hooks.reauthorize_evidence)
    result=asyncio.run(graph.ainvoke({'question':'¿Cómo va mi caso?'},{'configurable':{'thread_id':'case-missing'}}))
    assert result['response']['result_state']=='clarification'
    assert 'referencia' in result['response']['answer_markdown']
    query.assert_not_awaited(); assert model.await_count==1


def test_model_cannot_invent_a_case_reference_absent_from_user_questions():
    from app.ai_data_governance.governance_graph import GovernanceNodes
    _,_,rows=case_fixture(); hooks,query,_=hooks_for(rows)
    nodes=GovernanceNodes(authorize=hooks.authorize,resolve_assets=hooks.resolve_assets,read=hooks.read,
        call_model=AsyncMock(return_value=json.dumps({'intent':'case_followup','case_reference':CASE})),
        capabilities=hooks.capabilities)
    state={'assets':[],'question':'¿Cómo va mi caso?','question_history':['¿Cómo va mi caso?']}
    intent=asyncio.run(nodes.understand(state,{}))['intent']
    assert intent['case_reference'] is None and intent['clarification']
    query.assert_not_awaited()


@pytest.mark.parametrize('question,history,model_ref,expected', [
    ('Estado del caso '+'b'*64,['Estado del caso '+CASE],CASE,'b'*64),
    ('Estado del caso '+'a'*65,['Estado del caso '+'b'*64],CASE,None),
    ('Estado del caso '+'x'+CASE,[],CASE,None),
    ('Estado de '+CASE+' y '+'b'*64,[],CASE,None),
    ('¿Cómo sigue ese caso?',['Estado del caso '+CASE],CASE,CASE),
    ('¿Cómo sigue ese caso?',['Estado del caso '+CASE,'Estado del caso '+'b'*64],CASE,None),
])
def test_case_selection_prioritizes_exact_current_reference_over_model_and_history(question,history,model_ref,expected):
    from app.ai_data_governance.governance_graph import admitted_case_intent
    intent=admitted_case_intent(GovIntent(intent='case_followup',case_reference=model_ref),question,history)
    assert intent.case_reference==expected
    assert bool(intent.clarification) is (expected is None)


def test_positive_stategraph_reads_case_history_and_emits_registry_source_without_action_receipts():
    from langgraph.checkpoint.memory import InMemorySaver
    _,_,rows=case_fixture(); hooks,_,caller=hooks_for(rows)
    async def model(phase,payload,schema,config):
        if phase=='G1': return json.dumps({'intent':'case_followup','case_reference':CASE})
        if phase=='G3':
            key=payload['evidence'][0]['evidence_id']
            return json.dumps({'evidence_ids':[key],'supported_claims':[{'text':'Consta VERIFIED en el registro.',
                'evidence_ids':[key],'kind':'observed'}],'conflicts':[],'gaps':[],
                'coverage':'sufficient','next_action':'answer'})
        assert phase=='G6' and payload['action_receipts']==[]
        return json.dumps({'answer_markdown':'El registro indica VERIFIED [1]; no acredita ejecución.',
            'cited_evidence_ids':[payload['references'][0]['evidence_id']],'limitations':[],
            'case_ref':None,'result_state':'answered'})
    graph=build_governance_graph(authorize=hooks.authorize,resolve_assets=hooks.resolve_assets,read=hooks.read,
        call_model=model,capabilities=hooks.capabilities,checkpointer=InMemorySaver(),reauthorize_evidence=hooks.reauthorize_evidence)
    state=asyncio.run(graph.ainvoke({'question':'Estado del caso '+CASE},{'configurable':{'thread_id':'case-positive'}}))
    assert state['response']['result_state']=='answered' and state['response']['case_ref'] is None
    assert '[1] Registro de caso:' in render_governance_response(state)
    caller.assert_not_called()


def test_native_assembly_contains_case_reader_and_uses_same_admission_contract():
    runtime=ModuleType('case_runtime')
    exec(compile(governance_agent_source(binding()),'governance_runtime.py','exec'),runtime.__dict__)
    config,_,rows=case_fixture()
    assert runtime.case_history_snapshot(config,CASE,rows)==case_history_snapshot(config,CASE,rows)
    assert runtime.GovIntent(intent='case_followup',case_reference=CASE).case_reference==CASE
