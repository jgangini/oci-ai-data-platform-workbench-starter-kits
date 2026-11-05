"""Effect admission and real compiler/provider boundary exercised without external calls."""
import asyncio
from types import SimpleNamespace

import pytest

from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.runtime_core import Scope
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.collector import MarketCollector
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.effects import MarketEffectPending, MarketEffects, MarketObjectSnapshot, MarketRecordedModel
from tests.modules.ai_market_intelligence.test_market_intelligence_graph import MarketSnapshot, MarketFixtureModel, market_evidence, market_fixture, market_run


class MarketProviderFixture:
    def __init__(self):
        self.starts=[]
        self.reads=[]
        self.fail_start=False
        self.pending=False
        self.calls={}

    async def start(self, call):
        self.starts.append(call)
        identity='receipt-'+str(len(self.starts))
        self.calls[identity]=call
        if self.fail_start:
            raise TimeoutError('A start might have reached the provider')
        return {'runId':identity,'status':'RUNNING','datasetId':None,'spent_microusd':None}

    async def read(self, run_id):
        self.reads.append(run_id)
        return {'runId':run_id,'status':'RUNNING' if self.pending else 'SUCCEEDED',
                'datasetId':'dataset-'+run_id,'spent_microusd':500}

    async def observations(self, receipt, call, **kwargs):
        if 'scraperlink/' in call['actor']:
            row={'url':'https://facebook.com/fixturebrand','text':'Fixture brand public page',
                 'provenance':{'provider_run_id':receipt['runId'],'content_basis':'search_title_and_snippet'}}
        else:
            row={**market_evidence('facebook'),
                 'url':'https://facebook.com/fixturebrand/posts/1',
                 'provenance':{'provider_run_id':receipt['runId'],'content_basis':'post_text'}}
        return SimpleNamespace(items=[row],coverage=None,cost=receipt['spent_microusd'])


def market_collection_fixture(**kwargs):
    provider=MarketProviderFixture()
    effects=MarketEffects(MarketSnapshot(),'fixture-v2-revision','fixture-request')
    collector=MarketCollector(provider,effects,poll_attempts=1,poll_seconds=0,
                          search_context={'country_code':'CO','language_code':'es'},**kwargs)
    return provider,effects,collector


def market_reserve_prior_reads(effects,count=8):
    for n in range(count):
        key='prior-read-'+str(n);url=f'https://example.com/prior-{n}.pdf'
        claimed=effects.claim(key,'public_document',{'url':url},read_urls=[url])
        effects.update(key,claimed['claim'],state='completed',result={'synthetic_prior_capture':True})


def market_collect(collector, *, urls=(), source='facebook'):
    return asyncio.run(collector.collect(request_id='fixture-request',run_id='fixture-run',operation_key='fixture-operation',
        task={'source':source,'query':'Fixture brand reclamaciones','urls':list(urls)},
        brief={'start':'2026-10-27T12:00:00+00:00','end':'2026-11-02T12:00:00+00:00'},previous_operation_id=None))


def test_market_facebook_keyword_search_has_its_own_durable_receipt_and_native_attribution():
    provider,effects,collector=market_collection_fixture()
    first=market_collect(collector)
    again=market_collect(collector)
    assert len(provider.starts)==1 and first==again
    assert provider.starts[0]['actor']=='apify/facebook-search-scraper'
    assert provider.starts[0]['input']['searchType']=='posts'
    assert first['evidence'][0]['source']=='facebook' and first['evidence'][0]['discovery_channel']=='facebook'
    operations=effects.snapshot.read()[0]['operations']
    assert {row['state'] for row in operations.values()}=={'completed'}
    assert all(row['receipt']['runId'] for row in operations.values())


def test_market_known_youtube_run_with_empty_results_recovers_without_a_new_start():
    from tests.modules.ai_market_intelligence.test_market_intelligence_payload import provider_for
    from tests.modules.ai_market_intelligence.test_market_intelligence_youtube_provider import empty_search
    provider,effects,collector=market_collection_fixture()
    provider.pending=True
    assert market_collect(collector,source='youtube')['status']=='pending'
    provider.pending=False
    async def normalize(receipt,call,**kwargs):
        _,row=empty_search(call['input']['searchQueries'][0])
        normalizer,_=provider_for([row])
        return await normalizer.observations(receipt,call)
    provider.observations=normalize
    result=market_collect(collector,source='youtube')
    assert result['status']=='partial' and result['evidence']==[]
    assert market_collect(collector,source='youtube')==result
    assert len(provider.starts)==1 and len(provider.reads)==2
    operation=next(iter(effects.snapshot.read()[0]['operations'].values()))
    assert operation['state']=='completed' and operation['result']['items']==[]
    assert operation['receipt']['spent_microusd']==500


