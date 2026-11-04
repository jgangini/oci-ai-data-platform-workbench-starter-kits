"""Real graph transitions with synthetic decisions and no external services."""
import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import execution, research
from test_market_intelligence_workflow import configured, turn, TOPIC, THREAD


QUESTION = 'Investiga las novedades de D1 en Colombia de los últimos siete días.'
ANSWER = 'Prioriza los cambios de horarios de atención.'


def prepared_scenario(monkeypatch, *, always_ask=False, ask_after_capture=False):
    flow, model, provider, graph = configured()
    clock = [datetime.now(timezone.utc)]
    flow.clock = lambda: clock[0]
    monkeypatch.setattr(research, 'utc_now', lambda: clock[0].isoformat())
    class ExecutionClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(execution, 'datetime', ExecutionClock)
    original = model.ask
    planning = []
    async def scripted(name, schema, payload):
        if name == 'plan_research':
            planning.append(copy.deepcopy(payload))
            should_ask = always_ask or len(planning) == (2 if ask_after_capture else 1)
            if should_ask:
                model.calls.append((name, copy.deepcopy(payload)))
                return {'next_action': 'clarify', 'source_key': None, 'target_id': None, 'arguments': None,
                        'purpose': '', 'expected_contribution': '', 'user_question': '¿Qué aspecto deseas priorizar?'}
        result = await original(name, schema, payload)
        if name == 'understand':
            text = payload['question']
            result['clarification_action'] = ('cancel' if text == 'Cancela la investigación.' else
                'replace' if text == 'Otra pregunta completa sobre las novedades de D1 en Colombia de los últimos siete días.' else
                'continue' if payload.get('pending_clarification') else None)
            if result['clarification_action'] == 'cancel':
                result['intent'] = 'cancel'
        return result
    model.ask = scripted
    return flow, model, provider, graph, clock, planning


def pause(graph, flow):
    first = turn(graph, 'prepared-original', QUESTION)
    pending = first['pending_clarification']
    run = flow.records().get('ai_mi_research_runs', pending['prepared_run_id'])
    assert flow.research.paused_clarification(run['run_id']) == pending
    return first, pending, run


def test_prepared_question_continues_same_run_after_human_wait_without_resetting_brief_or_capture(monkeypatch):
    flow, model, provider, graph, clock, planning = prepared_scenario(monkeypatch)
    _, pending, before = pause(graph, flow)
    assert provider.starts == []
    clock[0] += timedelta(days=1)
    result = turn(graph, 'prepared-answer', ANSWER)
    after = flow.records().get('ai_mi_research_runs', result['run_id'])
    assert result['run_id'] == before['run_id']
    assert after['state'] == 'closed'
    assert flow.records().get('ai_mi_reports', result['response']['report_id'])['run_id'] == before['run_id']
    for key in ('brief_json', 'question_id', 'plan_window_json', 'reserved_microusd', 'completion_policy_json'):
        assert after[key] == before[key]
    assert json.loads(after['started_finished_at_json'])['started_at'] == json.loads(before['started_finished_at_json'])['started_at']
    assert research.run_elapsed_seconds(after, clock[0]) == 0
    assert len(flow.records().rows('ai_mi_questions')) == 1
    assert len(flow.records().rows('ai_mi_research_runs')) == 1
    assert planning[1]['question'] == QUESTION
    assert planning[1]['clarifications'] == [ANSWER]
    assert result['pending_clarification'] is None
    starts = list(provider.starts)
    calls = len(model.calls)
    turn(graph, 'prepared-answer', ANSWER)
    assert provider.starts == starts
    assert len(model.calls) == calls
    assert pending['answers'] == []


def test_resume_committed_before_checkpoint_recovers_accepted_intake_without_reinterpreting_answer(monkeypatch):
    flow, model, provider, graph, clock, _ = prepared_scenario(monkeypatch)
    _, pending, before = pause(graph, flow)
    clock[0] += timedelta(hours=1)
    state = {'request_id': 'prepared-answer', 'question': ANSWER, 'pending_clarification': pending,
             'request_received_at': clock[0].isoformat(), 'last_topic': TOPIC}
    accepted = asyncio.run(flow.understand(state))
    assert accepted['clarification_run_id'] == before['run_id']
    count = sum(name == 'understand' for name, _ in model.calls)
    result = turn(graph, 'prepared-answer', ANSWER)
    assert result['run_id'] == before['run_id']
    assert sum(name == 'understand' for name, _ in model.calls) == count
    assert len(provider.starts) == 2
    assert len(flow.records().rows('ai_mi_questions')) == 1


