"""Selected search-result pages retain partial coverage without weakening URL guards."""
import asyncio
import copy

import pytest

from test_market_intelligence_payload import provider_for


URLS = ['https://example.com/good', 'https://example.com/missing', 'https://example.com/empty']
CALL = {'actor': 'apify/website-content-crawler', 'input': {'startUrls': [{'url': url} for url in URLS]},
        'callOptions': {'maxItems': 3}}
RECEIPT = {'runId': 'existing-reader', 'status': 'SUCCEEDED', 'datasetId': 'existing-dataset', 'spent_microusd': 700}


def page(url=URLS[0], status=200, text='Contenido recuperado'):
    return {'url': url, 'markdown': text, 'crawl': {'depth': 0, 'httpStatusCode': status, 'loadedUrl': url}}


def normalize(rows, *, partial=True, call=None, receipt=None):
    client, tool = provider_for(rows)
    result = asyncio.run(client.observations(receipt or RECEIPT, call or CALL, allow_partial_pages=partial))
    return result, tool


def test_valid_page_survives_verified_404_and_empty_page_with_real_statuses():
    rows = [page(), page(URLS[1], 404, None), page(URLS[2], 202, '')]
    original = copy.deepcopy(rows)
    result, tool = normalize(rows)
    assert len(result.items) == 1 and result.items[0]['url'] == URLS[0]
    assert result.items[0]['text'] == rows[0]['markdown']
    assert result.items[0]['provenance']['scope'] == 'requested_pages'
    assert result.cost == 700
    assert result.coverage == {'version': 1, 'total_rows': 3, 'accepted_rows': 1,
        'failed_pages': [{'url': URLS[1], 'reason': 'http_error', 'http_status': 404},
                         {'url': URLS[2], 'reason': 'empty_content', 'http_status': 202}]}
    assert rows == original
    assert tool.calls == [{'datasetId':'existing-dataset','offset':0,'limit':50,'clean':True,
            'fields':'url,crawl,markdown,text,fullText,metadata,createdAt,publishedAt,error,isError'}]


def test_zero_rows_do_not_invent_http_failures_for_absent_requested_pages():
    result, _ = normalize([])
    assert result.items == [] and result.cost == 700
    assert result.coverage == {'version': 1, 'total_rows': 0, 'accepted_rows': 0, 'failed_pages': []}


def test_failed_page_without_loaded_url_is_allowed_but_missing_page_is_not_inferred():
    row = page(URLS[1], 500, None)
    del row['crawl']['loadedUrl']
    result, _ = normalize([row])
    assert result.items == []
    assert result.coverage['total_rows'] == 1
    assert result.coverage['failed_pages'] == [{'url': URLS[1], 'reason': 'http_error', 'http_status': 500}]


def test_empty_2xx_without_loaded_url_is_failure_but_valid_content_requires_it():
    row = page(text='')
    del row['crawl']['loadedUrl']
    result, _ = normalize([row])
    assert result.items == [] and result.coverage['failed_pages'][0]['reason'] == 'empty_content'
    row['markdown'] = 'Ahora hay contenido'
    with pytest.raises(ValueError):
        normalize([row])


def test_default_registered_and_pointed_reader_stays_strict_for_mixed_batch():
    client, _ = provider_for([page(), page(URLS[1], 404, None)])
    with pytest.raises(ValueError):
        asyncio.run(client.observations(RECEIPT, CALL))
    with pytest.raises(ValueError):
        normalize([page(), page(URLS[1], 404, None)], partial=False)


def test_same_origin_redirect_uses_existing_normalizer_and_original_request_audit():
    row = page()
    row['crawl']['loadedUrl'] = 'https://example.com/final?utm_source=redirect'
    result, _ = normalize([row])
    item, = result.items
    assert item['url'] == 'https://example.com/final'
    assert item['provenance']['requested_url'] == URLS[0]
    assert item['provenance']['redirected'] is True
    assert item['provenance']['provider_run_id'] == RECEIPT['runId']


