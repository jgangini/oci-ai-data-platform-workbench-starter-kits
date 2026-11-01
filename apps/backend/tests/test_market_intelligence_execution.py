import asyncio

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import PaidOperations, execute_selected
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Journal, utc_now
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.research import json_text
from test_market_intelligence_repository import MemorySnapshot, SCOPE


def prepared(limit=100000, run_limit=100000):
    journal = Journal(MemorySnapshot(), limit)
    run = SCOPE.key('research-1')
    source = SCOPE.key('source-x')

    def initial(records, state):
        records.upsert('ai_mi_sources', {'source_key': source, 'status': 'active',
            'connector_revision': 'x_search', 'allowed_scope_json': json_text({'allow_discovery': True})})
        records.upsert('ai_mi_research_runs', {'run_id': run, 'state': 'running',
            'reserved_microusd': run_limit, 'completion_policy_json': json_text({'max_steps': 4, 'max_elapsed_seconds': 300}),
            'started_finished_at_json': json_text({'started_at': utc_now()})})

    journal.transact(initial)
    return PaidOperations(journal), run, source


def test_same_request_is_reserved_and_started_only_once():
    ledger, run, source = prepared()
    key = ledger.reserve(run, source, None, {'actor': 'reviewed-x'}, 10000, 'Find current mentions', authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    assert ledger.reserve(run, source, None, {'actor': 'reviewed-x'}, 10000, 'Find current mentions', authorization_hash=ledger.authorize(run, source, None)['authorization_hash']) == key
    assert ledger.claim_start(key)
    assert ledger.claim_start(key) is None


def test_uncertain_start_cannot_be_retried_or_bypassed_with_a_new_request():
    ledger, run, source = prepared()
    args = {'actor': 'reviewed-x'}
    key = ledger.reserve(run, source, None, args, 10000, 'Find current mentions', authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    calls = []

    async def timeout(arguments):
        calls.append(arguments)
        raise TimeoutError('response was lost after provider start')

    with pytest.raises(RuntimeError, match='uncertain'):
        asyncio.run(execute_selected(ledger, key, args, timeout))
    assert asyncio.run(execute_selected(ledger, key, args, timeout))['state'] == 'reconcile'
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match='reconciled first'):
        ledger.reserve(run, source, None, {'actor': 'different'}, 10000, 'Another gap', authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])


def test_different_arguments_cannot_use_an_existing_permit():
    ledger, run, source = prepared()
    key = ledger.reserve(run, source, None, {'actor': 'reviewed-x'}, 10000, 'Find current mentions', authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])

    async def forbidden(arguments):
        pytest.fail('A changed actor must never start')

    with pytest.raises(ValueError, match='admitted request'):
        asyncio.run(execute_selected(ledger, key, {'actor': 'other'}, forbidden))
    assert ledger.journal.read()[0]['operations'][key]['state'] == 'reserved'


