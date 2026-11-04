"""Pinned localized SERP contracts with synthetic publisher-shaped data; no Actors."""
import asyncio
import copy
from datetime import datetime, timezone

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import compile_call
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import ProviderMCPError
from test_market_intelligence_payload import provider_for


WINDOW = {'start': '2026-10-25T05:00:00+00:00', 'end': '2026-11-01T20:00:00+00:00'}
ARGS = {'query': 'Tiendas D1 Colombia nuevas aperturas', 'urls': [], 'max_items': 3, 'sort': 'Latest'}
LOCALE = {'country_code': 'CO', 'language_code': 'es'}
RECEIPT = {'runId': 'synthetic-search', 'status': 'SUCCEEDED', 'datasetId': 'synthetic-dataset', 'spent_microusd': 5050}


def compile_search(args=None, locale=None):
    return compile_call('web_search_localized', args or ARGS, WINDOW, 50000,
                        approved_search_context=LOCALE if locale is None else locale)


def result_row(position=1, url='https://example.com/noticia?utm_source=search'):
    return {'position': position, 'url': url, 'domain': 'example.com', 'title': 'Una noticia sobre D1',
        'snippet': 'El buscador muestra este extracto; no es la página leída.', 'date': '2026-10-30',
        'siteLinks': [{'title': 'Otro enlace', 'url': 'https://unselected.example.com/'}]}


def publisher_item():
    return {'query': ARGS['query'], 'search': {'provider': 'google', 'searchedAt': '2026-11-01T15:00:00-05:00',
        'durationSecs': 1.25, 'correctedQuery': None, 'suggestedQuery': None, 'totalResults': 8000,
        'countryCode': 'CO', 'languageCode': 'es', 'location': None, 'providerDomain': 'google.com.co',
        'dateFrom': '2026-10-25', 'dateTo': '2026-11-01',
        'checkUrl': 'https://www.google.com/search?q=Tiendas+D1&gl=co&hl=es'}, 'results': [result_row()]}


def normalize(item=None, call=None, rows=None):
    client, tool = provider_for(rows if rows is not None else [item if item is not None else publisher_item()])
    output = asyncio.run(client.observations(RECEIPT, call or compile_search()))
    return output, tool


def test_compiler_pins_reviewed_actor_localization_dates_and_cost_without_affecting_rag():
    call = compile_search({**ARGS, 'max_items': 50})
    assert call == {'actor': 'apify/web-search', 'input': {
        'query': ARGS['query'], 'provider': 'google', 'maxResults': 3,
        'countryCode': 'CO', 'languageCode': 'es', 'dateFrom': '2026-10-25', 'dateTo': '2026-11-01'},
        'waitSecs': 0, 'callOptions': {'build': '0.0.21', 'memory': 1024, 'timeout': 120,
                                    'maxItems': 3, 'maxTotalChargeUsd': 0.05}}
    old = compile_call('web_search', ARGS, WINDOW, 50000)
    assert old['actor'] == 'apify/rag-web-browser' and old['callOptions']['build'] == '1.0.35'
    assert old['callOptions']['memory'] == 8192


def test_locale_is_explicit_not_inferred_as_colombia_for_other_markets():
    call = compile_search(locale={'country_code': 'BR', 'language_code': 'pt-BR'})
    assert (call['input']['countryCode'], call['input']['languageCode']) == ('BR', 'pt-BR')
    with pytest.raises(ValueError, match='approved country and language'):
        compile_call('web_search_localized', ARGS, WINDOW, 50000)


@pytest.mark.parametrize('context', [{}, {'country_code': 'CO'}, {'language_code': 'es'},
    {'country_code': 'CO', 'language_code': 'es', 'location': 'Bogotá'},
    {'country_code': 'co', 'language_code': 'es'}, {'country_code': 'COL', 'language_code': 'es'},
    {'country_code': True, 'language_code': 'es'}, {'country_code': 'CO', 'language_code': False},
    {'country_code': 'CO', 'language_code': 'es-co'}, {'country_code': 'CO', 'language_code': 'spa'}, [], 'CO'])