def test_market_youtube_channel_metadata_survives_the_provider_to_evidence_projection():
    _,_,collector=market_collection_fixture()
    row={**market_evidence('youtube'),'provenance':{'provider_run_id':'video-run',
         'content_basis':'title_and_description','channel_name':'Observed public channel',
         'channel_url':'https://www.youtube.com/@observedchannel'}}
    result=collector.evidence_row('youtube','youtube',row,{'runId':'video-run'},
        {'start':'2026-10-27T00:00:00Z','end':'2026-11-03T23:59:59Z'})
    assert result['author_name']=='Observed public channel'
    assert result['author_url']=='https://www.youtube.com/@observedchannel'
    assert result['content_basis']=='title_and_description'


def test_market_instagram_empty_marker_completes_its_known_receipt_and_does_not_restart():
    from tests.modules.ai_market_intelligence.test_market_intelligence_payload import provider_for
    provider,effects,collector=market_collection_fixture()
    async def normalize(receipt,call,**kwargs):
        route='https://www.instagram.com/popular/fixture-brand-reclamaciones'
        row={'inputUrl':route,'url':route,'error':'no_items',
             'errorDescription':'Empty or private data for provided input','requestErrorMessages':[]}
        normalizer,_=provider_for([row])
        return await normalizer.observations(receipt,call)
    provider.observations=normalize
    result=market_collect(collector,source='instagram')
    assert result['status']=='partial' and result['evidence']==[]
    assert market_collect(collector,source='instagram')==result and len(provider.starts)==1
    operation=next(iter(effects.snapshot.read()[0]['operations'].values()))
    assert operation['state']=='completed' and operation['result']['items']==[]
    assert operation['receipt']['spent_microusd']==500


def test_market_uncertain_start_is_not_repeated_even_without_returned_run_id():
    provider,effects,collector=market_collection_fixture(explicit_urls=['https://facebook.com/fixturebrand'])
    provider.fail_start=True
    with pytest.raises(TimeoutError):
        market_collect(collector,urls=['https://facebook.com/fixturebrand'])
    result=market_collect(collector,urls=['https://facebook.com/fixturebrand'])
    assert len(provider.starts)==1 and result['status']=='pending'
    assert not result['evidence']
    assert effects.snapshot.read()[0]['operations']['fixture-operation-read']['state']=='uncertain'


def test_market_known_start_rejection_diagnostic_is_durable_without_repeating_the_start():
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.runtime_core import ProviderStartRejected
    provider,effects,collector=market_collection_fixture(explicit_urls=['https://facebook.com/fixturebrand'])
    async def rejected(call):
        provider.starts.append(call)
        raise ProviderStartRejected({'reason':'invalid_input','input_field':'searchTerms'})
    provider.start=rejected
    first=market_collect(collector,urls=['https://facebook.com/fixturebrand'])
    second=market_collect(collector,urls=['https://facebook.com/fixturebrand'])
    operation=effects.snapshot.read()[0]['operations']['fixture-operation-read']
    assert first['status']==second['status']=='unavailable' and len(provider.starts)==1
    assert operation['state']=='rejected' and operation['receipt'] is None
    assert operation['result']=={'diagnostic':{'reason':'invalid_input','input_field':'searchTerms'}}


