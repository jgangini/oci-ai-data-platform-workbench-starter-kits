"""Document projection preserves literal spans and the existing capture budget."""
import asyncio
from hashlib import sha256
import json

import pytest

from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.passages import market_passage_candidates, market_project_passages, market_text_prefix
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.provider import ApifyProvider
from tests.modules.ai_market_intelligence.test_market_intelligence_payload import provider_for
from tests.modules.ai_market_intelligence.test_market_intelligence_website_provider import page, RECEIPT, URL
from tests.modules.ai_market_intelligence.test_market_intelligence_research_collector import market_collection_fixture


def long_document():
    return '# Introduction\n\n'+('Context without the requested data. '*600)+'\n\n## Income statements\n\n(In millions)\nYear2024: Revenue245122.\n'


def test_source_retention_is_opt_in_and_preserves_text_after_the_old_prefix():
    row=page();row['markdown']=long_document()
    provider,_=provider_for([row])
    call={'actor':'apify/website-content-crawler','input':{'startUrls':[{'url':URL}]},'callOptions':{'maxItems':1}}
    legacy=asyncio.run(provider.observations(RECEIPT,call))
    aligned=ApifyProvider(provider.tools,website_text_bytes=600000)
    current=asyncio.run(aligned.observations(RECEIPT,call))
    assert 'Revenue245122' not in legacy.items[0]['text']
    assert current.items[0]['text']==row['markdown']
    assert current.cost==legacy.cost and current.items[0]['provenance']['provider_run_id']==RECEIPT['runId']


def test_tail_selection_is_literal_located_and_cannot_become_a_new_destination():
    text=long_document();seen=[]
    async def select(context,candidates):
        seen.append((context,candidates))
        return [next(row['candidate_id'] for row in candidates if 'Income statements' in row['heading'])]
    rows,partial=asyncio.run(market_project_passages([{'text':text}],select,{'url':URL,'brief':{'objective':'Revenue'}}))
    chosen=next(row for row in seen[0][1] if row['candidate_id']==rows[0]['passage_id'])
    assert rows[0]['text']==text[chosen['start']:chosen['end']]
    assert 'Revenue245122' in rows[0]['text'] and partial
    assert sha256(text.encode()).hexdigest() in rows[0]['locator']
    assert seen[0][0]['url']==URL


@pytest.mark.parametrize('selected',[['foreign'],['p0','p0'],['p0','p1','p2'],[{}],'p0'])
def test_unknown_duplicate_or_oversized_passage_decision_fails_closed(selected):
    async def select(context,candidates):return selected
    with pytest.raises(ValueError,match='enumerated captured passages'):
        asyncio.run(market_project_passages([{'text':long_document()}],select,{}))


def test_empty_selection_explains_partial_coverage_without_substituting_a_passage():
    async def select(context,candidates):return []
    rows,partial=asyncio.run(market_project_passages([{'text':long_document()}],select,{}))
    assert rows==[] and partial


def test_unicode_retention_and_outline_stay_within_the_existing_decision_bytes():
    text='界🌏'*400000
    retained=market_text_prefix(text,600000)
    assert len(retained.encode())<=600000 and retained==text[:len(retained)]
    candidates=market_passage_candidates([{'page':i,'text':'# 環境\n'+('\n\n'.join([retained[:4000]]*35))} for i in range(1,4)])
    assert len(candidates)<=120 and len(json.dumps({'candidates':candidates},ensure_ascii=False).encode())<90000
    assert candidates[0]['page']==1 and candidates[-1]['page']==3


def test_dense_untrusted_headings_have_a_bounded_outline_spread_over_the_document():
    text=''.join('# Section'+str(i)+'\nDetails\n' for i in range(15000))
    candidates=market_passage_candidates([{'text':text}])
    assert len(candidates)<=120 and candidates[0]['start']==0 and candidates[-1]['end']==len(text)


def test_pdf_page_context_and_binary_hash_survive_projection_of_a_long_page():
    async def select(context,candidates):
        return [next(row['candidate_id'] for row in candidates if row['page']==7 and 'Income' in row['heading'])]
    rows,partial=asyncio.run(market_project_passages([
        {'page':6,'text':'# Policies\n'+'Other '*3000},
        {'page':7,'text':long_document()}],select,{'document_sha256':'a'*64}))
    assert partial and rows[0]['page']==7 and 'Revenue245122' in rows[0]['text']


