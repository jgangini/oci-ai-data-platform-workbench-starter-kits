"""Returned pages must remain bound to the admitted pointed or registered read."""

import asyncio

import copy



import pytest



from tests.modules.ai_market_intelligence.test_market_intelligence_payload import provider_for





URL = 'https://example.org/article'

RECEIPT = {'runId': 'run', 'status': 'SUCCEEDED', 'datasetId': 'dataset', 'spent_microusd': 1000}





def page(url=URL):

    return {'url': url, 'crawl': {'loadedUrl': url, 'depth': 0}, 'markdown': 'Page content'}





def read(rows, urls=(URL,), limit=10):

    provider, _ = provider_for(rows)

    result = asyncio.run(provider.observations(RECEIPT, {'actor': 'apify/website-content-crawler',

        'input': {'startUrls': [{'url': url} for url in urls]}, 'callOptions': {'maxItems': limit}}))

    assert result.coverage == {'version': 1, 'total_rows': len(rows),

                               'accepted_rows': len(result.items), 'failed_pages': []}

    return result.items, result.cost





@pytest.mark.parametrize('loaded', [URL, URL + '/', 'https://example.org/canonical'])

def test_queued_identity_and_actual_same_origin_destination_are_retained(loaded):

    row = page()

    row['crawl']['loadedUrl'] = loaded

    row['metadata'] = {'canonicalUrl': 'https://unrequested.example.org/canonical'}

    original = copy.deepcopy(row)

    items, cost = read([row])

    item, = items

    assert item['url'] == loaded and item['text'] == row['markdown'] and cost == 1000

    assert item['provenance']['requested_url'] == URL

    assert item['provenance']['redirected'] is (loaded != URL)

    assert item['provenance']['scope'] == 'requested_pages'

    assert row == original





@pytest.mark.parametrize('loaded', [None, 'http://example.org/article', 'https://example.org.evil.com/',

    'https://www.example.org/article', 'https://127.0.0.1/', 'https://localhost/',

    'https://user:password@example.org/', 'https://example.internal/'])

def test_missing_private_or_cross_origin_destination_invalidates_the_dataset(loaded):

    row = page()

    row['crawl']['loadedUrl'] = loaded

    with pytest.raises(ValueError):

        read([row])





@pytest.mark.parametrize('queued', ['https://example.org/other', 'https://other.example.org/article',

    'https://127.0.0.1/article', 'http://example.org/article'])

def test_returned_queued_url_cannot_substitute_an_unrequested_page(queued):

    row = page(queued)

    row['crawl']['loadedUrl'] = URL

    with pytest.raises(ValueError):

        read([row])





@pytest.mark.parametrize('change', [{'crawl': None}, {'crawl': {}}, {'error': 'blocked'},

    {'isError': True}, {'crawl': {'loadedUrl': URL, 'depth': 1}},

    {'crawl': {'loadedUrl': URL, 'depth': False}},

    {'crawl': {'loadedUrl': URL, 'depth': 0, 'error': 'failed'}},

    {'crawl': {'loadedUrl': URL, 'depth': 0, 'httpStatusCode': 404}},

    {'crawl': {'loadedUrl': URL, 'depth': 0, 'httpStatusCode': '200'}},

    {'crawl': {'loadedUrl': URL, 'depth': 0, 'httpStatusCode': True}}])

def test_failed_or_unverifiable_crawl_is_not_accepted_as_evidence(change):

    with pytest.raises(ValueError):

        read([{**page(), **change}])





@pytest.mark.parametrize('limit', [0, True, 51, 1.5])

def test_invalid_row_limit_is_rejected(limit):

    with pytest.raises(ValueError, match='row limit'):

        read([page()], limit=limit)





def test_rows_beyond_limit_and_duplicate_requested_pages_are_rejected():

    second = 'https://example.org/second'

    with pytest.raises(ValueError, match='row limit'):

        read([page(), page(second)], urls=(URL, second), limit=1)

    with pytest.raises(ValueError, match='duplicate'):

        read([page(), page()], urls=(URL, second))

    with pytest.raises(ValueError, match='unrequested'):

        read([page(), page('https://example.org/other')], urls=(URL, second))





@pytest.mark.parametrize('length', [12000, 12001])

def test_truncation_is_disclosed_without_changing_original_text(length):

    row = page()

    row['markdown'] = 'á' * length

    items, _ = read([row])

    assert items[0]['text'] == row['markdown'][:12000]

    assert items[0]['provenance']['text_truncated'] is (length > 12000)

    assert items[0]['provenance']['original_character_count'] == length





def test_missing_requested_pages_remain_missing_instead_of_becoming_fake_observations():

    second = 'https://example.org/second'

    items, _ = read([page()], urls=(URL, second))

    assert len(items) == 1

    assert read([], urls=(URL, second))[0] == []
