"""A full provider page must not require one unbounded structured model response."""
import asyncio

import pytest

pytest.importorskip('langgraph')
from test_market_intelligence_workflow import configured, turn, THREAD
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.model import evidence_batches


def test_assessment_and_draft_receive_selected_sources_and_only_this_runs_attempts():
    flow, model, provider, graph = configured()
    original = model.ask
    seen = []

    async def ask(name, schema, payload):
        if name in {'assess_evidence', 'draft_report'}:
            choices = {row['profile_id']: row for row in payload['research_source_options']}
            assert choices['website']['attempts'] == 1
            assert choices['x_search']['attempts'] == (0 if len(payload['evidence']) == 1 else 1)
            assert all(set(row) == {'source_key', 'profile_id', 'name', 'attempts'} for row in choices.values())
            seen.append((name, choices['x_search']['attempts']))
        return await original(name, schema, payload)

    model.ask = ask
    first = turn(graph, 'source-coverage-first')
    second = turn(graph, 'source-coverage-second')
    assert first['run_id'] != second['run_id'] and len(provider.starts) == 4
    assert seen == [('assess_evidence', 0), ('assess_evidence', 1), ('draft_report', 1)] * 2


def test_batches_respect_utf8_bytes_without_truncating_quoted_evidence():
    import json
    evidence = [{'evidence_id': str(i), 'text': '\U0001f600' * 8000} for i in range(9)]
    payload = {'question': 'Long text', 'topic': {}, 'window': {}}
    batches = evidence_batches(payload, evidence)
    assert [row for batch in batches for row in batch] == evidence
    assert all(len(batch) <= 8 and len(json.dumps({**payload, 'evidence': batch},
        ensure_ascii=False).encode()) <= 90000 for batch in batches)
    with pytest.raises(ValueError, match='One observation'):
        evidence_batches(payload, [{'text': '\U0001f600' * 25000}])


def test_full_page_is_assessed_in_complete_bounded_batches_with_two_model_calls_at_most():
    flow, model, provider, graph = configured()
    original_ask, original_observations = model.ask, provider.observations
    active = peak = 0
    sizes = []

    async def observations(receipt, call):
        rows, cost = await original_observations(receipt, call)
        return [{**rows[0], 'url': 'https://example.com/reference/' + str(index),
            'text': 'Synthetic observation ' + str(index)} for index in range(49)], cost

    async def ask(name, schema, payload):
        nonlocal active, peak
        if name != 'assess_evidence':
            return await original_ask(name, schema, payload)
        active += 1
        assert payload['corpus_evidence_count'] == 49
        import json
        assert len(json.dumps(payload, ensure_ascii=False).encode()) <= 90000
        peak = max(peak, active)
        sizes.append(len(payload['evidence']))
        await asyncio.sleep(0)
        active -= 1
        return {'assessments': [{'evidence_id': row['evidence_id'], 'stance': 'context',
            'reason': 'Identified synthetic context', 'independence': 'unknown'} for row in payload['evidence']],
            'continue_research': False, 'reason': 'Bounded test complete', 'gap': ''}

    model.ask, provider.observations = ask, observations
    result = turn(graph, 'full-page')
    evidence = flow.research.evidence(result['run_id'])
    assert sizes == [8, 8, 8, 8, 8, 8, 1] and peak == 2
    assert len(evidence) == 49 and all(row['assessment'] is not None for row in evidence)
    assert result['response']['report_id'] and len(provider.starts) == 1


def test_incomplete_assessment_batch_fails_without_publishing_a_success_report():
    flow, model, provider, graph = configured()
    original = model.ask
    async def ask(name, schema, payload):
        result = await original(name, schema, payload)
        if name == 'assess_evidence':
            result['assessments'] = []
        return result
    model.ask = ask
    with pytest.raises(ValueError, match='exactly its evidence batch'):
        turn(graph, 'missing-assessment')
    assert len(provider.starts) == 1 and not flow.records().rows('ai_mi_reports')


def test_failed_batch_drains_siblings_before_returning_in_a_live_event_loop():
    flow, model, provider, graph = configured()
    original_observations = provider.observations
    async def observations(receipt, call):
        rows, cost = await original_observations(receipt, call)
        return [{**rows[0], 'url': 'https://example.com/reference/' + str(i)} for i in range(24)], cost
    provider.observations = observations
    state = asyncio.run(graph.ainvoke({'request_id': 'drain-batches', 'question': 'D1'}, THREAD,
                                     interrupt_before=['assess_evidence']))
    started, stopped = [], []
    async def ask(name, schema, payload):
        number = payload['evidence'][0]['evidence_id']
        started.append(number)
        try:
            if number == started[0]:
                await asyncio.sleep(0)
                raise ValueError('Synthetic model failure')
            await asyncio.Event().wait()
        finally:
            stopped.append(number)
    model.ask = ask
    async def exercise():
        with pytest.raises(ValueError, match='Synthetic model failure'):
            await flow.assess_evidence(state)
        count = len(started)
        await asyncio.sleep(0)
        assert len(started) == count and set(stopped) == set(started)
    asyncio.run(exercise())


def test_two_legal_steps_can_assess_more_than_fifty_observations_and_publish_once():
    flow, model, provider, graph = configured()
    original_ask, original_observations = model.ask, provider.observations
    async def observations(receipt, call):
        rows, cost = await original_observations(receipt, call)
        count = 49 if len(provider.starts) == 1 else 2
        return [{**rows[0], 'url': 'https://example.com/step/' + receipt['runId'] + '/' + str(i),
            'text': 'Identified synthetic item ' + str(i)} for i in range(count)], cost
    async def ask(name, schema, payload):
        if name == 'assess_evidence':
            return {'assessments': [{'evidence_id': row['evidence_id'], 'stance': 'context',
                'reason': 'Synthetic context', 'independence': 'known'} for row in payload['evidence']],
                'continue_research': len(provider.starts) == 1,
                'reason': 'Compare two sources', 'gap': 'Need a second source' if len(provider.starts) == 1 else ''}
        if name == 'draft_report':
            payload = {**payload, 'evidence': payload['evidence'][:3]}
        return await original_ask(name, schema, payload)
    model.ask, provider.observations = ask, observations
    result = turn(graph, 'multi-step-volume')
    evidence = flow.research.evidence(result['run_id'])
    assert len(provider.starts) == 2 and len(evidence) == 51
    assert all(row['assessment'] is not None for row in evidence)
    assert len(flow.records().rows('ai_mi_reports')) == 1 and result['response']['report_id']
