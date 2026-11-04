"""Content identity is immutable; capture dates and provenance belong to each run."""
import json
from datetime import timedelta

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import PaidOperations, resolve_source_scope
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import json_text
from test_market_intelligence_reuse import captures, NOW, NEWER, reuse
from test_market_intelligence_comparison import captured, compare, URL


def ingest(research, source, run, observed, label, *, tool=None):
    paid = PaidOperations(research.journal)
    if tool is None:
        tool = paid.reserve(run, source, None, {'query': label}, 10000, 'Synthetic capture',
                            authorization_hash=paid.authorize(run, source, None)['authorization_hash'])
        permit = paid.claim_start(tool)
        paid.record_provider(tool, permit, 'provider-' + label, 'succeeded', {})
        paid.record_cost(tool, 'provider-' + label, 100)
    observation = {'url': 'https://www.youtube.com/watch?v=aaaaaaaaaaa',
        'text': 'Identical title and description', 'published_at': NEWER,
        'observed_at': observed, 'metrics': {'viewCount': 10},
        'provenance': {'source': 'streamers/youtube-scraper', 'provider_run_id': 'provider-' + label,
                       'content_basis': 'title_and_description', 'capture_label': label}}
    key, = research.add_evidence(run, tool, [observation])
    return key, tool


def close(research, run):
    research.publish(run, 'Synthetic result.', [], [], [], {}, 'unresolved')


def test_identical_content_has_current_per_run_clock_and_provenance_without_mutating_the_document(captures):
    research, source, run, _ = captures
    first = run('first')
    key1, _ = ingest(research, source, first, (NOW - timedelta(days=2)).isoformat(), 'first')
    close(research, first)
    original_documents = research.journal.read()[0]['tables']['ai_mi_documents']
    second = run('second')
    key2, _ = ingest(research, source, second, (NOW - timedelta(hours=2)).isoformat(), 'second')
    assert key1 != key2
    assert research.journal.read()[0]['tables']['ai_mi_documents'] == original_documents
    older, newer = research.evidence(first)[0], research.evidence(second)[0]
    assert older['observed_at'] == (NOW - timedelta(days=2)).isoformat()
    assert newer['observed_at'] == (NOW - timedelta(hours=2)).isoformat()
    assert older['provenance']['capture_label'] == 'first'
    assert newer['provenance']['capture_label'] == 'second'
    assert 'provider_run_id' not in newer['provenance'] and newer['metrics'] == {'viewCount': 10}
    close(research, second)

    current = run('reuse-newest-capture')
    before = research.journal.read()[0]
    linked = reuse(research, current, source)
    assert len(linked) == 1
    reused = research.evidence(current)[0]
    assert reused['observed_at'] == newer['observed_at']
    assert reused['provenance']['original_observed_at'] == newer['observed_at']
    assert reused['provenance']['capture_label'] == 'second'
    assert reused['provenance']['reused_publication_metadata'] is True and reused['metrics'] == {}
    span = json.loads(research.journal.read()[0]['tables']['ai_mi_run_evidence'][linked[0]]['quoted_span_json'])
    assert span['reuse']['original_run_id'] == second
    assert research.journal.read()[0]['operations'] == before['operations']
    assert reuse(research, current, source, now=NOW + timedelta(hours=1)) == []
    assert research.evidence(current)[0] == reused


def test_same_tool_replay_does_not_retimestamp_capture_but_an_actual_new_tool_can_refresh_it(captures):
    research, source, run, _ = captures
    current = run('same-run')
    key, tool = ingest(research, source, current, (NOW - timedelta(hours=2)).isoformat(), 'first')
    before = research.journal.read()[0]['tables']
    replay, _ = ingest(research, source, current, NOW.isoformat(), 'replayed-response', tool=tool)
    assert replay == key and research.journal.read()[0]['tables'] == before
    newer, newer_tool = ingest(research, source, current, (NOW - timedelta(hours=1)).isoformat(), 'new-tool')
    assert newer == key and newer_tool != tool
    item = research.evidence(current)[0]
    assert item['observed_at'] == (NOW - timedelta(hours=1)).isoformat()
    assert item['provenance']['capture_label'] == 'new-tool'
    assert len(research.evidence(current)) == len(research.journal.read()[0]['tables']['ai_mi_documents']) == 1


@pytest.mark.parametrize('timestamp', [None, True, '', 'not-a-date', '2026-11-01',
    '2026-11-01T12:00:00', '2026-11-01T12:00:00-05:00'])
def test_fresh_capture_requires_an_explicit_normalized_utc_instant(captures, timestamp):
    research, source, run, _ = captures
    current = run('invalid-clock')
    with pytest.raises(ValueError, match='explicit UTC'):
        ingest(research, source, current, timestamp, 'invalid')
    assert not research.evidence(current)
    assert not research.journal.read()[0]['tables']['ai_mi_documents']


@pytest.mark.parametrize('age,eligible', [(timedelta(hours=2), True),
    (timedelta(days=1, seconds=1), False), (timedelta(seconds=-1), False)])
def test_legacy_rows_use_original_document_clock_without_renewing_it(captures, age, eligible):
    research, source, run, _ = captures
    original = run('legacy')
    key, _ = ingest(research, source, original, NOW.isoformat(), 'legacy')
    close(research, original)
    observed = (NOW - age).isoformat()
    def legacy(records, state):
        item = records.get('ai_mi_run_evidence', key)
        span = json.loads(item['quoted_span_json'])
        span.pop('capture')
        records.upsert('ai_mi_run_evidence', {**item, 'quoted_span_json': json_text(span)})
        document = records.get('ai_mi_documents', item['observation_key'])
        records.upsert('ai_mi_documents', {**document, 'observed_at': observed})
    research.journal.transact(legacy)
    assert research.evidence(original)[0]['observed_at'] == observed
    current = run('legacy-reuse')
    assert bool(reuse(research, current, source)) is eligible
    if eligible:
        assert research.evidence(current)[0]['observed_at'] == observed
        assert research.evidence(current)[0]['metrics'] == {}


def test_a_new_capture_clock_does_not_create_a_false_content_change(captured):
    research, capture = captured
    baseline = capture('baseline-clock', {URL: 'Exact same captured text'}, day=0)
    original_documents = research.journal.read()[0]['tables']['ai_mi_documents']
    current = capture('fresh-clock', {URL: 'Exact same captured text'}, day=1, closed=False)
    assert research.evidence(current)[0]['observed_at'] != research.evidence(baseline)[0]['observed_at']
    assert research.journal.read()[0]['tables']['ai_mi_documents'] == original_documents
    comparison = compare(research, current)
    assert comparison['status'] == 'comparable' and comparison['unchanged'] == [URL]
    assert comparison['changed'] == [] and comparison['newly_observed'] == []
