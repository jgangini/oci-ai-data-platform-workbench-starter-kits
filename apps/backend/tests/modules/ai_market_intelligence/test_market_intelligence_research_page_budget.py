"""One durable ten-destination allowance across searches, documents and recovery."""
import asyncio
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.collector import MarketCollector
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.effects import MarketEffects, MarketReadBudgetExceeded
from tests.modules.ai_market_intelligence.test_market_intelligence_research_collector import market_collection_fixture, market_reserve_prior_reads


BRIEF={'start':'2026-11-01T00:00:00+00:00','end':'2026-11-02T00:00:00+00:00','retrieval_mode':'documentary'}


@pytest.mark.parametrize('scenario',['recover','empty','read_failed','read_failed_one','explicit','explicit_omitted'])
def test_web_batches_then_refines_only_with_remaining_budget(scenario):
    from tests.modules.ai_market_intelligence.test_market_intelligence_graph import market_fixture, market_run
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
    url='https://example.com/explicit'
    provider,effects,collector=market_collection_fixture(explicit_urls=[url] if scenario in {'explicit','explicit_omitted'} else [])
    store,model,_,_=market_fixture(('web',));original=model.ask
    choices=[];permissions=[]
    async def observations(receipt,call,**kwargs):
        if 'scraperlink/' not in call['actor'] and scenario in {'read_failed','read_failed_one','explicit'}:
            return SimpleNamespace(items=[],coverage=None,cost=500)
        return await page_observations(receipt,call,**kwargs)
    async def ask(name,schema,payload):
        if name=='market_select_sources':
            choices.append(payload)
            ids=[row['candidate_id'] for row in payload['candidates'][:1 if scenario=='read_failed_one' else 2]]
            return {'candidate_ids':ids if scenario in {'read_failed','read_failed_one'} or scenario=='recover' and len(choices)==2 else []}
        if name=='market_analyze':
            permissions.append(payload['refinement_allowed'])
            if not payload['evidence']:
                return {'findings':[],'financial_facts':[],'gap':'No se encontraron fuentes pertinentes.',
                    'refinement_query':{'groups':[['Fixture brand'],['duties']]}}
        value=await original(name,schema,payload)
        if name=='market_plan' and scenario=='explicit':value['tasks'][0]['urls']=[url]
        return value
    provider.observations=observations;model.ask=ask
    graph=MarketWorkflow(store,model,collector,enabled_sources=('web',)).build(InMemorySaver())
    _,state=market_run(graph);count=len(provider.starts)
    assert count=={'recover':3,'empty':2,'read_failed':6,'read_failed_one':11,'explicit':1,'explicit_omitted':1}[scenario]
    expected={'recover':[True,False],'empty':[True,False],'read_failed':[True]*4+[False],
              'read_failed_one':[True]*9+[False],'explicit':[False],'explicit_omitted':[False]}
    assert permissions==expected[scenario]
    assert len(effects.reserved_read_urls())=={'recover':2,'empty':0,'read_failed':10,'read_failed_one':10,'explicit':1,'explicit_omitted':0}[scenario]
    assert bool(state['response']['references']) is (scenario=='recover')
    assert state['response']['status'] in {'complete','partial'}
    assert market_run(graph)[1]['response']==state['response'] and len(provider.starts)==count


@pytest.mark.parametrize('suffix',['-read','-pdf-document'])
@pytest.mark.parametrize('status',['running','rejected','uncertain'])
def test_only_unresolved_web_attempt_keeps_refinement_closed_after_recreating_effects(suffix,status):
    from tests.modules.ai_market_intelligence.test_market_intelligence_graph import market_fixture
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.runtime_core import digest
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
    provider,effects,_=market_collection_fixture()
    store,model,_,_=market_fixture(('web',))
    store.begin('request','A public research question')
    store.save(brief={**BRIEF,'sources':['web']})
    task={'source':'web','query':'Fixture','urls':[]}
    key=digest([store.read()[0]['run_id'],task,BRIEF['start'],BRIEF['end']])
    operation=effects.claim(key+suffix,'apify' if suffix=='-read' else 'public_document',{},read_urls=['https://example.com/attempt'])
    effects.update(key+suffix,operation['claim'],state=status)
    restored=MarketCollector(provider,MarketEffects(effects.snapshot,'fixture-v2-revision','fixture-request'),
        search_context={'country_code':'CO','language_code':'es'},poll_attempts=1,poll_seconds=0)
    flow=MarketWorkflow(store,model,restored,enabled_sources=('web',))
    assert restored.maximum_targets('web',BRIEF)==2
    assert flow.can_refine(task) is (status=='rejected')
    assert effects.reserved_read_urls()==['https://example.com/attempt'] and not provider.starts