def test_journal_pause_without_matching_graph_checkpoint_restores_question_without_replanning(monkeypatch):
    flow, model, provider, graph, _, _ = prepared_scenario(monkeypatch)
    first, pending, run = pause(graph, flow)
    calls = len(model.calls)
    recovered = asyncio.run(flow.plan_research({**first, 'response': {}, 'pending_clarification': None}))
    assert recovered['pending_clarification'] == pending
    assert recovered['response']['markdown'] == pending['user_question']
    assert flow.research.paused_clarification(run['run_id']) == pending
    assert len(model.calls) == calls
    assert provider.starts == []


def test_closed_report_recovers_from_old_pending_checkpoint_without_model_capture_or_journal_changes(monkeypatch):
    flow, model, provider, graph, clock, _ = prepared_scenario(monkeypatch)
    first, _, _ = pause(graph, flow)
    clock[0] += timedelta(hours=1)
    completed = turn(graph, 'prepared-answer', ANSWER)
    before, _ = flow.journal.read()
    calls = len(model.calls)
    starts = copy.deepcopy(provider.starts)
    asyncio.run(graph.aupdate_state(THREAD, copy.deepcopy(first), as_node='deliver_and_read'))
    recovered = turn(graph, 'prepared-answer', ANSWER)
    assert recovered['response'] == completed['response']
    assert recovered['last_report_id'] == completed['last_report_id']
    assert len(model.calls) == calls
    assert provider.starts == starts
    assert flow.journal.read()[0] == before


@pytest.mark.parametrize('replacement', [False, True])
def test_end_committed_before_checkpoint_recovers_exact_new_turn_without_reinterpreting_it(monkeypatch, replacement):
    flow, model, provider, graph, clock, _ = prepared_scenario(monkeypatch)
    _, pending, old = pause(graph, flow)
    clock[0] += timedelta(hours=1)
    text = ('Otra pregunta completa sobre las novedades de D1 en Colombia de los últimos siete días.'
            if replacement else 'Cancela la investigación.')
    state = {'request_id': 'end-without-checkpoint', 'question': text, 'pending_clarification': pending,
             'request_received_at': clock[0].isoformat(), 'last_topic': TOPIC}
    asyncio.run(flow.understand(state))
    calls = sum(name == 'understand' for name, _ in model.calls)
    result = turn(graph, 'end-without-checkpoint', text)
    assert sum(name == 'understand' for name, _ in model.calls) == calls
    assert flow.records().get('ai_mi_research_runs', old['run_id'])['state'] == 'cancelled'
    assert result['pending_clarification'] is None
    if replacement:
        assert result['run_id'] != old['run_id']
        assert len(flow.records().rows('ai_mi_questions')) == 2
        assert len(provider.starts) == 2
    else:
        assert 'cancelado' in result['response']['markdown']
        assert provider.starts == []


@pytest.mark.parametrize('replacement', [False, True])
def test_cancel_or_replace_closes_only_the_empty_prepared_run(monkeypatch, replacement):
    flow, _, provider, graph, clock, _ = prepared_scenario(monkeypatch)
    _, _, before = pause(graph, flow)
    clock[0] += timedelta(hours=1)
    text = ('Otra pregunta completa sobre las novedades de D1 en Colombia de los últimos siete días.'
            if replacement else 'Cancela la investigación.')
    result = turn(graph, 'prepared-new-action', text)
    old = flow.records().get('ai_mi_research_runs', before['run_id'])
    assert old['state'] == 'cancelled'
    assert old['brief_json'] == before['brief_json']
    assert flow.research.paused_clarification(before['run_id']) is None
    assert result['pending_clarification'] is None
    if replacement:
        assert result['run_id'] != before['run_id']
        assert len(flow.records().rows('ai_mi_questions')) == 2
        assert len(provider.starts) == 2
    else:
        assert provider.starts == []
        assert flow.records().rows('ai_mi_reports') == []
        assert len(flow.records().rows('ai_mi_questions')) == 1


