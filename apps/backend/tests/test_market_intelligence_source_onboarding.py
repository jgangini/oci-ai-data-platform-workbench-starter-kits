"""An unconfigured topic must not become a two-minute empty research report."""
import json
import asyncio

import pytest

pytest.importorskip('langgraph', reason='Graph execution is checked in the pinned runtime CI job')

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import digest
from test_market_intelligence_workflow import configured, turn, TOPIC, SCOPE


def test_missing_base_explains_setup_without_a_report_research_planner_or_capture():
    flow, model, provider, graph = configured()
    def disable(records, state):
        for source in records.rows('ai_mi_sources'):
            records.upsert('ai_mi_sources', {**source, 'status': 'inactive'})
    flow.journal.transact(disable)
    result = turn(graph, 'missing-base', 'que se dice de d1 en colombia')
    assert 'fuentes habilitadas' in result['response']['markdown']
    assert 'No he realizado búsquedas' in result['response']['markdown']
    assert '¿Qué fuente deseas usar?' in result['response']['markdown']
    assert [name for name, _ in model.calls] == ['understand']
    assert not provider.starts and not result['run_id']
    assert flow.records().rows('ai_mi_research_runs') == flow.records().rows('ai_mi_reports') == []


@pytest.mark.parametrize('answer', ['sí', 'no'])
def test_x_registration_is_presented_before_consent_and_does_not_capture(answer):
    flow, model, provider, graph = configured()
    model.intents = ['source_request']
    proposal = turn(graph, 'x-registration', 'Habilita búsquedas públicas en X para D1 en Colombia')
    assert proposal['pending']['kind'] == 'operator_x_search'
    assert 'solo para este tema' in proposal['response']['markdown']
    assert 'No se limitarán a una cuenta' in proposal['response']['markdown']
    assert len(flow.records().rows('ai_mi_sources')) == 2
    result = turn(graph, 'x-decision', answer)
    assert result['pending'] is None and not provider.starts
    assert not flow.records().rows('ai_mi_tool_runs')
    sources = flow.records().rows('ai_mi_sources')
    assert len(sources) == (3 if answer == 'sí' else 2)
    if answer == 'sí':
        row = flow.records().get('ai_mi_sources', result['response']['record_id'])
        policy = json.loads(row['allowed_scope_json'])
        assert policy['topic_id'] == SCOPE.key(digest(['topic', TOPIC]))
        assert policy['approval']['actor_id'] == SCOPE.owner_id
        assert policy['approval']['request_id'] == 'x-decision'


def test_topic_discovery_approval_is_idempotent_and_cannot_authorize_another_topic():
    flow, _, _, _ = configured()
    topic = SCOPE.key(digest(['topic', TOPIC]))
    key = flow.catalog.register_operator_x_search(topic, 'explicit-approval')
    before = flow.records().get('ai_mi_sources', key)
    assert flow.catalog.register_operator_x_search(topic, 'explicit-approval') == key
    assert flow.records().get('ai_mi_sources', key) == before
    other_question, other_topic = flow.research.record_question('other-topic', 'Otra empresa', {**TOPIC, 'entity': 'Other'})
    from test_market_intelligence_research import WINDOW
    run = flow.research.prepare_run(other_question, WINDOW, 'Another topic', 100000)
    assert key not in {r['source_key'] for r in flow.catalog.approved_base(flow.records(), other_topic)}
    with pytest.raises(ValueError, match='discovery approval'):
        flow.paid.authorize(run, key, None)
    with pytest.raises(ValueError, match='owned research topic'):
        flow.catalog.register_operator_x_search(SCOPE.key('missing-topic'), 'forged-approval')


def test_runtime_public_search_researches_a_new_topic_without_manual_source_registration():
    flow, model, provider, graph = configured()
    flow.public_discovery = True
    original = model.ask
    async def explicit_framing(name, schema, payload):
        result = await original(name, schema, payload)
        if name == 'understand':
            result['search_context'] = {'country_code': 'CO', 'language_code': 'es'}
        return result
    model.ask = explicit_framing
    def disable(records, state):
        for source in records.rows('ai_mi_sources'):
            records.upsert('ai_mi_sources', {**source, 'status': 'inactive'})
    flow.journal.transact(disable)
    result = turn(graph, 'open-research', 'que se dice de d1 en colombia')
    assert len(provider.starts) == 2
    assert provider.starts[0]['actor'] == 'apidojo/twitter-scraper-lite'
    assert provider.starts[0]['input']['sort'] == 'Latest'
    assert 'from:' not in provider.starts[0]['input']['searchTerms'][0]
    assert provider.starts[1]['actor'] == 'scraperlink/google-search-results-serp-scraper'
    assert provider.starts[1]['input']['gl'] == 'CO'
    assert provider.starts[1]['input']['hl'] == 'es'
    assert result['response']['report_id'] and flow.research.evidence(result['run_id'])
    source = next(row for row in flow.records().rows('ai_mi_sources') if row['status'] == 'active' and row['connector_revision'] == 'x_search')
    assert json.loads(source['allowed_scope_json'])['runtime_policy'] == 'public-x-search-v1'
    assert len(flow.records().rows('ai_mi_source_targets')) == 1