async def page_observations(receipt,call,**kwargs):
    if 'scraperlink/' in call['actor']:
        rows=[{'url':f'https://example.com/{index}','text':f'Candidate {index}'} for index in range(10)]
    else:
        rows=[{'url':item['url'],'text':'Captured public text.','published_at':None,
               'observed_at':'2026-11-02T00:00:00+00:00',
               'provenance':{'provider_run_id':receipt['runId'],'requested_url':item['url']}}
              for item in call['input']['startUrls']]
    return SimpleNamespace(items=rows,coverage=None,cost=receipt['spent_microusd'])


@pytest.mark.parametrize('web_read_started',[False,True])
def test_web_can_refine_its_empty_discovery_after_another_domain_used_one_read(web_read_started):
    from tests.modules.ai_market_intelligence.test_market_intelligence_graph import market_fixture, market_run
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
    provider,effects,collector=market_collection_fixture()
    market_reserve_prior_reads(effects)
    store,model,_,_=market_fixture(('financial_document','web'));original=model.ask
    choices=[];permissions=[]
    async def observations(receipt,call,**kwargs):
        if call['actor']=='apify/website-content-crawler':
            # The first financial destination fails. It must retain its reservation.
            if len([c for c in provider.starts if c['actor']==call['actor']])==1 or web_read_started:
                return SimpleNamespace(items=[],coverage=None,cost=receipt['spent_microusd'])
        return await page_observations(receipt,call,**kwargs)
    async def ask(name,schema,payload):
        if name=='market_select_sources':
            source=payload['task']['source'];choices.append(source)
            if source=='financial_document':return {'candidate_ids':[payload['candidates'][0]['candidate_id']]}
            if choices.count('web',)==1 and not web_read_started:return {'candidate_ids':[]}
            return {'candidate_ids':[payload['candidates'][0]['candidate_id']]}
        if name=='market_analyze':
            source=payload['task']['source'];permissions.append((source,payload['refinement_allowed']))
            if not payload['evidence']:
                return {'findings':[],'financial_facts':[],'gap':'No se recupero contenido pertinente.',
                    'refinement_query':{'groups':[['general valuation']]}}
        result=await original(name,schema,payload)
        if name=='market_understand':result['retrieval_mode']='financial'
        return result
    provider.observations=observations;model.ask=ask
    graph=MarketWorkflow(store,model,collector,enabled_sources=('financial_document','web')).build(InMemorySaver())
    _,state=market_run(graph)
    assert permissions==([('financial_document',False),('web',False)] if web_read_started else
                         [('financial_document',False),('web',True),('web',False)])
    assert len(effects.reserved_read_urls())==10
    assert choices.count('web',)==(1 if web_read_started else 2)
    assert bool(state['response']['references']) is not web_read_started
    count=len(provider.starts)
    assert market_run(graph)[1]['response']==state['response'] and len(provider.starts)==count


