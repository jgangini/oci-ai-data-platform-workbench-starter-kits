import copy
from pathlib import Path
import json
import asyncio
from types import SimpleNamespace
import pytest

from test_market_intelligence_v2 import v2_fixture,v2_run
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_workflow import V2Workflow


def test_model_supplies_keyword_concepts_and_backend_builds_source_specific_queries():
    store,model,collector,graph=v2_fixture(('x','youtube'))
    original=model.ask
    contracts=[]
    async def ask(name,schema,payload):
        result=await original(name,schema,payload)
        if name=='v2_plan':
            contracts.append(schema['$defs']['V2Task']['properties']['query'])
            for task in result['tasks']:
                task['query']={'groups':[['Tiendas D1','tiendasd1'],['queja','reclamo']]}
        return result
    model.ask=ask
    _,state=v2_run(graph)
    assert state['response']['status']=='complete' and contracts[0]['type']=='object'
    assert collector.calls[0]['task']['query']=='("Tiendas D1" OR "tiendasd1") ("queja" OR "reclamo")'
    assert collector.calls[1]['task']['query']=='"Tiendas D1" "queja"'
    assert store.read()[0]['plan']['tasks'][0]['query']==collector.calls[0]['task']['query']


@pytest.mark.parametrize('source',['web','financial_document'])
def test_documentary_keywords_keep_variants_without_forcing_exact_brand_spelling(source):
    result=V2Workflow.bind_queries('v2_plan',{'tasks':[{'source':source,
        'query':{'groups':[['leche Lati','marca Lati'],['D1'],['fabricante','maquila']]}}]}, {})
    assert result['tasks'][0]['query']=='(leche Lati OR marca Lati) D1 (fabricante OR maquila)'


@pytest.mark.parametrize('term',['OR','D1 OR Éxito','D1 -fabricante','D1 +fabricante','2020..2025'])
def test_unquoted_documentary_terms_cannot_change_the_compiled_query_operators(term):
    result=V2Workflow.bind_queries('v2_plan',{'tasks':[{'source':'web','query':{'groups':[['D1'],[term]]}}]}, {})
    assert result['tasks'][0]['query']==''


def test_invalid_keyword_refinement_preserves_findings_without_a_replacement_search():
    store,model,collector,graph=v2_fixture()
    original=model.ask
    async def ask(name,schema,payload):
        result=await original(name,schema,payload)
        if name=='v2_analyze':
            result.update(refinement_query={'groups':[['site:private.local']]},gap='Falta un documento')
        return result
    model.ask=ask
    events,state=v2_run(graph)
    assert state['response']['status']=='partial' and len(collector.calls)==1
    assert any(event['type']=='draft.upsert' for event in events)
    assert 'palabras clave' in store.read()[0]['domain_outcomes']['web']['gap']
    assert state['response']['coverage']['domain_outcomes']['web']['gap']=='La evidencia recuperada no permitió verificar todos los aspectos del encargo.'


@pytest.mark.parametrize('with_first_finding',[True,False])
def test_invalid_refinement_closes_only_its_source_and_preserves_verified_content(with_first_finding):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import compile_call
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.runtime_core import ProviderQueryRejected
    store,model,collector,graph=v2_fixture(('x','web'))
    original_ask,original_collect=model.ask,collector.collect
    rejected=[]
    async def ask(name,schema,payload):
        result=await original_ask(name,schema,payload)
        if name=='v2_analyze' and payload['task']['source']=='x':
            result.update(refinement_query='Fixture from:fixture',gap='Falta contraste')
            if not with_first_finding:
                result['findings']=[]
        return result
    async def collect(**kwargs):
        task=kwargs['task']
        if 'from:' in task['query']:
            rejected.append(task['query'])
            with pytest.raises(ProviderQueryRejected):
                compile_call('x_search',{'query':task['query'],'urls':[],'max_items':20,'sort':'Latest'},
                    {'start':kwargs['brief']['start'],'end':kwargs['brief']['end']},None,unlimited=True)
            raise ProviderQueryRejected('Global topic search cannot add author or time overrides')
        return await original_collect(**kwargs)
    model.ask,collector.collect=ask,collect
    events,state=v2_run(graph)
    value=store.read()[0]
    assert state['response']['status']=='partial' and rejected==['Fixture from:fixture']
    assert [call['task']['source'] for call in collector.calls]==['x','web']
    assert model.calls.count('v2_analyze')==2 and value['task_index']==2
    assert value['refinements']=={'x':'Falta contraste'} and value['active_query'] is None
    assert value['domain_outcomes']['x']['status']=='partial'
    assert any('no es compatible' in limit for limit in state['response']['limitations'])
    x_blocks=[event for event in events if event.get('block_id')=='source-x']
    assert len(x_blocks)==int(with_first_finding)
    if with_first_finding:
        assert x_blocks[0]['evidence_ids']==['evidence-x-1']
        assert any(row['evidence_id']=='evidence-x-1' for row in state['response']['references'])
    before=len(collector.calls),len(model.calls)
    workflow=V2Workflow(store,model,collector,enabled_sources=('x','web'))
    assert asyncio.run(workflow.collect({}))=={'route':'consolidate'}
    assert before==(len(collector.calls),len(model.calls))


