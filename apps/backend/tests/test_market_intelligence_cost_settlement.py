"""Synthetic accounting receipts reproduce preliminary and later costs without paid calls."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import provider
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import (
    AdmissionBlocked, PaidOperations, ProviderReceiptUnverified, ProviderStatusUnavailable,
)
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow import MarketWorkflow
from test_market_intelligence_research import running


def run(cost, **fields):
    return {'runId': 'existing-run', 'status': 'SUCCEEDED', 'defaultDatasetId': 'existing-dataset',
            'usageTotalUsd': cost, 'finishedAt': '2026-01-01T00:00:00Z', **fields}


class Tool:
    def __init__(self, results):
        self.results, self.calls = list(results), []

    async def ainvoke(self, arguments):
        self.calls.append(arguments)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


def client(*, start=(), reads=(), items=()):
    tools = {'call-actor': Tool(start), 'get-actor-run': Tool(reads), 'get-dataset-items': Tool(items)}
    return provider.ApifyProvider(tools), tools


def test_start_retains_identity_but_does_not_release_reservation_from_preliminary_cost():
    native, tools = client(start=[run(0.00005)])
    result = asyncio.run(native.start({'actor': 'reviewed/actor'}))
    assert result == {'runId': 'existing-run', 'status': 'SUCCEEDED',
                      'datasetId': 'existing-dataset', 'spent_microusd': None}
    assert len(tools['call-actor'].calls) == 1
    assert not tools['get-actor-run'].calls


@pytest.mark.parametrize('finished', [None, 'recent'])
def test_recent_or_undated_terminal_receipt_is_read_again_after_documented_interval(monkeypatch, finished):
    delays = []
    async def wait(delay):
        delays.append(delay)
    monkeypatch.setattr(asyncio, 'sleep', wait)
    first = run(0.00005, finishedAt=datetime.now(timezone.utc).isoformat() if finished == 'recent' else finished)
    native, tools = client(reads=[first, run(0.00155)])
    assert asyncio.run(native.read('existing-run'))['spent_microusd'] == 1550
    assert len(delays) == 1 and 0 < delays[0] <= 10
    assert len(tools['get-actor-run'].calls) == 2
    assert not tools['call-actor'].calls


def test_old_terminal_receipt_does_not_add_an_unnecessary_wait(monkeypatch):
    async def unexpected_wait(delay):
        pytest.fail('Already-aged terminal receipt should not wait again')
    monkeypatch.setattr(asyncio, 'sleep', unexpected_wait)
    native, tools = client(reads=[run(0.00155)])
    assert asyncio.run(native.read('existing-run'))['spent_microusd'] == 1550
    assert len(tools['get-actor-run'].calls) == 1


@pytest.mark.parametrize('finished', [12, 'not-a-date', '2026-01-01T00:00:00',
    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()])
def test_unverified_completion_timestamp_cannot_release_cost(finished):
    native, tools = client(reads=[run(0.00005, finishedAt=finished)])
    with pytest.raises(ProviderReceiptUnverified):
        asyncio.run(native.read('existing-run'))
    assert len(tools['get-actor-run'].calls) == 1
    assert not tools['call-actor'].calls and not tools['get-dataset-items'].calls


@pytest.mark.parametrize('kind', ['invalid', 'naive', 'future', 'recent'])
def test_second_accounting_response_must_also_pass_completion_validation(monkeypatch, kind):
    async def wait(delay):
        pass
    monkeypatch.setattr(asyncio, 'sleep', wait)
    finished = {'invalid': 'not-a-date', 'naive': '2026-01-01T00:00:00',
        'future': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        'recent': datetime.now(timezone.utc).isoformat()}[kind]
    native, tools = client(reads=[run(0.00005, finishedAt=None), run(0.00155, finishedAt=finished)])
    with pytest.raises((ValueError, ProviderReceiptUnverified)):
        asyncio.run(native.read('existing-run'))
    assert len(tools['get-actor-run'].calls) == 2
    assert not tools['call-actor'].calls and not tools['get-dataset-items'].calls


def test_absent_completion_time_uses_later_read_without_inventing_provider_time(monkeypatch):
    delays = []
    async def wait(delay):
        delays.append(delay)
    monkeypatch.setattr(asyncio, 'sleep', wait)
    native, tools = client(reads=[run(0.00005, finishedAt=None), run(0.00155, finishedAt=None)])
    result = asyncio.run(native.read('existing-run'))
    assert result['spent_microusd'] == 1550 and 'finishedAt' not in result
    assert delays == [10] and len(tools['get-actor-run'].calls) == 2


@pytest.mark.parametrize('invalid_content', [False, True])
def test_controller_saves_accounting_before_consuming_content(invalid_content):
    research, rid, tool = running()
    row = {'url': 'https://127.0.0.1/a' if invalid_content else 'https://example.com/a', 'text': 'Retrieved text'}
    native, tools = client(reads=[run(0.00155, runId='provider-1')], items=[[row]])
    flow = MarketWorkflow(research.journal, None, native)
    state = {'run_id': rid, 'tool_id': tool,
        'provider': {'runId': 'provider-1', 'status': 'SUCCEEDED', 'datasetId': 'existing-dataset'},
        'call': {'actor': 'reviewed/actor'}}
    state.update(asyncio.run(flow.reconcile_step(state)))
    result = asyncio.run(flow.normalize_evidence(state))
    assert research.journal.read()[0]['operations'][tool]['spent_microusd'] == 1550
    assert len(research.evidence(rid)) == (0 if invalid_content else 1)
    assert bool(result.get('connector_issue')) is invalid_content
    assert len(tools['get-actor-run'].calls) == 1 and len(tools['get-dataset-items'].calls) == 1
    assert not tools['call-actor'].calls


def test_missing_dataset_stays_in_reconciliation_and_later_cost_change_cannot_be_lost():
    research, rid, tool = running()
    native, tools = client(reads=[run(0.00155, runId='provider-1', defaultDatasetId=None),
                                run(0.0018, runId='provider-1')])
    flow = MarketWorkflow(research.journal, None, native)
    state = {'run_id': rid, 'tool_id': tool, 'call': {'actor': 'reviewed/actor'}}
    with pytest.raises(ProviderReceiptUnverified, match='no verified dataset'):
        asyncio.run(flow.reconcile_step(state))
    assert research.journal.read()[0]['operations'][tool]['spent_microusd'] == 1550
    with pytest.raises(ProviderReceiptUnverified, match='discrepancy is recorded'):
        asyncio.run(flow.reconcile_step(state))
    operation = research.journal.read()[0]['operations'][tool]
    assert operation['state'] == 'uncertain' and operation['cost_discrepancy']['observed_microusd'] == 1800
    assert not research.evidence(rid)
    assert not tools['call-actor'].calls and not tools['get-dataset-items'].calls


def test_accounting_read_failure_remains_pending_without_content_or_new_start():
    native, tools = client(reads=[{'error': 'private diagnostic'}])
    with pytest.raises(ProviderStatusUnavailable):
        asyncio.run(native.read('existing-run'))
    assert not tools['get-dataset-items'].calls and not tools['call-actor'].calls


@pytest.mark.parametrize('error',[ValueError('tool validation failed'),RuntimeError('transport failed')])
def test_second_accounting_read_preserves_the_original_tool_exception(monkeypatch,error):
    async def wait(delay):
        pass
    monkeypatch.setattr(asyncio,'sleep',wait)
    native,tools=client(reads=[run(0.00005,finishedAt=None),error])
    with pytest.raises(type(error)) as raised:
        asyncio.run(native.read('existing-run'))
    assert raised.value is error
    assert len(tools['get-actor-run'].calls)==2
    assert not tools['call-actor'].calls and not tools['get-dataset-items'].calls


def test_second_accounting_read_preserves_known_capture_when_usage_metadata_disagrees(monkeypatch):
    async def wait(delay):
        pass
    monkeypatch.setattr(asyncio,'sleep',wait)
    later={'structuredContent':run(0.00155),
           '_meta':{'com.apify/ActorRun':{'runId':'another-run','usageTotalUsd':0.00155}}}
    native,tools=client(reads=[run(0.00005,finishedAt=None),later])
    with pytest.raises(ProviderReceiptUnverified,match='same capture'):
        asyncio.run(native.read('existing-run'))
    assert tools['get-actor-run'].calls==[{'runId':'existing-run','waitSecs':0}]*2
    assert not tools['call-actor'].calls and not tools['get-dataset-items'].calls


def test_missing_cost_keeps_reservation_and_waits_for_same_run():
    native, tools = client(reads=[run(None)])
    with pytest.raises(ProviderReceiptUnverified, match='reservation remains held'):
        asyncio.run(native.read('existing-run'))
    assert not tools['get-dataset-items'].calls and not tools['call-actor'].calls


def test_normalization_does_not_swallow_pending_accounting():
    research, rid, tool = running()
    class PendingProvider:
        async def observations(self, *args):
            raise ProviderReceiptUnverified('Accounting unavailable')
    flow = MarketWorkflow(research.journal, None, PendingProvider())
    before = research.journal.read()[0]
    with pytest.raises(ProviderReceiptUnverified):
        asyncio.run(flow.normalize_evidence({'run_id': rid, 'tool_id': tool, 'provider': {'runId': 'provider-1'}, 'call': {}}))
    assert research.journal.read()[0] == before


def test_changed_cost_persists_blocking_discrepancy_before_reporting_error():
    research, rid, tool = running()
    paid = PaidOperations(research.journal)
    paid.record_cost(tool, 'provider-1', 50)
    with pytest.raises(ProviderReceiptUnverified, match='discrepancy is recorded'):
        paid.record_cost(tool, 'provider-1', 1550)
    value, _ = research.journal.read()
    operation = value['operations'][tool]
    assert operation['state'] == 'uncertain' and operation['spent_microusd'] == 50
    assert operation['cost_discrepancy'] == {'previous_microusd': 50, 'observed_microusd': 1550, 'provider_state': 'succeeded'}
    row = value['tables']['ai_mi_tool_runs'][tool]
    assert row['state'] == 'uncertain' and row['spent_microusd'] == 50
    auth = paid.authorize(rid, row['source_key'], None)
    with pytest.raises(AdmissionBlocked, match='uncertain'):
        paid.reserve(rid, row['source_key'], None, {'new': 'query'}, 1000, 'A different query', authorization_hash=auth['authorization_hash'])
    with pytest.raises(ValueError, match='reconciliation'):
        paid.record_provider(tool, operation['permit'], 'provider-1', 'succeeded', {})
    assert research.journal.read()[0] == value


def test_cost_from_another_run_cannot_mutate_accounting():
    research, _, tool = running()
    paid = PaidOperations(research.journal)
    before = research.journal.read()[0]
    with pytest.raises(ValueError):
        paid.record_cost(tool, 'another-run', 1550)
    assert research.journal.read()[0] == before


def test_real_graph_resumes_accounting_read_without_duplicate_capture_or_intake():
    from test_market_intelligence_workflow import configured, turn, THREAD
    flow, model, _, graph = configured()
    starts, reads = [], []
    datasets = {}
    class NativeTool:
        def __init__(self, name):
            self.name = name
        async def ainvoke(self, arguments):
            if self.name == 'call-actor':
                starts.append(copy.deepcopy(arguments))
                run_id = 'run-' + str(len(starts))
                datasets[run_id] = arguments['actor']
                return run(0.00005, runId=run_id, defaultDatasetId=run_id)
            if self.name == 'get-actor-run':
                reads.append(arguments['runId'])
                if len(reads) == 1:
                    return {'error': ''}
                return run(0.00155, runId=arguments['runId'], defaultDatasetId=arguments['runId'])
            if datasets[arguments['datasetId']] == 'apify/website-content-crawler':
                return [{'url': 'https://example.com/reference', 'text': 'Synthetic reference',
                         'crawl': {'loadedUrl': 'https://example.com/reference', 'depth': 0}}]
            return [{'url': 'https://x.com/fixture/status/1', 'text': 'Synthetic opinion',
                     'createdAt': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}]
    flow.provider = provider.ApifyProvider({name: NativeTool(name) for name in ('call-actor', 'get-actor-run', 'get-dataset-items')})
    with pytest.raises(ProviderStatusUnavailable):
        turn(graph, 'accounting-recovery')
    before = flow.journal.read()[0]
    first = next(iter(before['operations'].values()))
    assert len(starts) == 1 and first['provider_run_id'] == 'run-1' and first['spent_microusd'] is None
    assert not before['tables']['ai_mi_reports']
    assert asyncio.run(graph.aget_state(THREAD)).next == ('reconcile_step',)
    intake_calls = sum(name == 'understand' for name, _ in model.calls)
    result = turn(graph, 'accounting-recovery')
    after = flow.journal.read()[0]
    assert len(starts) == 2 and reads == ['run-1', 'run-1', 'run-2']
    assert all(op['spent_microusd'] == 1550 for op in after['operations'].values())
    assert sum(name == 'understand' for name, _ in model.calls) == intake_calls
    assert len(after['tables']['ai_mi_questions']) == len(after['tables']['ai_mi_research_runs']) == 1
    assert len(after['tables']['ai_mi_documents']) == 2 and len(after['tables']['ai_mi_reports']) == 1
    assert turn(graph, 'accounting-recovery')['response'] == result['response']
    assert flow.journal.read()[0] == after and len(starts) == 2