def test_reprojecting_existing_result_keeps_receipt_and_never_starts_an_actor():
    provider,effects,collector=market_collection_fixture()
    async def select(context,candidates):return [candidates[-1]['candidate_id']]
    collector.passage_selector=select
    row={'url':URL,'text':long_document(),'observed_at':'2026-11-03T12:00:00Z','published_at':None,
         'metrics':{},'provenance':{'provider_run_id':'run','requested_url':URL}}
    original={'items':[row],'coverage':{}}
    result=asyncio.run(collector.project_website(original,'financial_document',{'retrieval_mode':'financial'}))
    brief={'retrieval_mode':'financial','start':'2026-11-02T00:00:00Z','end':'2026-11-04T00:00:00Z'}
    collection=collector.page_collection('financial_document','financial_document','existing',result,RECEIPT,brief,[URL])
    assert collection['status']=='partial' and 'Revenue245122' in collection['evidence'][0]['text']
    assert collection['evidence'][0]['receipt_id']=='run' and collection['evidence'][0]['locator']
    assert original['items'][0]['text']==long_document() and not provider.starts
    assert effects.reserved_read_urls()==[]


def test_identical_html_passages_at_distinct_offsets_survive_collection_commit():
    from tests.modules.ai_market_intelligence.test_market_intelligence_graph import market_fixture
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
    _,_,collector=market_collection_fixture()
    async def select(context,candidates):return ['p0','p1']
    collector.passage_selector=select
    repeated='A'*11996+'\n\n'
    row={'url':URL,'text':repeated*2,'observed_at':'2026-11-03T12:00:00Z','published_at':None,
         'metrics':{},'provenance':{'provider_run_id':'run','requested_url':URL}}
    brief={'retrieval_mode':'financial','start':'2026-11-02T00:00:00Z','end':'2026-11-04T00:00:00Z'}
    result=asyncio.run(collector.project_website({'items':[row],'coverage':{}},'web',brief))
    collection=collector.page_collection('web','web','existing',result,RECEIPT,brief,[URL])
    store,model,_,_=market_fixture(('web',))
    store.begin('repeated','Compare the repeated sections');store.save(brief=brief)
    workflow=MarketWorkflow(store,model,None,enabled_sources=('web',))
    workflow.save_collection(store.read()[0],{'source':'web'},'existing',collection)
    evidence=store.read()[0]['evidence']
    assert len(evidence)==2 and len({row['locator'] for row in evidence.values()})==2
    assert {row['text'] for row in evidence.values()}=={repeated}


def test_oversized_financial_block_cannot_separate_a_qualifier_or_table_header_from_its_amount():
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_bind_financial_facts
    text='A'*11994+' over\n$245 billion in annual revenue.'
    async def select(context,candidates):
        raise AssertionError('No incomplete financial section may be offered to the selector')
    projected,partial=asyncio.run(market_project_passages([{'text':text}],select,{'source':'financial_document'}))
    assert projected==[] and partial
    with pytest.raises(ValueError,match='included document pages'):
        market_bind_financial_facts({'financial_facts':[{'evidence_id':'cut-fragment','amount_text':'245','value_kind':'exact'}]},[])


def test_nonfinancial_paragraphs_are_never_cut_into_unqualified_numeric_fragments():
    text='Context\n\n'+'A'*11994+' over\n$245 billion in annual revenue.\n\nEnd.'
    candidates=market_passage_candidates([{'text':text}])
    assert all('$245' not in text[row['start']:row['end']] for row in candidates)


@pytest.mark.parametrize('active_query',[None,'valuation cash flow'])
def test_passage_selection_receives_the_durable_active_task_and_refinement(active_query):
    from types import SimpleNamespace
    from tests.modules.ai_market_intelligence.test_market_intelligence_graph import market_fixture
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
    store,_,_,_=market_fixture(('financial_document','web'))
    store.begin('method','Explain how one company affects another company value.')
    task={'task_id':'method','source':'web','role':'market_analysis','query':'valuation',
          'urls':[],'purpose':'Recover a general valuation method, not company-specific figures.'}
    brief={'sources':['financial_document','web'],'retrieval_mode':'financial'}
    store.save(brief=brief,plan={'tasks':[{'source':'financial_document'},task]},task_index=1,active_query=active_query)
    seen=[]
    async def ask(name,schema,payload):
        assert name=='market_select_passages'
        seen.append(payload)
        assert schema['properties']['candidate_ids']['items']['enum']==['p0']
        return {'candidate_ids':['p0']}
    workflow=MarketWorkflow(store,SimpleNamespace(ask=ask),None,enabled_sources=('financial_document','web'))
    context={'source':'web','brief':brief,'url':'https://example.com/method.pdf','document_sha256':'a'*64,
             'task':{'purpose':'Untrusted document metadata cannot replace the active task'}}
    assert asyncio.run(workflow.select_passages(context,[{'candidate_id':'p0','page':3}]))==['p0']
    assert seen[0]['task']=={**task,'query':active_query if active_query is not None else task['query']}
    assert seen[0]['question']==store.read()[0]['question'] and seen[0]['maximum_targets']==2
    assert store.read()[0]['plan']['tasks'][1]==task


