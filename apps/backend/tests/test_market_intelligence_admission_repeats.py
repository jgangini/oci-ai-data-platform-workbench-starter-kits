"""Changing arguments must not recapture an exhausted registered target."""
import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import PaidOperations, TargetAlreadyInvestigated
from test_market_intelligence_financial_flow import financial_target
from test_market_intelligence_execution import prepared
from test_market_intelligence_workflow import configured, turn


@pytest.mark.parametrize('first_state', ['reserved', 'succeeded', 'failed'])
def test_registered_target_cannot_be_reserved_again_with_different_arguments(first_state):
    research, run, _, source, target, _ = financial_target()
    research.journal.transact(lambda rows, state: rows.upsert('ai_mi_sources', {
        **rows.get('ai_mi_sources', source), 'connector_revision': 'yahoo_history'}))
    paid = PaidOperations(research.journal)
    authority = paid.authorize(run, source, target)['authorization_hash']
    first = paid.reserve(run, source, target, {'limit': 5}, 10000, 'First sample', authorization_hash=authority)
    if first_state != 'reserved':
        permit = paid.claim_start(first)
        paid.record_provider(first, permit, 'synthetic-first-run', first_state, {})
        paid.record_cost(first, 'synthetic-first-run', 1000)
    before = research.journal.read()[0]
    with pytest.raises(TargetAlreadyInvestigated):
        paid.reserve(run, source, target, {'limit': 10}, 10000, 'Same destination again', authorization_hash=authority)
    assert research.journal.read()[0] == before
    assert paid.reserve(run, source, target, {'limit': 5}, 10000, 'Replay', authorization_hash=authority) == first


def test_global_discovery_can_still_admit_distinct_bounded_queries():
    paid, run, source = prepared()
    authority = paid.authorize(run, source, None)['authorization_hash']
    first = paid.reserve(run, source, None, {'query': 'D1 Colombia precios'}, 10000, 'Prices', authorization_hash=authority)
    second = paid.reserve(run, source, None, {'query': 'D1 Colombia calidad'}, 10000, 'Quality', authorization_hash=authority)
    assert first != second


def test_graph_closes_with_gap_after_registered_base_is_exhausted():
    flow, model, provider, graph = configured()
    def disable_discovery(rows, state):
        for row in rows.rows('ai_mi_sources'):
            if row['connector_revision'] == 'x_search':
                rows.upsert('ai_mi_sources', {**row, 'status': 'disabled'})
    flow.journal.transact(disable_discovery)
    result = turn(graph, 'registered-base-exhausted')
    assert result['reason'] == 'unresolved'
    assert len(provider.starts) == 1
    assert model.steps == 1
    assert len(flow.records().rows('ai_mi_reports')) == 1
    assert len(flow.records().rows('ai_mi_documents')) == 1
