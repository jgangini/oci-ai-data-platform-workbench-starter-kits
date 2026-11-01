"""Catalog questions must not become registration or paid research requests."""
import pytest

pytest.importorskip('langgraph', reason='Graph execution is checked in the pinned runtime CI job')

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.presentation import response_markdown, source_base_markdown
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import digest, json_text
from test_market_intelligence_workflow import configured, turn, TOPIC, SCOPE


def listing_model(model, topic=TOPIC, *, missing=False):
    async def ask(name, schema, payload):
        assert name == 'understand', 'Listing must not ask a research planner or writer'
        model.calls.append(name)
        return {'intent': 'list_sources', 'canonical_question': 'Fuentes registradas',
                'topic': topic, 'window_spec': 'last_7_days',
                'missing_fields': ['tema'] if missing else [], 'user_question': None}
    model.ask = ask


def test_registered_sources_are_listed_without_research_capture_or_recurrence_inflation():
    flow, model, provider, graph = configured()
    listing_model(model)
    def other_topic(records, state):
        topic = SCOPE.key(digest(['topic', {**TOPIC, 'entity': 'Another company'}]))
        records.upsert('ai_mi_topics', {'topic_id': topic, 'canonical_scope_json': json_text({**TOPIC, 'entity': 'Another company'})})
        target = SCOPE.key('other-target')
        records.upsert('ai_mi_source_targets', {'target_id': target, 'source_key': SCOPE.key('web'),
            'canonical_url_or_id': 'https://example.com/other-private-topic', 'status': 'active'})
        records.upsert('ai_mi_topic_sources', {'topic_source_id': SCOPE.key('other-link'), 'topic_id': topic,
            'target_id': target, 'approved_scope_json': '{}', 'status': 'active'})
    flow.journal.transact(other_topic)
    result = turn(graph, 'list-sources', '¿Qué páginas tengo registradas para D1?')
    text = response_markdown(result['response'])
    assert 'https://example.com/reference' in text and 'Colombia' in text
    assert 'búsquedas abiertas' in text and 'x (Registrada)' in text
    assert 'other-private-topic' not in text and SCOPE.key('reference') not in text
    assert 'todavía no se ha buscado información nueva' in text
    assert '| Fuente | Referencia | Estado |' in text
    assert all(term not in text for term in ('Actors', 'Apify', 'adaptador', 'modelo configurado'))
    assert model.calls == ['understand'] and not provider.starts
    assert not flow.records().rows('ai_mi_research_runs')
    assert not flow.records().rows('ai_mi_reports')
    assert not flow.records().rows('ai_mi_source_candidates')
    questions = flow.records().rows('ai_mi_questions')
    assert len(questions) == 1 and questions[0]['question_kind'] == 'administration'
    assert not flow.proposals.recurring_questions(questions[0]['topic_id'])


@pytest.mark.parametrize(('table', 'key'), [
    ('ai_mi_sources', 'web'), ('ai_mi_source_targets', 'reference'), ('ai_mi_topic_sources', 'link')])
def test_inactive_source_target_or_topic_link_is_not_shown_as_approved(table, key):
    flow, model, provider, graph = configured()
    listing_model(model)
    def deactivate(records, state):
        row = records.get(table, SCOPE.key(key))
        records.upsert(table, {**row, 'status': 'inactive'})
    flow.journal.transact(deactivate)
    response = turn(graph, 'list-inactive')['response']['markdown']
    assert 'No hay destinos activos aprobados' in response
    assert 'https://example.com/reference' not in response and not provider.starts


@pytest.mark.parametrize('missing', [False, True])
def test_missing_topic_requests_clarification_without_exposing_catalog(missing):
    flow, model, provider, graph = configured()
    listing_model(model, None if not missing else TOPIC, missing=missing)
    response = turn(graph, 'ambiguous-list')['response']['markdown']
    assert '¿Para qué empresa' in response and 'example.com' not in response
    assert not provider.starts and not flow.records().rows('ai_mi_research_runs')


def test_suspended_connector_is_visible_but_not_described_as_available():
    flow, model, provider, graph = configured()
    listing_model(model)
    def suspend(records, state):
        source = records.get('ai_mi_sources', SCOPE.key('web'))
        records.upsert('ai_mi_sources', {**source, 'name': '<img> [Ir](https://other.example)',
                                       'connector_revision': 'yahoo_finance'})
    flow.journal.transact(suspend)
    text = turn(graph, 'list-suspended')['response']['markdown']
    assert 'No disponible' in text and '<img>' not in text
    assert '&lt;img&gt;' in text and '\\[Ir\\]\\(https://other.example\\)' in text
    assert not provider.starts


def test_large_catalog_states_its_limit_instead_of_silently_omitting_targets():
    base = [{'name': 'Web', 'profile_id': 'website', 'targets': [
        {'canonical_url_or_id': 'https://example.com/page-' + str(index)} for index in range(51)]}]
    text = source_base_markdown(TOPIC, base)
    assert 'primeros 50 de 51 destinos' in text
    assert 'page-49' in text and 'page-50' not in text


def test_discovery_does_not_describe_suspended_or_unknown_adapters_as_enabled(monkeypatch):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import PROFILES
    monkeypatch.setitem(PROFILES, 'x_search', {**PROFILES['x_search'], 'unavailable_reason': 'Suspended fixture'})
    text = source_base_markdown(TOPIC, [
        {'name': 'X', 'profile_id': 'x_search', 'targets': []},
        {'name': 'Unknown', 'profile_id': 'not-installed', 'targets': []}])
    assert 'X (No disponible)' in text and 'Unknown (Sin conexión configurada)' in text
    assert 'habilitados' not in text