def test_financial_collection_follows_one_observed_pdf_with_proof_and_no_recursive_or_repeat_fetch(monkeypatch):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence import collector as collector_module
    provider,effects,collector=market_collection_fixture(explicit_urls=['https://example.com/reports'])
    fetches=[]
    async def observations(receipt,call,**kwargs):
        row={'url':'https://example.com/reports','text':'[Informe](https://example.com/annual.pdf)',
             'published_at':None,'observed_at':'2026-11-02T12:00:00+00:00',
             'provenance':{'provider_run_id':receipt['runId'],'requested_url':'https://example.com/reports'}}
        return SimpleNamespace(items=[row],coverage=None,cost=receipt['spent_microusd'])
    async def fetch(url):
        fetches.append(url)
        return {'url':url,'source_url':url,'sha256':'a'*64,'byte_count':200,'page_count':1,
            'pages':[{'page':1,'text':'Ingresos 2025: 100. [No recursivo](https://example.com/next.pdf)'}],
            'limitations':[]}
    provider.observations=observations
    monkeypatch.setattr(collector_module,'market_fetch_pdf',fetch)
    arguments={'request_id':'fixture-request','run_id':'fixture-run','operation_key':'linked',
        'task':{'source':'financial_document','query':'Informe','urls':['https://example.com/reports']},
        'brief':{'start':'2026-11-01T00:00:00+00:00','end':'2026-11-02T00:00:00+00:00','retrieval_mode':'financial'},
        'previous_operation_id':None}
    first=asyncio.run(collector.collect(**arguments))
    again=asyncio.run(collector.collect(**arguments))
    assert first==again and first['status']=='partial' and len(first['evidence'])==2
    assert fetches==['https://example.com/annual.pdf'] and len(provider.starts)==1
    operation=next(o for o in effects.snapshot.read()[0]['operations'].values() if o['kind']=='public_document')
    assert operation['result']['discovered_from']['parent_receipt_id']=='receipt-1'
    assert operation['result']['discovered_from']['resolved_url']==fetches[0]
    assert first['evidence'][1]['requested_url']==fetches[0] and first['evidence'][1]['locator']=='Página 1'
    for status in ('pending','complete'):
        collection={**first,'status':status,'evidence':[first['evidence'][0]]}
        result=asyncio.run(collector.follow_document('no-budget','web','web',['https://example.com/'+str(i) for i in range(10)],
            arguments['brief'],collection))
        assert len(result['evidence'])==1 and fetches==['https://example.com/annual.pdf']
        if status=='complete':
            assert any('no se siguieron' in limit for limit in result['limitations'])
    redirected={**first['evidence'][1],'url':'https://example.com/annual.pdf','requested_url':'https://example.com/old.pdf'}
    already_read={**first,'evidence':[first['evidence'][0],redirected]}
    assert asyncio.run(collector.follow_document('redirected','web','web',
        ['https://example.com/reports','https://example.com/old.pdf'],arguments['brief'],already_read))==already_read
    assert fetches==['https://example.com/annual.pdf']


def test_web_search_reads_two_ranked_candidates_in_one_batch_and_reuses_the_selection(monkeypatch):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence import collector as collector_module
    provider,effects,collector=market_collection_fixture()
    async def observations(receipt,call,**kwargs):
        if 'scraperlink/' in call['actor']:
            rows=[{'url':f'https://example.com/result/{index}','text':f'Candidate {index}'} for index in range(10)]
        else:
            rows=[{'url':item['url'],'text':'Contenido. [No seguir](https://example.com/other.pdf)',
                'published_at':None,'observed_at':'2026-11-02T12:00:00+00:00',
                'provenance':{'provider_run_id':receipt['runId'],'requested_url':item['url']}}
                for item in call['input']['startUrls']]
        return SimpleNamespace(items=rows,coverage=None,cost=receipt['spent_microusd'])
    selections=[]
    async def select(task,brief,options):
        selections.append(options)
        return [options[2]['candidate_id'],options[8]['candidate_id']]
    async def no_pdf(url):
        raise AssertionError('Web page links cannot authorize additional automatic reads')
    provider.observations,collector.source_selector=observations,select
    monkeypatch.setattr(collector_module,'market_fetch_pdf',no_pdf)
    arguments=dict(request_id='fixture-request',run_id='fixture-run',operation_key='ranked-web',
        task={'source':'web','query':'Fixture report','urls':[]},
        brief={'start':'2026-11-01T00:00:00+00:00','end':'2026-11-02T00:00:00+00:00','retrieval_mode':'documentary'},
        previous_operation_id=None)
    result=asyncio.run(collector.collect(**arguments))
    assert asyncio.run(collector.collect(**arguments))==result
    assert len(provider.starts)==2 and len(result['evidence'])==2
    assert len(selections)==1 and len(selections[0])==10
    call=provider.starts[1]
    assert [item['url'] for item in call['input']['startUrls']]==[f'https://example.com/result/{i}' for i in (2,8)]
    assert call['input']['maxCrawlDepth']==0 and call['input']['maxCrawlPages']==2