@pytest.mark.parametrize('pending_web',[True,False])
def test_financial_refinement_preserves_a_first_read_for_a_pending_web_task(pending_web):
    from tests.modules.ai_market_intelligence.test_market_intelligence_graph import market_fixture, market_run
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
    sources=('financial_document','web') if pending_web else ('financial_document',)
    provider,effects,collector=market_collection_fixture();provider.observations=page_observations
    market_reserve_prior_reads(effects)
    store,model,_,_=market_fixture(sources);original=model.ask;permissions=[]
    async def ask(name,schema,payload):
        if name=='market_select_sources':
            return {'candidate_ids':[row['candidate_id'] for row in payload['candidates'][:payload['maximum_targets']]]}
        if name=='market_analyze' and payload['task']['source']=='financial_document':
            permissions.append(payload['refinement_allowed'])
            return {'findings':[],'financial_facts':[],'gap':'El documento no trata de la entidad investigada.',
                    'refinement_query':{'groups':[['Fixture brand'],['additional evidence']]}}
        result=await original(name,schema,payload)
        if name=='market_understand':result['retrieval_mode']='financial'
        return result
    model.ask=ask
    graph=MarketWorkflow(store,model,collector,enabled_sources=sources).build(InMemorySaver())
    _,state=market_run(graph)
    current=store.read()[0]
    assert permissions==([False] if pending_web else [True,False])
    assert ('financial_document' in current['refinements']) is not pending_web
    reads=[call['input']['startUrls'] for call in provider.starts if call['actor']=='apify/website-content-crawler']
    assert list(map(len,reads))==[1,1] and len(set(effects.reserved_read_urls()))==10
    assert bool(current['evidence'] and any(row['source']=='web' for row in current['evidence'].values())) is pending_web
    assert len(provider.starts)==4
    before=len(provider.starts);assert market_run(graph)[1]['response']==state['response'] and len(provider.starts)==before


@pytest.mark.parametrize('pending_web',[True,False])
def test_financial_pdf_hop_preserves_a_first_read_for_a_pending_web_task(monkeypatch,pending_web):
    from tests.modules.ai_market_intelligence.test_market_intelligence_graph import market_fixture, market_run
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence import collector as collector_module
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
    sources=('financial_document','web') if pending_web else ('financial_document',)
    provider,effects,collector=market_collection_fixture()
    market_reserve_prior_reads(effects)
    store,model,_,_=market_fixture(sources);original=model.ask;pdf_reads=[]
    async def observations(receipt,call,**kwargs):
        result=await page_observations(receipt,call,**kwargs)
        if call['actor']=='apify/website-content-crawler':
            for row in result.items:
                if row['url']=='https://example.com/0':row['text']='[Annual results](report.pdf)'
        return result
    async def read_pdf(url):
        pdf_reads.append(url)
        return {'url':url,'sha256':'a'*64,'byte_count':100,'pages':[{'page':1,'text':'Captured public text.'}],'limitations':[]}
    async def ask(name,schema,payload):
        if name=='market_select_sources':
            return {'candidate_ids':[row['candidate_id'] for row in payload['candidates'][:payload['maximum_targets']]]}
        result=await original(name,schema,payload)
        if name=='market_understand':result['retrieval_mode']='financial'
        return result
    provider.observations=observations;model.ask=ask;monkeypatch.setattr(collector_module,'market_fetch_pdf',read_pdf)
    graph=MarketWorkflow(store,model,collector,enabled_sources=sources).build(InMemorySaver())
    _,state=market_run(graph);current=store.read()[0]
    assert pdf_reads==([] if pending_web else ['https://example.com/report.pdf'])
    assert len(effects.reserved_read_urls())==10
    assert any(row['source']=='web' for row in current['evidence'].values()) is pending_web
    before=len(provider.starts);assert market_run(graph)[1]['response']==state['response'] and len(provider.starts)==before


@pytest.mark.parametrize('status',['completed','running'])
def test_new_pdf_follow_guard_does_not_hide_an_already_admitted_operation(monkeypatch,status):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.runtime_core import digest
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence import collector as collector_module
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.documents import market_document_links
    from tests.modules.ai_market_intelligence.test_market_intelligence_graph import market_evidence
    _,effects,collector=market_collection_fixture()
    parent={**market_evidence('financial_document',),'url':'https://example.com/index',
        'text':'[Annual results](report.pdf)','content_basis':'page_text','receipt_id':'parent-receipt'}
    url='https://example.com/report.pdf';proof=market_document_links([parent],{parent['url']})[url]
    key='first-linked-pdf-'+digest(url)
    op=effects.claim(key,'public_document',{'url':url,'discovered_from':proof},read_urls=[url])
    result={'url':url,'sha256':'b'*64,'byte_count':100,'observed_at':parent['observed_at'],
        'pages':[{'page':1,'text':'Previously captured text.'}],'limitations':[],'discovered_from':proof}
    effects.update(key,op['claim'],state=status,result=result if status=='completed' else {'discovered_from':proof})
    collector.document_follow_allowed=lambda source:False
    async def no_pdf(url):raise AssertionError('An already admitted PDF cannot be captured again')
    monkeypatch.setattr(collector_module,'market_fetch_pdf',no_pdf)
    collection={'status':'complete','evidence':[parent],'limitations':[],'operation_id':'first'}
    actual=asyncio.run(collector.follow_document('first','financial_document','web',[parent['url']],BRIEF,collection))
    assert actual['status']==('partial' if status=='completed' else 'pending')
    assert len(actual['evidence'])==(2 if status=='completed' else 1)
    assert effects.operation(key)['state']==status and len(effects.reserved_read_urls())==1


