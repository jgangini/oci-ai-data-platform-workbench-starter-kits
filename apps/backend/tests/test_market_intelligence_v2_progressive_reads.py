"""Progressive acquisition controls use synthetic bodies, never business acceptance."""
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import v2_effects,v2_collector,v2_workflow
from test_market_intelligence_v2 import v2_fixture,v2_run
from test_market_intelligence_v2_collector import v2_collection_fixture


def progressive_fixture(*,found_at=None,empty_selection=False,query_size=10,body_size=0):
    provider,effects,collector=v2_collection_fixture()
    store,model,_,_=v2_fixture(('web',));original=model.ask
    async def observations(receipt,call,**kwargs):
        if 'scraperlink/' in call['actor']:
            rows=[{'url':f'https://example.com/document-{n}','text':f'Candidate document {n}'} for n in range(query_size)]
        else:
            rows=[{'url':item['url'],'text':('TEST_ONLY_ATTRIBUTION' if item['url'].endswith('-'+str(found_at)) else 'Catalogue without the requested attribution.')+'x'*body_size,
                'published_at':None,'observed_at':'2026-11-02T00:00:00+00:00',
                'provenance':{'provider_run_id':receipt['runId'],'requested_url':item['url']}}
                for item in call['input']['startUrls']]
        return SimpleNamespace(items=rows,coverage=None,cost=receipt['spent_microusd'])
    async def ask(name,schema,payload):
        if name=='v2_select_sources':
            return {'candidate_ids':[] if empty_selection else [row['candidate_id'] for row in payload['candidates'][:payload['maximum_targets']]]}
        if name=='v2_analyze':
            ids=[row['evidence_id'] for row in payload['evidence'] if 'TEST_ONLY_ATTRIBUTION' in row['text']]
            return {'findings':[{'claim':'Synthetic attribution.','kind':'observation','supports':ids,'refutes':[],'limitations':[]}] if ids else [],
                    'financial_facts':[],'gap':None if ids else 'Falta la atribución pedida.',
                    'refinement_query':None if ids else {'groups':[['Fixture'],['attribution']]}}
        return await original(name,schema,payload)
    provider.observations=observations;model.ask=ask
    def graph():
        return v2_workflow.V2Workflow(store,model,collector,enabled_sources=('web',)).build(InMemorySaver())
    return provider,effects,collector,store,model,graph


def test_late_support_is_reachable_with_ten_but_not_two_destinations(monkeypatch):
    for cap,expected_reads,has_references in [(2,2,False),(10,8,True)]:
        for module in (v2_effects,v2_collector,v2_workflow):monkeypatch.setattr(module,'V2_MAX_READ_DESTINATIONS',cap)
        provider,effects,_,_,_,graph=progressive_fixture(found_at=7)
        _,state=v2_run(graph())
        assert len(effects.reserved_read_urls())==expected_reads
        assert bool(state['response']['references']) is has_references
        assert sum('scraperlink/' in call['actor'] for call in provider.starts)==1
        assert all(len(call['input']['startUrls'])<=2 for call in provider.starts if 'startUrls' in call['input'])


@pytest.mark.parametrize('found_at,expected_reads',[(0,2),(7,8),(None,10)])
def test_progressive_reads_stop_on_support_or_ten_and_replay_without_effects(found_at,expected_reads):
    provider,effects,_,store,_,graph=progressive_fixture(found_at=found_at)
    _,state=v2_run(graph())
    assert len(set(effects.reserved_read_urls()))==expected_reads
    assert state['response']['coverage']['read_progress']['attempted_destinations']==expected_reads
    if found_at is None:
        assert state['response']['status']=='partial' and not state['response']['references']
        assert 'en las fuentes consultadas' in state['response']['markdown']
        assert 'no existe' not in state['response']['markdown'].lower()
    count=len(provider.starts)
    assert v2_run(graph())[1]['response']==state['response'] and len(provider.starts)==count
    assert store.read()[0]['read_batches'] or expected_reads==2


