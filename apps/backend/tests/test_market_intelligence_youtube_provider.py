"""YouTube fixtures describe metadata, not watched video or collected comments."""
import asyncio
from urllib.parse import urlencode

import pytest

from test_market_intelligence_payload import provider_for


ACTOR = 'streamers/youtube-scraper'
VIDEO_ID = 'AbCdEfGh_12'
URL = 'https://www.youtube.com/watch?v=' + VIDEO_ID
RECEIPT = {'runId': 'synthetic-run', 'status': 'SUCCEEDED', 'datasetId': 'synthetic-dataset',
           'spent_microusd': 1234}


def video_row():
    return {'id': VIDEO_ID, 'url': URL, 'type': 'video', 'title': 'Tiendas D1 en Colombia',
            'text': 'Una descripción pública; no constituye una transcripción.',
            'date': '2026-10-31T16:10:00.000Z', 'channelName': 'Canal de ejemplo',
            'channelUrl': 'https://www.youtube.com/@example',
            'viewCount': 100, 'likes': 0, 'commentsCount': None}


def observations(rows, limit=3, actor=ACTOR):
    provider, tool = provider_for(rows)
    result = asyncio.run(provider.observations(RECEIPT,
        {'actor': actor, 'callOptions': {'maxItems': limit}}))
    assert result.coverage is None
    return (result.items, result.cost), tool


def empty_search(query='D1 Colombia'):
    call={'actor':ACTOR,'input':{'searchQueries':[query],'startUrls':[],
          'dateFilter':'week','sortingOrder':'date'},'callOptions':{'maxItems':3}}
    row={'url':'https://www.youtube.com/results?'+urlencode({'search_query':query,'sp':'CAISBAgDEAE='}),
         'input':query,'error':'NO_VIDEOS','note':'No videos found on the page.'}
    return call,row


def test_known_empty_search_is_not_video_evidence_and_preserves_accounted_cost():
    call,row=empty_search()
    provider,_=provider_for([row])
    result=asyncio.run(provider.observations(RECEIPT,call))
    assert result.items==[] and result.cost==1234 and result.coverage is None


@pytest.mark.parametrize('change',[
    {'input':'another query'}, {'error':'ZERO_TRIPS'}, {'error':'CAPTCHA'},
    {'note':'No videos'}, {'isError':True},
    {'url':'http://www.youtube.com/results?search_query=D1+Colombia&sp=CAISBAgDEAE%3D'},
    {'url':'https://youtube.com.evil.test/results?search_query=D1+Colombia&sp=CAISBAgDEAE%3D'},
    {'url':'https://www.youtube.com/watch?search_query=D1+Colombia&sp=CAISBAgDEAE%3D'},
    {'url':'https://www.youtube.com/results?search_query=other&sp=CAISBAgDEAE%3D'},
    {'url':'https://www.youtube.com/results?search_query=D1+Colombia&search_query=other&sp=CAISBAgDEAE%3D'},
    {'url':'https://www.youtube.com/results?search_query=D1+Colombia&sp=other'},
])
def test_empty_markers_cannot_hide_other_errors_or_escape_query_and_filter_authority(change):
    call,row=empty_search()
    provider,_=provider_for([{**row,**change}])
    with pytest.raises(ValueError):
        asyncio.run(provider.observations(RECEIPT,call))


def test_empty_search_marker_mixed_with_a_video_is_not_a_verified_empty_batch():
    call,row=empty_search()
    provider,_=provider_for([row,video_row()])
    with pytest.raises(ValueError):
        asyncio.run(provider.observations(RECEIPT,call))


def test_official_video_metadata_retains_identity_counts_attribution_and_content_limits():
    row = video_row()
    (items, cost), tool = observations([row])
    item, = items
    assert item['provenance']['source_title']==row['title']
    assert item['provenance']['source_description']==row['text']
    assert set(item) == {'url', 'text', 'published_at', 'observed_at', 'metrics', 'provenance'}
    assert item['url'] == URL and item['text'] == row['title'] + '\n\n' + row['text']
    assert item['published_at'] == '2026-10-31T16:10:00+00:00'
    assert item['metrics'] == {'viewCount': 100, 'likes': 0, 'commentsCount': None}
    provenance = item['provenance']
    assert provenance['source'] == ACTOR and provenance['provider_run_id'] == RECEIPT['runId']
    assert provenance['content_basis'] == 'title_and_description'
    assert provenance['video_content_analyzed'] is provenance['transcript_retrieved'] is provenance['comments_retrieved'] is False
    assert provenance['channel_name'] == row['channelName'] and provenance['channel_url'] == row['channelUrl']
    assert provenance['publication_date_raw'] == row['date'] and provenance['publication_date_precision'] == 'timestamp'
    assert provenance['location'] is None and cost == 1234
    assert tool.calls == [{'datasetId': 'synthetic-dataset', 'offset': 0, 'limit': 50, 'clean': True}]


@pytest.mark.parametrize('value, expected, precision', [
    ('2026-10-31T10:10:00-06:00', '2026-10-31T16:10:00+00:00', 'timestamp'),
    ('2026-10-31', None, 'day'), ('10 months ago', None, 'relative_or_unknown'),
    ('2026-10-31T10:10:00', None, 'datetime_without_timezone'), (None, None, 'unknown'),
])
def test_only_timezone_qualified_publication_dates_become_instants(value, expected, precision):
    row = {**video_row(), 'date': value, 'createdAt': '2020-01-01T00:00:00Z',
           'publishedAt': '2021-01-01T00:00:00Z', 'observedAt': '2022-01-01T00:00:00Z'}
    item = observations([row])[0][0][0]
    assert item['published_at'] == expected
    assert item['provenance']['publication_date_raw'] == value
    assert item['provenance']['publication_date_precision'] == precision