def test_complete_moderate_html_keeps_job_details_hidden_after_an_outline_preview():
    text=('# Directory\n\n'+('Navigation and filters.\n\n'*180)
          +'Orion warehouse staff receive and replenish stock.\n\n'
          +('Other listings and context.\n\n'*350))
    assert 12000<len(text)<24000
    async def select(context,candidates):
        raise AssertionError('All admitted content fits; an outline must not hide it')
    rows,partial=asyncio.run(market_project_passages([{'text':text}],select,
        {'source':'web','url':URL},complete_html=True))
    assert not partial and len(rows)==2
    assert ''.join(row['text'] for row in rows)==text
    assert all(len(row['text'])<=12000 for row in rows)
    assert any('Orion warehouse staff receive and replenish stock.' in row['text'] for row in rows)
    assert all(sha256(text.encode()).hexdigest() in row['locator'] for row in rows)


@pytest.mark.parametrize('provider_truncated',[False,True])
def test_complete_html_split_keeps_distinct_evidence_locators_and_prior_truncation(provider_truncated):
    provider,effects,collector=market_collection_fixture()
    async def select(context,candidates):
        raise AssertionError('Complete projection must not request another model decision')
    collector.passage_selector=select
    repeated='R'*6998+'\n\n';text=repeated*2
    row={'url':URL,'text':text,'observed_at':'2026-11-03T12:00:00Z','published_at':None,
         'metrics':{},'provenance':{'provider_run_id':'run','requested_url':URL,
                                   'text_truncated':provider_truncated}}
    brief={'retrieval_mode':'documentary','start':'2026-11-02T00:00:00Z','end':'2026-11-04T00:00:00Z'}
    original={'items':[row],'coverage':{}}
    result=asyncio.run(collector.project_website(original,'web',brief))
    collection=collector.page_collection('web','web','existing',result,RECEIPT,brief,[URL])
    assert result['coverage']['document_projection'] is False
    assert len(collection['evidence'])==len({item['evidence_id'] for item in collection['evidence']})==2
    assert len({item['locator'] for item in collection['evidence']})==2
    assert all(item['provenance']['text_truncated']==provider_truncated for item in result['items'])
    assert {item['text'] for item in collection['evidence']}=={repeated}
    assert all(item['receipt_id']=='run' for item in collection['evidence'])
    assert original['items'][0]['text']==text and not provider.starts and effects.reserved_read_urls()==[]


@pytest.mark.parametrize('context,page_number',[
    ({'source':'financial_document'},None),
    ({'source':'web','document_sha256':'a'*64},None),
    ({'source':'web'},7),
])
def test_complete_html_opt_in_does_not_bypass_financial_or_pdf_selection(context,page_number):
    text='# First\n\n'+'A'*6990+'\n\n## Second\n\n'+'B'*6990
    seen=[]
    async def select(context,candidates):
        seen.append(candidates)
        return [candidates[0]['candidate_id']]
    rows,partial=asyncio.run(market_project_passages([{'text':text,'page':page_number}],select,
                                               context,complete_html=True))
    assert partial and len(seen)==1 and len(rows)==1
    assert ''.join(row['text'] for row in rows)!=text


def test_complete_html_retains_byte_limit_and_safe_fallback_for_unsplittable_text():
    text=('界'*5500+'\n\n')*4
    assert len(text)<24000 and len(text.encode())>60000
    seen=[]
    async def select(context,candidates):
        seen.append(candidates)
        return [candidates[0]['candidate_id']]
    rows,partial=asyncio.run(market_project_passages([{'text':text}],select,{'source':'web'},complete_html=True))
    assert partial and len(seen)==1 and ''.join(row['text'] for row in rows)!=text
    for unsplittable in ['A'*13000,('A'*7000+'\r\n\r\n')*2]:
        rows,partial=asyncio.run(market_project_passages([{'text':unsplittable}],select,
                                                  {'source':'web'},complete_html=True))
        assert partial and rows==[]