def test_discovery_topics_remain_available_for_canonical_reuse_without_registered_targets():
    flow, model, provider, _ = configured()
    def disable(records, state):
        for source in records.rows('ai_mi_sources'):
            records.upsert('ai_mi_sources', {**source, 'status': 'inactive'})
    flow.journal.transact(disable)
    flow.catalog.ensure_public_x_search()
    flow.research.record_question('known-topic', 'Consulta anterior', TOPIC)
    asyncio.run(flow.understand({'question': 'Otra pregunta similar de D1'}))
    topics = model.calls[-1][1]['recent_research_topics']
    assert len(topics) == 1
    assert {key: topics[0][key] for key in TOPIC} == TOPIC
    assert not provider.starts


def test_first_youtube_question_can_select_the_capability_without_visiting_other_sources():
    from langgraph.checkpoint.memory import InMemorySaver
    flow, model, provider, _ = configured()
    flow.public_discovery = True
    original = model.ask
    async def select_youtube(name, schema, payload):
        result = await original(name, schema, payload)
        if name == 'understand':
            choice = next(row for row in payload['sources'] if row['name'] == 'YouTube')
            assert flow.records().get('ai_mi_sources', choice['source_key']) is None
            result['selected_source_key'] = choice['source_key']
        if name == 'plan_research':
            assert {row['profile_id'] for row in payload['catalog']} == {'youtube_search'}
        return result
    model.ask = select_youtube
    result = turn(flow.build(InMemorySaver()), 'youtube-only', 'Busca solo en YouTube videos de D1 Colombia')
    assert result['response']['report_id']
    assert len(provider.starts) == 1 and provider.starts[0]['actor'] == 'streamers/youtube-scraper'
    assert len(flow.records().rows('ai_mi_source_targets')) == 1


def test_intake_public_options_are_read_only_and_do_not_restore_a_disabled_capability():
    flow, _, provider, _ = configured()
    before = flow.records().rows('ai_mi_sources')
    options = flow.catalog.public_search_options(flow.records())
    assert {row['name'] for row in options} == {'X', 'Búsqueda web', 'YouTube'}
    assert flow.records().rows('ai_mi_sources') == before and not provider.starts
    key = flow.catalog.ensure_public_youtube_search()
    flow.journal.transact(lambda records, state: records.upsert('ai_mi_sources', {
        **records.get('ai_mi_sources', key), 'status': 'inactive'}))
    assert key not in {row['source_key'] for row in flow.catalog.public_search_options(flow.records())}


@pytest.mark.parametrize('method', ['ensure_public_x_search', 'ensure_public_web_search', 'ensure_public_youtube_search'])
def test_public_capability_materialization_is_idempotent_and_respects_operator_disable(method):
    flow, _, provider, _ = configured()
    ensure = getattr(flow.catalog, method)
    key = ensure()
    saved = flow.records().get('ai_mi_sources', key)
    assert ensure() == key
    assert flow.records().get('ai_mi_sources', key) == saved
    flow.journal.transact(lambda records, state: records.upsert('ai_mi_sources', {**saved, 'status': 'inactive'}))
    assert ensure() == key
    assert flow.records().get('ai_mi_sources', key)['status'] == 'inactive'
    assert not provider.starts


def test_public_discovery_does_not_present_duplicate_topic_x_capabilities():
    flow, _, _, _ = configured()
    def disable_existing(records, state):
        for source in records.rows('ai_mi_sources'):
            records.upsert('ai_mi_sources', {**source, 'status': 'inactive'})
    flow.journal.transact(disable_existing)
    topic = SCOPE.key(digest(['topic', TOPIC]))
    approved = flow.catalog.register_operator_x_search(topic, 'explicit-approval')
    runtime = flow.catalog.ensure_public_x_search()
    base = flow.catalog.approved_base(flow.records(), topic)
    public = [row for row in base if row['profile_id'] == 'x_search' and not row['targets']]
    assert len(public) == 1 and public[0]['source_key'] == approved
    selected = flow.catalog_for_question({'intake': {'selected_source_key': runtime},
        'question': 'Investiga D1 en X'}, topic, None)
    assert len(selected) == 1 and selected[0]['source_key'] == approved
    flow.journal.transact(lambda records, state: records.upsert('ai_mi_sources', {
        **records.get('ai_mi_sources', approved), 'status': 'inactive'}))
    base = flow.catalog.approved_base(flow.records(), topic)
    public = [row for row in base if row['profile_id'] == 'x_search' and not row['targets']]
    assert len(public) == 1 and public[0]['source_key'] == runtime
    assert flow.records().get('ai_mi_sources', approved)['status'] == 'inactive'


