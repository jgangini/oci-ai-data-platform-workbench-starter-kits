"""Partial page failures retain evidence and pass through ordinary admissions."""
import asyncio
import copy
import json

import pytest

pytest.importorskip('langgraph')
from test_market_intelligence_workflow import configured, turn, SCOPE
from test_market_intelligence_research import running
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import PaidOperations
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import ObservationResult
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import json_text


def coverage(accepted=1):
    return {'version': 1, 'total_rows': accepted + 1, 'accepted_rows': accepted,
            'failed_pages': [{'url': 'https://example.com/unavailable', 'reason': 'http_error', 'http_status': 500}]}


def test_retrieval_receipt_is_bound_idempotent_and_survives_terminal_reconciliation():
    research, run, tool = running()
    paid = PaidOperations(research.journal)
    paid.record_retrieval(tool, 'provider-1', coverage())
    paid.record_retrieval(tool, 'provider-1', coverage())
    state, _ = research.journal.read()
    permit = state['operations'][tool]['permit']
    paid.record_provider(tool, permit, 'provider-1', 'succeeded', {'runId': 'provider-1', 'status': 'SUCCEEDED'})
    state, _ = research.journal.read()
    assert json.loads(state['tables']['ai_mi_tool_runs'][tool]['provider_receipt_json'])['retrieval_coverage'] == coverage()
    with pytest.raises(ValueError, match='immutable'):
        paid.record_retrieval(tool, 'provider-1', coverage(0))
    with pytest.raises(ValueError, match='same successful'):
        paid.record_retrieval(tool, 'different-provider', coverage())


@pytest.mark.parametrize('mutate', [
    lambda v:v.update(accepted_rows=9), lambda v:v.update(version=True),
    lambda v:v['failed_pages'][0].update(url='https://127.0.0.1/'),
    lambda v:v['failed_pages'][0].update(http_status=True),
    lambda v:v['failed_pages'][0].update(reason='unverified_error'),
    lambda v:v['failed_pages'][0].update(http_status=200),
])
def test_malformed_retrieval_coverage_cannot_be_saved(mutate):
    research, _, tool = running()
    value=coverage(); mutate(value)
    before=research.journal.read()[0]
    with pytest.raises(ValueError):
        PaidOperations(research.journal).record_retrieval(tool, 'provider-1', value)
    assert research.journal.read()[0] == before


def configured_partial(all_failed=False):
    flow, model, provider, graph = configured()
    def prepare(records, state):
        row=records.get('ai_mi_sources',SCOPE.key('web'))
        records.upsert('ai_mi_sources',{**row,'connector_revision':'web_search','allowed_scope_json':json_text({'allow_discovery':True})})
        for name in ('ai_mi_source_targets','ai_mi_topic_sources'):
            for item in records.rows(name):records.upsert(name,{**item,'status':'inactive'})
    flow.journal.transact(prepare)
    original_ask, original_observations=model.ask,provider.observations
    contexts=[]
    async def ask(name,schema,payload):
        if name=='plan_research':
            model.calls.append((name,payload));contexts.append((name,payload))
            profile='web_search' if not provider.starts else 'x_search'
            source=next(r for r in payload['catalog'] if r['profile_id']==profile)
            return {'next_action':'execute','source_key':source['source_key'],'target_id':None,
                'arguments':{'query':'D1 Colombia','urls':[],'max_items':5,'sort':'Latest'},
                'purpose':'Resolve a concrete corroboration gap','expected_contribution':'Independent context','user_question':None}
        if name=='assess_evidence':
            model.calls.append((name,payload));contexts.append((name,payload))
            assert payload['retrieval_coverage'][0]['failed_reasons']=={'http_error':1}
            return {'assessments':[{'evidence_id':r['evidence_id'],'stance':'context','reason':'Synthetic evidence','independence':'unknown'} for r in payload['evidence']],
                'continue_research':len(provider.starts)==1,'reason':'Compare another authorized source',
                'gap':'Need corroboration' if len(provider.starts)==1 else ''}
        result=await original_ask(name,schema,payload)
        if name=='draft_report':contexts.append((name,payload))
        return result
    async def observations(receipt,call):
        rows,cost=await original_observations(receipt,call)
        if len(provider.starts)==1:
            items=[] if all_failed else rows
            return ObservationResult(items,cost,coverage(len(items)))
        return rows,cost
    model.ask,provider.observations=ask,observations
    return flow,model,provider,graph,contexts


