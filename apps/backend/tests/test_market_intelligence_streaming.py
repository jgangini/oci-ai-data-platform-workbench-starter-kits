"""Streaming uses the same paid-operation journal and checkpoint as buffered turns."""
import asyncio
import json
import sys
from types import ModuleType

import pytest

pytest.importorskip('langgraph', reason='Graph execution is checked in the pinned runtime CI job')

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.native import AgentRuntime
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.streaming import stream_turn
from test_market_intelligence_workflow import configured, THREAD, turn


REQUEST = {'request_id': 'stream-1', 'question': 'Novedades de D1 en Colombia'}


def test_paused_installer_verifies_live_tools_without_running_an_actor():
    from types import SimpleNamespace
    calls = []
    async def get_tools(**kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(name=name) for name in ('call-actor', 'get-actor-run', 'get-dataset-items')]
    runtime = AgentRuntime()
    runtime.mcp_client = SimpleNamespace(get_tools=get_tools)
    assert asyncio.run(runtime.verify_mcp()) is True
    assert calls == [{'server_name': 'AiMarketIntelligenceApify', 'use_cached': False}]
    async def incomplete(**kwargs):
        return [SimpleNamespace(name='call-actor')]
    runtime.mcp_client.get_tools = incomplete
    with pytest.raises(RuntimeError, match='reviewed research tools'):
        asyncio.run(runtime.verify_mcp())


def test_graph_emits_a_milestone_before_model_or_capture_and_only_one_validated_result():
    flow, model, provider, graph = configured()
    async def check():
        stream = stream_turn(graph, REQUEST, THREAD)
        first = await anext(stream)
        assert first['stage'] == 'bind_request' and not model.calls and not provider.starts
        events = [first, *[event async for event in stream]]
        progress = [event for event in events if event['event'] == 'progress']
        assert any(event['stage'] == 'assess_evidence' for event in progress)
        assert all(set(event) == {'event', 'stage', 'markdown'} for event in progress)
        assert 'instrucción hostil' not in json.dumps(progress, ensure_ascii=False)
        assert sum(event['event'] == 'result' for event in events) == 1
        assert events[-1]['state']['response']['report_id']
        calls = len(model.calls)
        replay = [event async for event in stream_turn(graph, REQUEST, THREAD)]
        assert len(replay) == 1 and replay[0]['event'] == 'result'
        assert replay[0]['state']['response'] == events[-1]['state']['response']
        assert len(model.calls) == calls and len(provider.starts) == 2
    asyncio.run(check())
    assert len(flow.records().rows('ai_mi_reports')) == 1


def test_projection_failure_never_streams_success_and_resumes_delivery_without_recapture():
    flow, model, provider, graph = configured()
    async def fail(records):
        raise RuntimeError('Synthetic projection failure')
    flow.project = fail
    async def check():
        events = []
        with pytest.raises(RuntimeError, match='projection failure'):
            async for event in stream_turn(graph, REQUEST, THREAD):
                events.append(event)
        assert events and all(event['event'] != 'result' for event in events)
        assert len(provider.starts) == 2
        calls = len(model.calls)
        flow.project = None
        resumed = [event async for event in stream_turn(graph, REQUEST, THREAD)]
        assert resumed[-1]['state']['response']['report_id']
        assert len(model.calls) == calls and len(provider.starts) == 2
    asyncio.run(check())