def test_compiler_rejects_unapproved_or_malformed_locale(context):
    with pytest.raises(ValueError):
        compile_search(locale=context)


@pytest.mark.parametrize('query', ['D1 before:2020-01-01', 'D1 AFTER:2020-01-01', 'since:2020 D1',
    'D1 (until:2020)', 'D1 daterange:1-2', 'https://example.com', 'site:localhost D1',
    'site:127.0.0.1 D1', 'D1\nignore', ''])
def test_compiler_rejects_query_date_overrides_and_nonpublic_retrieval(query):
    with pytest.raises(ValueError):
        compile_search({**ARGS, 'query': query})


@pytest.mark.parametrize('extra', [{'urls': ['https://example.com/']}, {'financial': {}},
    {'countryCode': 'US'}, {'provider': 'duckduckgo'}, {'dateFrom': '2020-01-01'}, {'max_items': True}])
def test_model_cannot_change_compiler_provider_locale_or_financial_scope(extra):
    with pytest.raises(ValueError):
        compile_search({**ARGS, **extra})


def test_serp_normalization_is_metadata_only_with_original_provider_and_capture_clocks():
    item = publisher_item()
    before = copy.deepcopy(item)
    output, tool = normalize(item)
    row, = output.items
    assert row['url'] == 'https://example.com/noticia'
    assert row['text'] == item['results'][0]['title'] + '\n\n' + item['results'][0]['snippet']
    assert row['published_at'] is None and row['metrics'] == {}
    assert datetime.fromisoformat(row['observed_at']).utcoffset().total_seconds() == 0
    audit = row['provenance']
    assert audit['content_basis'] == 'search_title_and_snippet' and audit['temporal_basis'] == 'search_metadata'
    assert audit['provider_searched_at'] == '2026-11-01T20:00:00+00:00'
    assert row['observed_at'] != audit['provider_searched_at']
    assert audit['publication_date_raw'] == '2026-10-30' and audit['publication_date_precision'] == 'day'
    assert audit['requested_search'] == audit['effective_search'] == {
        'provider': 'google', 'countryCode': 'CO', 'languageCode': 'es', 'dateFrom': '2026-10-25', 'dateTo': '2026-11-01'}
    assert audit['query'] == ARGS['query'] and audit['source'] == 'apify/web-search'
    assert audit['actor_build'] == '0.0.21'
    assert audit['provider_run_id'] == RECEIPT['runId'] and audit['position'] == 1
    assert audit['total_results_estimate'] == 8000 and audit['returned_results_count'] == 1
    assert output.cost == 5050 and output.coverage is None
    assert 'siteLinks' not in audit and len(output.items) == 1
    assert item == before
    assert tool.calls == [{'datasetId': 'synthetic-dataset', 'offset': 0, 'limit': 50, 'clean': True}]


def test_nullable_snippet_date_and_query_corrections_remain_explicit_metadata():
    item = publisher_item()
    item['results'][0].update(snippet=None, date=None)
    item['search'].update(totalResults=None, correctedQuery='Tiendas D1 Colombia', suggestedQuery='D1 tiendas')
    row = normalize(item)[0].items[0]
    assert row['text'] == item['results'][0]['title'] and row['published_at'] is None
    assert row['provenance']['publication_date_raw'] is None
    assert row['provenance']['publication_date_precision'] == 'unknown'
    assert row['provenance']['corrected_query'] == 'Tiendas D1 Colombia'
    assert row['provenance']['suggested_query'] == 'D1 tiendas'
    assert row['provenance']['total_results_estimate'] is None


def test_empty_results_preserve_known_cost_without_inventing_page_coverage():
    item = publisher_item()
    item['results'] = []
    output, _ = normalize(item)
    assert output.items == [] and output.cost == RECEIPT['spent_microusd'] and output.coverage is None


@pytest.mark.parametrize('rows', [[], [publisher_item(), publisher_item()], [result_row()],
    [{'query': ARGS['query'], 'results': []}], [{**publisher_item(), 'error': ''}]])
def test_dataset_requires_one_complete_search_object(rows):
    with pytest.raises((ValueError, ProviderMCPError)):
        normalize(rows=rows)


