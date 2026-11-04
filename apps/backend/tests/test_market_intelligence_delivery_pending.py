"""Delivery contention stays pending and resumes without repeating paid work."""
import asyncio
import copy

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.native import pending_delivery_response
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import ProjectionPending, digest, utc_now
from test_market_intelligence_streaming import verified_projection
from test_market_intelligence_workflow import THREAD, SCOPE


REQUEST = {'request_id': 'delivery-contention', 'question': 'Novedades de D1 en Colombia'}


@pytest.mark.parametrize('reason', ['busy', 'changed'])
def test_pending_delivery_message_does_not_claim_a_verified_result(reason):
    response = pending_delivery_response(ProjectionPending(reason), REQUEST['request_id'])
    assert response['state'] == 'pending' and response['pending_reason'] == 'delivery_' + reason
    assert response['request_id'] == REQUEST['request_id']
    assert 'misma pregunta' in response['markdown'] and 'sin repetir' in response['markdown']
    assert 'todavía no está verificada' in response['markdown']
    assert not any(term in response['markdown'] for term in ('SQL', 'lease', 'Actor', 'Apify'))


def test_busy_projection_cannot_clear_another_writers_lease_or_start_sql(verified_projection):
    p = verified_projection
    held, _ = p.flow.journal.claim_projection('other-writer')
    before = p.flow.journal.read()[0]
    with pytest.raises(ProjectionPending) as caught:
        asyncio.run(p.runtime.project(p.flow.journal))
    assert caught.value.reason == 'busy' and not p.queries
    assert p.flow.journal.read()[0] == before
    p.flow.journal.finish_projection('other-writer', held)


def test_changed_projection_releases_only_its_lease_and_retries_full_verification(verified_projection):
    p = verified_projection
    verified_receipts = copy.deepcopy(p.flow.journal.read()[0]['projection_receipts'])
    p.phase['failure'] = 'snapshot_change'
    with pytest.raises(ProjectionPending) as caught:
        asyncio.run(p.runtime.project(p.flow.journal))
    assert caught.value.reason == 'changed'
    value = p.flow.journal.read()[0]
    assert value['projection_lease'] is None and value['projection_receipts'] == verified_receipts
    assert any((row['description'] or '').endswith(' revised') for row in p.flow.records().rows('ai_mi_sources'))
    assert not p.provider.starts and not p.model.calls
    p.queries.clear()
    p.phase['failure'] = None
    asyncio.run(p.runtime.project(p.flow.journal))
    assert len(p.queries) == 2 and p.queries[0].startswith('MERGE INTO')
    assert '`ai_mi_sources`' in p.queries[0]
    assert p.queries[1].count(' AS check_id') == sum(bool(rows) for rows in value['tables'].values())
    assert p.flow.journal.read()[0]['projection_receipts']['version'] == 2


async def paused_delivery(p):
    p.flow.project = lambda records: p.runtime.project(p.flow.journal)
    graph = p.flow.build(InMemorySaver())
    await graph.ainvoke(REQUEST, THREAD, interrupt_before=['deliver_and_read'])
    saved = await graph.aget_state(THREAD)
    assert saved.next == ('deliver_and_read',) and saved.values['response']['report_id']
    return graph, saved.values['response']


def test_native_pending_delivery_resumes_the_same_checkpoint_without_recapture_or_model(verified_projection):
    p = verified_projection
    async def exercise():
        graph, original = await paused_delivery(p)
        held, _ = p.flow.journal.claim_projection('other-writer')
        before = copy.deepcopy(p.flow.journal.read()[0])
        calls, starts = len(p.model.calls), len(p.provider.starts)
        first = [event async for event in p.runtime.research_events(p.flow, graph, REQUEST, THREAD)]
        assert first[-1]['response']['pending_reason'] == 'delivery_busy'
        assert (await graph.aget_state(THREAD)).next == ('deliver_and_read',)
        assert p.flow.journal.read()[0] == before
        p.flow.journal.finish_projection('other-writer', held)
        second = [event async for event in p.runtime.research_events(p.flow, graph, REQUEST, THREAD)]
        assert second[-1]['response'] == original
        assert not (await graph.aget_state(THREAD)).next
        after = p.flow.journal.read()[0]
        assert after['operations'] == before['operations'] and after['tables'] == before['tables']
        assert after['projection_lease'] is None and after['projection_receipts']['version'] == 2
        assert len(p.model.calls) == calls and len(p.provider.starts) == starts
    asyncio.run(exercise())