@pytest.mark.parametrize('failure',['404','not_pdf'])
def test_optional_document_failure_preserves_html_and_durable_rejection(monkeypatch,failure):
    import httpx
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence import collector as collector_module
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.research_contracts import MarketEvidence
    provider,effects,collector=market_collection_fixture()
    row={**market_evidence('web',),'text':'Dato útil. [Informe](https://example.com/annual.pdf)',
         'content_basis':'page_text'}
    row=MarketEvidence.model_validate(row).model_dump(mode='json')
    collection={'status':'complete','evidence':[row],'limitations':[],'operation_id':'parent'}
    fetches=[]
    async def fetch(url):
        fetches.append(url)
        if failure=='404':
            raise httpx.HTTPStatusError('private-body',request=httpx.Request('GET',url),response=httpx.Response(404))
        raise ValueError('A bounded PDF document is required')
    monkeypatch.setattr(collector_module,'market_fetch_pdf',fetch)
    arguments=('optional','web','web',[row['url']],{'retrieval_mode':'documentary'},collection)
    first=asyncio.run(collector.follow_document(*arguments))
    again=asyncio.run(collector.follow_document(*arguments))
    assert first==again and first['status']=='partial' and first['evidence']==[row]
    assert len(fetches)==1 and provider.starts==[]
    operation=next(iter(effects.snapshot.read()[0]['operations'].values()))
    assert operation['state']=='rejected' and operation['result']['discovered_from']['parent_receipt_id']==row['receipt_id']
    assert 'private-body' not in str(operation)


@pytest.mark.parametrize('linked',[False,True])
@pytest.mark.parametrize('error_type',[TimeoutError,asyncio.CancelledError,RuntimeError])
def test_unresolved_document_retains_proof_and_never_repeats(monkeypatch,linked,error_type):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence import collector as collector_module
    _,effects,collector=market_collection_fixture()
    proof={'parent_receipt_id':'verified-parent','observed_link':'[Informe](annual.pdf)'} if linked else None
    fetches=[]
    async def fetch(url):
        fetches.append(url)
        raise error_type('unknown download result')
    monkeypatch.setattr(collector_module,'market_fetch_pdf',fetch)
    with pytest.raises(error_type):
        asyncio.run(collector.pdf_operation('linked','https://example.com/annual.pdf',discovered_from=proof))
    assert asyncio.run(collector.pdf_operation('linked','https://example.com/annual.pdf',discovered_from=proof)) is None
    operation=effects.snapshot.read()[0]['operations']['linked']
    assert operation['state']=='uncertain' and len(fetches)==1
    assert operation['result']==({'discovered_from':proof} if linked else None)


@pytest.mark.parametrize('failure',['404','not_pdf'])
def test_direct_pdf_rejection_preserves_other_page_budget_and_recovery(monkeypatch,failure):
    import httpx
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence import collector as collector_module
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.effects import MarketReadBudgetExceeded
    pdf='https://example.com/annual.pdf';html='https://example.com/results'
    provider,effects,collector=market_collection_fixture(explicit_urls=[pdf,html])
    fetches=[]
    async def fetch(url):
        fetches.append(url)
        if failure=='404':
            raise httpx.HTTPStatusError('private-body',request=httpx.Request('GET',url),response=httpx.Response(404))
        raise ValueError('A bounded PDF document is required')
    async def observations(receipt,call,**kwargs):
        return SimpleNamespace(items=[{'url':html,'text':'Dato publicado por la entidad.',
            'published_at':None,'observed_at':'2026-11-02T00:00:00+00:00',
            'provenance':{'provider_run_id':receipt['runId'],'requested_url':html}}],coverage=None,cost=500)
    monkeypatch.setattr(collector_module,'market_fetch_pdf',fetch)
    provider.observations=observations
    arguments=dict(request_id='fixture-request',run_id='fixture-run',operation_key='direct-pdf',
        task={'source':'financial_document','query':'Annual revenue','urls':[pdf,html]},
        brief={'start':'2026-11-01T00:00:00+00:00','end':'2026-11-02T00:00:00+00:00','retrieval_mode':'financial'},
        previous_operation_id=None)
    result=asyncio.run(collector.collect(**arguments))
    assert result['status']=='partial' and [row['url'] for row in result['evidence']]==[html]
    assert asyncio.run(collector.collect(**arguments))==result
    assert fetches==[pdf] and len(provider.starts)==1
    operations=effects.read()[0]['operations'].values()
    rejected=[row for row in operations if row['kind']=='public_document']
    assert len(rejected)==1 and rejected[0]['state']=='rejected'
    assert rejected[0]['result']['diagnostic']['error_type']==('HTTPStatusError' if failure=='404' else 'ValueError')
    assert all(row['state'] in {'completed','rejected'} for row in operations)
    assert set(effects.reserved_read_urls())=={pdf,html} and 'private-body' not in str(rejected)
    market_reserve_prior_reads(effects)
    with pytest.raises(MarketReadBudgetExceeded):
        effects.claim('third','public_document',{'url':'https://example.com/third.pdf'},read_urls=['https://example.com/third.pdf'])