def test_repair_admission_is_per_real_inventory_idempotent_and_limited_to_two():
    store,_,_,_=v2_fixture()
    store.begin('request','Research')
    assert store.reserve_draft_repair('web','first',evidence_revision='inventory-1') is True
    assert store.reserve_draft_repair('web','first',evidence_revision='inventory-1') is True
    assert store.reserve_draft_repair('web','different',evidence_revision='inventory-1') is False
    assert store.reserve_draft_repair('web','second',evidence_revision='inventory-2') is True
    assert store.reserve_draft_repair('web','third',evidence_revision='inventory-3') is False
    assert store.reserve_draft_repair('web','second',evidence_revision='inventory-2') is True
    assert len(store.read()[0]['draft_repairs'])==2
    assert store.reserve_draft_repair('x','legacy') is True
    assert store.reserve_draft_repair('x','new',evidence_revision='inventory-2') is False


def test_both_distinct_collections_can_repair_once_and_publish_verified_content():
    store,model,collector,graph=v2_fixture(('x',))
    model.refine=True
    actual=model.ask
    repaired=[]
    async def ask(name,schema,payload):
        result=await actual(name,schema,payload)
        if name=='v2_repair_units':
            repaired.append(len(collector.calls))
        if name=='v2_draft':
            result['text']='El total confirmado es 999.'
        if name=='v2_verify' and '999' in payload['block']['text']:
            row=payload['evidence'][0]
            result={'accepted':False,'issues':[{'claim':payload['block']['text'],'evidence_id':row['evidence_id'],
                'quoted_text':row['text'],'reason':'The quoted source does not report 999.'}]}
        return result
    model.ask=ask
    events,state=v2_run(graph)
    assert repaired==[1,2] and len(collector.calls)==2
    assert len(store.read()[0]['draft_repairs'])==2
    assert state['response']['status']=='partial' and len(state['response']['references'])==2
    assert any(event['type']=='draft.corrected' and event['status']=='provisional' for event in events)
    assert all('999' not in event['text'] for event in events)


@pytest.mark.parametrize('event_limit',[2,256])
def test_failed_refinement_withdraws_a_global_old_claim_and_its_artifacts(event_limit):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_wire import v2_replay_packet
    store,model,collector,graph=v2_fixture(('financial_document',),event_limit=event_limit)
    model.refine=True
    actual_ask,actual_collect=model.ask,collector.collect
    async def collect(**kwargs):
        result=await actual_collect(**kwargs)
        row=result['evidence'][0]
        row['text']='La primera nota no identifica fabricante.' if len(collector.calls)==1 else 'Otra noticia atribuye la fabricación a Empresa A.'
        row['financial']={'metric':'value','entity':'Fixture','indicator':'VALUE','unit':'USD','currency':'USD',
            'points':[{'period':'2024','value':10.0}]}
        return result
    async def ask(name,schema,payload):
        result=await actual_ask(name,schema,payload)
        if name=='v2_analyze':
            row=payload['evidence'][-1]
            result['findings']=[{'claim':row['text'],'kind':'observation','supports':[row['evidence_id']],
                'refutes':[],'limitations':[]}]
        if name=='v2_draft':
            result['text']='La evidencia disponible no identifica al fabricante.' if len(collector.calls)==1 else 'El fabricante único y actual es Empresa B.'
        if name=='v2_verify' and 'Empresa B' in payload['block']['text']:
            row=payload['evidence'][-1]
            result={'accepted':False,'issues':[{'claim':payload['block']['text'],'evidence_id':row['evidence_id'],
                'quoted_text':row['text'],'reason':'The source attributes A, not an exclusive current B.'}]}
        return result
    model.ask,collector.collect=ask,collect
    events,state=v2_run(graph)
    assert len(collector.calls)==2 and model.calls.count('v2_draft')==3
    retired=next(event for event in events if event['type']=='draft.corrected' and event['status']=='failed')
    assert retired['supersedes'] and 'se retira' in retired['text']
    assert any(event['type']=='artifact.upsert' and event['status']=='provisional' for event in events)
    assert state['response']['artifacts']==[] and state['response']['status']=='partial'
    assert 'no identifica al fabricante' not in state['response']['markdown']
    assert 'Empresa B' not in state['response']['markdown']
    assert all('Empresa B' not in event['text'] for event in events)
    assert store.read()[0]['blocks']['source-financial_document']['status']=='failed'
    for response in (state['response'],None):
        store.save(response=response)
        packet=json.loads(v2_replay_packet(store,'fixture-request',0))
        assert packet['artifacts']==[]
        if packet['replay']['snapshot'] is not None:
            assert packet['replay']['snapshot']['artifacts']=={}
    assert store.read()[0]['artifacts']  # Receipts remain private even when the public preview is withdrawn.