@pytest.mark.parametrize('failure', ['merge_failure', 'readback_failure'])
def test_unverified_storage_errors_are_not_misrepresented_as_contention(verified_projection, failure):
    p = verified_projection
    async def exercise():
        graph, _ = await paused_delivery(p)
        p.phase['failure'] = failure
        before = copy.deepcopy(p.flow.journal.read()[0])
        calls, starts = len(p.model.calls), len(p.provider.starts)
        with pytest.raises(RuntimeError) as caught:
            _ = [event async for event in p.runtime.research_events(p.flow, graph, REQUEST, THREAD)]
        assert not isinstance(caught.value, ProjectionPending)
        after = p.flow.journal.read()[0]
        assert after['projection_lease'] is None and 'projection_receipts' not in after
        assert after['operations'] == before['operations'] and after['tables'] == before['tables']
        assert len(p.model.calls) == calls and len(p.provider.starts) == starts
        assert (await graph.aget_state(THREAD)).next == ('deliver_and_read',)
    asyncio.run(exercise())


def test_cancelled_delivery_is_not_reported_as_a_pending_success(verified_projection):
    p = verified_projection
    async def exercise():
        graph, _ = await paused_delivery(p)
        p.phase['failure'] = 'cancelled'
        events = [event async for event in p.runtime.research_events(p.flow, graph, REQUEST, THREAD)]
        assert events[-1]['response']['state'] == 'pending'
        assert events[-1]['response']['pending_reason'] == 'turn_incomplete'
        assert 'report_id' not in events[-1]['response']
        assert (await graph.aget_state(THREAD)).values.get('delivery_verified_for') != REQUEST['request_id']
        assert p.flow.journal.read()[0]['projection_lease'] is None
        before = copy.deepcopy(p.flow.journal.read()[0])
        calls, starts = len(p.model.calls), len(p.provider.starts)
        p.phase['failure'] = None
        recovered = [event async for event in p.runtime.research_events(p.flow, graph, REQUEST, THREAD)]
        assert recovered[-1]['response']['report_id']
        assert (await graph.aget_state(THREAD)).values['delivery_verified_for'] == REQUEST['request_id']
        assert p.flow.journal.read()[0]['operations'] == before['operations']
        assert len(p.model.calls) == calls and len(p.provider.starts) == starts
    asyncio.run(exercise())


def test_notification_delivery_contention_preserves_read_identity_and_owned_report(verified_projection):
    p = verified_projection
    async def exercise():
        graph, original = await paused_delivery(p)
        report_id = original['report_id']
        key = digest([report_id, SCOPE.owner_id])
        notice = SCOPE.key(key)
        def seed(records, state):
            records.upsert('ai_mi_notifications', {'notification_id': notice, 'report_id': report_id,
                'delivery_key': key, 'created_at': utc_now()})
        p.flow.journal.transact(seed)
        held, _ = p.flow.journal.claim_projection('other-writer')
        first = await p.runtime.notification_response(p.flow, p.flow.journal, notice)
        assert first['pending_reason'] == 'delivery_busy' and first['request_id'] == notice
        after = p.flow.journal.read()[0]
        read_at = p.flow.records().get('ai_mi_notifications', notice)['read_at']
        assert after['projection_lease'] == 'other-writer' and read_at
        p.flow.journal.finish_projection('other-writer', held)
        calls, starts = len(p.model.calls), len(p.provider.starts)
        second = await p.runtime.notification_response(p.flow, p.flow.journal, notice)
        assert second == original
        assert p.flow.records().get('ai_mi_notifications', notice)['read_at'] == read_at
        assert p.flow.journal.read()[0]['operations'] == after['operations']
        assert len(p.model.calls) == calls and len(p.provider.starts) == starts
    asyncio.run(exercise())


def test_unknown_notification_is_not_translated_into_a_retriable_delivery(verified_projection):
    p = verified_projection
    with pytest.raises(ValueError, match='Unknown notification'):
        asyncio.run(p.runtime.notification_response(p.flow, p.flow.journal, SCOPE.key('unknown')))
    assert not p.queries

