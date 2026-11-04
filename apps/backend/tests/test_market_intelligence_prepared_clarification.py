"""A prepared research can wait for a person only before any admitted effect."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import execution, research as module
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.contracts import ResearchRequest
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import AdmissionBlocked, PaidOperations
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Journal, Scope, json_text
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.research import (
    PreparedClarificationIneligible, PreparedClarificationPending, Research, run_elapsed_seconds, run_is_paused)
from test_market_intelligence_repository import MemorySnapshot, SCOPE
from test_market_intelligence_research import TOPIC, WINDOW, observation


START = datetime(2026, 11, 1, tzinfo=timezone.utc)
QUESTION = 'Investiga en X las novedades de D1 en Colombia.'


@pytest.fixture
def prepared(monkeypatch):
    monkeypatch.setattr(module, 'utc_now', lambda: START.isoformat())
    class Clock(datetime):
        current = START
        @classmethod
        def now(cls, tz=None):
            return cls.current
    monkeypatch.setattr(execution, 'datetime', Clock)
    research = Research(Journal(MemorySnapshot(), 100000))
    question, topic = research.record_question('original-turn', QUESTION, TOPIC)
    run = research.prepare_run(question, WINDOW, 'Objetivo original', 50000)
    source = SCOPE.key('source')
    research.journal.transact(lambda records, state: records.upsert('ai_mi_sources', {
        'source_key': source, 'status': 'active', 'connector_revision': 'x_search',
        'allowed_scope_json': json_text({'allow_discovery': True})}))
    pending = {'version': 1, 'request_id': 'original-turn', 'question': QUESTION,
        'scope_key': research.request_key('original-turn', QUESTION), 'answers': [],
        'topic': TOPIC, 'missing_fields': ['objetivo'], 'user_question': '¿Qué novedad deseas contrastar?',
        'selected_source_key': source}
    return research, run, pending, Clock


def run_row(research, run):
    return research.journal.read()[0]['tables']['ai_mi_research_runs'][run]


def update_run(research, run, **fields):
    research.journal.transact(lambda records, state: records.upsert('ai_mi_research_runs', {
        **records.get('ai_mi_research_runs', run), **fields}))


def reply(pending, number=1):
    return {**{key: pending[key] for key in ResearchRequest.model_fields},
            'answers': [*pending['answers'], {'request_id': f'answer-{number}', 'text': f'Detalle literal {number}'}]}


def intake():
    return {'intent': 'research', 'canonical_question': QUESTION, 'topic': TOPIC,
            'window_spec': 'last_7_days', 'missing_fields': [], 'user_question': None,
            'selected_source_key': SCOPE.key('source'), 'clarification_action': 'continue', 'search_context': None}


def reserve(research, run, source=None):
    paid = PaidOperations(research.journal)
    source = source or SCOPE.key('source')
    return paid, paid.reserve(run, source, None, {}, 1000, 'Contrastar novedades',
        authorization_hash=paid.authorize(run, source, None)['authorization_hash'])


def test_pause_resume_preserves_original_records_and_consumed_active_time(prepared):
    research, run, pending, clock = prepared
    original = run_row(research, run)
    waiting = research.pause_clarification(run, pending, START + timedelta(seconds=40))
    assert waiting['prepared_run_id'] == run
    assert research.paused_clarification(run) == waiting
    assert research.check_clarification(run, waiting) == waiting
    assert run_elapsed_seconds(run_row(research, run), START + timedelta(days=7)) == 40
    answer = reply(waiting)
    received = START + timedelta(days=7)
    research.resume_clarification(run, waiting, answer, received, intake=intake())
    updated = run_row(research, run)
    assert not run_is_paused(updated)
    assert run_elapsed_seconds(updated, received + timedelta(seconds=12)) == 52
    for key in ('question_id', 'topic_id', 'brief_json', 'reserved_microusd', 'plan_window_json', 'completion_policy_json'):
        assert updated[key] == original[key]
    assert json.loads(updated['started_finished_at_json'])['started_at'] == START.isoformat()
    audit = json.loads(updated['decision_log_json'])['prepared_clarification']
    assert audit['answers'] == answer['answers']
    state = research.journal.read()[0]
    assert len(state['tables']['ai_mi_questions']) == len(state['tables']['ai_mi_research_runs']) == 1
    assert not state['operations'] and not state['tables']['ai_mi_reports']
    clock.current = received + timedelta(seconds=12)
    reserve(research, run)


def test_resume_does_not_restore_already_consumed_execution_budget(prepared):
    research, run, pending, clock = prepared
    waiting = research.pause_clarification(run, pending, START + timedelta(seconds=305))
    clock.current = START + timedelta(days=2)
    research.resume_clarification(run, waiting, reply(waiting), clock.current)
    with pytest.raises(AdmissionBlocked, match='time limit'):
        reserve(research, run)
    assert not research.journal.read()[0]['operations']


def test_pause_and_resume_replays_keep_exact_wait_and_answer_audit(prepared):
    research, run, pending, _ = prepared
    waiting = research.pause_clarification(run, pending, START + timedelta(seconds=10))
    assert research.pause_clarification(run, pending, START + timedelta(hours=1)) == waiting
    assert json.loads(run_row(research, run)['started_finished_at_json'])['clarification_paused_at'] == (START + timedelta(seconds=10)).isoformat()
    answer = reply(waiting)
    research.resume_clarification(run, waiting, answer, START + timedelta(days=1), intake=intake())
    first = run_row(research, run)
    research.resume_clarification(run, waiting, answer, START + timedelta(days=2), intake=intake())
    assert run_row(research, run) == first
    changed = reply(waiting)
    changed['answers'][-1]['text'] = 'Otra interpretación'
    with pytest.raises(ValueError):
        research.resume_clarification(run, waiting, changed, START + timedelta(days=2))


def test_accepted_intake_recovers_exact_answer_before_or_after_another_pause(prepared):
    research, run, pending, _ = prepared
    waiting = research.pause_clarification(run, pending, START + timedelta(seconds=5))
    answer = reply(waiting)
    research.resume_clarification(run, waiting, answer, START + timedelta(hours=1), intake=intake())
    expected = {'request': answer, 'intake': intake()}
    assert research.resumed_clarification(run, waiting, 'answer-1', 'Detalle literal 1') == expected
    assert research.resumed_clarification(run, waiting, 'answer-1', 'Otro texto') is None
    assert research.resumed_clarification(run, waiting, 'other-id', 'Detalle literal 1') is None
    next_pending = {**waiting, 'answers': answer['answers'], 'user_question': '¿Qué detalle falta?'}
    second = research.pause_clarification(run, next_pending, START + timedelta(hours=1, seconds=5))
    assert research.resumed_clarification(run, waiting, 'answer-1', 'Detalle literal 1') == expected
    research.end_clarification(run, second, 'cancel', START + timedelta(hours=2))
    assert research.resumed_clarification(run, waiting, 'answer-1', 'Detalle literal 1') is None


def test_accepted_intake_can_recover_published_report_without_authorizing_more_effects(prepared):
    research, run, pending, _ = prepared
    waiting = research.pause_clarification(run, pending, START)
    research.resume_clarification(run, waiting, reply(waiting), START + timedelta(seconds=5), intake=intake())
    research.publish(run, 'No hay evidencia suficiente.', [], [], [], {}, 'no_data')
    assert research.resumed_clarification(run, waiting, 'answer-1', 'Detalle literal 1')['intake'] == intake()
    with pytest.raises(PreparedClarificationIneligible):
        research.pause_clarification(run, {**waiting, 'answers': reply(waiting)['answers']}, START + timedelta(seconds=6))


def test_resume_without_model_audit_has_no_semantic_recovery(prepared):
    research, run, pending, _ = prepared
    waiting = research.pause_clarification(run, pending, START)
    research.resume_clarification(run, waiting, reply(waiting), START + timedelta(seconds=1))
    assert research.resumed_clarification(run, waiting, 'answer-1', 'Detalle literal 1') is None


@pytest.mark.parametrize('action', ['cancel', 'replace', 'limit'])
def test_end_is_replay_safe_cancelled_without_fake_report_or_new_cost(prepared, action):
    research, run, pending, _ = prepared
    original = run_row(research, run)
    waiting = research.pause_clarification(run, pending, START + timedelta(seconds=10))
    research.end_clarification(run, waiting, action, START + timedelta(hours=1))
    first = run_row(research, run)
    research.end_clarification(run, waiting, action, START + timedelta(hours=2))
    assert run_row(research, run) == first
    assert first['state'] == 'cancelled' and first['termination_reason'] == 'unresolved'
    assert first['brief_json'] == original['brief_json'] and first['plan_window_json'] == original['plan_window_json']
    assert not run_is_paused(first) and research.paused_clarification(run) is None
    state = research.journal.read()[0]
    assert not state['operations'] and not state['tables']['ai_mi_reports']
    with pytest.raises(PreparedClarificationIneligible):
        research.resume_clarification(run, waiting, reply(waiting), START + timedelta(hours=2))


def test_three_answers_include_immutable_brief_and_allow_truthful_system_limit(prepared):
    research, run, pending, _ = prepared
    initial = [{'request_id': 'initial-answer', 'text': 'Primera aclaración'}]
    update_run(research, run, brief_json=json_text({'objective': 'Objetivo original', 'clarifications': initial}))
    pending['answers'] = initial
    for number in (1, 2):
        waiting = research.pause_clarification(run, pending, START + timedelta(seconds=number * 10))
        answer = reply(waiting, number)
        research.resume_clarification(run, waiting, answer, START + timedelta(seconds=number * 10 + 5))
        pending = {**waiting, 'answers': answer['answers']}
    waiting = research.pause_clarification(run, pending, START + timedelta(seconds=30))
    with pytest.raises(ValueError):
        research.resume_clarification(run, waiting, reply(waiting, 3), START + timedelta(seconds=31))
    research.end_clarification(run, waiting, 'limit', START + timedelta(seconds=32))
    row = run_row(research, run)
    audit = json.loads(row['decision_log_json'])['prepared_clarification']
    assert len(audit['answers']) == 2
    assert json.loads(row['brief_json'])['clarifications'] == initial


@pytest.mark.parametrize('field,value', [('request_id', 'other-turn'), ('question', 'Texto diferente'),
    ('scope_key', 'foreign-scope'), ('topic', {**TOPIC, 'market': 'Otro'}),
    ('version', 2), ('prepared_run_id', 'foreign-run')])
def test_pending_binding_changes_are_validation_errors_not_eligibility_fallback(prepared, field, value):
    research, run, pending, _ = prepared
    with pytest.raises(ValueError) as error:
        research.pause_clarification(run, {**pending, field: value}, START)
    assert not isinstance(error.value, PreparedClarificationIneligible)
    assert not run_is_paused(run_row(research, run))


@pytest.mark.parametrize('field,value', [('user_question', 'Otra pregunta'), ('selected_source_key', None),
    ('missing_fields', ['otro campo']), ('answers', [{'request_id': 'unseen', 'text': 'Inventado'}])])
def test_saved_full_pending_cannot_be_replaced_on_check_or_resume(prepared, field, value):
    research, run, pending, _ = prepared
    waiting = research.pause_clarification(run, pending, START)
    changed = {**waiting, field: value}
    with pytest.raises(ValueError):
        research.check_clarification(run, changed)
    with pytest.raises(ValueError):
        research.resume_clarification(run, changed, reply(changed), START + timedelta(seconds=1))


def test_original_question_drift_or_foreign_scope_cannot_restore_pending(prepared):
    research, run, pending, _ = prepared
    waiting = research.pause_clarification(run, pending, START)
    for scope in (Scope('other-tenant', SCOPE.owner_id, SCOPE.generation_id),
                  Scope(SCOPE.tenant_id, 'other-owner', SCOPE.generation_id),
                  Scope(SCOPE.tenant_id, SCOPE.owner_id, '00000000-0000-4000-8000-000000000002')):
        assert Research(type('BoundJournal', (), {'scope': scope})()).request_key('original-turn', QUESTION) != pending['scope_key']
    research.journal.transact(lambda records, state: records.upsert('ai_mi_questions', {
        **records.get('ai_mi_questions', run_row(research, run)['question_id']), 'question_text': 'Changed original'}))
    with pytest.raises(ValueError, match='original research'):
        research.paused_clarification(run)


@pytest.mark.parametrize('state', ['closed', 'cancelled'])
def test_terminal_research_is_ineligible(prepared, state):
    research, run, pending, _ = prepared
    update_run(research, run, state=state)
    with pytest.raises(PreparedClarificationIneligible):
        research.pause_clarification(run, pending, START)


def test_scheduled_research_is_ineligible(prepared):
    research, run, pending, _ = prepared
    def schedule(records, state):
        records.upsert('ai_mi_schedules', {'schedule_id': SCOPE.key('schedule'), 'topic_id': records.get('ai_mi_research_runs', run)['topic_id']})
        records.upsert('ai_mi_research_runs', {**records.get('ai_mi_research_runs', run), 'schedule_id': SCOPE.key('schedule')})
    research.journal.transact(schedule)
    with pytest.raises(PreparedClarificationIneligible):
        research.pause_clarification(run, pending, START)


@pytest.mark.parametrize('effect', ['reserved', 'uncertain', 'evidence', 'operation-only'])
def test_any_admitted_effect_forbids_pause_without_freeing_reservation(prepared, effect):
    research, run, pending, _ = prepared
    paid, tool = reserve(research, run)
    if effect != 'reserved':
        permit = paid.claim_start(tool)
        if effect == 'uncertain':
            paid.mark_uncertain(tool, permit)
        elif effect == 'evidence':
            paid.record_provider(tool, permit, 'existing-provider', 'succeeded', {})
            research.add_evidence(run, tool, [observation()])
        else:
            research.journal.transact(lambda records, state: state['tables']['ai_mi_tool_runs'].clear())
    before = research.journal.read()[0]
    with pytest.raises(PreparedClarificationIneligible):
        research.pause_clarification(run, pending, START)
    assert research.journal.read()[0] == before


def test_pause_blocks_reserve_and_publication_before_any_effect(prepared):
    research, run, pending, _ = prepared
    research.pause_clarification(run, pending, START)
    with pytest.raises(PreparedClarificationPending):
        reserve(research, run)
    with pytest.raises(PreparedClarificationPending):
        research.publish(run, 'No cerrar mientras se espera.', [], [], [], {}, 'no_data')
    assert not research.journal.read()[0]['operations']


def test_start_revalidates_paused_guard_and_preserves_existing_hold(prepared):
    research, run, _, _ = prepared
    paid, tool = reserve(research, run)
    row = run_row(research, run)
    update_run(research, run, started_finished_at_json=json_text({
        **json.loads(row['started_finished_at_json']), 'clarification_paused_at': START.isoformat()}))
    before = research.journal.read()[0]
    with pytest.raises(PreparedClarificationPending):
        paid.claim_start(tool)
    with pytest.raises(PreparedClarificationPending):
        reserve(research, run)
    assert research.journal.read()[0] == before


@pytest.mark.parametrize('moment', [START.replace(tzinfo=None), START.astimezone(timezone(timedelta(hours=1))), START - timedelta(seconds=1)])
def test_invalid_pause_clocks_do_not_commit(prepared, moment):
    research, run, pending, _ = prepared
    before = research.journal.read()[0]
    with pytest.raises(ValueError):
        research.pause_clarification(run, pending, moment)
    assert research.journal.read()[0] == before


def test_resume_receipt_preceding_pause_and_malformed_wait_fail_closed(prepared):
    research, run, pending, _ = prepared
    waiting = research.pause_clarification(run, pending, START + timedelta(seconds=10))
    with pytest.raises(ValueError):
        research.resume_clarification(run, waiting, reply(waiting), START + timedelta(seconds=9))
    row = run_row(research, run)
    timing = json.loads(row['started_finished_at_json'])
    timing['clarification_wait_microseconds'] = True
    update_run(research, run, started_finished_at_json=json_text(timing))
    with pytest.raises(ValueError, match='accumulated'):
        research.resume_clarification(run, waiting, reply(waiting), START + timedelta(seconds=20))


def test_legacy_unpaused_timing_remains_compatible(prepared):
    research, run, _, _ = prepared
    assert run_elapsed_seconds(run_row(research, run), START - timedelta(seconds=5)) == -5
    assert research.paused_clarification(run) is None


@pytest.mark.parametrize('winner', ['reserve', 'pause'])
def test_conditional_race_cannot_commit_pause_and_reservation_together(prepared, winner):
    research, run, pending, _ = prepared
    snapshot = research.journal.snapshot
    write = snapshot.write
    injected = False
    def race(value, etag):
        nonlocal injected
        if not injected:
            injected = True
            snapshot.write = write
            if winner == 'reserve':
                reserve(research, run)
            else:
                research.pause_clarification(run, pending, START)
        write(value, etag)
    snapshot.write = race
    if winner == 'reserve':
        with pytest.raises(PreparedClarificationIneligible):
            research.pause_clarification(run, pending, START)
    else:
        with pytest.raises(PreparedClarificationPending):
            reserve(research, run)
    state = research.journal.read()[0]
    assert bool(state['operations']) != run_is_paused(run_row(research, run))


def test_corrupt_version_or_mismatched_pause_audit_is_not_recovery(prepared):
    research, run, pending, _ = prepared
    waiting = research.pause_clarification(run, pending, START)
    row = run_row(research, run)
    audit = json.loads(row['decision_log_json'])
    audit['prepared_clarification']['version'] = True
    update_run(research, run, decision_log_json=json_text(audit))
    with pytest.raises(ValueError, match='audit'):
        research.check_clarification(run, waiting)
    update_run(research, run, decision_log_json=json_text({'decisions': []}))
    with pytest.raises(ValueError, match='no matching'):
        research.paused_clarification(run)


@pytest.mark.parametrize('change', ['missing_events', 'wrong_state', 'altered_saved_pending', 'lost_resume'])
def test_recovery_rejects_inconsistent_transition_audit(prepared, change):
    research, run, pending, _ = prepared
    waiting = research.pause_clarification(run, pending, START)
    research.resume_clarification(run, waiting, reply(waiting), START + timedelta(seconds=1), intake=intake())
    row = run_row(research, run)
    log = json.loads(row['decision_log_json'])
    audit = log['prepared_clarification']
    if change == 'missing_events':
        audit['events'] = []
    elif change == 'wrong_state':
        audit['state'] = 'waiting'
    elif change == 'altered_saved_pending':
        audit['pending']['user_question'] = 'Pregunta alterada'
    else:
        audit['last_resume'] = None
    update_run(research, run, decision_log_json=json_text(log))
    with pytest.raises(ValueError):
        research.resumed_clarification(run, waiting, 'answer-1', 'Detalle literal 1')


def test_recovery_preserves_prepared_window_binding_and_requires_exact_initial_answers(prepared):
    research, run, pending, _ = prepared
    waiting = research.pause_clarification(run, {**pending, 'prepared_window_spec': 'last_7_days'}, START)
    with pytest.raises(ValueError):
        research.check_clarification(run, {**waiting, 'prepared_window_spec': 'last_31_days'})
    changed = reply(waiting)
    changed['question'] = 'Objetivo reformulado por el modelo'
    with pytest.raises(ValueError):
        research.resume_clarification(run, waiting, changed, START + timedelta(seconds=1))
    assert research.paused_clarification(run) == waiting


@pytest.mark.parametrize('action', ['cancel', 'replace'])
def test_exact_ending_turn_recovers_without_reopening_or_reinterpreting(prepared, action):
    research, run, pending, _ = prepared
    waiting = research.pause_clarification(run, pending, START)
    decision = {**intake(), 'intent': 'cancel' if action == 'cancel' else 'research', 'clarification_action': action}
    continuation = {'request_id': 'ending-turn', 'question': 'Cancela.' if action == 'cancel' else 'Investiga otra pregunta completa.', 'intake': decision}
    research.end_clarification(run, waiting, action, START + timedelta(hours=1), continuation=continuation)
    before = research.journal.read()[0]
    expected = {'action': action, 'intake': decision}
    assert research.ended_clarification(run, waiting, continuation['request_id'], continuation['question']) == expected
    assert research.ended_clarification(run, waiting, 'unseen-turn', continuation['question']) is None
    assert research.ended_clarification(run, waiting, continuation['request_id'], 'Otra pregunta') is None
    research.end_clarification(run, waiting, action, START + timedelta(hours=2), continuation=continuation)
    assert run_row(research, run) == before['tables']['ai_mi_research_runs'][run]
    assert not research.journal.read()[0]['operations']
    with pytest.raises(ValueError):
        research.end_clarification(run, waiting, action, START + timedelta(hours=2),
            continuation={**continuation, 'question': 'Texto cambiado'})
    assert research.resumed_clarification(run, waiting, 'ending-turn', continuation['question']) is None


def test_system_limit_recovers_third_answer_from_old_checkpoint_pending_only(prepared):
    research, run, pending, _ = prepared
    for number in (1, 2, 3):
        waiting = research.pause_clarification(run, pending, START + timedelta(seconds=number * 10))
        answer = reply(waiting, number)
        research.resume_clarification(run, waiting, answer, START + timedelta(seconds=number * 10 + 2), intake=intake())
        pending = {**waiting, 'answers': answer['answers']}
    final = research.pause_clarification(run, pending, START + timedelta(seconds=33))
    research.end_clarification(run, final, 'limit', START + timedelta(seconds=34))
    before = research.journal.read()[0]
    assert research.ended_clarification(run, waiting, 'answer-3', 'Detalle literal 3') == {'action': 'limit', 'intake': intake()}
    assert research.ended_clarification(run, waiting, 'unseen-id', 'Detalle literal 3') is None
    assert research.ended_clarification(run, waiting, 'answer-3', 'Otro texto') is None
    assert research.ended_clarification(run, {**waiting, 'user_question': 'Otra aclaración'}, 'answer-3', 'Detalle literal 3') is None
    assert research.journal.read()[0] == before
    assert run_row(research, run)['state'] == 'cancelled'


@pytest.mark.parametrize('context', [{'request_id': 'end', 'question': 'Texto'},
    {'request_id': True, 'question': 'Texto', 'intake': intake()},
    {'request_id': 'end', 'question': ' ', 'intake': intake()},
    {'request_id': 'end', 'question': 'Texto', 'intake': {**intake(), 'extra': True}}])
def test_malformed_ending_context_does_not_commit(prepared, context):
    research, run, pending, _ = prepared
    waiting = research.pause_clarification(run, pending, START)
    before = research.journal.read()[0]
    with pytest.raises(ValueError):
        research.end_clarification(run, waiting, 'replace', START + timedelta(seconds=1), continuation=context)
    assert research.journal.read()[0] == before