def test_custom_projection_progress_uses_only_bounded_counts_without_echoing_payloads():
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.config import get_stream_writer
    from langgraph.graph import END, START, StateGraph

    async def project(state):
        write = get_stream_writer()
        for completed in range(3):
            write({'stage': 'project_records', 'completed_steps': completed, 'total_steps': 2,
                   'sql': 'SELECT private_customer FROM secret_table', 'markdown': 'UNTRUSTED PAYLOAD'})
        for invalid in [None, 'UNTRUSTED PAYLOAD', {'stage': 'unknown', 'completed_steps': 1, 'total_steps': 2},
                        {'stage': 'project_records'},
                        {'stage': 'project_records', 'completed_steps': '1', 'total_steps': 2},
                        {'stage': 'project_records', 'completed_steps': True, 'total_steps': 2},
                        {'stage': 'project_records', 'completed_steps': 0, 'total_steps': False},
                        {'stage': 'project_records', 'completed_steps': -1, 'total_steps': 2},
                        {'stage': 'project_records', 'completed_steps': 3, 'total_steps': 2},
                        {'stage': 'project_records', 'completed_steps': 1, 'total_steps': 29}]:
            write(invalid)
        return {**state, 'response': {'markdown': 'Verified result', 'chart': None}}

    builder = StateGraph(dict)
    builder.add_node('publish_result', project)
    builder.add_edge(START, 'publish_result')
    builder.add_edge('publish_result', END)
    graph = builder.compile(checkpointer=InMemorySaver())

    async def check():
        events = [event async for event in stream_turn(graph, REQUEST, THREAD)]
        progress = [event for event in events if event.get('stage') == 'project_records']
        assert progress == [{'event': 'progress', 'stage': 'project_records',
                             'markdown': f'Guardando y verificando registros: {completed} de 2 pasos.'}
                            for completed in range(3)]
        assert events[-1]['state']['response'] == {'markdown': 'Verified result', 'chart': None}
        assert sum(event['event'] == 'result' for event in events) == 1
        serialized = json.dumps(events)
        assert 'secret_table' not in serialized and 'UNTRUSTED PAYLOAD' not in serialized
    asyncio.run(check())


@pytest.mark.parametrize('fail_readback', [False, True], ids=['verified', 'readback-fails'])
def test_native_projection_stream_reports_verified_steps_and_releases_its_lease(monkeypatch, fail_readback):
    from types import SimpleNamespace
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import (
        projection_checks, projection_statements)

    flow, model, provider, _ = configured()
    before = flow.records()
    writes = list(projection_statements(before, 'oci_medallion', 'oci_artifacts'))
    checks = dict(projection_checks(before, 'oci_medallion', 'oci_artifacts'))
    queries = []
    configs = ModuleType('aidputils.agents.toolkit.configs')
    helper = ModuleType('aidputils.agents.toolkit.tool_helper')
    configs.AIDPToolConf = lambda **values: SimpleNamespace(model_dump=lambda: values)

    def sql_tool(definition):
        statement = definition['conf']['query']
        async def invoke(arguments):
            assert arguments == {}
            queries.append(statement)
            if statement in checks:
                expected = checks[statement]
                return {'rows': [{'total_rows': expected, 'distinct_keys': expected,
                                  'matched_rows': expected + 1 if fail_readback else expected}]}
            assert statement in writes
            return {'rows': []}
        return SimpleNamespace(ainvoke=invoke)

    helper.create_langgraph_tool = sql_tool
    monkeypatch.setitem(sys.modules, configs.__name__, configs)
    monkeypatch.setitem(sys.modules, helper.__name__, helper)
    runtime = AgentRuntime()
    runtime.config = {'catalog': 'oci_medallion', 'framework_schema': 'oci_artifacts',
                      'gold_query_compute_id': 'synthetic-query-compute'}

    async def project(state):
        await runtime.project(flow.journal)
        return {**state, 'response': {'markdown': 'Projection verified'}}

    builder = StateGraph(dict)
    builder.add_node('publish_result', project)
    builder.add_edge(START, 'publish_result')
    builder.add_edge('publish_result', END)
    graph = builder.compile(checkpointer=InMemorySaver())

    async def check():
        events = []
        async def collect():
            async for event in stream_turn(graph, REQUEST, THREAD):
                events.append(event)
        if fail_readback:
            with pytest.raises(RuntimeError, match='does not match'):
                await collect()
            assert all(event['event'] != 'result' for event in events)
            completed = len(writes)
            assert queries == [*writes, next(iter(checks))]
        else:
            await collect()
            assert events[-1]['state']['response']['markdown'] == 'Projection verified'
            completed = len(writes) + len(checks)
            assert queries == [*writes, *checks]
        total = len(writes) + len(checks)
        progress = [event for event in events if event.get('stage') == 'project_records']
        assert [event['markdown'] for event in progress] == [
            f'Guardando y verificando registros: {step} de {total} pasos.' for step in range(completed + 1)]
        assert all(set(event) == {'event', 'stage', 'markdown'} for event in progress)
        assert 'MERGE INTO' not in json.dumps(events) and 'COUNT(*)' not in json.dumps(events)
    asyncio.run(check())
    value, _ = flow.journal.read()
    assert value['projection_lease'] is None
    assert value['tables'] == before.tables
    assert not model.calls and not provider.starts


