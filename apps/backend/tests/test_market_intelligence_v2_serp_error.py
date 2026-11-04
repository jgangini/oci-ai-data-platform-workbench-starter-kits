"""The observed bound SERP error settles its paid run without inventing evidence."""
import asyncio
import copy

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.runtime_core import ConnectorUnavailable
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.serp import serp_observations
from test_market_intelligence_payload import provider_for
from test_market_intelligence_v2_collector import v2_collection_fixture,v2_collect


def error_page(call):
    return {'error':'Unexpected error: This operation was aborted','page_number':1,
            'keyword':call['input']['keyword']}


def search_call():
    _,_,collector=v2_collection_fixture()
    return collector.call('web_search_serp',{'start':'2026-10-27T12:00:00Z','end':'2026-11-02T12:00:00Z'},
                          query='Fixture brand reclamaciones')


def test_bound_serp_page_error_is_unavailability_and_not_a_claim_of_no_results():
    call=search_call()
    with pytest.raises(ConnectorUnavailable,match='buscador') as captured:
        serp_observations([error_page(call)],'known-run',call)
    assert 'Unexpected error' not in str(captured.value)
    assert getattr(captured.value,'code',None)=='serp_page_error'


@pytest.mark.parametrize('mutation',[
    {'keyword':'Another query'},{'page_number':2},{'page_number':True},{'error':''},
    {'error':None},{'error':'x'*2001},{'results':[]},{'isError':True}])
def test_malformed_foreign_or_mixed_error_pages_keep_strict_validation(mutation):
    call=search_call();row={**error_page(call),**mutation}
    with pytest.raises(ValueError):serp_observations([row],'known-run',call)


def test_error_cannot_hide_an_additional_dataset_page():
    call=search_call()
    with pytest.raises(ValueError):serp_observations([error_page(call),error_page(call)],'known-run',call)


def test_valid_empty_serp_remains_distinct_from_a_provider_error():
    call=search_call()
    assert serp_observations([{'page_number':1,'search_term':call['input']['keyword'],'results':[]}],'known-run',call)==[]


def test_error_collection_and_cached_recovery_have_identical_status_receipt_and_cost():
    provider,effects,collector=v2_collection_fixture()
    dataset_reads=[]
    async def normalize(receipt,call,**kwargs):
        normalizer,tool=provider_for({'datasetId':receipt['datasetId'],'items':[error_page(call)]})
        try:return await normalizer.observations(receipt,call,**kwargs)
        finally:dataset_reads.extend(tool.calls)
    async def selector(*args):raise AssertionError('A provider error cannot authorize a destination')
    provider.observations=normalize;collector.source_selector=selector
    first=v2_collect(collector,source='web');saved=copy.deepcopy(effects.snapshot.read()[0])
    assert first['status']=='unavailable' and first['evidence']==[]
    assert 'buscador' in first['limitations'][0]
    assert v2_collect(collector,source='web')==first and effects.snapshot.read()[0]==saved
    assert len(provider.starts)==len(provider.reads)==len(dataset_reads)==1
    assert effects.reserved_read_urls()==[]
    operation=next(iter(saved['operations'].values()))
    assert operation['state']=='completed' and operation['receipt']['status']=='SUCCEEDED'
    assert operation['receipt']['runId']=='receipt-1' and operation['receipt']['spent_microusd']==500
    assert operation['result']['items']==[] and operation['result']['coverage']['provider_result_error']=='serp_page_error'


@pytest.mark.parametrize('row',[{'page_number':1,'results':[]},
    {'page_number':1,'search_term':'Foreign query','results':[]}])
def test_invalid_terminal_dataset_closes_receipt_without_accepting_or_restarting_it(row):
    provider,effects,collector=v2_collection_fixture()
    dataset_reads=[]
    async def normalize(receipt,call,**kwargs):
        normalizer,tool=provider_for({'datasetId':receipt['datasetId'],'items':[row]})
        try:return await normalizer.observations(receipt,call,**kwargs)
        finally:dataset_reads.extend(tool.calls)
    provider.observations=normalize
    first=v2_collect(collector,source='web');saved=copy.deepcopy(effects.snapshot.read()[0])
    assert first['status']=='unavailable' and first['evidence']==[]
    assert 'incompatibles' in first['limitations'][0]
    operation=next(iter(saved['operations'].values()))
    assert operation['state']=='completed' and operation['receipt']['status']=='SUCCEEDED'
    assert operation['receipt']['spent_microusd']==500
    assert operation['result']['coverage']['provider_result_error']=='invalid_dataset'
    assert effects.reserved_read_urls()==[]
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_collector import V2Collector
    restored=V2Collector(provider,effects,search_context={'country_code':'CO','language_code':'es'},poll_attempts=1,poll_seconds=0)
    assert v2_collect(restored,source='web')==first and effects.snapshot.read()[0]==saved
    assert len(provider.starts)==len(provider.reads)==len(dataset_reads)==1