@pytest.mark.parametrize('status', [None, True, '404', 0, 199, 301, 600])
def test_unverified_http_status_remains_whole_batch_failure(status):
    with pytest.raises(ValueError):
        normalize([page(), page(URLS[1], status, '')])


@pytest.mark.parametrize('crawl', [None, {}, {'depth': True, 'httpStatusCode': 404},
    {'depth': 1, 'httpStatusCode': 404}, {'depth': 0, 'httpStatusCode': 404, 'error': ''}])
def test_invalid_crawl_shape_cannot_be_softened_into_partial_success(crawl):
    broken = page(URLS[1], 404, None)
    broken['crawl'] = crawl
    with pytest.raises(ValueError):
        normalize([page(), broken])


@pytest.mark.parametrize('field,value', [('error', ''), ('error', False), ('error', {}),
    ('isError', True), ('isError', 'false'), ('markdown', []), ('markdown', False)])
def test_ambiguous_error_and_text_types_remain_whole_batch_failure(field, value):
    broken = page(URLS[1], 404, None)
    broken[field] = value
    with pytest.raises(ValueError):
        normalize([page(), broken])


@pytest.mark.parametrize('destination', ['https://127.0.0.1/', 'https://localhost/',
    'https://user:secret@example.com/', 'http://example.com/', None])
@pytest.mark.parametrize('status', [200, 404])
def test_private_or_invalid_loaded_url_always_rejects_batch(destination, status):
    broken = page(URLS[1], status, '' if status == 404 else 'Contenido')
    broken['crawl']['loadedUrl'] = destination
    with pytest.raises(ValueError):
        normalize([page(), broken])


@pytest.mark.parametrize('status',[200,404])
def test_other_public_origin_is_excluded_without_its_text_or_new_authority(status):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import validate_retrieval_coverage
    redirected=page(URLS[1],status,'UNAUTHORIZED_DESTINATION_TEXT')
    redirected['crawl']['loadedUrl']='https://other.example.com/page'
    result,tool=normalize([page(),redirected])
    assert [row['url'] for row in result.items]==[URLS[0]]
    assert 'UNAUTHORIZED_DESTINATION_TEXT' not in str(result.items)
    assert result.coverage['failed_pages']==[{'url':URLS[1],'http_status':status,
        'reason':'origin_changed' if status==200 else 'http_error'}]
    validate_retrieval_coverage(result.coverage)
    assert len(tool.calls)==1 and result.cost==RECEIPT['spent_microusd']
    all_excluded,_=normalize([redirected])
    assert all_excluded.items==[] and all_excluded.coverage['accepted_rows']==0


@pytest.mark.parametrize('url', ['https://127.0.0.1/', 'https://example.internal/',
    'https://user:secret@example.com/', 'https://example.com/unrequested'])
def test_queued_url_must_be_public_and_selected_even_on_404(url):
    with pytest.raises(ValueError):
        normalize([page(), page(url, 404, None)])


def test_duplicate_and_excess_rows_include_failures_and_cannot_pass_as_partial():
    with pytest.raises(ValueError, match='duplicate'):
        normalize([page(), page(URLS[0], 404, None)])
    with pytest.raises(ValueError, match='row limit'):
        normalize([page(), page(URLS[1], 404, None), page(URLS[2], 500, None), page(URLS[2], 500, None)])


@pytest.mark.parametrize('flag', [None, 0, 1, 'true'])
def test_partial_mode_requires_an_explicit_runtime_boolean(flag):
    with pytest.raises(ValueError, match='runtime'):
        normalize([], partial=flag)


def test_partial_flag_does_not_reinterpret_other_actors_or_unknown_cost():
    row = {'url': 'https://x.com/fixture/status/1', 'text': 'Opinión', 'createdAt': '2026-10-30T12:00:00Z'}
    call = {'actor': 'apidojo/twitter-scraper-lite', 'callOptions': {'maxItems': 3}}
    receipt = {**RECEIPT, 'spent_microusd': None}
    result, _ = normalize([row], call=call, receipt=receipt)
    assert len(result.items) == 1 and result.coverage is None and result.cost is None
