"""Explicit page reads do not enroll destinations or delegate URL choice to a model."""
import json
from datetime import datetime, timezone

import pytest

pytest.importorskip('langgraph', reason='Graph execution is checked in the pinned runtime CI job')

from test_market_intelligence_workflow import configured, turn


def pointed_flow():
    flow, model, provider, graph = configured()
    flow.public_discovery = True
    def clear(records, state):
        for name in ('ai_mi_sources', 'ai_mi_source_targets', 'ai_mi_topic_sources'):
            records.tables[name] = {}
    flow.journal.transact(clear)
    async def observations(receipt, call):
        return [{'url': item['url'], 'text': 'Contenido público sintético ' + str(index),
                 'published_at': None, 'observed_at': datetime.now(timezone.utc).isoformat(),
                 'metrics': {}, 'provenance': {'source': call['actor'], 'synthetic': True}}
                for index, item in enumerate(call['input']['startUrls'])], 1000
    provider.observations = observations
    return flow, model, provider, graph


@pytest.mark.parametrize('urls', [['https://example.com/one-off'],
                                 ['https://example.com/first', 'https://other.example.org/second']])
def test_a_first_pointed_read_uses_exact_user_pages_without_registering_them(urls):
    flow, model, provider, graph = pointed_flow()
    result = turn(graph, 'pointed-page-list', 'Investiga estas páginas: ' + ' '.join(urls))
    assert result['response']['report_id'] and len(provider.starts) == 1
    call, = provider.starts
    assert call['actor'] == 'apify/website-content-crawler'
    assert {item['url'] for item in call['input']['startUrls']} == set(urls)
    assert call['input']['maxCrawlDepth'] == 0 and call['input']['maxCrawlPages'] == len(urls)
    assert 'plan_research' not in [name for name, _ in model.calls]
    records = flow.records()
    source, = records.rows('ai_mi_sources')
    assert source['connector_revision'] == 'website'
    assert json.loads(source['allowed_scope_json'])['allow_discovery'] is False
    assert not records.rows('ai_mi_source_targets') and not records.rows('ai_mi_topic_sources')
    assert len(flow.research.evidence(result['run_id'])) == len(urls)
    assert not records.rows('ai_mi_schedules') and not records.rows('ai_mi_source_candidates')


def test_a_disabled_reader_is_not_replaced_by_discovery_or_reactivated():
    flow, model, provider, graph = pointed_flow()
    key = flow.catalog.ensure_website_reader()
    flow.journal.transact(lambda records, state: records.upsert('ai_mi_sources', {
        **records.get('ai_mi_sources', key), 'status': 'inactive'}))
    result = turn(graph, 'pointed-disabled', 'Investiga https://example.com/one-off')
    assert 'lector activo' in result['response']['markdown'] and not provider.starts
    assert flow.records().get('ai_mi_sources', key)['status'] == 'inactive'
    assert len(flow.records().rows('ai_mi_sources')) == 1
    assert not flow.records().rows('ai_mi_research_runs')


@pytest.mark.parametrize('invalid_output', [False, True], ids=['empty-page', 'unverified-content'])
def test_unreadable_pointed_page_explains_the_limit_and_next_step_without_guessing_the_cause(invalid_output):
    flow, _, provider, graph = pointed_flow()
    async def unreadable(receipt, call):
        if invalid_output:
            raise ValueError('untrusted private response')
        return [], None
    provider.observations = unreadable
    result = turn(graph, 'pointed-unreadable-' + str(invalid_output), 'Lee https://example.com/missing')
    text = result['response']['markdown']
    assert 'páginas indicadas' in text and 'Comprueba el enlace' in text
    assert 'No puedo confirmar si la causa' in text
    assert all(word not in text for word in ('proveedor', 'untrusted', 'problema no exista'))
    assert len(provider.starts) == 1
    assert not flow.records().rows('ai_mi_documents')
    assert not flow.records().rows('ai_mi_source_targets')
    assert len(flow.records().rows('ai_mi_reports')) == 1


