import json

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.catalog import Catalog
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Records, digest
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


@pytest.mark.parametrize('unavailable', ['other_topic', 'link', 'target', 'source'])
def test_approved_base_omits_web_sources_without_an_active_target_for_the_requested_topic(unavailable):
    research, run, _ = running()
    catalog = Catalog(research.journal)
    topic = research.journal.read()[0]['tables']['ai_mi_research_runs'][run]['topic_id']
    target = catalog.register_operator_website(topic, 'https://example.com/research', 'approve-page')
    value, _ = research.journal.read()
    source = value['tables']['ai_mi_source_targets'][target]['source_key']
    link = next(row['topic_source_id'] for row in value['tables']['ai_mi_topic_sources'].values()
                if row['target_id'] == target)
    if unavailable != 'other_topic':
        table, key = {'source': ('ai_mi_sources', source), 'target': ('ai_mi_source_targets', target),
                      'link': ('ai_mi_topic_sources', link)}[unavailable]
        research.journal.transact(lambda records, state: records.upsert(table, {**records.get(table, key), 'status': 'disabled'}))
    else:
        topic = SCOPE.key('another-topic')
    value, _ = research.journal.read()
    base = catalog.approved_base(Records(SCOPE, value['tables']), topic)
    assert source not in {row['source_key'] for row in base}
    assert [row['source_key'] for row in base] == [SCOPE.key('source-1')]
    assert base[0]['targets'] == []  # This existing X source explicitly permits discovery.


def test_a_scheduled_empty_target_allowlist_does_not_enable_global_discovery():
    research, run, _ = running()
    catalog = Catalog(research.journal)
    topic = research.journal.read()[0]['tables']['ai_mi_research_runs'][run]['topic_id']
    catalog.register_operator_website(topic, 'https://example.com/research', 'approve-page')
    value, _ = research.journal.read()
    assert catalog.approved_base(Records(SCOPE, value['tables']), topic, permitted_targets=set()) == []