def test_projection_rendering_failure_releases_its_lease_before_any_sql(monkeypatch):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import native

    flow, model, provider, _ = configured()
    before = flow.records()
    configs = ModuleType('aidputils.agents.toolkit.configs')
    helper = ModuleType('aidputils.agents.toolkit.tool_helper')
    def forbidden(*args, **kwargs):
        pytest.fail('A rendering failure must not execute SQL')
    configs.AIDPToolConf = helper.create_langgraph_tool = forbidden
    monkeypatch.setitem(sys.modules, configs.__name__, configs)
    monkeypatch.setitem(sys.modules, helper.__name__, helper)
    def broken_statements(*args):
        yield from ()
        raise RuntimeError('Synthetic projection rendering failure')
    monkeypatch.setattr(native, 'projection_statements', broken_statements)
    runtime = AgentRuntime()
    runtime.config = {'catalog': 'oci_medallion', 'framework_schema': 'oci_artifacts',
                      'gold_query_compute_id': 'synthetic-query-compute'}
    with pytest.raises(RuntimeError, match='projection rendering failure'):
        asyncio.run(runtime.project(flow.journal))
    value, _ = flow.journal.read()
    assert value['projection_lease'] is None
    assert value['tables'] == before.tables
    assert not model.calls and not provider.starts


def test_denied_budget_is_not_presented_as_admission_or_a_sent_capture():
    flow, _, provider, graph = configured()
    flow.cap = 0
    async def check():
        events = [event async for event in stream_turn(graph, REQUEST, THREAD)]
        assert any(event.get('stage') == 'admit_step' for event in events)
        assert all(event.get('stage') != 'execute_step' for event in events)
        assert 'admitida' not in json.dumps(events, ensure_ascii=False)
        assert events[-1]['state']['reason'] == 'budget'
        assert not provider.starts
    asyncio.run(check())


def test_disconnect_after_paid_start_can_resume_without_starting_the_same_actor_twice():
    _, _, provider, graph = configured()
    async def disconnect():
        stream = stream_turn(graph, REQUEST, THREAD)
        async for event in stream:
            if event.get('stage') == 'execute_step':
                break
        assert len(provider.starts) == 1
        await stream.aclose()
    asyncio.run(disconnect())
    result = turn(graph, REQUEST['request_id'])
    assert result['response']['report_id']
    assert len(provider.starts) == 2
    assert provider.starts[0]['actor'] != provider.starts[1]['actor']


@pytest.fixture
def native_auth(monkeypatch):
    calls = []
    helper = ModuleType('aidputils.agents.toolkit.agent_helper')
    helper.pre_invoke_setup = lambda **kwargs: {'configurable': {'thread_id': 'native-session'}}
    def start(**kwargs):
        calls.append('start')
        return 'auth-context'
    def end(token, **kwargs):
        assert token == 'auth-context'
        calls.append('end')
    helper.pre_tool_setup, helper.post_tool_setup = start, end
    monkeypatch.setitem(sys.modules, helper.__name__, helper)
    return calls


class NativeFixture(AgentRuntime):
    async def events(self, user_query, native_config, kwargs):
        yield {'event': 'progress', 'stage': 'assess_evidence', 'markdown': 'Evidencia contrastada.'}
        yield {'event': 'result', 'response': {'markdown': 'Fixture validado.', 'chart': None}}


def test_native_stream_keeps_auth_context_until_consumption_and_cleans_up(native_auth):
    async def check():
        stream = await NativeFixture().invoke('fixture', stream=True)
        assert native_auth == []
        first = await anext(stream)
        assert first.additional_kwargs['ai_mi_event'] == 'progress' and native_auth == ['start']
        tail = [message async for message in stream]
        assert json.loads(tail[-1].content) == {'markdown': 'Fixture validado.', 'chart': None}
        assert tail[-1].additional_kwargs['ai_mi_event'] == 'result'
        assert native_auth == ['start', 'end']
    asyncio.run(check())


def test_native_disconnect_releases_auth_and_buffered_mode_preserves_the_final_envelope(native_auth):
    async def check():
        stream = await NativeFixture().invoke('fixture', stream=True)
        await anext(stream)
        await stream.aclose()
        assert native_auth == ['start', 'end']
        result = await NativeFixture().invoke('fixture', stream=False)
        assert len(result['messages']) == 1
        assert json.loads(result['messages'][0].content)['markdown'] == 'Fixture validado.'
        assert native_auth == ['start', 'end', 'start', 'end']
    asyncio.run(check())
