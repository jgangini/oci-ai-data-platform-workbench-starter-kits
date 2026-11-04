"""Checkpoints retain references; quoted evidence is reconstructed from the journal."""
import asyncio
import copy
from contextvars import ContextVar
from threading import Event

import pytest

pytest.importorskip('langgraph')
from test_market_intelligence_workflow import configured, THREAD
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow import MarketWorkflow


MARKER = 'CAPTURED_TEXT_ONLY_IN_THE_AUTHORITATIVE_JOURNAL'


@pytest.fixture
def paused_research():
    flow, model, provider, graph = configured()
    original = provider.observations

    async def observations(receipt, call):
        rows, cost = await original(receipt, call)
        return [{**rows[0], 'url': f'https://example.com/item/{i}', 'text': f'{MARKER} {i}'}
                for i in range(2)], cost

    provider.observations = observations
    state = asyncio.run(graph.ainvoke({'request_id': 'checkpoint-refs', 'question': 'D1'}, THREAD,
                                     interrupt_before=['assess_evidence']))
    return flow, model, provider, graph, state


def test_normalization_checkpoints_only_references_and_resume_rebuilds_without_an_actor(paused_research):
    flow, model, provider, graph, state = paused_research
    durable = flow.research.evidence(state['run_id'])
    assert len(durable) == 2 and all(MARKER in row['text'] for row in durable)
    assert state['evidence'] == []
    assert state['evidence_refs'] == [row['evidence_id'] for row in durable]
    for checkpoint in graph.get_state_history(THREAD):
        assert checkpoint.values.get('evidence', []) == []

    async def no_capture(*args):
        pytest.fail('Resuming contrast must reuse the existing capture')

    provider.start, provider.observations = no_capture, no_capture
    rebuilt = MarketWorkflow(flow.journal, model, provider, run_cap_microusd=100000,
                             step_cap_microusd=50000, usage_cost_verified=True).build(graph.checkpointer)
    result = asyncio.run(rebuilt.ainvoke(None, THREAD))
    assessed = next(payload for name, payload in model.calls if name == 'assess_evidence')
    assert assessed['evidence'] == durable
    assert result['evidence'] == [] and result['evidence_refs'] == state['evidence_refs']
    assert result['response']['report_id'] and len(provider.starts) == 1


@pytest.mark.parametrize('refs', [None, [], ['foreign-evidence'], ['duplicate', 'duplicate']])
def test_missing_or_tampered_references_fail_before_the_model(paused_research, refs):
    flow, model, provider, graph, state = paused_research
    before = len(model.calls)
    graph.update_state(THREAD, {'evidence_refs': refs}, as_node='normalize_evidence')
    with pytest.raises(ValueError, match='Checkpoint evidence references'):
        asyncio.run(graph.ainvoke(None, THREAD))
    assert len(model.calls) == before and len(provider.starts) == 1
    assert not flow.records().rows('ai_mi_reports')


def test_legacy_checkpoint_text_and_ids_are_ignored_when_rebuilding_from_journal(paused_research):
    flow, model, provider, _, state = paused_research
    legacy = copy.deepcopy(state)
    legacy.pop('evidence_refs')
    legacy['evidence'] = [{'evidence_id': 'attacker-selected', 'text': 'Invented checkpoint quote'}]
    result = asyncio.run(flow.assess_evidence(legacy))
    payload = next(payload for name, payload in model.calls if name == 'assess_evidence')
    assert all(MARKER in row['text'] and row['evidence_id'] != 'attacker-selected'
               for row in payload['evidence'])
    assert result['evidence'] == []
    assert result['evidence_refs'] == state['evidence_refs']
    assert len(provider.starts) == 1


def test_legacy_checkpoint_after_assessment_is_cleaned_by_the_next_node_without_rewriting_history(paused_research):
    flow, model, provider, graph, state = paused_research
    assessed = asyncio.run(graph.ainvoke(None, THREAD, interrupt_before=['draft_report']))
    assert graph.get_state(THREAD).next == ('draft_report',)
    legacy_quote = [{'evidence_id': 'untrusted-legacy', 'text': 'LEGACY_CHECKPOINT_QUOTE'}]
    legacy_config = graph.update_state(THREAD, {'evidence': legacy_quote}, as_node='decide_next')
    drafted = asyncio.run(graph.ainvoke(None, THREAD, interrupt_after=['draft_report']))
    assert drafted['evidence'] == [] and drafted['evidence_refs'] == assessed['evidence_refs']
    assert graph.get_state(legacy_config).values['evidence'] == legacy_quote
    result = asyncio.run(graph.ainvoke(None, THREAD))
    assert result['response']['report_id'] and len(provider.starts) == 1
    assert len(flow.research.evidence(state['run_id'])) == 2
    assert all('LEGACY_CHECKPOINT_QUOTE' not in str(payload)
               for _, payload in model.calls)


@pytest.mark.parametrize('other_identity', ['owner', 'generation'])
def test_checkpoint_references_do_not_grant_access_to_another_authenticated_identity(paused_research, other_identity):
    from test_market_intelligence_repository import MemorySnapshot, SCOPE
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Journal, Scope
    _, model, provider, _, state = paused_research
    snapshot = MemorySnapshot()
    snapshot.scope = Scope(SCOPE.tenant_id, 'other-owner' if other_identity == 'owner' else SCOPE.owner_id,
                           '00000000-0000-4000-8000-000000000002' if other_identity == 'generation' else SCOPE.generation_id)
    other = MarketWorkflow(Journal(snapshot, 1000000), model, provider)
    before = len(model.calls)
    with pytest.raises(ValueError, match='authenticated scope'):
        asyncio.run(other.assess_evidence(state))
    assert len(model.calls) == before and len(provider.starts) == 1