def test_market_pending_read_resumes_the_same_admitted_provider_run():
    provider,effects,collector=market_collection_fixture(explicit_urls=['https://facebook.com/fixturebrand'])
    provider.pending=True
    first=market_collect(collector,urls=['https://facebook.com/fixturebrand'])
    assert first['status']=='pending'
    provider.pending=False
    second=market_collect(collector,urls=['https://facebook.com/fixturebrand'])
    assert second['status']=='complete' and len(provider.starts)==1
    assert len(set(provider.reads))==1


def test_market_model_proposal_cannot_authorize_its_own_url():
    provider,effects,collector=market_collection_fixture()
    with pytest.raises(ValueError,match='no explicit or discovered authority'):
        market_collect(collector,urls=['https://facebook.com/invented'])
    assert provider.starts==[] and effects.snapshot.read()[0] is None


def test_explicit_social_authority_accepts_only_the_reviewed_canonical_profile_alias():
    provider,effects,collector=market_collection_fixture(explicit_urls=['https://www.facebook.com/fixturebrand/'])
    result=market_collect(collector,urls=['https://facebook.com/fixturebrand'])
    assert result['status']=='complete' and len(provider.starts)==1
    with pytest.raises(ValueError,match='no explicit or discovered authority'):
        market_collect(collector,urls=['https://facebook.com/anotherbrand'])


def test_market_effect_claims_are_exclusive_and_candidate_owner_bound():
    provider,effects,collector=market_collection_fixture()
    arguments={'query':'fixture'}
    first=effects.claim('operation','apify',arguments)
    assert first['claimed'] and not effects.claim('operation','apify',arguments)['claimed']
    with pytest.raises(ValueError,match='changed arguments'):
        effects.claim('operation','apify',{'query':'changed'})
    other=MarketEffects(effects.snapshot,'other-revision','fixture-request')
    with pytest.raises(ValueError,match='another request, owner or candidate'):
        other.claim('operation','apify',arguments)


def test_recovery_candidate_reads_the_same_matching_admitted_capture_without_another_start():
    provider,old,collector=market_collection_fixture()
    provider.pending=True
    assert market_collect(collector)['status']=='pending' and len(provider.starts)==1
    recorded=old.snapshot.read()[0]['operations']
    effects=MarketEffects(MarketSnapshot(),'corrected-candidate','new-request',prior_operations=recorded)
    corrected=MarketCollector(provider,effects,poll_attempts=1,poll_seconds=0,
        search_context={'country_code':'CO','language_code':'es'})
    provider.pending=False
    result=market_collect(corrected)
    assert result['status']=='complete'
    assert len(provider.starts)==1
    reused=effects.snapshot.read()[0]['operations']['fixture-operation-search']
    assert reused['reused_from'] and reused['receipt']['runId']=='receipt-1'


