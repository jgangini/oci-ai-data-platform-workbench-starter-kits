"""Visible references resolve only selected scoped metadata, not captured bodies."""
import copy

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Scope
from test_market_intelligence_research import running, observation, TOPIC, WINDOW
from test_market_intelligence_repository import SCOPE


@pytest.fixture
def references():
    research, run, tool = running()
    first, second, third = research.add_evidence(run, tool, [observation('First captured fragment'),
        observation('Another fragment at the same URL'), observation('Different page', 'https://example.org/other')])
    return research, run, tool, (first, second, third)


def test_selected_urls_are_canonical_deduplicated_and_stable_without_writing_or_hydrating_content(references, monkeypatch):
    research, run, _, (first, second, third) = references
    def forbid_hydration(*args, **kwargs):
        pytest.fail('A reference lookup must not hydrate evidence text or metadata payloads')
    monkeypatch.setattr(research, 'evidence', forbid_hydration)
    def unreadable_bodies(records, state):
        for row in records.rows('ai_mi_documents'):
            records.upsert('ai_mi_documents', {**row, 'metrics_json': 'not parsed', 'provenance_json': 'not parsed'})
        for row in records.rows('ai_mi_run_evidence'):
            records.upsert('ai_mi_run_evidence', {**row, 'quoted_span_json': 'not parsed'})
    research.journal.transact(unreadable_bodies)
    before = copy.deepcopy(research.journal.read()[0])
    assert research.reference_urls(run, [third, first, second, third]) == [
        'https://example.org/other', 'https://example.com/publicacion']
    assert research.reference_urls(run, [third]) == ['https://example.org/other']
    assert research.journal.read()[0] == before


def test_empty_citations_still_require_an_existing_run(references):
    research, run, _, _ = references
    assert research.reference_urls(run, []) == []
    with pytest.raises(ValueError, match='existing owned'):
        research.reference_urls('unknown-run', [])


@pytest.mark.parametrize('citations', [None, 'not-a-list', ('not-a-list',), [True], [1], [''], ['x' * 501]])
def test_citation_keys_use_the_existing_strict_contract(references, citations):
    research, run, _, _ = references
    with pytest.raises(ValueError):
        research.reference_urls(run, citations)


def test_fifty_citations_are_admitted_and_fifty_one_are_rejected(references):
    research, run, tool, _ = references
    ids = research.add_evidence(run, tool, [observation('Selected citation ' + str(index),
        'https://example.org/page/' + str(index)) for index in range(50)])
    assert len(research.reference_urls(run, ids)) == 50
    with pytest.raises(ValueError):
        research.reference_urls(run, [*ids, ids[0]])


def test_another_run_cannot_borrow_evidence_even_for_the_same_owner(references):
    research, run, _, (first, _, _) = references
    question, _ = research.record_question('another-request', 'Another scoped question', TOPIC)
    other = research.prepare_run(question, WINDOW, 'Another scoped research', 10000)
    with pytest.raises(ValueError, match='outside this research'):
        research.reference_urls(other, [first])
    with pytest.raises(ValueError, match='outside this research'):
        research.reference_urls(run, ['missing-evidence'])


@pytest.mark.parametrize('identity', ['research-owner', 'journal-owner', 'generation'])
def test_reference_lookup_rejects_foreign_scope_or_generation(references, identity):
    research, run, _, ids = references
    scope = Scope(SCOPE.tenant_id, 'someone-else' if identity != 'generation' else SCOPE.owner_id,
                  '00000000-0000-4000-8000-000000000002' if identity == 'generation' else SCOPE.generation_id)
    if identity == 'research-owner':
        research.scope = scope
    else:
        research.journal.scope = scope
        research.scope = scope
    with pytest.raises((ValueError, RuntimeError), match='scope|binding'):
        research.reference_urls(run, list(ids))


@pytest.mark.parametrize('url', ['http://example.org/article', 'https://localhost/article',
    'https://127.0.0.1/article', 'https://user:password@example.org/article'])
def test_a_private_or_unsafe_document_url_cannot_be_rendered_as_a_reference(references, url):
    research, run, _, (first, _, _) = references
    def unsafe(records, state):
        item = records.get('ai_mi_run_evidence', first)
        document = records.get('ai_mi_documents', item['observation_key'])
        records.upsert('ai_mi_documents', {**document, 'source_url': url})
    research.journal.transact(unsafe)
    with pytest.raises(ValueError):
        research.reference_urls(run, [first])


def test_missing_document_fails_the_existing_journal_integrity_boundary(references):
    research, run, _, (first, _, _) = references
    snapshot = research.journal.snapshot
    item = snapshot.value['tables']['ai_mi_run_evidence'][first]
    del snapshot.value['tables']['ai_mi_documents'][item['observation_key']]
    with pytest.raises(ValueError, match='Missing scoped reference'):
        research.reference_urls(run, [first])
