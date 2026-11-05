"""One bounded ScraperLink SERP page; snippets never stand in for page reads."""
from datetime import datetime, timezone
from difflib import SequenceMatcher
import re
import unicodedata
from urllib.parse import urlsplit

from .runtime_core import ProviderResultUnavailable, public_url, digest


SERP_ACTOR = 'scraperlink/google-search-results-serp-scraper'
SERP_LANGUAGES = frozenset('af ar hy be bg ca zh-CN zh-TW hr cs da nl en eo et tl fi fr de el iw hi hu is id it ja ko lv lt no fa pl pt ro ru sr sk sl es sw sv th tr uk vi'.split())


def compile_serp_input(localized):
    if localized['maxResults'] < 10:
        raise ValueError('SERP search requires approval for one page of up to ten results')
    language = localized['languageCode']
    language = language if language in SERP_LANGUAGES else language.split('-')[0]
    if language not in SERP_LANGUAGES:
        raise ValueError('Search language is unsupported by the reviewed SERP build')
    result = {'keyword': localized['query'], 'limit': '10', 'include_merged': False,
              'page': 1, 'gl': localized['countryCode'], 'hl': language}
    if 'dateFrom' in localized:
        start, end = (datetime.strptime(localized[key], '%Y-%m-%d').strftime('%m/%d/%Y')
                      for key in ('dateFrom', 'dateTo'))
        result['tbs'] = f'cdr:1,cd_min:{start},cd_max:{end}'
    return result


def _serp_page(rows, call):
    if len(rows) != 1:
        raise ValueError('SERP search requires exactly one dataset page')
    page = rows[0]
    if (isinstance(page,dict) and set(page)=={'error','page_number','keyword'}
            and type(page['page_number']) is int and page['page_number']==1
            and page['keyword']==call['input']['keyword']
            and isinstance(page['error'],str) and 1<=len(page['error'])<=2000 and page['error'].strip()):
        raise ProviderResultUnavailable()
    if (not isinstance(page, dict)
            or type(page.get('page_number')) is not int or page['page_number'] != 1
            or any(key in page for key in ('error', 'isError', 'errorMessage'))):
        raise ValueError('SERP response must echo the single query and first page')
    serp_query(page,call['input']['keyword'],call['input'].get('tbs'))
    return page


def serp_query(page,requested,tbs=None):
    effective=page.get('search_term')
    date_filter=re.fullmatch(r'cdr:1,cd_min:(\d{2}/\d{2}/\d{4}),cd_max:(\d{2}/\d{2}/\d{4})',tbs or '')
    if date_filter:
        suffix=' after:'+datetime.strptime(date_filter[1],'%m/%d/%Y').strftime('%Y-%m-%d')
        if isinstance(effective,str) and effective.endswith(suffix):
            effective=effective[:-len(suffix)]
    if effective==requested:
        return effective
    related=page.get('related_keywords')
    suggestion=related.get('spelling_suggestion') if isinstance(related,dict) else None
    def normalize(value):
        return ''.join(char for char in unicodedata.normalize('NFKC',value).casefold() if not char.isspace())
    if (not isinstance(effective,str) or not 1<=len(effective)<=500 or not isinstance(suggestion,str)
            or normalize(suggestion)!=normalize(effective)
            or SequenceMatcher(None,normalize(requested),normalize(effective)).ratio()<0.94):
        raise ValueError('SERP response belongs to another query without a verified bounded spelling correction')
    return effective


def serp_destination(row):
    if any(key in row for key in ('error','isError','errorMessage')):
        raise ValueError('SERP result contains an error envelope')
    url=row.get('url')
    if isinstance(url,str) and urlsplit(url).scheme=='http':
        # Validation does not fetch or upgrade the provider's HTTP destination.
        public_url('https'+url[4:])
        return None
    return public_url(url)


def serp_results(page,call):
    results = page.get('results')
    if (call['input'].get('limit') != '10' or call['input'].get('include_merged') is not False
            or call['callOptions']['maxItems'] != 1 or not isinstance(results, list) or len(results) > 10):
        raise ValueError('SERP response exceeds its admitted one-page limit')
    positions = [row.get('position') for row in results if isinstance(row, dict)]
    if (len(positions) != len(results) or any(type(value) is not int for value in positions)
            or positions != list(range(1, len(results) + 1))):
        raise ValueError('SERP organic positions must be dense unique integers')
    return results