def test_market_model_receipts_cache_replies_and_preserve_uncertain_calls():
    provider,effects,collector=market_collection_fixture()
    raw=MarketFixtureModel()
    model=MarketRecordedModel(raw,effects)
    payload={'question':'fixture'}
    first=asyncio.run(model.ask('market_understand',{},payload))
    assert asyncio.run(model.ask('market_understand',{},payload))==first and raw.calls==['market_understand']
    async def uncertain(*args):
        raise TimeoutError('Model response did not return')
    raw.ask=uncertain
    with pytest.raises(TimeoutError):
        asyncio.run(model.ask('another-node',{},payload))
    with pytest.raises(MarketEffectPending):
        asyncio.run(model.ask('another-node',{},payload))
    with pytest.raises(MarketEffectPending):
        asyncio.run(model.ask('another-node',{}, {'question':'changed-context'}))


def test_disconnected_consumer_drains_only_the_admitted_model_response_into_its_receipt():
    async def scenario():
        provider,effects,collector=market_collection_fixture()
        entered,release=asyncio.Event(),asyncio.Event()
        calls=[]
        async def ask(name,schema,payload):
            calls.append(name)
            entered.set()
            await release.wait()
            return {'captured':'response'}
        model=MarketRecordedModel(SimpleNamespace(ask=ask),effects)
        consumer=asyncio.create_task(model.ask('admitted',{},{}))
        await entered.wait()
        consumer.cancel()
        await asyncio.sleep(0)
        assert not consumer.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        operation=next(iter(effects.snapshot.read()[0]['operations'].values()))
        assert operation['state']=='completed' and operation['result']=={'captured':'response'}
        assert await model.ask('admitted',{},{} )==operation['result'] and calls==['admitted']
    asyncio.run(scenario())


def test_market_intake_resume_preserves_its_time_and_never_repeats_an_uncertain_model_call():
    from datetime import datetime, timezone
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
    from langgraph.checkpoint.memory import InMemorySaver
    store,raw,collector,_=market_fixture()
    effects=MarketEffects(MarketSnapshot(),'fixture-v2-revision','fixture-request')
    model=MarketRecordedModel(raw,effects)
    calls=[]
    async def uncertain(name,schema,payload):
        calls.append(payload['now'])
        raise TimeoutError('No model response returned')
    raw.ask=uncertain
    first=MarketWorkflow(store,model,collector,enabled_sources=('web',),
        clock=lambda:datetime(2026,11,2,12,tzinfo=timezone.utc)).build(InMemorySaver())
    _,first_state=market_run(first)
    assert first_state['response']['status']=='failed'
    assert first_state['response']['failure']=={'stage':'understand','error_type':'TimeoutError'}
    resumed=MarketWorkflow(store,model,collector,enabled_sources=('web',),
        clock=lambda:datetime(2026,11,3,12,tzinfo=timezone.utc)).build(InMemorySaver())
    _,resumed_state=market_run(resumed)
    assert resumed_state['response']['status']=='pending'
    assert calls==['2026-11-02T12:00:00+00:00'] and collector.calls==[]


def test_market_read_missing_requested_pages_is_partial_even_when_returned_rows_all_parse():
    provider,effects,collector=market_collection_fixture()
    original=provider.observations
    async def observations(receipt,call,**kwargs):
        if 'scraperlink/' in call['actor']:
            return SimpleNamespace(items=[{'url':f'https://example.com/{i}'} for i in range(3)],coverage=None,cost=receipt['spent_microusd'])
        row={**market_evidence(),'url':'https://example.com/1',
             'provenance':{'provider_run_id':receipt['runId'],'requested_url':'https://example.com/1'}}
        return SimpleNamespace(items=[row],coverage={'failed_pages':[]},cost=receipt['spent_microusd'])
    provider.observations=observations
    result=market_collect(collector,source='web')
    assert result['status']=='partial' and len(result['evidence'])==1
    assert 'No se recuperaron 1 de las 2 páginas solicitadas.' in result['limitations']