def test_empty_selection_does_not_advance_an_unproductive_read_cursor():
    provider,effects,_,store,_,graph=progressive_fixture(empty_selection=True)
    _,state=v2_run(graph())
    assert len(provider.starts)==2 and not effects.reserved_read_urls()
    assert store.read()[0]['read_batches']=={} and state['response']['status']=='partial'


def test_second_batch_interruption_recovers_the_same_receipt_and_selection():
    provider,effects,_,store,_,graph=progressive_fixture(found_at=3)
    read=provider.read;pause=[True]
    async def interrupt(run_id):
        result=await read(run_id)
        if pause[0] and run_id=='receipt-3':result['status']='RUNNING'
        return result
    provider.read=interrupt
    _,pending=v2_run(graph())
    assert pending['response']['status']=='pending' and len(provider.starts)==3
    assert len(effects.reserved_read_urls())==4 and list(store.read()[0]['read_batches'].values())==[1]
    pause[0]=False
    _,done=v2_run(graph())
    assert done['response']['references'] and len(provider.starts)==3
    assert len(effects.reserved_read_urls())==4


def test_late_batch_reaches_analysis_even_when_older_bodies_fill_the_context():
    _,effects,_,_,_,graph=progressive_fixture(found_at=9,body_size=11000)
    _,state=v2_run(graph())
    assert len(effects.reserved_read_urls())==10 and state['response']['references']
    assert any(row['url'].endswith('-9') for row in state['response']['references'])
    assert state['response']['coverage']['domain_outcomes']['web']['evidence_coverage']['omitted']>0


def test_same_origin_effective_destination_is_not_selected_for_a_second_read():
    import asyncio
    from test_market_intelligence_v2_page_budget import BRIEF,page_observations
    provider,effects,collector=v2_collection_fixture()
    async def observations(receipt,call,**kwargs):
        result=await page_observations(receipt,call,**kwargs)
        if 'scraperlink/' in call['actor']:result.items=result.items[:3]
        else:result.items[0]['url']='https://example.com/2'
        return result
    provider.observations=observations
    task={'source':'web','query':'Fixture','urls':[]}
    asyncio.run(collector.collect(request_id='fixture-request',run_id='fixture-run',operation_key='redirect',
        task=task,brief=BRIEF,previous_operation_id=None))
    assert effects.reserved_read_urls()==['https://example.com/0','https://example.com/1']
    assert not collector.has_unread_candidates('redirect',task,BRIEF)


def test_three_explicit_urls_are_read_in_two_batches_without_dropping_the_last():
    provider,effects,collector,store,model,_=progressive_fixture(found_at=0)
    urls=[f'https://example.com/document-{n}' for n in range(3)];collector.explicit_urls=set(urls)
    ask=model.ask
    async def explicit(name,schema,payload):
        value=await ask(name,schema,payload)
        if name=='v2_plan':value['tasks'][0]['urls']=urls
        return value
    model.ask=explicit
    graph=v2_workflow.V2Workflow(store,model,collector,enabled_sources=('web',)).build(InMemorySaver())
    _,state=v2_run(graph)
    assert effects.reserved_read_urls()==urls and state['response']['references']
    assert [len(call['input']['startUrls']) for call in provider.starts]==[2,1]


@pytest.mark.parametrize('state',['completed','running','uncertain'])
def test_an_already_reserved_explicit_url_never_turns_into_a_google_search(state):
    import asyncio
    from test_market_intelligence_v2_page_budget import BRIEF
    url='https://example.com/explicit'
    provider,effects,collector=v2_collection_fixture(explicit_urls=[url])
    operation=effects.claim('prior-document','public_document',{'url':url},read_urls=[url])
    effects.update('prior-document',operation['claim'],state=state,result={'url':url} if state=='completed' else None)
    result=asyncio.run(collector.collect(request_id='fixture-request',run_id='fixture-run',operation_key='explicit-web',
        task={'source':'web','query':'Fixture','urls':[url]},brief=BRIEF,previous_operation_id=None))
    assert result['status']==('partial' if state=='completed' else 'pending')
    assert not provider.starts and effects.reserved_read_urls()==[url]