def collect(collector,key,query):
    return asyncio.run(collector.collect(request_id='fixture-request',run_id='fixture-run',operation_key=key,
        task={'source':'financial_document','query':query,'urls':[]},brief=BRIEF,previous_operation_id=None))


@pytest.mark.parametrize('scenario',['selected_valid','selected_invalid','other','none'])
def test_discovered_pdf_hint_routes_only_the_selected_destination_to_the_validating_reader(monkeypatch,scenario):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence import collector as collector_module
    url='https://example.com/download.do?document=annual';pdf_reads=[]
    provider,effects,collector=market_collection_fixture()
    async def observations(receipt,call,**kwargs):
        if 'scraperlink/' in call['actor']:
            rows=[{'url':url,'text':'[PDF] Financial results' if scenario.startswith('selected') else 'Financial results'}]
            if scenario=='other':rows.append({'url':'https://example.com/other','text':'[PDF] Other document'})
            return SimpleNamespace(items=rows,coverage=None,cost=500)
        return await page_observations(receipt,call,**kwargs)
    async def select(task,brief,options):return [options[0]['candidate_id']]
    async def pdf(url):
        pdf_reads.append(url)
        if scenario=='selected_invalid':raise ValueError('The response is HTML, not PDF')
        return {'url':url,'sha256':'c'*64,'byte_count':100,'pages':[{'page':1,'text':'Actual PDF text.'}],'limitations':[]}
    provider.observations=observations;collector.source_selector=select
    monkeypatch.setattr(collector_module,'market_fetch_pdf',pdf)
    result=collect(collector,'pdf-hint','Company results')
    assert pdf_reads==([url] if scenario.startswith('selected') else [])
    assert len(effects.reserved_read_urls())==1
    assert len(provider.starts)==(1 if scenario.startswith('selected') else 2)
    if scenario=='selected_invalid':assert not result['evidence'] and result['status']=='partial'
    else:assert result['evidence'][0]['content_basis']==('pdf_text' if scenario=='selected_valid' else 'page_text')
    before=len(provider.starts);assert collect(collector,'pdf-hint','Company results')==result
    assert len(provider.starts)==before and len(pdf_reads)==int(scenario.startswith('selected'))


def test_last_two_reads_survive_recreation_and_stop_an_eleventh_destination():
    provider,effects,collector=market_collection_fixture()
    market_reserve_prior_reads(effects)
    provider.observations=page_observations
    async def select(task,brief,options):return [row['candidate_id'] for row in options[:1]]
    collector.source_selector=select
    first=collect(collector,'first','Initial query')
    assert len(first['evidence'])==1
    restored=MarketCollector(provider,MarketEffects(effects.snapshot,'fixture-v2-revision','fixture-request'),
        search_context={'country_code':'CO','language_code':'es'},poll_attempts=1,poll_seconds=0)
    second=collect(restored,'second','Refined query')
    assert len(second['evidence'])==1
    reads=[call for call in provider.starts if call['actor']=='apify/website-content-crawler']
    assert [len(call['input']['startUrls']) for call in reads]==[1,1]
    assert len(set(effects.reserved_read_urls()))==10
    starts=len(provider.starts)
    third=collect(restored,'third','Another query')
    assert third['status']=='unavailable' and 'diez' in third['limitations'][0]
    assert len(provider.starts)==starts
    assert collect(restored,'first','Initial query')==first
    assert len(provider.starts)==starts