def test_empty_strict_reader_preserves_unreturned_coverage_through_graph_and_replay():
    from test_market_intelligence_payload import provider_for
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow_actions import provider_limitations
    flow, _, provider, graph = pointed_flow()
    actual_parser, _ = provider_for([])
    async def empty_dataset(receipt, call):
        return await actual_parser.observations({**receipt, 'datasetId': 'dataset',
                                                'spent_microusd': 1000}, call)
    provider.observations = empty_dataset
    result = turn(graph, 'pointed-empty-dataset', 'Lee https://example.com/missing')
    assert len(provider.starts) == 1 and not flow.research.evidence(result['run_id'])
    context, = flow.retrieval_context(result['run_id'])
    assert context['requested_pages'] == context['unreturned_pages'] == 1
    assert context['total_rows'] == context['accepted_rows'] == 0
    assert context['unreturned_urls'] == ['https://example.com/missing']
    assert context['failed_reasons'] == {}
    notices = '\n'.join(provider_limitations(flow.records(), result['run_id']))
    assert '1 de las páginas solicitadas' in notices
    assert 'No pude comprobar su contenido ni la causa' in notices
    assert all(word not in notices.lower() for word in ('403', 'http', 'robots', 'captcha'))
    before = flow.journal.read()[0]
    replay = turn(graph, 'pointed-empty-dataset', 'Lee https://example.com/missing')
    assert replay['response'] == result['response'] and len(provider.starts) == 1
    assert flow.journal.read()[0] == before


@pytest.mark.parametrize('url', ['http://example.com/page', 'https://localhost/page',
                                'https://127.0.0.1/page', 'https://u:p@example.com/page'])
def test_invalid_pointed_destinations_do_not_materialize_capabilities_or_capture(url):
    flow, model, provider, graph = pointed_flow()
    result = turn(graph, 'invalid-pointed', 'Investiga ' + url)
    assert 'HTTPS públicas' in result['response']['markdown'] and not provider.starts
    assert not flow.records().rows('ai_mi_sources')
    assert not flow.records().rows('ai_mi_research_runs')


def test_unverified_crawl_cost_returns_an_actionable_result_without_a_paid_start():
    flow, model, provider, graph = pointed_flow()
    flow.usage_verified = False
    result = turn(graph, 'pointed-cost-unverified', 'Investiga https://example.com/one-off')
    assert 'límite de coste verificado' in result['response']['markdown']
    assert result['response']['report_id'] and not provider.starts
    assert not flow.journal.read()[0]['operations']


def test_pointed_compilation_cannot_drop_or_add_a_page_even_with_a_forged_planning_view():
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import compile_call, resolve_window
    window = resolve_window('last_7_days', datetime.now(timezone.utc), 'America/Bogota')
    urls = ['https://example.com/first', 'https://other.example.org/second']
    args = {'query': '', 'urls': urls[:1], 'max_items': 2, 'sort': 'Latest'}
    with pytest.raises(ValueError, match='exactly'):
        compile_call('website', args, window, 50000, approved_urls=urls,
                     usage_cost_verified=True, require_all_urls=True)
    with pytest.raises(ValueError, match='exceed'):
        compile_call('website', {**args, 'urls': [*urls, 'https://unrequested.example.org/']},
                     window, 50000, approved_urls=urls, usage_cost_verified=True, require_all_urls=True)


@pytest.mark.parametrize('phase', ['admission', 'reservation', 'start'])
def test_late_pointed_reader_revocation_returns_a_review_report_instead_of_a_native_error(phase):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import SourceScopeError
    flow, _, provider, graph = pointed_flow()
    def revoke():
        def change(records, state):
            source, = records.rows('ai_mi_sources')
            records.upsert('ai_mi_sources', {**source, 'status': 'inactive'})
        flow.journal.transact(change)
    if phase == 'admission':
        original = flow.plan_pointed_pages
        def changed_plan(*args):
            result = original(*args)
            revoke()
            return result
        flow.plan_pointed_pages = changed_plan
    else:
        method = 'reserve' if phase == 'reservation' else 'claim_start'
        original = getattr(flow.paid, method)
        def changed_authority(*args, **kwargs):
            revoke()
            return original(*args, **kwargs)
        setattr(flow.paid, method, changed_authority)
    result = turn(graph, 'pointed-revoked-' + phase, 'Investiga https://example.com/one-off')
    assert result['response']['report_id'] and result['reason'] == 'unresolved'
    assert SourceScopeError.user_message in result['response']['markdown']
    assert not provider.starts and not flow.research.evidence(result['run_id'])
    operations = list(flow.journal.read()[0]['operations'].values())
    if phase == 'start':
        operation, = operations
        assert operation['state'] == 'reserved' and operation['spent_microusd'] is None
        assert 'permit' not in operation
    else:
        assert operations == []