@pytest.mark.parametrize('field', ['run_id', 'topic_id', 'question_id'])
def test_checkpoint_cannot_mix_evidence_with_another_run_or_scope_binding(paused_research, field):
    flow, model, provider, _, state = paused_research
    altered = {**state, field: 'outside-this-research'}
    before = len(model.calls)
    with pytest.raises(ValueError, match='authenticated scope'):
        asyncio.run(flow.assess_evidence(altered))
    assert len(model.calls) == before and len(provider.starts) == 1


def test_invalid_provider_output_also_keeps_legacy_extracts_out_of_the_checkpoint(paused_research):
    flow, _, provider, _, state = paused_research

    async def invalid_output(*args):
        raise ValueError('Synthetic invalid provider data')

    provider.observations = invalid_output
    result = asyncio.run(flow.normalize_evidence({**state, 'evidence': [{'text': MARKER}]}))
    assert result['evidence'] == [] and result['evidence_refs'] == state['evidence_refs']
    assert result['connector_issue']


@pytest.mark.parametrize('node', ['plan_research', 'draft_report'])
@pytest.mark.parametrize('field,value', [('evidence_refs', ['outside-this-research']),
    ('topic_id', ''), ('topic_id', None), ('question_id', ''), ('question_id', None)])
def test_resuming_plan_or_draft_revalidates_references_and_exact_bindings_before_any_model(
        paused_research, node, field, value):
    flow, model, provider, graph, state = paused_research
    if node == 'draft_report':
        asyncio.run(graph.ainvoke(None, THREAD, interrupt_before=[node]))
    else:
        flow, model, provider, graph = configured()
        asyncio.run(graph.ainvoke({'request_id': 'before-plan', 'question': 'D1'}, THREAD,
                                 interrupt_before=[node]))
    assert graph.get_state(THREAD).next == (node,)
    predecessor = 'prepare_run' if node == 'plan_research' else 'decide_next'
    graph.update_state(THREAD, {field: value}, as_node=predecessor)
    before = len(model.calls)
    with pytest.raises(ValueError, match='references|authenticated scope'):
        asyncio.run(graph.ainvoke(None, THREAD))
    assert len(model.calls) == before and len(provider.starts) == (1 if node == 'draft_report' else 0)
    assert not flow.records().rows('ai_mi_reports')


@pytest.mark.parametrize('node', ['plan_research', 'draft_report'])
@pytest.mark.parametrize('field', ['topic_id', 'question_id'])
def test_missing_checkpoint_binding_is_not_treated_as_a_legacy_evidence_fallback(paused_research, node, field):
    flow, model, provider, _, state = paused_research
    incomplete = dict(state)
    incomplete.pop(field)
    before = len(model.calls)
    with pytest.raises(ValueError, match='authenticated scope'):
        asyncio.run(getattr(flow, node)(incomplete))
    assert len(model.calls) == before and len(provider.starts) == 1


def test_null_question_binding_is_valid_only_when_the_authoritative_run_has_no_question(paused_research):
    flow, _, _, _, state = paused_research
    def scheduled_binding(records, snapshot):
        run = records.get('ai_mi_research_runs', state['run_id'])
        records.upsert('ai_mi_research_runs', {**run, 'question_id': None})
    flow.journal.transact(scheduled_binding)
    assert flow.run_evidence({**state, 'question_id': None})
    with pytest.raises(ValueError, match='authenticated scope'):
        flow.run_evidence(state)


def blocked_prepare(flow):
    from langgraph.config import get_stream_writer
    entered, release, finished = Event(), Event(), Event()
    context = ContextVar('checkpoint_test_context', default='missing')
    original = flow.prepare_run
    def prepare(state):
        get_stream_writer()({'stage': 'blocked-prepare', 'context': context.get()})
        entered.set()
        try:
            if not release.wait(5):
                raise AssertionError('The test did not release its synchronous node')
            return original(state)
        finally:
            finished.set()
    flow.prepare_run = prepare
    return flow.build(), entered, release, finished, context


def test_synchronous_nodes_leave_the_loop_responsive_and_keep_stream_context():
    flow, _, _, _ = configured()
    graph, entered, release, finished, context = blocked_prepare(flow)
    async def exercise():
        seen = asyncio.Event()
        chunks = []
        async def consume():
            async for mode, data in graph.astream({'request_id': 'threaded-progress', 'question': 'D1'}, THREAD,
                                                 stream_mode=['custom', 'updates']):
                if mode == 'custom':
                    chunks.append(data)
                    seen.set()
        token = context.set('caller-context')
        task = asyncio.create_task(consume())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(seen.wait(), 2)
            assert chunks == [{'stage': 'blocked-prepare', 'context': 'caller-context'}]
            assert not finished.is_set() and not task.done()
        finally:
            release.set()
            await task
            context.reset(token)
        assert finished.is_set()
    asyncio.run(exercise())


def test_cancelled_graph_drains_its_synchronous_journal_worker_before_returning():
    flow, _, provider, _ = configured()
    graph, entered, release, finished, _ = blocked_prepare(flow)
    async def exercise():
        task = asyncio.create_task(graph.ainvoke({'request_id': 'drain-sync-worker', 'question': 'D1'}, THREAD))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert finished.is_set() and not provider.starts
        assert len(flow.records().rows('ai_mi_research_runs')) == 1
    asyncio.run(exercise())
