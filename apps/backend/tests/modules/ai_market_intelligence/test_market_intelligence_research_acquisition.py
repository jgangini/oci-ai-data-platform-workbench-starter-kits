"""Source-specific reference searches, browser reads and settled receipt validation."""
import asyncio
from datetime import datetime,timedelta,timezone

import pytest

from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.actor_tools import actor_tool
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.provider import ApifyProvider, _website_result
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.collector import market_publication_matches
from tests.modules.ai_market_intelligence.test_market_intelligence_payload import run_envelope, provider_for
from tests.modules.ai_market_intelligence.test_market_intelligence_website_provider import page, URL, RECEIPT
from tests.modules.ai_market_intelligence.test_market_intelligence_research_collector import market_collection_fixture


WINDOW={'start':'2026-11-01T00:00:00Z','end':'2026-11-03T00:00:00Z'}


@pytest.mark.parametrize('country',['ES','MX','FR','JP',None])
def test_resolved_search_region_overrides_default_without_mutating_service_binding(country):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.research_contracts import MarketBrief
    _,_,collector=market_collection_fixture()
    raw={'entity':'Example','market':'Resolved market','objective':'Public duties','sources':['web'],
        'clarification':None,'assumptions':[],**WINDOW,'retrieval_mode':'documentary'}
    if country is not None:raw['search_country_code']=country
    brief=MarketBrief.model_validate(raw).model_dump(mode='json')
    call=collector.call('web_search_serp',brief,query='Example employee duties',search_mode='navigation')
    assert call['input']['gl']==(country or 'CO') and call['input']['hl']=='es'
    assert collector.search_context=={'country_code':'CO','language_code':'es'}
    collector.search_context=None
    with pytest.raises(ValueError,match='approved country'):
        collector.call('web_search_serp',brief,query='Example employee duties',search_mode='navigation')


@pytest.mark.parametrize('country',['es','ESP','E','ES&hl=en','https://example.com'])
def test_search_region_cannot_smuggle_query_language_or_destination(country):
    from pydantic import ValidationError
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.research_contracts import MarketBrief
    with pytest.raises(ValidationError):
        MarketBrief.model_validate({'entity':'Example','market':'Spain','objective':'Public duties',
            'search_country_code':country,'sources':['web'],'clarification':None,'assumptions':[],**WINDOW})


def test_running_receipt_does_not_compare_nonfinal_cost_snapshots():
    raw=run_envelope(0.002);raw['structuredContent'].update(status='RUNNING',usageTotalUsd=0.001)
    provider,_=provider_for(raw)
    receipt=asyncio.run(provider.read('existing-run'))
    assert receipt['status']=='RUNNING' and receipt['spent_microusd'] is None


def test_terminal_receipt_waits_for_settlement_before_reconciling_both_cost_channels(monkeypatch):
    now=datetime.now(timezone.utc)
    early=run_envelope(0.002);early['structuredContent'].update(usageTotalUsd=0.001,finishedAt=(now-timedelta(seconds=1)).isoformat())
    settled=run_envelope(0.003);settled['structuredContent'].update(usageTotalUsd=0.003,finishedAt=(now-timedelta(seconds=20)).isoformat())
    class Tool:
        def __init__(self):self.values=iter([early,settled]);self.calls=[]
        async def ainvoke(self,args):
            assert args=={'runId':'existing-run','waitSecs':0}
            self.calls.append(args);return next(self.values)
    tool=Tool();provider=ApifyProvider({k:tool for k in ('call-actor','get-actor-run','get-dataset-items')})
    delays=[]
    async def sleep(delay):delays.append(delay)
    monkeypatch.setattr(asyncio,'sleep',sleep)
    receipt=asyncio.run(provider.read('existing-run'))
    assert receipt['spent_microusd']==3000 and len(tool.calls)==2 and len(delays)==1
    assert 0<delays[0]<=10


def test_reference_x_search_is_bounded_top_keywords_without_a_recent_time_filter():
    tool=actor_tool('x_search');args={'query':'Example grains insects','max_items':50,'sort':'Latest'}
    default=tool.compile(args,WINDOW,unlimited=True)
    reference=tool.compile(args,WINDOW,unlimited=True,reference_search=True)
    assert 'since:' in default['input']['searchTerms'][0] and default['input']['sort']=='Latest'
    assert reference['input']['searchTerms']==['(Example grains insects)'] and reference['input']['sort']=='Top'
    assert reference['input']['maxItems']==20 and reference['actor']==default['actor']


@pytest.mark.parametrize('operator',['from:someone','since:2010-01-01','until:2030-01-01'])
def test_reference_search_cannot_smuggle_author_or_time_overrides(operator):
    with pytest.raises(ValueError):
        actor_tool('x_search').compile({'query':'Example '+operator,'max_items':20},WINDOW,unlimited=True,reference_search=True)


def test_browser_read_preserves_target_budget_and_disables_linked_downloads_and_captures():
    _,_,collector=market_collection_fixture()
    call=collector.call('website',WINDOW,urls=[URL])
    inputs=call['input']
    assert inputs['crawlerType']=='playwright:adaptive' and call['callOptions']['memory']==4096
    assert inputs['maxCrawlPages']==inputs['maxResults']==1 and inputs['maxCrawlDepth']==0
    assert inputs['maxConcurrency']==1 and 'saveContentTypes' not in inputs and not inputs['saveFiles']
    assert not inputs['saveScreenshots'] and inputs['maxRequestRetries']==0
    assert 'cookies' not in inputs


def test_runtime_read_flags_cannot_be_set_by_model_arguments_or_other_actors():
    with pytest.raises(ValueError):
        actor_tool('x_search').compile({'query':'Example','max_items':20,'reference_search':True},WINDOW,unlimited=True)
    with pytest.raises(ValueError,match='X adapter'):
        actor_tool('website').compile({'urls':[URL],'max_items':1},WINDOW,unlimited=True,
            approved_urls=[URL],usage_cost_verified=True,reference_search=True)


def test_reference_dates_are_shared_between_collection_and_commit_without_loosening_conversation():
    reference={**WINDOW,'retrieval_mode':'documentary'}
    conversation={**WINDOW,'retrieval_mode':'conversation'}
    assert market_publication_matches('x',reference,'2018-03-01T00:00:00Z')
    assert not market_publication_matches('x',conversation,'2018-03-01T00:00:00Z')
    assert not market_publication_matches('x',reference,'2027-01-01T00:00:00Z')
    assert not market_publication_matches('web',WINDOW,'2018-03-01T00:00:00Z')


def test_access_challenge_is_a_failed_page_with_no_topic_evidence():
    row=page();row['markdown']='# Bot Detection\n\n### COMPUTING PROOF OF WORK\n\nYour browser is solving a small puzzle.'
    row['crawl']['httpStatusCode']=200
    call={'actor':'apify/website-content-crawler','input':{'startUrls':[{'url':URL}]},'callOptions':{'maxItems':1}}
    observed=_website_result([row],RECEIPT,call,True,600000)
    assert observed.items==[] and observed.coverage['failed_pages'][0]['reason']=='access_challenge'
    with pytest.raises(ValueError,match='access challenge'):
        _website_result([row],RECEIPT,call,False)
    row['markdown']='# How bot detection works\n\nA research article explains computing proof of work.'
    assert _website_result([row],RECEIPT,call,True,600000).items