@pytest.mark.parametrize('intent', ['list_sources', 'followup', 'read_report'])
def test_nonresearch_turns_never_materialize_public_search(intent):
    flow, model, provider, graph = configured()
    flow.public_discovery = True
    model.intents = [intent]
    before = flow.records().rows('ai_mi_sources')
    turn(graph, 'readonly-' + intent, 'Consulta el contexto anterior')
    assert flow.records().rows('ai_mi_sources') == before and not provider.starts


def test_web_discovery_can_follow_one_base_attempt_without_consuming_every_registered_page():
    from langgraph.checkpoint.memory import InMemorySaver
    flow, model, provider, _ = configured()
    flow.public_discovery = True
    topic = SCOPE.key(digest(['topic', TOPIC]))
    second = flow.catalog.register_operator_website(topic, 'https://example.com/less-relevant', 'approve-second')
    original = model.ask
    async def plan(name, schema, payload):
        if name != 'plan_research':
            result = await original(name, schema, payload)
            if name == 'understand':
                result['search_context'] = {'country_code': 'CO', 'language_code': 'es'}
            return result
        model.calls.append((name, payload))
        discovery = bool(payload['evidence'])
        if discovery:
            assert any(target['target_id'] == second for source in payload['catalog'] for target in source['targets'])
            source = next(row for row in payload['catalog'] if row['profile_id'] == 'web_search_serp')
        else:
            assert all(row['targets'] for row in payload['catalog'])
            source = next(row for row in payload['catalog'] if any(target['canonical_url_or_id'] == 'https://example.com/reference' for target in row['targets']))
        return {'next_action': 'execute', 'source_key': source['source_key'],
                'target_id': None if discovery else source['targets'][0]['target_id'],
                'arguments': {'query': 'D1 fabricante leche' if discovery else '',
                              'urls': [] if discovery else ['https://example.com/reference'], 'max_items': 10 if discovery else 3, 'sort': 'Latest'},
                'purpose': 'Contrastar una brecha del fixture', 'expected_contribution': 'Referencia primaria nueva', 'user_question': None}
    model.ask = plan
    result = turn(flow.build(InMemorySaver()), 'base-then-discovery')
    assert [call['actor'] for call in provider.starts] == ['apify/website-content-crawler', 'scraperlink/google-search-results-serp-scraper']
    assert result['response']['report_id']
    assert flow.records().get('ai_mi_source_targets', second)['status'] == 'active'
    assert len(flow.records().rows('ai_mi_source_targets')) == 2


def test_discovery_does_not_reenable_the_website_reader_or_approve_discovered_destinations():
    flow, _, provider, _ = configured()
    key = flow.catalog.ensure_website_reader()
    reader = flow.records().get('ai_mi_sources', key)
    flow.journal.transact(lambda records, state: records.upsert('ai_mi_sources', {**reader, 'status': 'inactive'}))
    flow.catalog.ensure_public_web_search()
    assert flow.records().get('ai_mi_sources', key)['status'] == 'inactive'
    assert len(flow.records().rows('ai_mi_source_targets')) == 1
    assert flow.records().rows('ai_mi_source_candidates') == [] and not provider.starts


@pytest.mark.parametrize('url', ['https://127.0.0.1/data', 'http://example.com/page'])
def test_requested_nonpublic_pages_cannot_fall_back_to_public_search(url):
    flow, _, provider, graph = configured()
    flow.public_discovery = True
    result = turn(graph, 'no-url-substitution', 'Investiga solo ' + url)
    assert 'HTTPS públicas' in result['response']['markdown']
    assert not provider.starts and not flow.records().rows('ai_mi_research_runs')


@pytest.mark.parametrize('urls', [['https://not-approved.example.org/page'],
    ['https://example.com/reference', 'https://not-approved.example.org/other']])
def test_public_pages_requested_for_one_read_do_not_become_a_search_or_permanent_base(urls):
    from datetime import datetime, timezone
    flow, _, provider, graph = configured()
    flow.public_discovery = True
    async def observations(receipt, call):
        return [{'url': item['url'], 'text': 'Página sintética indicada por el usuario',
            'published_at': None, 'observed_at': datetime.now(timezone.utc).isoformat(),
            'metrics': {}, 'provenance': {'source': call['actor']}}
            for item in call['input']['startUrls']], 1000
    provider.observations = observations
    result = turn(graph, 'one-read-no-search', 'Investiga solo ' + ' '.join(urls))
    assert result['response']['report_id'] and len(provider.starts) == 1
    call, = provider.starts
    assert call['actor'] == 'apify/website-content-crawler'
    assert {item['url'] for item in call['input']['startUrls']} == set(urls)
    assert len(flow.records().rows('ai_mi_source_targets')) == 1
    assert len(flow.records().rows('ai_mi_topic_sources')) == 1
