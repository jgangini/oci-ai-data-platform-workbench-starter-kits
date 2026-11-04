"""Temporal comparison is scoped captured-content evidence, not market trend inference."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.catalog import Catalog
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import PaidOperations, resolve_source_scope
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Journal, json_text
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.research import Research
from test_market_intelligence_repository import MemorySnapshot


TOPIC = {'entity': 'D1', 'market': 'Colombia', 'objective': 'Novedades'}
URL = 'https://example.com/report'
EPOCH = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


@pytest.fixture
def captured():
    research = Research(Journal(MemorySnapshot(), 1000000))
    catalog, paid = Catalog(research.journal), PaidOperations(research.journal)

    def capture(name, texts, *, day=0, closed=True, topic=TOPIC, published_at=None, metrics=None):
        question, topic_id = research.record_question(name, 'Consulta ' + name, topic)
        end = EPOCH + timedelta(days=day)
        window = {'start': (end - timedelta(days=7)).isoformat(), 'end': end.isoformat()}
        run = research.prepare_run(question, window, 'Comparación sintética', 100000)
        for index, (url, text) in enumerate(texts.items()):
            value, _ = research.journal.read()
            existing = next((row for row in value['tables']['ai_mi_source_targets'].values()
                if row['canonical_url_or_id'] == url), None)
            target = existing['target_id'] if existing else catalog.register_operator_website(topic_id, url, name + '-approve')
            source = research.journal.read()[0]['tables']['ai_mi_source_targets'][target]['source_key']
            # A second topic needs its own explicit link even if the destination already exists.
            links = research.journal.read()[0]['tables']['ai_mi_topic_sources'].values()
            if not any(row['topic_id'] == topic_id and row['target_id'] == target for row in links):
                catalog.register_operator_website(topic_id, url, name + '-link')
            tool = paid.reserve(run, source, target, {'urls': [url]}, 10000, 'Fixture de lectura de página',
                authorization_hash=paid.authorize(run, source, target)['authorization_hash'])
            permit = paid.claim_start(tool)
            provider = name + '-provider-' + str(index)
            paid.record_provider(tool, permit, provider, 'succeeded', {})
            paid.record_cost(tool, provider, 1000)
            if text is not None:
                research.add_evidence(run, tool, [{'url': url, 'text': text, 'published_at': published_at,
                    'observed_at': end.isoformat(), 'metrics': metrics or {}, 'provenance': {'synthetic': True}}])
        if closed:
            research.publish(run, 'Fixture; no dato de mercado.', [], [], ['Captura sintética'], {}, 'unresolved')
        def set_timing(records, state):
            row = records.get('ai_mi_research_runs', run)
            records.upsert('ai_mi_research_runs', {**row, 'started_finished_at_json': json_text({
                'started_at': end.isoformat(),
                'finished_at': (end + timedelta(minutes=1)).isoformat() if closed else None})})
        research.journal.transact(set_timing)
        return run
    return research, capture


def compare(research, run):
    return research.compare(run, scope_resolver=resolve_source_scope)


def test_first_research_persists_explicit_absent_baseline_and_window(captured):
    research, capture = captured
    run = capture('first', {URL: 'Texto'}, closed=False)
    context = compare(research, run)
    saved = research.journal.read()[0]['tables']['ai_mi_research_runs'][run]
    assert context == json.loads(saved['comparison_context_json'])
    assert context['version'] == 1 and context['status'] == 'no_baseline'
    assert context['baseline_run_id'] is None and context['baseline_window'] is None
    assert context['current_window'] == json.loads(saved['plan_window_json'])
    assert not context['changed'] and not context['newly_observed']


def test_baseline_is_latest_closed_same_topic_before_current_start(captured):
    research, capture = captured
    capture('old', {URL: 'Anterior'}, day=0)
    latest = capture('latest', {URL: 'Igual'}, day=1)
    current = capture('current', {URL: 'Igual'}, day=2, closed=False)
    capture('after-current', {URL: 'Posterior'}, day=3)
    capture('other-topic', {URL: 'Ajeno al objetivo'}, day=1,
            topic={**TOPIC, 'objective': 'Otro objetivo'})
    result = compare(research, current)
    assert result['baseline_run_id'] == latest and result['status'] == 'comparable'
    assert result['unchanged'] == [URL] and result['changed'] == []


def test_later_and_other_topic_runs_cannot_supply_a_missing_baseline(captured):
    research, capture = captured
    current = capture('current', {URL: 'Texto'}, day=1, closed=False)
    capture('later', {URL: 'Texto'}, day=2)
    capture('other', {URL: 'Texto'}, day=0, topic={**TOPIC, 'entity': 'Otra empresa'})
    assert compare(research, current)['status'] == 'no_baseline'


def test_fixed_scope_classifies_only_observed_content_and_never_disappearance(captured):
    research, capture = captured
    urls = [f'https://example.com/report-{index}' for index in range(4)]
    baseline = capture('before', dict(zip(urls, ['Original', 'Igual', None, 'Ya observado'])))
    current = capture('after', dict(zip(urls, ['Editado', 'Igual', 'Recuperado ahora', None])), day=1, closed=False)
    result = compare(research, current)
    assert result['status'] == 'comparable' and result['baseline_run_id'] == baseline
    assert result['changed'] == [urls[0]] and result['unchanged'] == [urls[1]]
    assert result['newly_observed'] == [urls[2]]
    assert 'disappeared' not in result and urls[3] not in sum((result[key] for key in ('changed', 'unchanged', 'newly_observed')), [])
    assert result['coverage'] == {'fixed_targets': 4, 'current_observed_urls': 3, 'baseline_observed_urls': 3}
    assert any('no demuestra una publicación nueva' in limit for limit in result['limitations'])
    assert any('no demuestra desaparición' in limit for limit in result['limitations'])


def test_changed_destinations_cannot_be_reported_as_a_new_market_observation(captured):
    research, capture = captured
    capture('before', {URL: 'Antes'})
    current = capture('after', {'https://example.com/other': 'Después'}, day=1, closed=False)
    result = compare(research, current)
    assert result['status'] == 'not_comparable' and result['reason'] == 'source_target_or_profile_scope_changed'
    assert result['newly_observed'] == result['changed'] == result['unchanged'] == []


def test_identical_captured_text_is_unchanged_despite_different_extracted_metadata(captured):
    research, capture = captured
    capture('before', {URL: 'Texto idéntico.'}, published_at=(EPOCH - timedelta(days=1)).isoformat(),
            metrics={'extracted_title': 'Título anterior'})
    current = capture('after', {URL: 'Texto idéntico.'}, day=1, closed=False,
        published_at=(EPOCH + timedelta(hours=1)).isoformat(), metrics={'extracted_title': 'Otro título'})
    documents = research.journal.read()[0]['tables']['ai_mi_documents'].values()
    assert len({document['content_hash'] for document in documents}) == 2
    result = compare(research, current)
    assert result['status'] == 'comparable' and result['basis'] == 'captured_text'
    assert result['unchanged'] == [URL] and result['changed'] == result['newly_observed'] == []


@pytest.mark.parametrize('missing', ['authorization', 'profile', 'resolver'])
def test_missing_or_changed_capture_authority_fails_closed(captured, missing):
    research, capture = captured
    baseline = capture('before', {URL: 'Antes'})
    current = capture('after', {URL: 'Después'}, day=1, closed=False)
    if missing != 'resolver':
        def alter(records, state):
            tool = next(row for row in records.rows('ai_mi_tool_runs') if row['run_id'] == baseline)
            if missing == 'authorization':
                del state['operations'][tool['tool_run_id']]['authorization_hash']
            else:
                source = records.get('ai_mi_sources', tool['source_key'])
                records.upsert('ai_mi_sources', {**source, 'connector_revision': 'x_search'})
        research.journal.transact(alter)
    result = research.compare(current) if missing == 'resolver' else compare(research, current)
    assert result['status'] == 'not_comparable' and result['reason'] == 'capture_scope_not_verifiable'


def test_saved_comparison_is_immutable_on_replay_even_if_the_catalog_changes(captured):
    research, capture = captured
    capture('before', {URL: 'Igual'})
    current = capture('after', {URL: 'Igual'}, day=1, closed=False)
    result = compare(research, current)
    assert result['status'] == 'comparable'
    def disable(records, state):
        source = records.rows('ai_mi_sources')[0]
        records.upsert('ai_mi_sources', {**source, 'status': 'inactive'})
    research.journal.transact(disable)
    assert compare(research, current) == result


def test_open_searches_cannot_be_compared_even_when_their_request_hashes_match(captured):
    research, capture = captured
    capture('before', {URL: 'Antes'})
    current = capture('after', {URL: 'Después'}, day=1, closed=False)
    def open_search(records, state):
        for tool in records.rows('ai_mi_tool_runs'):
            records.upsert('ai_mi_tool_runs', {**tool, 'target_id': None, 'request_hash': 'same-opaque-hash'})
    research.journal.transact(open_search)
    result = compare(research, current)
    assert result['status'] == 'not_comparable'
    assert result['newly_observed'] == result['changed'] == result['unchanged'] == []
    assert any('request_hash no acredita' in limit for limit in result['limitations'])


@pytest.mark.parametrize('foreign', ['owner', 'generation'])
def test_comparison_rejects_foreign_persisted_records(captured, foreign):
    research, capture = captured
    baseline = capture('before', {URL: 'Texto'})
    current = capture('after', {URL: 'Texto'}, day=1, closed=False)
    row = research.journal.snapshot.value['tables']['ai_mi_research_runs'][baseline]
    if foreign == 'owner':
        row['owner_id'] = 'other-owner'
    else:
        row['run_id'] = '00000000-0000-4000-8000-000000000002:foreign-run'
    with pytest.raises(ValueError):
        compare(research, current)