def test_market_pdf_replay_preserves_capture_identity_while_a_new_operation_has_its_own_receipt(monkeypatch):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence import collector as collector_module
    urls=['https://example.com/page','https://example.com/report.pdf']
    provider,effects,collector=market_collection_fixture(explicit_urls=urls)
    downloads=[]
    async def fetch(url):
        downloads.append(url)
        return {'url':url,'sha256':'a'*64,'byte_count':100,'pages':[{'page':1,'text':'Revenue 2025: 120'}],'limitations':[]}
    monkeypatch.setattr(collector_module,'market_fetch_pdf',fetch)
    provider.pending=True
    first=market_collect(collector,urls=urls,source='financial_document')
    provider.pending=False
    second=market_collect(collector,urls=urls,source='financial_document')
    pdf=lambda collection:next(row for row in collection['evidence'] if row['content_basis']=='pdf_text')
    assert first['status']=='pending' and pdf(first)==pdf(second) and len(downloads)==1
    newer=asyncio.run(collector.read_documents('new-collection','financial_document','web',[urls[1]],
        {'start':'2026-10-27T12:00:00+00:00','end':'2026-11-02T12:00:00+00:00'}))
    assert newer['status']=='partial' and not newer['evidence'] and len(downloads)==1
    next_request=MarketCollector(provider,MarketEffects(MarketSnapshot(),'fixture-v2-revision','next-request'))
    separate=asyncio.run(next_request.read_documents('next-collection','financial_document','web',[urls[1]],
        {'start':'2026-10-27T12:00:00+00:00','end':'2026-11-02T12:00:00+00:00'}))
    assert pdf(separate)['evidence_id']!=pdf(first)['evidence_id'] and len(downloads)==2


def test_market_cancellation_during_first_pdf_prevents_admission_of_second_document(monkeypatch):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence import collector as collector_module
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.state import MarketCancelled
    urls=['https://example.com/first.pdf','https://example.com/second.pdf']
    provider,effects,collector=market_collection_fixture(explicit_urls=urls)
    downloads=[]
    def guard():
        if downloads:
            raise MarketCancelled('Cancelled while first request was running')
    collector.before_effect=guard
    async def fetch(url):
        downloads.append(url)
        return {'url':url,'sha256':'a'*64,'byte_count':100,'pages':[{'page':1,'text':'A financial figure'}],'limitations':[]}
    monkeypatch.setattr(collector_module,'market_fetch_pdf',fetch)
    with pytest.raises(MarketCancelled):
        market_collect(collector,urls=urls,source='financial_document')
    assert downloads==urls[:1] and len(effects.snapshot.read()[0]['operations'])==1
    assert next(iter(effects.snapshot.read()[0]['operations'].values()))['state']=='completed'


def test_market_combined_pdf_limit_marks_omitted_pages_as_unanalysed(monkeypatch):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence import collector as collector_module
    urls=['https://example.com/first.pdf','https://example.com/second.pdf']
    _,_,collector=market_collection_fixture(explicit_urls=urls)
    async def fetch(url):
        pages=[{'page':n,'text':f'First document page {n}'} for n in range(1,51)] if url==urls[0] else [
            {'page':1,'text':'Unique contradiction in the second document.'}]
        return {'url':url,'sha256':'a'*64,'byte_count':100,'pages':pages,'limitations':[]}
    monkeypatch.setattr(collector_module,'market_fetch_pdf',fetch)
    result=market_collect(collector,urls=urls,source='financial_document')
    assert result['status']=='partial' and len(result['evidence'])==50
    assert 'dejó fuera 1 páginas' in result['limitations'][0]
    assert 'no se analizaron' in result['limitations'][0]


def test_market_success_without_dataset_remains_pending_until_the_same_run_is_complete():
    provider,effects,collector=market_collection_fixture(explicit_urls=['https://facebook.com/fixturebrand'])
    read=provider.read
    async def missing_dataset(run_id):
        result=await read(run_id)
        result['datasetId']=None
        return result
    provider.read=missing_dataset
    assert market_collect(collector,urls=['https://facebook.com/fixturebrand'])['status']=='pending'
    operation=effects.snapshot.read()[0]['operations']['fixture-operation-read']
    assert operation['state']=='running' and operation['receipt']['datasetId'] is None
    provider.read=read
    assert market_collect(collector,urls=['https://facebook.com/fixturebrand'])['status']=='complete'
    assert len(provider.starts)==1


def test_market_request_objects_are_separate_from_legacy_and_each_other():
    scope=Scope('fixture-tenant','fixture-user','d27966a4-6b46-4734-a672-b648a3e7e375')
    first=MarketObjectSnapshot(None,'namespace','bucket',scope,'revision','first','request')
    second=MarketObjectSnapshot(None,'namespace','bucket',scope,'revision','second','request')
    receipts=MarketObjectSnapshot(None,'namespace','bucket',scope,'revision','first','effects')
    assert len({first.key,second.key,receipts.key,scope.prefix+'state.json'})==4


