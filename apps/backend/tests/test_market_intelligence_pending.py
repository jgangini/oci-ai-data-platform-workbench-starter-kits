"""Short provider runs complete within one turn; long runs retain their identity."""
import pytest

pytest.importorskip('langgraph')
from test_market_intelligence_workflow import configured, turn
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import ProviderPending


@pytest.mark.parametrize('ready_after', [2, 5, 8, 16, 31, 61, None])
def test_polling_never_restarts_capture_and_pending_reuses_the_same_run(ready_after, monkeypatch):
    flow, model, provider, graph = configured()
    original_start, original_ask = provider.start, model.ask
    reads = []
    delays = []
    async def sleep(seconds):
        delays.append(seconds)
    monkeypatch.setattr('asyncio.sleep', sleep)
    complete = False
    async def start(call):
        result = await original_start(call)
        return {**result, 'status': 'RUNNING'}
    async def read(run_id):
        reads.append(run_id)
        ready = complete or ready_after is not None and len(reads) >= ready_after
        return {'runId': run_id, 'status': 'SUCCEEDED' if ready else 'RUNNING'}
    async def ask(name, schema, payload):
        result = await original_ask(name, schema, payload)
        if name == 'assess_evidence':
            result.update(continue_research=False, gap='')
        return result
    provider.start, provider.read, model.ask = start, read, ask
    if ready_after is None:
        with pytest.raises(ProviderPending):
            turn(graph, 'poll-test')
        assert len(reads) == 61 and len(provider.starts) == 1 and delays == [2] * 60
        complete = True
    result = turn(graph, 'poll-test')
    assert len(provider.starts) == 1 and len(set(reads)) == 1
    assert result['response']['report_id']
    assert flow.records().rows('ai_mi_tool_runs')[0]['state'] == 'succeeded'
    assert len(reads) == (62 if ready_after is None else ready_after)


def test_polling_stops_at_its_elapsed_deadline_even_when_reads_are_slow(monkeypatch):
    import asyncio
    flow, _, provider, _ = configured()
    clock = iter([100, 161])
    monkeypatch.setattr('app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow.monotonic', lambda: next(clock))
    reads = []
    async def read(run_id):
        reads.append(run_id)
        return {'runId': run_id, 'status': 'RUNNING'}
    async def no_sleep(*args):
        pytest.fail('The deadline already elapsed')
    provider.read = read
    monkeypatch.setattr('asyncio.sleep', no_sleep)
    result = asyncio.run(flow.poll_provider('existing-run', max_wait_seconds=60))
    assert result['status'] == 'RUNNING' and reads == ['existing-run']
    assert not provider.starts


def test_polling_does_not_start_another_read_when_sleep_wakes_at_the_deadline(monkeypatch):
    import asyncio
    flow, _, provider, _ = configured()
    clock = iter([100, 101, 160])
    monkeypatch.setattr('app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow.monotonic', lambda: next(clock))
    reads, delays = [], []
    async def read(run_id):
        reads.append(run_id)
        return {'runId': run_id, 'status': 'RUNNING'}
    async def sleep(seconds):
        delays.append(seconds)
    provider.read = read
    monkeypatch.setattr('asyncio.sleep', sleep)
    result = asyncio.run(flow.poll_provider('existing-run', max_wait_seconds=60))
    assert result['status'] == 'RUNNING' and reads == ['existing-run'] and delays == [2]


@pytest.mark.parametrize('wait', [0, -1, True, 61, 3600])
def test_polling_does_not_accept_an_unbounded_or_unreviewed_wait(wait):
    import asyncio
    flow, _, provider, _ = configured()
    with pytest.raises(ValueError, match='admitted provider timeout'):
        asyncio.run(flow.poll_provider('existing-run', max_wait_seconds=wait))
    assert not provider.starts
