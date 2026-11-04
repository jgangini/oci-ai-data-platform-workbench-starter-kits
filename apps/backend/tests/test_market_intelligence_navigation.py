"""Source navigation is independent from the publication dates of news."""
import asyncio
import copy
import json

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import compile_call
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import execute_selected, SourceScopeError
from test_market_intelligence_discovery_authority import discovery_case, read_grant, records
from test_market_intelligence_localized_search import ARGS, LOCALE, WINDOW, RECEIPT, normalize, publisher_item
from test_market_intelligence_payload import provider_for


NAVIGATION = {**ARGS, 'query': 'Tiendas D1 Colombia sitio oficial', 'search_mode': 'navigation'}


def navigation_call():
    return compile_call('web_search_localized', NAVIGATION, WINDOW, 50000, approved_search_context=LOCALE)


def test_source_navigation_does_not_filter_a_company_homepage_by_news_dates():
    call = navigation_call()
    assert call['input'] == {'query': NAVIGATION['query'], 'provider': 'google', 'maxResults': 3,
                             'countryCode': 'CO', 'languageCode': 'es'}
    assert call['callOptions']['build'] == '0.0.21'
    assert call['callOptions']['maxTotalChargeUsd'] == 0.05
    regular = compile_call('web_search_localized', ARGS, WINDOW, 50000, approved_search_context=LOCALE)
    assert regular['input']['dateFrom'] == '2026-10-25'
    assert regular['input']['dateTo'] == '2026-11-01'


def test_navigation_snippets_keep_unknown_publication_and_exact_effective_filters():
    item = publisher_item()
    item['query'] = NAVIGATION['query']
    item['search'].update(dateFrom=None, dateTo=None)
    item['results'][0]['date'] = '2025-01-01'
    output, _ = normalize(item, navigation_call())
    row, = output.items
    assert row['published_at'] is None
    assert row['provenance']['content_basis'] == 'search_title_and_snippet'
    assert row['provenance']['requested_search']['dateFrom'] is None
    assert row['provenance']['effective_search']['dateTo'] is None
    item['search']['dateFrom'] = '2026-10-25'
    with pytest.raises(ValueError, match='approved provider, locale or dates'):
        normalize(item, navigation_call())


def test_navigation_authorizes_only_its_own_organic_page_and_does_not_register_it():
    case = discovery_case()
    call = navigation_call()
    grant = case.paid.authorize(case.run, case.source, None)
    tool = case.paid.reserve(case.run, case.source, None, call, 50000, 'Localizar fuente primaria',
                             authorization_hash=grant['authorization_hash'])
    receipt = {**RECEIPT, 'runId': 'navigation-control'}

    async def start(arguments):
        assert arguments == call
        return copy.deepcopy(receipt)

    asyncio.run(execute_selected(case.paid, tool, call, start))
    item = publisher_item()
    item['query'] = NAVIGATION['query']
    item['search'].update(dateFrom=None, dateTo=None)
    item['results'][0].update(url='https://example.com/company', date=None)
    provider, _ = provider_for([item])
    output = asyncio.run(provider.observations(receipt, call))
    case.research.add_evidence(case.run, tool, output.items)
    assert read_grant(case, ['https://example.com/company'])['urls'] == ['https://example.com/company']
    assert records(case).rows('ai_mi_source_targets') == []
    with pytest.raises(SourceScopeError):
        read_grant(case, ['https://example.com/guessed'])
    row = records(case).get('ai_mi_tool_runs', tool)
    purpose = json.loads(row['step_purpose_json'])
    assert purpose.pop('search_navigation') is True
    case.journal.transact(lambda rows, state: rows.upsert('ai_mi_tool_runs', {
        **row, 'step_purpose_json': json.dumps(purpose)}))
    with pytest.raises(SourceScopeError):
        read_grant(case, ['https://example.com/company'])


@pytest.mark.parametrize('change', [{'query': 'D1 after:2020-01-01'}, {'max_items': True},
                                  {'search_mode': 'unbounded'}, {'urls': ['https://localhost/']}])
def test_navigation_preserves_query_locale_limit_and_url_guards(change):
    with pytest.raises(ValueError):
        compile_call('web_search_localized', {**NAVIGATION, **change}, WINDOW, 50000, approved_search_context=LOCALE)


@pytest.mark.parametrize('profile', ['x_search', 'website', 'youtube_search', 'world_bank'])
def test_navigation_mode_cannot_change_another_connector(profile):
    with pytest.raises(ValueError):
        compile_call(profile, NAVIGATION, WINDOW, 50000, approved_search_context=LOCALE)


def test_navigation_then_page_runs_through_the_graph_and_replays_without_capture(monkeypatch):
    from test_market_intelligence_localized_flow import localized_graph, URLS, PAGE, QUESTION, result_row
    from test_market_intelligence_workflow import turn
    flow, model, provider, _, delivered = localized_graph(monkeypatch)
    original_ask, original_observations = model.ask, provider.observations

    async def ask(name, schema, payload):
        decision = await original_ask(name, schema, payload)
        if name == 'plan_research' and decision.get('arguments', {}).get('query'):
            decision['arguments'].update(search_mode='navigation', query=NAVIGATION['query'])
        return decision

    async def observations(receipt, call, *, allow_partial_pages=False):
        if call['actor'] != 'scraperlink/google-search-results-serp-scraper':
            return await original_observations(receipt, call, allow_partial_pages=allow_partial_pages)
        from test_market_intelligence_scraperlink import publisher_item as serp_item, result_row as serp_row
        item = serp_item()
        item['search_term'] = call['input']['keyword']
        item['results'] = [serp_row(index, url) for index, url in enumerate(URLS, 1)]
        client, _ = provider_for([item])
        return await client.observations(receipt, call)

    model.ask, provider.observations = ask, observations
    from langgraph.checkpoint.memory import InMemorySaver
    graph = flow.build(InMemorySaver())
    question = QUESTION
    result = turn(graph, 'navigation-integration', question)
    assert len(provider.starts) == 2, result['response']
    assert 'tbs' not in provider.starts[0]['input']
    assert provider.starts[1]['input']['startUrls'] == [{'url': URLS[0]}]
    assert PAGE in result['response']['markdown']
    assert result['response']['references'] == [URLS[0]]
    assert delivered == [result['response']['report_id']]
    before, calls = flow.journal.read()[0], len(model.calls)
    replay = turn(graph, 'navigation-integration', question)
    assert replay['response'] == result['response']
    assert flow.journal.read()[0] == before
    assert len(provider.starts) == 2 and len(model.calls) == calls
