import json

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.catalog import Catalog
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import digest
from test_market_intelligence_repository import SCOPE
from test_market_intelligence_research import running, observation


def candidate():
    research, run, tool = running()
    evidence, = research.add_evidence(run, tool, [observation()])
    source = SCOPE.key('website-connector')
    research.journal.transact(lambda records, state: records.upsert('ai_mi_sources', {
        'source_key': source, 'status': 'active', 'platform': 'web'}))
    state, _ = research.journal.read()
    topic = state['tables']['ai_mi_research_runs'][run]['topic_id']
    catalog = Catalog(research.journal)
    key = catalog.propose_target(topic, source, 'https://example.com/noticias', 'Noticias del mercado', [evidence])
    state, _ = research.journal.read()
    proposal = json.loads(state['tables']['ai_mi_source_candidates'][key]['proposed_target_json'])
    return catalog, key, digest(proposal)


def test_source_candidate_stays_out_of_approved_base_until_confirmed():
    catalog, key, revision = candidate()
    assert catalog.journal.read()[0]['tables']['ai_mi_source_targets'] == {}
    with pytest.raises(ValueError, match='presented candidate'):
        catalog.decide_target(key, 'stale', True, 'user-yes')
    target = catalog.decide_target(key, revision, True, 'user-yes')
    state, _ = catalog.journal.read()
    assert state['tables']['ai_mi_source_targets'][target]['approval_revision'] == 1
    assert state['tables']['ai_mi_source_candidates'][key]['state'] == 'approved'
    assert len(state['tables']['ai_mi_topic_sources']) == 1


def test_rejecting_candidate_never_creates_target_or_topic_link():
    catalog, key, revision = candidate()
    assert catalog.decide_target(key, revision, False, 'user-no') is None
    state, _ = catalog.journal.read()
    assert state['tables']['ai_mi_source_targets'] == state['tables']['ai_mi_topic_sources'] == {}