@pytest.mark.parametrize('failure', ['unfinished', 'different_request', 'different_response', 'unverified'])
def test_stream_end_does_not_imply_verified_delivery(failure):
    from types import SimpleNamespace
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.streaming import stream_turn, TurnIncomplete
    response = {'markdown': 'Prepared, not yet verified.'}
    class StreamGraph:
        def __init__(self):
            self.reads = 0
            self.closed = False
        async def aget_state(self, config):
            self.reads += 1
            if self.reads == 1:
                return SimpleNamespace(values={}, next=())
            return SimpleNamespace(values={'request_id': 'another' if failure == 'different_request' else REQUEST['request_id'],
                'response': {'markdown': 'Another response'} if failure == 'different_response' else response,
                'delivery_verified_for': None if failure == 'unverified' else REQUEST['request_id']},
                next=('deliver_and_read',) if failure == 'unfinished' else ())
        async def astream(self, *args, **kwargs):
            try:
                yield 'values', {**REQUEST, 'response': response, 'delivery_verified_for': REQUEST['request_id']}
            finally:
                self.closed = True
    graph = StreamGraph()
    async def exercise():
        events = []
        with pytest.raises(TurnIncomplete):
            async for event in stream_turn(graph, REQUEST, THREAD):
                events.append(event)
        assert not any(event['event'] == 'result' for event in events)
        assert graph.closed and graph.reads == 2
    asyncio.run(exercise())

@pytest.mark.parametrize('marker', [None, 'another-request'])
def test_native_question_reuses_an_unverified_delivery_identity(marker):
    from test_market_intelligence_native_chat import CheckpointGraph, adapt, QUESTION
    values = {'request_id': 'saved-delivery', 'question': QUESTION,
              'response': {'markdown': 'Prepared'}, 'delivery_verified_for': marker}
    assert adapt(graph=CheckpointGraph(values)) == {'request_id': 'saved-delivery', 'question': QUESTION}
    with pytest.raises(RuntimeError, match='Resume'):
        adapt('Otra pregunta', graph=CheckpointGraph(values))


def test_verified_delivery_does_not_hold_a_new_question():
    from test_market_intelligence_native_chat import CheckpointGraph, adapt, QUESTION
    values = {'request_id': 'saved-delivery', 'question': QUESTION,
              'response': {'markdown': 'Verified'}, 'delivery_verified_for': 'saved-delivery'}
    result = adapt('Otra pregunta', graph=CheckpointGraph(values))
    assert result['request_id'] != 'saved-delivery' and result['question'] == 'Otra pregunta'


def test_completed_legacy_checkpoint_reexecutes_only_real_delivery_before_marking_verified(verified_projection):
    p = verified_projection
    async def exercise():
        p.flow.project = lambda records: p.runtime.project(p.flow.journal)
        original_delivery = p.flow.deliver
        original_binding = p.flow.bind_request
        def legacy_binding(state):
            updates = original_binding(state)
            updates.pop('delivery_verified_for')
            return updates
        async def legacy_delivery(state):
            updates = await original_delivery(state)
            updates.pop('delivery_verified_for')
            return updates
        p.flow.deliver = legacy_delivery
        p.flow.bind_request = legacy_binding
        checkpointer = InMemorySaver()
        old_graph = p.flow.build(checkpointer)
        original = await old_graph.ainvoke(REQUEST, THREAD)
        assert not (await old_graph.aget_state(THREAD)).next
        assert 'delivery_verified_for' not in original
        assert 'delivery_verified_for' not in (await old_graph.aget_state(THREAD)).values
        from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.native import chat_request
        adapted = await chat_request(REQUEST['question'], 'new-native-trace', old_graph, THREAD)
        assert adapted == REQUEST
        before = copy.deepcopy(p.flow.journal.read()[0])
        calls, starts = len(p.model.calls), len(p.provider.starts)
        p.flow.deliver = original_delivery
        p.flow.bind_request = original_binding
        graph = p.flow.build(checkpointer)
        events = [event async for event in p.runtime.research_events(p.flow, graph, adapted, THREAD)]
        assert events[-1]['response'] == original['response']
        saved = await graph.aget_state(THREAD)
        assert not saved.next and saved.values['delivery_verified_for'] == REQUEST['request_id']
        assert p.flow.journal.read()[0]['operations'] == before['operations']
        assert p.flow.journal.read()[0]['tables'] == before['tables']
        assert len(p.model.calls) == calls and len(p.provider.starts) == starts
    asyncio.run(exercise())