def test_completed_draft_correction_replays_after_a_checkpoint_gap_without_another_decision():
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
    store,model,collector,_=market_fixture()
    row=market_evidence()
    store.begin('one','Research')
    store.save(evidence={row['evidence_id']:row})
    effects=MarketEffects(MarketSnapshot(),'fixture-v2-revision','one')
    original=model.ask
    async def corrected(name,schema,payload):
        result=await original(name,schema,payload)
        if name=='market_verify' and payload['block']['text']=='Wrong attribution.':
            return {'accepted':False,'issues':[{'claim':'Wrong attribution.','evidence_id':row['evidence_id'],
                'quoted_text':row['text'],'reason':'Attribution must follow the source.'}]}
        return result
    model.ask=corrected
    workflow=MarketWorkflow(store,MarketRecordedModel(model,effects),collector,enabled_sources=('web',))
    state={'task':{'source':'web'},'block':{'text':'Wrong attribution.','evidence_ids':[row['evidence_id']],'limitations':[]},
        'analysis':{'financial_facts':[],'findings':[{'supports':[row['evidence_id']],'refutes':[]}]}}
    first=asyncio.run(workflow.reviewed_draft(state))
    calls=list(model.calls)
    second=asyncio.run(workflow.reviewed_draft(state))
    assert first==second and first[1] is True and model.calls==calls
    assert calls==['market_verify','market_repair_units','market_verify_repair'] and collector.calls==[]


def test_market_website_and_financial_parser_metadata_survive_collection():
    provider,effects,collector=market_collection_fixture()
    brief={'start':'2026-10-27T12:00:00+00:00','end':'2026-11-02T12:00:00+00:00'}
    receipt={'runId':'receipt-web'}
    row={**market_evidence(),'provenance':{'provider_run_id':receipt['runId']}}
    result=collector.collection('web','web','operation',{'items':[row]},receipt,brief)
    assert result['evidence'][0]['content_basis']=='page_text'
    row['published_at']=None
    row['metrics']={'metric':'value','entity':'CO','indicator':'INDICATOR','unit':'USD','currency':'USD',
                    'points':[{'period':'2025','value':12.0}]}
    row['provenance']['data_last_updated']='2026-10-08'
    result=collector.collection('world_bank','world_bank','operation',{'items':[row]},receipt,brief)
    assert result['evidence'][0]['content_basis']=='financial_series'
    assert result['evidence'][0]['financial']==row['metrics']
    assert result['evidence'][0]['data_last_updated']=='2026-10-08'
    assert result['evidence'][0]['limitations']==[]


def test_market_unverified_start_preserves_a_safe_diagnostic_without_repeating_the_call():
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.runtime_core import ProviderStartResponseError
    provider,effects,collector=market_collection_fixture(explicit_urls=['https://facebook.com/fixturebrand'])
    calls=[]
    async def invalid_start(call):
        calls.append(call)
        raise ProviderStartResponseError({'response_type':'str','provider_validation_message':'Memory is below the supported minimum.'})
    provider.start=invalid_start
    with pytest.raises(ProviderStartResponseError):
        market_collect(collector,urls=['https://facebook.com/fixturebrand'])
    operation=effects.snapshot.read()[0]['operations']['fixture-operation-read']
    assert operation['state']=='uncertain' and operation['receipt'] is None
    assert 'minimum' in operation['result']['diagnostic']['provider_validation_message']
    assert market_collect(collector,urls=['https://facebook.com/fixturebrand'])['status']=='pending'
    assert len(calls)==1


def test_market_all_eight_sources_close_within_the_explicit_graph_bound():
    sources=('web','x','youtube','facebook','instagram','market_data','world_bank','financial_document')
    store,model,collector,graph=market_fixture(sources)
    model.refine=True
    events,state=market_run(graph)
    assert len(collector.calls)==13 and state['response']['status']=='partial'
    assert all(sum(call['task']['source']==source for call in collector.calls)==1 for source in ('market_data','world_bank'))
    assert events[-1]['type']=='run.completed'
