"""Reuse links are current-scope evidence, never a synthetic provider operation."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import research as research_module
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import execution as execution_module
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.catalog import Catalog
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import PROFILES
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import PaidOperations, resolve_source_scope
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Journal, Scope, json_text
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.research import Research
from test_market_intelligence_repository import MemorySnapshot, SCOPE
from test_market_intelligence_research import TOPIC


NOW = datetime(2026, 11, 1, 18, tzinfo=timezone.utc)
WINDOW = {'start': (NOW - timedelta(days=7)).isoformat(), 'end': NOW.isoformat()}
NEWER = '2026-10-31T18:00:00+00:00'
OLDER = '2026-10-30T18:00:00+00:00'


@pytest.fixture
def captures(monkeypatch):
    monkeypatch.setattr(research_module, 'utc_now', lambda: NOW.isoformat())
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(execution_module, 'datetime', FixedClock)
    research = Research(Journal(MemorySnapshot(), 10000000))
    catalog, paid = Catalog(research.journal), PaidOperations(research.journal)
    source = catalog.ensure_public_youtube_search()

    def run(name, *, topic=TOPIC):
        question, _ = research.record_question(name, 'Consulta ' + name, topic)
        return research.prepare_run(question, WINDOW, 'Metadatos de YouTube', 100000)

    def capture(name, *, published=NEWER, url='https://www.youtube.com/watch?v=aaaaaaaaaaa',
                topic=TOPIC, closed=True, text='Título y descripción', observed=NOW, metrics=None, profile='youtube_search'):
        run_id = run(name, topic=topic)
        chosen = source if profile == 'youtube_search' else catalog.ensure_public_x_search()
        tool = paid.reserve(run_id, chosen, None, {'query': name}, 10000, 'Captura sintética',
                            authorization_hash=paid.authorize(run_id, chosen, None)['authorization_hash'])
        permit = paid.claim_start(tool)
        paid.record_provider(tool, permit, 'provider-' + name, 'succeeded', {})
        paid.record_cost(tool, 'provider-' + name, 100)
        ids = research.add_evidence(run_id, tool, [{'url': url, 'text': text, 'published_at': published,
            'observed_at': observed.isoformat(), 'metrics': metrics or {'viewCount': 10},
            'provenance': {'source': 'streamers/youtube-scraper', 'provider_run_id': 'provider-' + name,
                           'content_basis': 'title_and_description', 'channel_name': 'Canal de prueba'}}])
        if closed:
            research.publish(run_id, 'Resultado sintético.', [], [], [], {}, 'unresolved')
        return run_id, tool, ids

    return research, source, run, capture


def reuse(research, run, source, **kwargs):
    return research.reuse_publication_metadata(run, [source], scope_resolver=resolve_source_scope, now=kwargs.get('now', NOW))


def test_new_capture_of_october_thirty_keeps_authorized_october_thirty_one(captures):
    research, source, run, capture = captures
    original, original_tool, originals = capture('prior', closed=False)
    research.assess(original, [{'evidence_id': originals[0], 'stance': 'supports',
                               'reason': 'Juicio de la investigación anterior', 'independence': 'known'}],
                    {'continue': False, 'reason': 'Muestra anterior', 'gap': ''})
    research.publish(original, 'Resultado anterior.', originals, [], [], {}, 'sufficient')
    current, tool, current_ids = capture('current', published=OLDER, url='https://www.youtube.com/watch?v=bbbbbbbbbbb', closed=False)
    before = research.journal.read()[0]
    linked = reuse(research, current, source)
    assert len(linked) == 1 and linked[0] not in originals + current_ids
    after = research.journal.read()[0]
    assert after['operations'] == before['operations']
    for table in ('ai_mi_documents', 'ai_mi_tool_runs', 'ai_mi_research_runs'):
        assert after['tables'][table] == before['tables'][table]
    rows = research.evidence(current)
    recent = max(rows, key=lambda row: row['published_at'])
    assert recent['published_at'] == NEWER and recent['assessment'] is None
    assert recent['metrics'] == {} and recent['provenance']['reused_publication_metadata'] is True
    assert recent['provenance']['original_observed_at'] == NOW.isoformat()
    assert not any(key in json.dumps(recent['provenance']) for key in (original, original_tool, originals[0], 'provider-prior'))
    stored = after['tables']['ai_mi_run_evidence'][linked[0]]
    assert stored['tool_run_id'] is None
    lineage = json.loads(stored['quoted_span_json'])['reuse']
    assert lineage['original_run_id'] == original and lineage['original_tool_run_id'] == original_tool
    assert lineage['original_evidence_key'] == originals[0]
    assert reuse(research, current, source) == []
    assert research.journal.read()[0]['tables'] == after['tables']
    assert research.journal.read()[0]['operations'] == after['operations']
    report = research.publish(current, 'El más reciente recuperado es del 31 de octubre.', linked, [],
                              ['Metadatos reutilizados; no nueva captura.'], {}, 'sufficient')
    assert report and len(research.evidence(current)) == 2


@pytest.mark.parametrize('case', ['outside_window', 'unknown_publication', 'old_capture', 'future_capture',
                                  'future_publication', 'other_topic', 'not_closed', 'other_profile'])
def test_only_recent_dated_same_topic_closed_youtube_evidence_is_reused(captures, case):
    research, source, run, capture = captures
    kwargs = {'published': None} if case == 'unknown_publication' else {}
    if case == 'other_topic':
        kwargs['topic'] = {**TOPIC, 'entity': 'Otra empresa'}
    if case == 'not_closed':
        kwargs['closed'] = False
    if case == 'other_profile':
        kwargs['profile'] = 'x_search'
    original, tool, ids = capture('prior', **kwargs)
    def alter(records, state):
        item = records.get('ai_mi_run_evidence', ids[0])
        doc = records.get('ai_mi_documents', item['observation_key'])
        changes = {'outside_window': {'published_at': (NOW - timedelta(days=8)).isoformat()},
                   'old_capture': {'observed_at': (NOW - timedelta(days=1, seconds=1)).isoformat()},
                   'future_capture': {'observed_at': (NOW + timedelta(seconds=1)).isoformat()},
                   'future_publication': {'published_at': (NOW + timedelta(seconds=1)).isoformat()}}
        if case in changes:
            if case in {'old_capture', 'future_capture'}:
                span = json.loads(item['quoted_span_json'])
                span['capture']['observed_at'] = changes[case]['observed_at']
                records.upsert('ai_mi_run_evidence', {**item, 'quoted_span_json': json_text(span)})
            else:
                records.upsert('ai_mi_documents', {**doc, **changes[case]})
    research.journal.transact(alter)
    current = run('current')
    if case == 'future_publication':
        research.journal.transact(lambda records, state: records.upsert('ai_mi_research_runs', {
            **records.get('ai_mi_research_runs', current), 'plan_window_json': json_text({
                **WINDOW, 'end': (NOW + timedelta(hours=1)).isoformat()})}))
    before = research.journal.read()[0]
    assert reuse(research, current, source) == []
    assert research.journal.read()[0]['tables'] == before['tables']
    assert research.journal.read()[0]['operations'] == before['operations']


@pytest.mark.parametrize('case', ['not_selected', 'inactive', 'policy', 'build', 'missing_operation', 'operation_failed', 'tool_failed'])
def test_current_selection_and_original_success_authority_are_required(captures, monkeypatch, case):
    research, source, run, capture = captures
    _, tool, _ = capture('prior')
    current = run('current')
    def revoke(records, state):
        if case in {'inactive', 'policy'}:
            row = records.get('ai_mi_sources', source)
            change = {'status': 'inactive'} if case == 'inactive' else {'allowed_scope_json': json_text({'allow_discovery': False})}
            records.upsert('ai_mi_sources', {**row, **change})
        elif case == 'missing_operation':
            del state['operations'][tool]
        elif case == 'operation_failed':
            state['operations'][tool]['state'] = 'failed'
        elif case == 'tool_failed':
            records.upsert('ai_mi_tool_runs', {**records.get('ai_mi_tool_runs', tool), 'state': 'failed'})
    research.journal.transact(revoke)
    if case == 'build':
        monkeypatch.setitem(PROFILES, 'youtube_search', {**PROFILES['youtube_search'], 'build': 'new-build'})
    before = research.journal.read()[0]
    allowed = [] if case == 'not_selected' else [source]
    assert research.reuse_publication_metadata(current, allowed, scope_resolver=resolve_source_scope, now=NOW) == []
    assert research.journal.read()[0]['tables'] == before['tables']
    assert research.journal.read()[0]['operations'] == before['operations']


def test_new_successful_capture_replaces_the_reuse_link_without_duplicate_or_old_assessment(captures):
    research, source, run, capture = captures
    _, _, ids = capture('prior')
    current = run('current')
    linked, = reuse(research, current, source)
    research.assess(current, [{'evidence_id': linked, 'stance': 'supports', 'reason': 'Revisado', 'independence': 'known'}],
                    {'continue': False, 'reason': 'Muestra', 'gap': ''})
    paid = PaidOperations(research.journal)
    tool = paid.reserve(current, source, None, {'query': 'fresh'}, 10000, 'Nueva captura real',
                        authorization_hash=paid.authorize(current, source, None)['authorization_hash'])
    permit = paid.claim_start(tool)
    paid.record_provider(tool, permit, 'new-provider', 'succeeded', {})
    fresh = research.add_evidence(current, tool, [{'url': 'https://www.youtube.com/watch?v=aaaaaaaaaaa',
        'text': 'Título y descripción', 'published_at': NEWER, 'observed_at': NOW.isoformat(),
        'metrics': {'viewCount': 10}, 'provenance': {'source': 'streamers/youtube-scraper'}}])
    assert fresh == [linked] and len(research.evidence(current)) == 1
    row = research.evidence(current)[0]
    assert row['metrics'] == {'viewCount': 10} and row['assessment'] is None
    assert 'reused_publication_metadata' not in row['provenance']
    stored = research.journal.read()[0]['tables']['ai_mi_run_evidence'][linked]
    assert stored['tool_run_id'] == tool and 'reuse' not in json.loads(stored['quoted_span_json'])


@pytest.mark.parametrize('scope', [Scope('tenant-a', 'owner-other', SCOPE.generation_id),
                                  Scope('tenant-a', SCOPE.owner_id, '00000000-0000-4000-8000-000000000002')])
def test_another_owner_or_generation_cannot_reuse_original_journal(captures, scope):
    research, source, run, capture = captures
    capture('prior')
    current = run('current')
    other = MemorySnapshot()
    other.scope = scope
    other.value = research.journal.read()[0]
    with pytest.raises(RuntimeError, match='Scoped journal binding changed'):
        Research(Journal(other, 10000000)).reuse_publication_metadata(current, [source],
            scope_resolver=resolve_source_scope, now=NOW)


def test_reuse_is_bounded_to_fifty_unique_observations_in_deterministic_publication_order(captures):
    research, source, run, capture = captures
    # Two original tools each remain within the existing fifty-observation admission.
    for part in range(2):
        original, tool, _ = capture('prior-' + str(part), closed=False,
                                    url='https://www.youtube.com/watch?v=' + ('a' if part == 0 else 'b') * 11)
        batch = [{'url': 'https://www.youtube.com/watch?v=' + str(part * 30 + i).zfill(11),
                  'text': 'Título ' + str(part * 30 + i),
                  'published_at': (NOW - timedelta(hours=part * 30 + i + 1)).isoformat(),
                  'observed_at': NOW.isoformat(), 'metrics': {},
                  'provenance': {'source': 'streamers/youtube-scraper', 'content_basis': 'title_and_description'}}
                 for i in range(30)]
        research.add_evidence(original, tool, batch)
        research.publish(original, 'Metadatos sintéticos.', [], [], [], {}, 'unresolved')
    current = run('current')
    before = research.journal.read()[0]
    assert len(reuse(research, current, source)) == 50
    rows = research.evidence(current)
    assert len(rows) == 50 and len({row['url'] for row in rows}) == 50
    assert [row['published_at'] for row in rows] == sorted((row['published_at'] for row in rows), reverse=True)
    assert reuse(research, current, source) == []
    assert research.journal.read()[0]['operations'] == before['operations']


def test_reuse_rejects_a_closed_current_run_and_an_implicit_or_non_utc_clock(captures):
    research, source, run, capture = captures
    closed, _, _ = capture('closed')
    with pytest.raises(ValueError, match='running'):
        reuse(research, closed, source)
    current = run('current')
    with pytest.raises(ValueError, match='UTC clock'):
        reuse(research, current, source, now=NOW.replace(tzinfo=None))


def metadata_rows(offset, count):
    return [{'url': 'https://www.youtube.com/watch?v=' + str(offset + index).zfill(11),
             'text': 'Metadatos ' + str(offset + index), 'published_at': NEWER,
             'observed_at': NOW.isoformat(), 'metrics': {},
             'provenance': {'source': 'streamers/youtube-scraper', 'content_basis': 'title_and_description'}}
            for index in range(count)]


def prior_candidates(research, capture):
    prior, tool, _ = capture('prior', closed=False)
    research.add_evidence(prior, tool, metadata_rows(0, 49))
    research.publish(prior, 'Muestra anterior.', [], [], [], {}, 'unresolved')


def set_step_limit(research, run, limit):
    research.journal.transact(lambda records, state: records.upsert('ai_mi_research_runs', {
        **records.get('ai_mi_research_runs', run),
        'completion_policy_json': json_text({'max_steps': limit, 'max_elapsed_seconds': 300})}))


@pytest.mark.parametrize('current_count,expected', [(481, 19), (500, 0)])
def test_reuse_respects_the_remaining_assessment_capacity(captures, current_count, expected):
    research, source, _, capture = captures
    prior_candidates(research, capture)
    current, tool, _ = capture('current', closed=False, url='https://www.youtube.com/watch?v=bbbbbbbbbbb')
    set_step_limit(research, current, 1)
    # Populate the boundary through the existing bounded observation write path.
    for offset in range(0, current_count - 1, 50):
        research.add_evidence(current, tool, metadata_rows(1000 + offset, min(50, current_count - 1 - offset)))
    assert len(research.evidence(current)) == current_count
    operations = research.journal.read()[0]['operations']
    assert len(reuse(research, current, source)) == expected
    assert len(research.evidence(current)) == 500
    assert research.journal.read()[0]['operations'] == operations
    assert reuse(research, current, source) == []


def test_reuse_reserves_slots_for_every_future_fifty_observation_step(captures):
    research, source, _, capture = captures
    prior_candidates(research, capture)
    current, first_tool, _ = capture('current', closed=False, url='https://www.youtube.com/watch?v=bbbbbbbbbbb')
    set_step_limit(research, current, 10)
    research.add_evidence(current, first_tool, metadata_rows(1000, 2))
    assert len(research.evidence(current)) == 3
    assert len(reuse(research, current, source)) == 47
    paid = PaidOperations(research.journal)
    for step in range(1, 10):
        tool = paid.reserve(current, source, None, {'batch': step}, 10000, 'Captura posterior acotada',
                            authorization_hash=paid.authorize(current, source, None)['authorization_hash'])
        permit = paid.claim_start(tool)
        paid.record_provider(tool, permit, 'provider-later-' + str(step), 'succeeded', {})
        paid.record_cost(tool, 'provider-later-' + str(step), 100)
        research.add_evidence(current, tool, metadata_rows(2000 + step * 50, 50))
        assert len(research.evidence(current)) == 50 + step * 50
        assert reuse(research, current, source) == []
    assert len(research.evidence(current)) == 500
    assert len(research.journal.read()[0]['operations']) == 11  # One historical and ten current captures.