def serp_destinations(results):
    destinations,unusable={},[]
    for row in results:
        if any(key in row for key in ('error','isError','errorMessage')):
            raise ValueError('SERP result contains an error envelope')
        try:
            destinations[row['position']]=serp_destination(row)
        except ValueError:
            destinations[row['position']]=None
            unusable.append(row['position'])
    excluded=[position for position,url in destinations.items() if url is None and position not in unusable]
    return destinations,excluded,unusable


def serp_observations(rows, run_id, call):
    page = _serp_page(rows, call)
    results=serp_results(page,call)
    destinations,excluded,unusable=serp_destinations(results)
    base_query=serp_query(page,call['input']['keyword'],call['input'].get('tbs'))
    metadata = {'source': SERP_ACTOR, 'actor_build': call['callOptions']['build'], 'provider_run_id': run_id,
        'query': call['input']['keyword'], 'effective_query':page['search_term'],
        'query_correction':base_query!=call['input']['keyword'],'request_arguments_hash': digest(call),
        'provider_date_suffix':page['search_term'][len(base_query):] or None,
        'excluded_http_positions':excluded,
        'excluded_unusable_positions':unusable,
        'requested_search': {key: call['input'].get(key) for key in ('gl', 'hl', 'tbs')},
        'effective_search': None, 'filter_verification': 'not_reported_by_provider',
        'scope': 'bounded_search_results', 'location': None, 'content_basis': 'search_title_and_snippet',
        'temporal_basis': 'search_metadata', 'page_number': 1, 'maximum_search_pages': 1,
        'maximum_results': 10, 'returned_results_count': len(results)}
    now = datetime.now(timezone.utc).isoformat()
    return [_serp_observation(row, metadata, now) for row in results if destinations[row['position']] is not None]


def _serp_observation(row, metadata, observed_at):
    if any(key in row for key in ('error', 'isError', 'errorMessage')):
        raise ValueError('SERP result contains an error envelope')
    title, snippet, date = row.get('title'), row.get('description'), row.get('date')
    if not isinstance(title, str) or not title.strip() or len(title) > 1000:
        raise ValueError('SERP result needs a bounded title')
    if 'description' not in row or snippet is not None and (not isinstance(snippet, str) or len(snippet) > 12000):
        raise ValueError('SERP snippet must be bounded optional text')
    if date is not None and (not isinstance(date, str) or len(date) > 100 or any(ord(c) < 32 for c in date)):
        raise ValueError('SERP publication hint must be bounded optional text')
    text = title + ('\n\n' + snippet if snippet else '')
    return {'url': public_url(row.get('url')), 'text': text[:12000], 'published_at': None,
        'observed_at': observed_at, 'metrics': {}, 'provenance': {**metadata,
            'publication_date_raw': date, 'publication_date_precision': 'unknown',
            'position': row['position'], 'text_truncated': len(text) > 12000,
            'original_character_count': len(text)}}


def serp_receipt_matches(provenance, tool, purpose, requested, profile):
    call = purpose.get('search_call')
    if not isinstance(call, dict):
        return False
    expected_hash = digest({'source_key': tool['source_key'], 'target_id': tool['target_id'], 'arguments': call})
    localized = {'query': provenance.get('query'), 'maxResults': 10, **requested}
    if purpose.get('search_navigation') is True:
        localized = {key: value for key, value in localized.items() if key not in {'dateFrom', 'dateTo'}}
    expected_input = compile_serp_input(localized)
    expected = {'source': profile['actor'], 'actor_build': profile['build'],
        'provider_run_id': tool['provider_run_id'], 'content_basis': 'search_title_and_snippet',
        'request_arguments_hash': digest(call), 'requested_search': {key: expected_input.get(key) for key in ('gl', 'hl', 'tbs')},
        'effective_search': None, 'filter_verification': 'not_reported_by_provider', 'page_number': 1}
    return (expected_hash == tool['request_hash'] and call.get('actor') == profile['actor']
            and call.get('input') == expected_input and call.get('callOptions', {}).get('build') == profile['build']
            and all(key in provenance and provenance[key] == value for key, value in expected.items()))
