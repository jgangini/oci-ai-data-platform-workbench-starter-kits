"""Synthetic status failures resume the real graph without another paid start."""
import asyncio
import copy
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import native
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import (
    ProviderPending, ProviderReceiptUnverified, ProviderStatusUnavailable)
from test_market_intelligence_workflow import configured, turn, THREAD, SCOPE


QUESTION = 'Investiga las novedades de D1 en Colombia de los últimos siete días.'


def interrupted_capture(error):
    flow, model, provider, graph = configured()
    start = provider.start
    reads = []

    async def start_second_running(call):
        receipt = await start(call)
        return {**receipt, 'status': 'RUNNING'} if len(provider.starts) == 2 else receipt

    async def read_existing(run_id):
        if run_id == 'synthetic-1':
            return {'runId': run_id, 'status': 'SUCCEEDED', 'spent_microusd': 1000}
        reads.append(run_id)
        if len(reads) == 1:
            if isinstance(error, Exception):
                raise error
            return error
        return {'runId': run_id, 'status': 'SUCCEEDED', 'spent_microusd': 1000}

    provider.start, provider.read = start_second_running, read_existing
    return flow, model, provider, graph, reads


@pytest.mark.parametrize('error, expected', [
    (ProviderStatusUnavailable('Unverified provider error'), ProviderStatusUnavailable),
    (ValueError('Receipt identity mismatch'), ProviderReceiptUnverified),
    ({'runId': 'unrelated', 'status': 'SUCCEEDED'}, ProviderReceiptUnverified),
    ({'runId': 'synthetic-2', 'status': 'UNKNOWN'}, ProviderReceiptUnverified),
    ({'runId': 'synthetic-2', 'status': {}}, ProviderReceiptUnverified),
    ({'runId': 'synthetic-2', 'status': []}, ProviderReceiptUnverified),
    ({'runId': 'synthetic-2', 'status': False}, ProviderReceiptUnverified),
    (None, ProviderReceiptUnverified),
])
def test_same_request_recovers_second_capture_preserving_evidence_receipts_and_unknown_cost(error, expected):
    flow, model, provider, graph, reads = interrupted_capture(error)
    with pytest.raises(expected):
        turn(graph, 'status-resume', QUESTION)
    before = copy.deepcopy(flow.journal.read()[0])
    assert len(provider.starts) == 2 and reads == ['synthetic-2']
    assert len(before['tables']['ai_mi_documents']) == 1
    assert not before['tables']['ai_mi_reports']
    pending = next(row for row in before['operations'].values() if row['provider_run_id'] == 'synthetic-2')
    assert pending['state'] == 'running' and pending.get('spent_microusd') is None
    assert asyncio.run(graph.aget_state(THREAD)).next == ('reconcile_step',)
    understand_calls = sum(name == 'understand' for name, _ in model.calls)

    result = turn(graph, 'status-resume', QUESTION)
    after = flow.journal.read()[0]
    assert reads == ['synthetic-2', 'synthetic-2'] and len(provider.starts) == 2
    assert sum(name == 'understand' for name, _ in model.calls) == understand_calls
    assert len(after['tables']['ai_mi_questions']) == len(after['tables']['ai_mi_research_runs']) == 1
    assert len(after['tables']['ai_mi_documents']) == 2 and len(after['tables']['ai_mi_reports']) == 1
    for key, old in before['operations'].items():
        current = after['operations'][key]
        assert current['provider_run_id'] == old['provider_run_id'] and current['permit'] == old['permit']
        assert current['spent_microusd'] == 1000
    for key, old in before['tables']['ai_mi_documents'].items():
        assert after['tables']['ai_mi_documents'][key] == old
    assert result['response']['references'] == ['https://example.com/reference', 'https://x.com/fixture/status/1']
    assert turn(graph, 'status-resume', QUESTION)['response'] == result['response']
    assert flow.journal.read()[0] == after and len(provider.starts) == 2


@pytest.mark.parametrize('error', [PermissionError('authorization'), RuntimeError('transport')])
def test_other_status_exceptions_are_not_reported_as_verified_running(error):
    flow, _, provider, graph, _ = interrupted_capture(error)
    with pytest.raises(type(error)) as raised:
        turn(graph, 'status-unclassified', QUESTION)
    assert raised.value is error and not isinstance(raised.value, ProviderPending)
    value = flow.journal.read()[0]
    assert len(provider.starts) == 2 and not value['tables']['ai_mi_reports']
    assert len(value['tables']['ai_mi_documents']) == 1


@pytest.mark.parametrize('error, reason, phrase', [
    (ProviderStatusUnavailable('private provider diagnostic'), 'status_unavailable', 'No pude consultar el estado'),
    (ValueError('private invalid receipt'), 'receipt_unverified', 'No pude confirmar'),
    (ProviderPending('verified running'), 'running', 'La búsqueda sigue en curso'),
])
def test_native_stream_returns_truthful_pending_and_resumes_exact_operation(monkeypatch, error, reason, phrase):
    flow, model, provider, _, reads = interrupted_capture(error)
    saver = InMemorySaver()
    monkeypatch.setattr(native, 'ObjectSnapshot', lambda *args: object())
    monkeypatch.setattr(native, 'Journal', lambda *args: flow.journal)
    monkeypatch.setattr(native, 'MarketWorkflow', lambda *args, **kwargs: flow)
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import memory
    monkeypatch.setattr(memory, 'native_memory', lambda *args: saver)
    runtime = native.AgentRuntime()
    runtime.config = {'tenant_id': SCOPE.tenant_id, 'generation_id': SCOPE.generation_id,
        'namespace': 'synthetic', 'bucket': 'synthetic', 'budget_microusd': 1000000, 'timezone': 'UTC',
        'run_cap_microusd': 100000, 'step_cap_microusd': 50000, 'usage_cost_verified': True, 'research_enabled': True}
    runtime.model, runtime.provider, runtime.storage = model, provider, object()
    runtime.runtime_binding = flow.research.runtime_binding
    command = json.dumps({'request_id': 'native-status-resume', 'question': QUESTION})

    async def invoke():
        return [event async for event in runtime.events(command, THREAD, {'subject_id': 'synthetic-owner'})]

    first = asyncio.run(invoke())
    response = first[-1]['response']
    assert response['state'] == 'pending' and response['pending_reason'] == reason
    assert response['request_id'] == 'native-status-resume' and phrase in response['markdown']
    assert 'private' not in response['markdown'] and 'Apify' not in response['markdown']
    if reason != 'running':
        assert 'sigue en curso' not in response['markdown']
    assert reads == ['synthetic-2'] and len(provider.starts) == 2
    assert len(flow.records().rows('ai_mi_documents')) == 1
    second = asyncio.run(invoke())
    assert second[-1]['response']['report_id']
    assert reads == ['synthetic-2', 'synthetic-2'] and len(provider.starts) == 2
    assert len(flow.records().rows('ai_mi_reports')) == 1
