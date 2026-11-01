"""Native chat adaptation preserves retry identity without starting research."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import native
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import digest


QUESTION = '¿Qué novedades hay sobre D1 en Colombia?'
CONFIG = {'configurable': {'thread_id': 'native-owner-session'}, 'recursion_limit': 100}


class CheckpointGraph:
    def __init__(self, values=None, next_nodes=()):
        self.state = SimpleNamespace(values=values, next=next_nodes)
        self.reads = []

    async def aget_state(self, config):
        self.reads.append(copy.deepcopy(config))
        return self.state

    def astream(self, *args, **kwargs):
        raise AssertionError('Adapting a request must not execute the graph')

    async def ainvoke(self, *args, **kwargs):
        raise AssertionError('Adapting a request must not invoke the graph')


def adapt(question=QUESTION, trace='native-trace-1', *, graph=None, config=None):
    graph = graph if graph is not None else CheckpointGraph()
    config = copy.deepcopy(CONFIG) if config is None else config
    return asyncio.run(native.chat_request(question, trace, graph, config))


def test_plain_native_chat_trims_text_and_binds_the_native_invocation():
    graph = CheckpointGraph()
    config = copy.deepcopy(CONFIG)
    before = copy.deepcopy(config)
    request = adapt(' \n' + QUESTION + '\t ', graph=graph, config=config)
    assert request == {'request_id': 'native:' + digest(['native-owner-session', 'native-trace-1']),
                       'question': QUESTION}
    assert graph.reads == [before]
    assert config == before


@pytest.mark.parametrize('command', [
    {'request_id': 'explicit-retry', 'question': QUESTION},
    {'read_notification': 'notification-1'},
    {'schedule_id': 'schedule-1'},
])
def test_explicit_json_commands_keep_their_existing_contract_and_ids(command):
    graph = CheckpointGraph()
    assert adapt(' \n' + json.dumps(command) + ' ', None, graph=graph, config={}) == command
    assert graph.reads == []


@pytest.mark.parametrize('question', ['', ' ', '\n\t', None, 12, {'question': QUESTION}, 'a' * 501])
def test_invalid_natural_questions_fail_before_checkpoint_access(question):
    graph = CheckpointGraph()
    with pytest.raises(ValueError):
        adapt(question, graph=graph)
    assert graph.reads == []


@pytest.mark.parametrize('question', ['a', 'á' * 500, ' \n' + 'á' * 500 + '\t '])
def test_question_bounds_apply_to_trimmed_characters(question):
    assert adapt(question)['question'] == question.strip()


@pytest.mark.parametrize('question', ['{', '{"question":', '{not json}', '{"question":"D1",}'])
def test_malformed_json_commands_are_not_silently_sent_to_research(question):
    graph = CheckpointGraph()
    with pytest.raises(ValueError):
        adapt(question, graph=graph)
    assert graph.reads == []


@pytest.mark.parametrize('trace', [None, '', ' ', '\n\t', False, 123, 'a' * 129])
def test_natural_chat_requires_a_bounded_native_trace_id(trace):
    graph = CheckpointGraph()
    with pytest.raises(ValueError):
        adapt(trace=trace, graph=graph)
    assert graph.reads == []


@pytest.mark.parametrize('config', [{}, {'configurable': {}}, {'configurable': {'thread_id': None}},
                                  {'configurable': {'thread_id': ''}}, {'configurable': {'thread_id': 123}}])
def test_natural_chat_requires_a_native_session(config):
    graph = CheckpointGraph()
    with pytest.raises(ValueError):
        adapt(graph=graph, config=config)
    assert graph.reads == []


def test_request_identity_replays_the_same_trace_and_separates_other_turns_and_sessions():
    first = adapt()
    assert adapt() == first
    assert adapt(trace='native-trace-2')['request_id'] != first['request_id']
    assert adapt(config={'configurable': {'thread_id': 'another-owner-session'}})['request_id'] != first['request_id']
    assert adapt(trace='t' * 128)['request_id'].startswith('native:')


def test_interrupted_question_reuses_its_checkpoint_id_even_under_a_new_trace():
    values = {'request_id': 'prior-paid-operation', 'question': QUESTION, 'run_id': 'existing-run'}
    graph = CheckpointGraph(values, ('reconcile_step',))
    before = copy.deepcopy(values)
    assert adapt(' \t' + QUESTION + '\n', 'new-native-trace', graph=graph) == {
        'request_id': 'prior-paid-operation', 'question': QUESTION}
    assert graph.state.values == before
    assert len(graph.reads) == 1


def test_a_different_question_cannot_replace_an_interrupted_paid_request():
    values = {'request_id': 'prior-paid-operation', 'question': QUESTION}
    graph = CheckpointGraph(values, ('execute_step',))
    with pytest.raises(RuntimeError, match='interrupted|Resume'):
        adapt('Otra pregunta sobre otra empresa', graph=graph)
    assert graph.state.values == values


@pytest.mark.parametrize('values', [None, {}, {'question': QUESTION},
                                   {'question': QUESTION, 'request_id': ''}])
def test_an_incomplete_pending_checkpoint_cannot_become_a_new_paid_turn(values):
    graph = CheckpointGraph(values, ('execute_step',))
    with pytest.raises(RuntimeError, match='interrupted|Resume'):
        adapt(graph=graph)


def test_a_completed_checkpoint_does_not_collapse_a_later_question_into_the_previous_turn():
    graph = CheckpointGraph({'request_id': 'old-turn', 'question': QUESTION, 'response': {'markdown': 'Saved'}})
    request = adapt('Resúmelo en dos frases', 'later-native-trace', graph=graph)
    assert request['request_id'] == 'native:' + digest(['native-owner-session', 'later-native-trace'])
    assert request['question'] == 'Resúmelo en dos frases'


def test_adaptation_never_creates_a_random_id_model_or_provider(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Request adaptation must not create or execute research resources')
    for name in ('uuid4', 'DecisionModel', 'ApifyProvider'):
        monkeypatch.setattr(native, name, forbidden)
    request = adapt(graph=CheckpointGraph())
    assert request['question'] == QUESTION