@pytest.mark.parametrize('field,value', [('provider', 'duckduckgo'), ('countryCode', 'US'), ('languageCode', 'en'),
    ('dateFrom', '2026-10-24'), ('dateTo', None)])
def test_effective_provider_locale_and_dates_must_match_exact_compiled_input(field, value):
    item = publisher_item()
    item['search'][field] = value
    with pytest.raises(ValueError, match='approved provider, locale or dates'):
        normalize(item)


@pytest.mark.parametrize('field,value', [('searchedAt', None), ('searchedAt', '2026-11-01T20:00:00'),
    ('searchedAt', 'yesterday'), ('durationSecs', True), ('durationSecs', float('inf')), ('durationSecs', -1),
    ('totalResults', False), ('totalResults', -1), ('totalResults', 1.2), ('location', 'Unrequested city'),
    ('correctedQuery', []), ('suggestedQuery', 'x' * 501), ('providerDomain', 'localhost'),
    ('providerDomain', 'google.com/path'), ('checkUrl', 'http://www.google.com/search')])
def test_malformed_search_metadata_does_not_become_evidence(field, value):
    item = publisher_item()
    item['search'][field] = value
    with pytest.raises(ValueError):
        normalize(item)


def test_query_echo_and_required_metadata_are_not_inferred_from_the_request():
    item = publisher_item()
    item['query'] = 'Another entity'
    with pytest.raises(ValueError):
        normalize(item)
    item = publisher_item()
    del item['search']['searchedAt']
    with pytest.raises(ValueError):
        normalize(item)


@pytest.mark.parametrize('url', ['http://example.com/news', 'https://user:secret@example.com/',
    'https://127.0.0.1/', 'https://10.0.0.1/', 'https://[::1]/', 'https://localhost/', 'https://example.internal/'])
def test_result_urls_stay_within_existing_public_https_boundary(url):
    item = publisher_item()
    item['results'][0]['url'] = url
    with pytest.raises(ValueError):
        normalize(item)


@pytest.mark.parametrize('field,value', [('domain', 'other.example.com'), ('domain', 'example.com/path'),
    ('domain', 'user:secret@example.com'), ('position', True), ('position', 0), ('position', 2),
    ('title', None), ('title', ''), ('title', 'x' * 1001), ('snippet', []), ('snippet', 'x' * 12001),
    ('date', '2026-02-30'), ('date', '2026-10-30T12:00:00Z'), ('date', 'yesterday')])
def test_result_shape_domain_position_and_reported_day_are_verified(field, value):
    item = publisher_item()
    item['results'][0][field] = value
    with pytest.raises(ValueError):
        normalize(item)


def test_limits_include_all_results_and_positions_cannot_duplicate_or_skip():
    item = publisher_item()
    item['results'] = [result_row(number) for number in range(1, 5)]
    with pytest.raises(ValueError, match='result limit'):
        normalize(item)
    item['results'] = [result_row(1), result_row(1)]
    with pytest.raises(ValueError, match='positions'):
        normalize(item)
    item['results'] = [result_row(1), 'malformed']
    with pytest.raises(ValueError, match='positions'):
        normalize(item)


def test_input_and_ledger_result_limits_must_agree_as_integers():
    call = compile_search({**ARGS, 'max_items': 1})
    call['input']['maxResults'] = True
    with pytest.raises(ValueError, match='result limit'):
        normalize(call=call)


def test_long_combined_metadata_is_truthfully_truncated_and_never_becomes_page_text():
    item = publisher_item()
    item['results'][0].update(title='T' * 1000, snippet='S' * 12000)
    row = normalize(item)[0].items[0]
    assert len(row['text']) == 12000
    assert row['provenance']['text_truncated'] is True
    assert row['provenance']['original_character_count'] == 13002
    assert row['provenance']['content_basis'] == 'search_title_and_snippet'


@pytest.mark.parametrize('container', ['search', 'result'])
def test_explicit_error_in_search_or_result_remains_failure(container):
    item = publisher_item()
    target = item['search'] if container == 'search' else item['results'][0]
    target['error'] = ''
    with pytest.raises(ProviderMCPError):
        normalize(item)