@pytest.mark.parametrize('global_cap,run_cap', [(9999, 100000), (100000, 9999)])
def test_global_and_per_question_budget_are_enforced_before_start(global_cap, run_cap):
    ledger, run, source = prepared(global_cap, run_cap)
    with pytest.raises(RuntimeError, match='budget exhausted'):
        ledger.reserve(run, source, None, {'actor': 'reviewed-x'}, 10000, 'Find current mentions', authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    assert ledger.journal.read()[0]['operations'] == {}


def test_unknown_cost_keeps_reservation_until_measured():
    ledger, run, source = prepared(10000)
    key = ledger.reserve(run, source, None, {'actor': 'reviewed-x'}, 10000, 'Find current mentions', authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    permit = ledger.claim_start(key)
    ledger.record_provider(key, permit, 'provider-run-1', 'succeeded', {'status': 'SUCCEEDED'})
    with pytest.raises(RuntimeError, match='budget exhausted'):
        ledger.reserve(run, source, None, {'actor': 'another'}, 1000, 'Contrast', authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    ledger.record_cost(key, 'provider-run-1', 2000)
    assert ledger.reserve(run, source, None, {'actor': 'another'}, 1000, 'Contrast', authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])


@pytest.mark.parametrize('status, expected_state', [('FAILED', 'failed'), ('ABORTED', 'aborted'), ('TIMED-OUT', 'timed_out')])
@pytest.mark.parametrize('cost', [0, 2000, None, 10001], ids=['zero', 'measured', 'unknown', 'overrun'])
def test_terminal_start_receipts_persist_measured_cost_and_keep_unknown_or_exceeded_budgets_blocked(status, expected_state, cost):
    ledger, run, source = prepared(limit=10000)
    arguments = {'actor': 'reviewed-x'}
    authorization = ledger.authorize(run, source, None)['authorization_hash']
    tool = ledger.reserve(run, source, None, arguments, 10000, 'Find current mentions', authorization_hash=authorization)
    calls = []
    async def terminal(arguments):
        calls.append(arguments)
        return {'runId': 'terminal-provider-run', 'status': status, 'spent_microusd': cost}
    result = asyncio.run(execute_selected(ledger, tool, arguments, terminal))
    assert result['status'] == status and calls == [arguments]
    value, _ = ledger.journal.read()
    operation = value['operations'][tool]
    row = value['tables']['ai_mi_tool_runs'][tool]
    assert operation['provider_run_id'] == row['provider_run_id'] == 'terminal-provider-run'
    assert operation['spent_microusd'] == row['spent_microusd'] == cost
    if cost is not None and cost > 10000:
        assert operation['state'] == row['state'] == 'uncertain'
        assert operation['cost_overrun'] is True
        with pytest.raises(RuntimeError, match='reconciled first'):
            ledger.reserve(run, source, None, {'actor': 'contrast'}, 1000, 'Contrast', authorization_hash=authorization)
    else:
        assert operation['state'] == row['state'] == expected_state
        if cost is None:
            with pytest.raises(RuntimeError, match='budget exhausted'):
                ledger.reserve(run, source, None, {'actor': 'contrast'}, 1000, 'Contrast', authorization_hash=authorization)
        else:
            assert ledger.reserve(run, source, None, {'actor': 'contrast'}, 1000, 'Contrast', authorization_hash=authorization)


def test_observed_overrun_blocks_further_starts():
    ledger, run, source = prepared()
    key = ledger.reserve(run, source, None, {}, 10000, 'Find current mentions', authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    permit = ledger.claim_start(key)
    ledger.record_provider(key, permit, 'provider-run-1', 'succeeded', {})
    ledger.record_cost(key, 'provider-run-1', 10001)
    with pytest.raises(ValueError, match='overrun'):
        ledger.record_provider(key, permit, 'provider-run-1', 'succeeded', {})
    with pytest.raises(RuntimeError, match='reconciled first'):
        ledger.reserve(run, source, None, {'new': True}, 1000, 'Contrast', authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])


def test_previous_reservation_cannot_bypass_an_uncertain_start():
    ledger, run, source = prepared()
    first = ledger.reserve(run, source, None, {'q': 'first'}, 10000, 'First gap', authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    second = ledger.reserve(run, source, None, {'q': 'second'}, 10000, 'Second gap', authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    permit = ledger.claim_start(first)
    ledger.mark_uncertain(first, permit)
    with pytest.raises(RuntimeError, match='reconciled first'):
        ledger.claim_start(second)
    assert ledger.journal.read()[0]['operations'][second]['state'] == 'reserved'


@pytest.mark.parametrize('change', ['close_run', 'disable_source', 'expire_run'])
def test_start_revalidates_the_current_authorization_and_deadline(change):
    ledger, run, source = prepared()
    key = ledger.reserve(run, source, None, {}, 10000, 'Find current mentions', authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])

    def mutate(records, state):
        if change == 'disable_source':
            row = records.get('ai_mi_sources', source)
            records.upsert('ai_mi_sources', {**row, 'status': 'disabled'})
        else:
            row = records.get('ai_mi_research_runs', run)
            fields = {'state': 'closed'} if change == 'close_run' else {
                'started_finished_at_json': json_text({'started_at': '2020-01-01T00:00:00+00:00'})}
            records.upsert('ai_mi_research_runs', {**row, **fields})

    ledger.journal.transact(mutate)
    with pytest.raises((ValueError, RuntimeError), match='admitted|active|time limit'):
        ledger.claim_start(key)
    assert ledger.journal.read()[0]['operations'][key]['state'] == 'reserved'


def test_revoking_a_source_blocks_new_starts_but_allows_an_existing_charge_to_reconcile():
    ledger, run, source = prepared()
    key = ledger.reserve(run, source, None, {}, 10000, 'Find current mentions',
                         authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    permit = ledger.claim_start(key)
    ledger.mark_uncertain(key, permit)

    def revoke(records, state):
        row = records.get('ai_mi_sources', source)
        records.upsert('ai_mi_sources', {**row, 'status': 'disabled'})

    ledger.journal.transact(revoke)
    assert ledger.claim_start(key) is None
    ledger.record_provider(key, permit, 'existing-provider-run', 'succeeded', {})
    ledger.record_cost(key, 'existing-provider-run', 2000)
    operation = ledger.journal.read()[0]['operations'][key]
    assert operation['state'] == 'succeeded'
    assert operation['spent_microusd'] == 2000
    with pytest.raises(ValueError, match='active reviewed connector'):
        ledger.authorize(run, source, None)
