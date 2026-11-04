"""Clarification audit preserves actual requests without granting URL authority."""
import copy
import json

import pytest
from pydantic import TypeAdapter

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.contracts import (
    ClarificationAnswer, Clarifications, IntakeDecision, PendingClarification, ResearchRequest)
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Journal
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.research import Research
from test_market_intelligence_repository import MemorySnapshot
from test_market_intelligence_research import TOPIC, WINDOW


def answer(number=1):
    return {'request_id': 'actual-user-turn-' + str(number), 'text': 'Últimos siete días'}


def request():
    return {'version': 1, 'request_id': 'original-user-turn', 'question': 'Investiga la alianza entre Nu y D1.',
            'scope_key': 'authenticated-session-binding', 'answers': [answer()]}


def test_request_and_pending_contracts_preserve_literal_text_and_have_no_effects():
    raw = request()
    assert ResearchRequest.model_validate(raw).model_dump(mode='json') == raw
    pending = {**raw, 'topic': TOPIC, 'missing_fields': ['periodo'], 'user_question': '¿Qué periodo deseas?'}
    assert PendingClarification.model_validate(pending).model_dump(mode='json') == {
        **pending, 'selected_source_key': None, 'prepared_run_id': None, 'prepared_window_spec': None,
        'search_context': None}
    assert ClarificationAnswer.model_validate(answer()).text == answer()['text']
    three = {**raw, 'answers': [answer(i) for i in range(3)]}
    assert ResearchRequest.model_validate(three).model_dump(mode='json') == three


def test_pending_clarification_preserves_the_initial_source_and_defaults_legacy_selection_to_none():
    pending = {**request(), 'topic': TOPIC, 'missing_fields': ['periodo'], 'user_question': '¿Qué periodo deseas?'}
    assert PendingClarification.model_validate(pending).selected_source_key is None
    selected = PendingClarification.model_validate({**pending, 'selected_source_key': 'initial-source-key'})
    assert PendingClarification.model_validate_json(selected.model_dump_json()).selected_source_key == 'initial-source-key'


@pytest.mark.parametrize('field,value', [('version', True), ('version', 1.0), ('version', '1'), ('version', 2),
    ('request_id', 1), ('request_id', ''), ('request_id', 'x' * 129), ('question', True),
    ('question', ''), ('question', 'x' * 501), ('scope_key', ''), ('scope_key', 'x' * 501),
    ('answers', None), ('answers', [answer(i) for i in range(4)])])
def test_request_contract_rejects_invalid_types_version_or_bounds(field, value):
    with pytest.raises(ValueError):
        ResearchRequest.model_validate({**request(), field: value})


@pytest.mark.parametrize('value', [(), 'text', [True], [{'request_id': 'id', 'text': 7}],
    [{'request_id': 'id', 'text': ''}], [{'request_id': 'id', 'text': 'x' * 501}],
    [{'request_id': '', 'text': 'A reply'}], [{'request_id': 'x' * 129, 'text': 'A reply'}]])
def test_clarifications_are_strict_bounded_actual_turns(value):
    with pytest.raises(ValueError):
        TypeAdapter(Clarifications).validate_python(value, strict=True)


def test_legacy_intake_without_action_still_validates_but_unknown_actions_do_not():
    legacy = {'intent': 'research', 'canonical_question': 'Original question', 'topic': TOPIC,
              'window_spec': 'last_7_days', 'missing_fields': [], 'user_question': None}
    assert IntakeDecision.model_validate(legacy).clarification_action is None
    for action in ('continue', 'replace', 'cancel'):
        assert IntakeDecision.model_validate({**legacy, 'clarification_action': action}).clarification_action == action
    with pytest.raises(ValueError):
        IntakeDecision.model_validate({**legacy, 'clarification_action': 'approve'})


@pytest.fixture
def original_question():
    research = Research(Journal(MemorySnapshot(), 100000))
    text = 'Investiga la alianza entre Nu y D1.'
    question, _ = research.record_question('original-user-turn', text, TOPIC)
    return research, question, text


def test_prepared_run_audits_answers_without_changing_the_original_question_or_creating_costs(original_question):
    research, question, text = original_question
    answers = [answer(1), {'request_id': 'actual-user-turn-2', 'text': 'Colombia; revisa también https://example.org/new'}]
    original = copy.deepcopy(answers)
    run = research.prepare_run(question, WINDOW, text, 100000, clarifications=answers)
    value, _ = research.journal.read()
    brief = json.loads(value['tables']['ai_mi_research_runs'][run]['brief_json'])
    assert brief == {'version': 1, 'objective': text, 'clarifications': original}
    stored = value['tables']['ai_mi_questions'][question]
    assert stored['question_text'] == text and stored['client_request_id'] == 'original-user-turn'
    assert len(value['tables']['ai_mi_questions']) == 1
    assert value['operations'] == {} and value['tables']['ai_mi_tool_runs'] == value['tables']['ai_mi_reports'] == {}
    assert value['tables']['ai_mi_source_targets'] == {} and answers == original
    assert research.prepare_run(question, WINDOW, text, 100000, clarifications=original) == run
    assert research.journal.read()[0]['tables'] == value['tables']


@pytest.mark.parametrize('changed', [[], [answer(2)], [answer(1), answer(2)]])
def test_a_prepared_run_cannot_replace_its_clarification_audit(original_question, changed):
    research, question, text = original_question
    research.prepare_run(question, WINDOW, text, 100000, clarifications=[answer()])
    before = copy.deepcopy(research.journal.read()[0])
    with pytest.raises(ValueError, match='immutable'):
        research.prepare_run(question, WINDOW, text, 100000, clarifications=changed)
    assert research.journal.read()[0] == before


def test_none_keeps_the_legacy_resume_contract_without_rewriting_an_existing_audit(original_question):
    research, question, text = original_question
    run = research.prepare_run(question, WINDOW, text, 100000, clarifications=[answer()])
    before = copy.deepcopy(research.journal.read()[0]['tables'])
    assert research.prepare_run(question, WINDOW, text, 100000) == run
    assert research.journal.read()[0]['tables'] == before


@pytest.mark.parametrize('clarifications', [None, []])
def test_empty_or_legacy_clarifications_do_not_add_a_brief_field(original_question, clarifications):
    research, question, text = original_question
    run = research.prepare_run(question, WINDOW, text, 100000, clarifications=clarifications)
    brief = json.loads(research.journal.read()[0]['tables']['ai_mi_research_runs'][run]['brief_json'])
    assert brief == {'version': 1, 'objective': text}


@pytest.mark.parametrize('answers', [[answer(), answer()], [answer(i) for i in range(4)]])
def test_duplicate_or_unbounded_answer_turns_cannot_prepare_a_run(original_question, answers):
    research, question, text = original_question
    before = copy.deepcopy(research.journal.read()[0])
    with pytest.raises(ValueError):
        research.prepare_run(question, WINDOW, text, 100000, clarifications=answers)
    assert research.journal.read()[0] == before


def test_a_legacy_run_cannot_acquire_an_unrecorded_clarification_after_preparation(original_question):
    research, question, text = original_question
    research.prepare_run(question, WINDOW, text, 100000)
    before = copy.deepcopy(research.journal.read()[0])
    with pytest.raises(ValueError, match='immutable'):
        research.prepare_run(question, WINDOW, text, 100000, clarifications=[answer()])
    assert research.journal.read()[0] == before
