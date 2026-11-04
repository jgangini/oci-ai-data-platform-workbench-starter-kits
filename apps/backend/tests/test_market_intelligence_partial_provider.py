"""Ordinary verified page failures preserve other evidence, never unsafe rows."""
import asyncio
import copy

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import ObservationResult
from test_market_intelligence_payload import provider_for
from test_market_intelligence_web_search_provider import publisher_row, RECEIPT, ACTOR


def page(url, status=200, request_status='handled', markdown='Retrieved content'):
    row = publisher_row()
    row['metadata']['url'] = url
    row['crawl'].update(httpStatusCode=status, requestStatus=request_status)
    row['markdown'] = markdown
    return row


def read(rows, limit=3):
    provider, tool = provider_for(rows)
    result = asyncio.run(provider.observations(RECEIPT,
        {'actor': ACTOR, 'callOptions': {'maxItems': limit}}))
    assert len(tool.calls) == 1
    return result


def test_actual_mixed_200_202_and_500_shape_preserves_only_verified_nonempty_content():
    rows = [page('https://example.org/good'), page('https://example.org/empty', 202, markdown=''),
            page('https://example.org/timeout', 500, 'failed', None)]
    rows[2]['crawl']['errorMessage'] = 'Untrusted timeout message must not become evidence or a user error'
    rows[2].pop('markdown')
    before = copy.deepcopy(rows)
    result = read(rows)
    assert isinstance(result, ObservationResult)
    assert result.cost == RECEIPT['spent_microusd'] and len(result.items) == 1
    assert result.items[0]['url'] == 'https://example.org/good'
    assert result.items[0]['text'] == 'Retrieved content' and result.items[0]['published_at'] is None
    assert result.coverage == {'version': 1, 'total_rows': 3, 'accepted_rows': 1, 'failed_pages': [
        {'url': 'https://example.org/empty', 'reason': 'empty_content', 'http_status': 202},
        {'url': 'https://example.org/timeout', 'reason': 'http_error', 'http_status': 500}]}
    assert rows == before and 'Untrusted' not in str(result)
    with pytest.raises(TypeError):
        iter(result)


def test_all_verified_pages_failed_has_explicit_coverage_and_no_invented_observation():
    result = read([page('https://example.org/missing', 404),
                   page('https://example.org/forbidden', 403, 'failed'),
                   page('https://example.org/blank', 200, markdown=None)])
    assert result.items == [] and result.cost == RECEIPT['spent_microusd']
    assert result.coverage['total_rows'] == len(result.coverage['failed_pages']) == 3
    assert result.coverage['accepted_rows'] == 0
    assert [row['reason'] for row in result.coverage['failed_pages']] == ['http_error', 'http_error', 'empty_content']


@pytest.mark.parametrize('url', ['http://example.org/page', 'https://127.0.0.1/', 'https://localhost/',
                               'https://user:password@example.org/'])
@pytest.mark.parametrize('status', [200, 404, 500])
def test_unsafe_effective_url_invalidates_the_whole_batch_even_in_a_failed_page(url, status):
    with pytest.raises(ValueError):
        read([page('https://example.org/valid'), page(url, status, markdown=None)])


@pytest.mark.parametrize('replacement', [None, {}, {'canonicalUrl': 'https://example.org/unverified'}])
def test_failed_page_without_verified_effective_url_does_not_use_search_or_canonical_urls(replacement):
    failed = page('https://example.org/failed', 500, 'failed', None)
    failed['metadata'] = replacement
    with pytest.raises(ValueError):
        read([page('https://example.org/valid'), failed])


@pytest.mark.parametrize('status', [None, True, '500', 100, 301, 600])
def test_missing_malformed_and_unknown_http_statuses_are_not_ordinary_failures(status):
    with pytest.raises(ValueError):
        read([page('https://example.org/valid'), page('https://example.org/invalid', status, 'failed', None)])


@pytest.mark.parametrize('request_status', [None, True, [], {}, 'unknown'])
def test_malformed_request_status_is_not_softened_even_when_http_failed(request_status):
    with pytest.raises(ValueError):
        read([page('https://example.org/failed', 500, request_status, None)])


@pytest.mark.parametrize('markdown', [True, 12, {}, []])
def test_malformed_markdown_is_not_softened_even_when_http_failed(markdown):
    with pytest.raises(ValueError):
        read([page('https://example.org/failed', 500, 'failed', markdown)])


@pytest.mark.parametrize('change', [{'error': 'ambiguous failure'}, {'isError': True},
                                  {'crawl': {'httpStatusCode': 500, 'requestStatus': 'failed', 'error': 'ambiguous'}}])
def test_ambiguous_error_flags_remain_whole_batch_failures(change):
    with pytest.raises(ValueError):
        read([page('https://example.org/valid'), {**page('https://example.org/error', 500), **change}])


def test_failed_pages_still_count_toward_the_admitted_provider_row_limit():
    with pytest.raises(ValueError, match='admitted row limit'):
        read([page('https://example.org/good'), page('https://example.org/failed', 500, 'failed', None)], limit=1)


def test_zero_rows_is_empty_verified_coverage_not_a_fabricated_page_failure():
    result = read([])
    assert result.items == []
    assert result.coverage == {'version': 1, 'total_rows': 0, 'accepted_rows': 0, 'failed_pages': []}