def test_replay_keeps_source_links_and_icon_metadata_without_raw_evidence_or_owner_data():
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_wire import v2_replay_packet
    store,_,_,graph=v2_fixture(event_limit=2)
    v2_run(graph)
    value=store.read()[0]
    reads=[]
    actual_read=store.snapshot.read
    def one_read():
        reads.append(True)
        return actual_read()
    store.snapshot.read=one_read
    packet=json.loads(v2_replay_packet(store,value['request_id'],0))
    assert len(reads)==1
    assert packet['replay']['snapshot'] is not None
    assert packet['references']
    for reference in packet['references']:
        assert reference['evidence_id'] in value['evidence'] and reference['source']=='web'
        assert reference['url']==value['evidence'][reference['evidence_id']]['url']
        assert not {'text','owner_id','receipt_id','scope','claim','result'}&reference.keys()
    source_block=packet['replay']['snapshot']['blocks']['source-web']
    assert source_block['role_key']=='market_analysis' and source_block['source_keys']==['web']


def test_source_choice_can_read_a_relevant_later_hit_without_expanding_capture_limits():
    from test_market_intelligence_v2_collector import v2_collection_fixture,v2_collect
    from test_market_intelligence_v2 import v2_evidence
    provider,_,collector=v2_collection_fixture()
    async def observations(receipt,call,**kwargs):
        if 'scraperlink/' in call['actor']:
            items=[{'url':f'https://example.org/result/{index}','text':f'Candidate {index}',
                    'provenance':{'provider_run_id':receipt['runId']}} for index in range(1,11)]
        else:
            url=call['input']['startUrls'][0]['url']
            items=[{**v2_evidence('web'),'url':url,'provenance':{'provider_run_id':receipt['runId'],'requested_url':url}}]
        return SimpleNamespace(items=items,coverage=None,cost=receipt['spent_microusd'])
    async def choose(task,brief,options):
        assert len(options)==10 and all(len(item['text'])<=1500 for item in options)
        return [options[8]['candidate_id']]
    provider.observations,collector.source_selector=observations,choose
    result=v2_collect(collector,source='financial_document')
    assert result['evidence'][0]['url']=='https://example.org/result/9'
    assert len(provider.starts)==2 and len(provider.starts[1]['input']['startUrls'])==1


@pytest.mark.parametrize('source',['web','financial_document'])
@pytest.mark.parametrize('selected',[['foreign'],['candidate-1','candidate-1'],['candidate-1']*4,[{}]])
def test_source_choice_never_authorizes_a_destination_outside_the_admitted_candidates(selected,source):
    from test_market_intelligence_v2_collector import v2_collection_fixture,v2_collect
    provider,_,collector=v2_collection_fixture()
    async def candidates(receipt,call,**kwargs):
        return SimpleNamespace(items=[{'url':'https://example.org/public-page','text':'Public search candidate',
            'provenance':{'provider_run_id':receipt['runId']}}],coverage=None,cost=receipt['spent_microusd'])
    provider.observations=candidates
    async def choose(*args):
        return selected
    collector.source_selector=choose
    with pytest.raises(ValueError,match='escaped'):
        v2_collect(collector,source=source)
    assert len(provider.starts)==1 and 'scraperlink/' in provider.starts[0]['actor']


def test_source_choice_schema_is_bound_to_this_candidate_set_and_can_abstain():
    store,model,collector,_=v2_fixture()
    store.begin('one','Find the dated company report')
    async def choose(name,schema,payload):
        assert name=='v2_select_sources'
        assert schema['properties']['candidate_ids']['items']['enum']==['candidate-1','candidate-2']
        assert schema['properties']['candidate_ids']['maxItems']==2
        return {'candidate_ids':[]}
    model.ask=choose
    flow=V2Workflow(store,model,collector,enabled_sources=('web',))
    assert asyncio.run(flow.select_sources({'source':'web'}, {},[
        {'candidate_id':'candidate-1','url':'https://example.org/1','text':'Company description'},
        {'candidate_id':'candidate-2','url':'https://example.org/2','text':'Dated corporate report'}]))==[]