def test_three_prepared_answers_then_material_gap_stops_without_a_fourth_answer_or_capture(monkeypatch):
    flow, _, provider, graph, _, _ = prepared_scenario(monkeypatch, always_ask=True)
    _, _, before = pause(graph, flow)
    for number in range(3):
        result = turn(graph, 'bounded-answer-' + str(number), 'Precisión ' + str(number))
    assert result['pending_clarification'] is None
    assert 'pregunta completa' in result['response']['markdown']
    assert flow.records().get('ai_mi_research_runs', before['run_id'])['state'] == 'cancelled'
    assert len(flow.records().rows('ai_mi_questions')) == 1
    assert provider.starts == []


def test_planner_question_after_capture_closes_partial_instead_of_pausing_or_reopening_effects(monkeypatch):
    flow, _, provider, graph, _, _ = prepared_scenario(monkeypatch, ask_after_capture=True)
    result = turn(graph, 'effectful-question', QUESTION)
    run = flow.records().get('ai_mi_research_runs', result['run_id'])
    assert run['state'] == 'closed'
    assert flow.records().get('ai_mi_reports', result['response']['report_id'])['run_id'] == run['run_id']
    assert run['termination_reason'] == 'unresolved'
    assert result.get('pending_clarification') is None
    assert flow.research.paused_clarification(run['run_id']) is None
    assert len(provider.starts) == 1


def test_cancelled_prepared_topic_does_not_replace_the_topic_of_the_previous_report():
    flow, model, provider, graph = configured()
    previous = turn(graph, 'report-A')
    before_starts = copy.deepcopy(provider.starts)
    other = {'entity': 'Ara', 'market': 'Colombia', 'objective': 'Horarios'}
    text = 'Investiga los horarios de Ara en Colombia de los últimos siete días.'
    question, _ = flow.research.record_question('prepared-B', text, other)
    now = datetime.now(timezone.utc)
    run = flow.research.prepare_run(question, {'start': (now - timedelta(days=7)).isoformat(), 'end': now.isoformat()}, text, 100000)
    pending = flow.research.pause_clarification(run, {'version': 1, 'request_id': 'prepared-B', 'question': text,
        'scope_key': flow.research.request_key('prepared-B', text), 'answers': [], 'topic': other,
        'missing_fields': ['alcance'], 'user_question': '¿Qué aspecto deseas revisar?', 'selected_source_key': None,
        'prepared_run_id': run, 'prepared_window_spec': 'last_7_days'})
    asyncio.run(graph.aupdate_state(THREAD, {'request_id': 'prepared-B', 'question': text,
        'pending_clarification': pending, 'last_topic': other, 'last_report_id': previous['last_report_id'],
        'response': {'markdown': pending['user_question']}, 'delivery_verified_for': 'prepared-B'},
        as_node='deliver_and_read'))
    original = model.ask
    async def use_conversation_topic(name, schema, payload):
        result = await original(name, schema, payload)
        if name == 'understand':
            if payload['question'] == 'Cancela esa investigación.':
                result.update(intent='cancel', clarification_action='cancel')
            else:
                result.update(intent='followup', topic=payload['last_topic'])
        return result
    model.ask = use_conversation_topic
    cancelled = turn(graph, 'cancel-B', 'Cancela esa investigación.')
    assert cancelled['last_topic'] == TOPIC
    assert cancelled['last_report_id'] == previous['last_report_id']
    followup = turn(graph, 'follow-report-A', 'Resume el informe anterior, sin nuevas búsquedas.')
    assert followup['response']['report_id'] == previous['last_report_id']
    topic = flow.records().get('ai_mi_questions', followup['question_id'])['topic_id']
    original_run = flow.records().get('ai_mi_research_runs', previous['run_id'])
    assert topic == original_run['topic_id']
    assert provider.starts == before_starts