def test_pending_two_page_batch_reuses_its_selection_and_receipt_after_restart():
    provider,effects,collector=market_collection_fixture()
    provider.observations=page_observations
    normal_read=provider.read
    async def pending_read(run_id):
        result=await normal_read(run_id)
        if provider.calls[run_id]['actor']=='apify/website-content-crawler':result['status']='RUNNING'
        return result
    provider.read=pending_read
    assert collect(collector,'first','Initial query')['status']=='pending'
    assert len(effects.reserved_read_urls())==2 and len(provider.starts)==2
    provider.read=normal_read
    restored=MarketCollector(provider,MarketEffects(effects.snapshot,'fixture-v2-revision','fixture-request'),
        search_context={'country_code':'CO','language_code':'es'},poll_attempts=1,poll_seconds=0)
    async def no_selection(*args):raise AssertionError('A pending read must reuse its saved selection')
    restored.source_selector=no_selection
    result=collect(restored,'first','Initial query')
    assert len(result['evidence'])==2 and len(provider.starts)==2


def test_shared_effect_admission_cannot_reserve_more_than_ten_or_repeat_a_destination():
    _,effects,_=market_collection_fixture()
    market_reserve_prior_reads(effects)
    first=effects.claim('one','apify',{'input':'first'},read_urls=['https://example.com/a'])
    other=MarketEffects(effects.snapshot,'fixture-v2-revision','fixture-request')
    with pytest.raises(MarketReadBudgetExceeded):
        other.claim('two','apify',{'input':'second'},read_urls=['https://example.com/c','https://example.com/d'])
    assert not other.claim('one','apify',{'input':'first'},read_urls=['https://example.com/a'])['claimed']
    other.claim('pdf','public_document',{'url':'https://example.com/c.pdf'},read_urls=['https://example.com/c.pdf'])
    with pytest.raises(MarketReadBudgetExceeded):
        effects.claim('again','public_document',{'url':'https://example.com/a'},read_urls=['https://example.com/a'])
    assert first['claimed'] and len(other.reserved_read_urls())==10
    assert effects.operation('two') is None and effects.operation('again') is None


def test_a_saved_selection_cannot_be_rebound_to_other_candidates():
    _,effects,collector=market_collection_fixture()
    task={'source':'web','query':'Report','urls':[]}
    items=[{'url':'https://example.com/report','text':'Report'}]
    selected=asyncio.run(collector.select_discovery_targets(task,BRIEF,items,operation_key='selection'))
    assert selected==['https://example.com/report']
    with pytest.raises(ValueError,match='changed arguments'):
        asyncio.run(collector.select_discovery_targets(task,BRIEF,[{'url':'https://example.com/other','text':'Other'}],operation_key='selection'))


def test_budget_rejection_does_not_close_a_pending_html_run_before_its_recovery(monkeypatch):
    from tests.modules.ai_market_intelligence.test_market_intelligence_graph import market_fixture, market_run
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence import collector as collector_module
    urls=['https://example.com/a','https://example.com/report.pdf']
    provider,effects,collector=market_collection_fixture(explicit_urls=urls)
    market_reserve_prior_reads(effects)
    provider.observations=page_observations
    prior=effects.claim('prior','public_document',{'url':'https://example.com/prior.pdf'},read_urls=['https://example.com/prior.pdf'])
    effects.update('prior',prior['claim'],state='completed',result={'captured':True})
    store,model,_,_=market_fixture(('financial_document',))
    original=model.ask
    async def ask(name,schema,payload):
        result=await original(name,schema,payload)
        if name=='market_plan':result['tasks'][0]['urls']=urls
        return result
    model.ask=ask
    async def no_pdf(url):raise AssertionError('The fourth read cannot be admitted')
    monkeypatch.setattr(collector_module,'market_fetch_pdf',no_pdf)
    graph=MarketWorkflow(store,model,collector,enabled_sources=('financial_document',)).build(InMemorySaver())
    provider.pending=True
    events,state=market_run(graph)
    assert state['response']['status']=='pending'
    assert not any(event['type']=='run.completed' for event in events)
    assert len(provider.starts)==1 and len(effects.reserved_read_urls())==10
    provider.pending=False
    events,state=market_run(graph)
    assert state['response']['status']=='partial'
    assert events[-1]['type']=='run.completed' and len(provider.starts)==1
    assert len(state['response']['references'])==1