@pytest.mark.parametrize('value', [True, 0, [], '', '2026-02-30'])
def test_invalid_date_types_and_impossible_calendar_days_are_not_fabricated(value):
    with pytest.raises(ValueError):
        observations([{**video_row(), 'date': value}])


@pytest.mark.parametrize('url', ['https://youtube.com/watch?v=' + VIDEO_ID + '&t=10',
    'https://m.youtube.com/watch?v=' + VIDEO_ID + '&utm_source=feed'])
def test_known_watch_hosts_and_tracking_parameters_keep_one_canonical_video(url):
    assert observations([{**video_row(), 'url': url}])[0][0][0]['url'] == URL


@pytest.mark.parametrize('url', ['http://www.youtube.com/watch?v=' + VIDEO_ID,
    'https://user:secret@www.youtube.com/watch?v=' + VIDEO_ID,
    'https://127.0.0.1/watch?v=' + VIDEO_ID, 'https://10.0.0.1/watch?v=' + VIDEO_ID,
    'https://youtube.com.evil.example/watch?v=' + VIDEO_ID,
    'https://www.youtube.com/shorts/' + VIDEO_ID, 'https://youtu.be/' + VIDEO_ID,
    'https://www.youtube.com/watch?v=ZbCdEfGh_12', URL + '&v=' + VIDEO_ID,
    'https://www.youtube.com/watch', 'https://www.youtube.com/playlist?list=' + VIDEO_ID])
def test_untrusted_or_mismatched_video_urls_cannot_become_evidence(url):
    with pytest.raises(ValueError):
        observations([{**video_row(), 'url': url}])


@pytest.mark.parametrize('identity', [None, True, '', 'short', 'AbCdEfGh/12'])
def test_video_identity_is_required_and_cannot_be_coerced(identity):
    with pytest.raises(ValueError, match='video identity'):
        observations([{**video_row(), 'id': identity}])


@pytest.mark.parametrize('change', [{'type': 'shorts'}, {'type': 'stream'}, {'type': None},
    {'error': 'unavailable'}, {'isError': True}, {'title': None}, {'title': '  '}, {'text': True},
    {'channelName': False}, {'channelUrl': ['https://www.youtube.com/@example']}])
def test_failed_or_unsupported_video_metadata_is_rejected(change):
    with pytest.raises(ValueError):
        observations([{**video_row(), **change}])


@pytest.mark.parametrize('field', ['viewCount', 'likes', 'commentsCount'])
@pytest.mark.parametrize('value', [True, -1, '100', 1.0, float('inf'), float('nan')])
def test_counters_require_nonnegative_integers_or_unknown(field, value):
    with pytest.raises(ValueError, match='counters'):
        observations([{**video_row(), field: value}])


def test_unknown_optional_metadata_does_not_become_zero_or_an_asserted_channel():
    row = {'id': VIDEO_ID, 'url': URL, 'title': 'Solo título'}
    item = observations([row])[0][0][0]
    assert item['text'] == row['title'] and item['published_at'] is None
    assert item['metrics'] == {'viewCount': None, 'likes': None, 'commentsCount': None}
    assert item['provenance']['channel_name'] is item['provenance']['channel_url'] is None


@pytest.mark.parametrize('length', [12000, 12001])
def test_long_description_is_bounded_with_explicit_original_size(length):
    row = {**video_row(), 'title': 'T', 'text': 'á' * (length - 3)}
    item = observations([row])[0][0][0]
    assert len(item['text']) == min(length, 12000)
    assert item['provenance']['original_character_count'] == length
    assert item['provenance']['text_truncated'] is (length > 12000)


def test_provider_addons_are_not_silently_treated_as_video_or_comment_content():
    row = {**video_row(), 'subtitles': [{'srt': 'Extra subtitle content'}],
           'aiVideoSummary': 'Unapproved generated summary', 'comments': ['A comment'],
           'location': 'Colombia', 'channelLocation': 'Colombia'}
    item = observations([row])[0][0][0]
    assert item['text'] == row['title'] + '\n\n' + row['text']
    assert item['provenance']['location'] is None
    assert 'Extra subtitle content' not in item['text'] and 'Unapproved' not in item['text']


@pytest.mark.parametrize('limit', [0, True, 51])
def test_youtube_dataset_must_respect_the_admitted_positive_row_limit(limit):
    with pytest.raises(ValueError, match='admitted row limit'):
        observations([video_row()], limit)


def test_excess_video_rows_are_rejected_before_becoming_a_partial_dataset():
    with pytest.raises(ValueError, match='admitted row limit'):
        observations([video_row(), video_row()], limit=1)


def test_x_post_normalization_does_not_inherit_the_youtube_date_or_title_contract():
    row = {'url': 'https://x.com/example/status/1', 'text': 'Mensaje', 'createdAt': '2026-10-31T12:00:00Z'}
    item = observations([row], actor='apidojo/twitter-scraper-lite')[0][0][0]
    assert item['text'] == 'Mensaje' and item['published_at'] == '2026-10-31T12:00:00+00:00'
    assert item['provenance']['content_basis']=='post_text'
