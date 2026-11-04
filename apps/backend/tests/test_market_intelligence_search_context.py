"""Country/language framing is explicit, scoped and immutable once accepted."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.contracts import SearchContext
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.research import research_search_context
from test_market_intelligence_workflow import configured, TOPIC, SCOPE


CO = {'country_code': 'CO', 'language_code': 'es'}
MX = {'country_code': 'MX', 'language_code': 'es'}


def assert_revision_only_write(before, after):
    assert after['revision'] == before['revision'] + 1
    assert {key: value for key, value in after.items() if key != 'revision'} == {
        key: value for key, value in before.items() if key != 'revision'}


def research(context=None):
    flow, _, _, _ = configured()
    question, _ = flow.research.record_question('search-framing', 'Novedades de D1 en Colombia', TOPIC)
    now = datetime.now(timezone.utc)
    window = {'start': (now - timedelta(days=7)).isoformat(), 'end': now.isoformat()}
    run = flow.research.prepare_run(question, window, 'Novedades de D1', 100000, search_context=context)
    return flow, question, run, window


@pytest.mark.parametrize('context', [CO, MX, {'country_code': 'BR', 'language_code': 'pt-BR'}])
def test_initial_framing_is_stored_and_replay_does_not_change_it(context):
    flow, question, run, window = research(context)
    saved = copy.deepcopy(flow.journal.read()[0])
    row = flow.records().get('ai_mi_research_runs', run)
    assert json.loads(row['brief_json'])['search_context'] == context
    assert research_search_context(row) == context
    assert flow.research.prepare_run(question, window, 'Novedades de D1', 100000, search_context=context) == run
    assert_revision_only_write(saved, flow.journal.read()[0])


def test_changing_an_accepted_context_cannot_reinterpret_an_existing_question():
    flow, question, run, window = research(CO)
    before = copy.deepcopy(flow.journal.read()[0])
    with pytest.raises(ValueError, match='immutable'):
        flow.research.prepare_run(question, window, 'Novedades de D1', 100000, search_context=MX)
    with pytest.raises(ValueError, match='immutable'):
        flow.research.bind_search_context(run, MX)
    assert flow.journal.read()[0] == before


def test_accepting_missing_framing_preserves_initial_brief_window_and_limits():
    flow, _, run, _ = research()
    before = copy.deepcopy(flow.records().get('ai_mi_research_runs', run))
    assert research_search_context(before) is None
    assert flow.research.bind_search_context(run, CO) == CO
    after = flow.records().get('ai_mi_research_runs', run)
    for field in ('brief_json', 'plan_window_json', 'completion_policy_json', 'reserved_microusd', 'started_finished_at_json'):
        assert after[field] == before[field]
    assert research_search_context(after) == CO
    assert json.loads(after['decision_log_json'])['search_context'] == CO
    saved = copy.deepcopy(flow.journal.read()[0])
    flow.research.bind_search_context(run, CO)
    assert_revision_only_write(saved, flow.journal.read()[0])


def test_missing_framing_cannot_be_added_after_a_capture_is_reserved():
    flow, _, run, _ = research()
    source = SCOPE.key('x')
    auth = flow.paid.authorize(run, source, None)
    flow.paid.reserve(run, source, None, {'actor': 'fixture'}, 50000, 'Synthetic reservation',
                      authorization_hash=auth['authorization_hash'])
    before = copy.deepcopy(flow.journal.read()[0])
    with pytest.raises(ValueError, match='before the first capture'):
        flow.research.bind_search_context(run, CO)
    assert flow.journal.read()[0] == before


@pytest.mark.parametrize('context', [{}, {'country_code': 'co', 'language_code': 'es'},
    {'country_code': 'COL', 'language_code': 'es'}, {'country_code': True, 'language_code': 'es'},
    {'country_code': 'CO', 'language_code': 'spa'}, {'country_code': 'CO', 'language_code': 'es-co'},
    {'country_code': 'CO', 'language_code': 'es', 'location': 'Bogotá'}, None])
def test_malformed_framing_is_rejected_without_mutating_records(context):
    flow, _, run, _ = research()
    before = copy.deepcopy(flow.journal.read()[0])
    with pytest.raises(ValueError):
        SearchContext.model_validate(context)
    with pytest.raises(ValueError):
        flow.research.bind_search_context(run, context)
    assert flow.journal.read()[0] == before


def test_missing_or_ended_run_cannot_accept_framing():
    flow, _, run, _ = research()
    with pytest.raises(ValueError, match='owned active'):
        flow.research.bind_search_context(SCOPE.key('missing'), CO)
    flow.journal.transact(lambda records, state: records.upsert('ai_mi_research_runs', {
        **records.get('ai_mi_research_runs', run), 'state': 'cancelled'}))
    before = copy.deepcopy(flow.journal.read()[0])
    with pytest.raises(ValueError, match='owned active'):
        flow.research.bind_search_context(run, CO)
    assert flow.journal.read()[0] == before


def test_inconsistent_stored_framing_is_not_silently_preferred():
    flow, _, run, _ = research(CO)
    row = flow.records().get('ai_mi_research_runs', run)
    row['decision_log_json'] = json.dumps({'decisions': [], 'search_context': MX})
    with pytest.raises(ValueError, match='differs'):
        research_search_context(row)