@pytest.mark.parametrize('all_failed',[False,True])
def test_failed_pages_do_not_skip_contrast_or_block_an_authorized_alternative(all_failed):
    flow,model,provider,graph,contexts=configured_partial(all_failed)
    result=turn(graph,'partial-pages-'+str(all_failed))
    assert [call['actor'] for call in provider.starts]==['apify/rag-web-browser','apidojo/twitter-scraper-lite']
    evidence=flow.research.evidence(result['run_id'])
    assert len(evidence)==(1 if all_failed else 2)
    assert not any('unavailable' in row['url'] for row in evidence)
    report=flow.records().get('ai_mi_reports',result['response']['report_id'])
    assert 'no pudieron leerse' in report['markdown'] and 'No pude verificar el contenido recibido' not in report['markdown']
    assert json.loads(report['coverage_json'])['retrieval_coverage'][0]['accepted_rows']==(0 if all_failed else 1)
    assert len([p for n,p in contexts if n=='assess_evidence'])==2
    assert all(p['retrieval_coverage'] for n,p in contexts if n in {'assess_evidence','draft_report'})
    assert not flow.records().rows('ai_mi_schedules')


def test_time_limit_preserves_partial_and_prevents_another_capture():
    flow,model,provider,graph,_=configured_partial()
    original=flow.close_limit
    flow.close_limit=lambda run_id:'time' if provider.starts else original(run_id)
    result=turn(graph,'partial-page-time-limit')
    assert len(provider.starts)==1
    assert flow.records().get('ai_mi_research_runs',result['run_id'])['termination_reason']=='time'
    assert 'no pudieron leerse' in result['response']['markdown']


def test_equal_retrieval_counts_from_distinct_operations_are_aggregated_not_dropped():
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow_actions import provider_limitations
    class Records:
        def rows(self,name):
            return [{'run_id':'run','state':'succeeded','provider_receipt_json':json.dumps({'retrieval_coverage':coverage()})} for _ in range(2)]
    notices=provider_limitations(Records(),'run')
    assert len(notices)==1
    assert '2 consultas' in notices[0] and '4 resultados' in notices[0]
    assert '2 aportaron' in notices[0] and '2 no pudieron' in notices[0]
    assert 'repetir una misma' in notices[0]


def test_assessment_batches_share_one_retrieval_snapshot():
    from test_market_intelligence_workflow import THREAD
    flow,model,provider,graph=configured()
    original_observations=provider.observations
    async def observations(receipt,call):
        rows,cost=await original_observations(receipt,call)
        return [{**rows[0],'url':f'https://example.com/{i}'} for i in range(17)],cost
    provider.observations=observations
    state=asyncio.run(graph.ainvoke({'request_id':'shared-retrieval','question':'D1'},THREAD,interrupt_before=['assess_evidence']))
    reads=[];seen=[]
    def one_snapshot(run):
        reads.append(run)
        return [{'source_key':'synthetic','total_rows':1,'accepted_rows':1,'failed_reasons':{}}]
    flow.retrieval_context=one_snapshot
    original_ask=model.ask
    async def ask(name,schema,payload):
        if name=='assess_evidence':seen.append(payload['retrieval_coverage'])
        return await original_ask(name,schema,payload)
    model.ask=ask
    asyncio.run(flow.assess_evidence(state))
    assert reads==[state['run_id']]
    expected=[{'source_key':'synthetic','total_rows':1,'accepted_rows':1,'failed_reasons':{}}]
    assert len(seen)==3 and all(value==expected for value in seen)
