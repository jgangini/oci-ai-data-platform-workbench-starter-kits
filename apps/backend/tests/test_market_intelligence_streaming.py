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
        return {**state, 'response': {'markdown': 'Verified result', 'chart': None},
                'delivery_verified_for': state['request_id']}

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
        projection_batch_check, projection_checks, projection_statements)

    flow, model, provider, _ = configured()
    before = flow.records()
    writes = list(projection_statements(before, 'oci_medallion', 'oci_artifacts'))
    batch, expected_checks = projection_batch_check(list(projection_checks(before, 'oci_medallion', 'oci_artifacts')))
    queries = []
    registry = {}
    active = peak = 0
    configs = ModuleType('aidputils.agents.toolkit.configs')
    helper = ModuleType('aidputils.agents.toolkit.tool_helper')
    configs.AIDPToolConf = lambda **values: SimpleNamespace(model_dump=lambda: values)

    def sql_tool(definition):
        statement = definition['conf']['query']
        registry[definition['name']] = statement
        async def invoke(arguments):
            nonlocal active, peak
            assert arguments == {}
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            assert registry[definition['name']] == statement
            queries.append(statement)
            active -= 1
            if statement == batch:
                assert set(queries[:-1]) == set(writes)
                expected = expected_checks
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
        return {**state, 'response': {'markdown': 'Projection verified'}, 'delivery_verified_for': state['request_id']}

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
            assert queries == [*writes, batch]
        else:
            await collect()
            assert events[-1]['state']['response']['markdown'] == 'Projection verified'
            completed = len(writes) + 1
            assert queries == [*writes, batch]
        total = len(writes) + 1
        progress = [event for event in events if event.get('stage') == 'project_records']
        assert [event['markdown'] for event in progress] == [
            f'Guardando y verificando registros: {step} de {total} pasos.' for step in range(completed + 1)]
        assert all(set(event) == {'event', 'stage', 'markdown'} for event in progress)
        assert 'MERGE INTO' not in json.dumps(events) and 'COUNT(*)' not in json.dumps(events)
    asyncio.run(check())
    value, _ = flow.journal.read()
    assert value['projection_lease'] is None
    assert value['tables'] == before.tables
    assert peak == min(2, len(writes))
    assert not model.calls and not provider.starts


@pytest.mark.parametrize('cancel_requests', [0, 1, 2], ids=['write-failure', 'caller-cancelled', 'caller-cancelled-twice'])
def test_parallel_writes_drain_before_failure_or_cancellation_returns(cancel_requests):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.native import project_writes
    async def exercise():
        release, started = asyncio.Event(), asyncio.Event()
        active = peak = 0
        completed, progress = [], []
        async def execute(statement, *, slot):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                started.set()
            try:
                await started.wait()
                if not cancel_requests and slot == 0:
                    raise RuntimeError('Synthetic write failure')
                await release.wait()
                completed.append(slot)
            finally:
                active -= 1
        task = asyncio.create_task(project_writes(list(enumerate(['a', 'b', 'c'])), execute, progress.append))
        await started.wait()
        for _ in range(cancel_requests):
            task.cancel()
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel_requests else RuntimeError):
            await task
        assert active == 0 and peak == 2
        assert sorted(completed) == ([0, 1, 2] if cancel_requests else [1, 2])
        assert progress == list(range(1, len(completed) + 1))
    asyncio.run(exercise())


@pytest.mark.parametrize('scope_change', ['owner', 'generation'])
def test_concurrent_projection_tools_resolve_only_the_sql_for_their_own_scope(monkeypatch, scope_change):
    from dataclasses import replace
    from types import SimpleNamespace
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import (
        Journal, Records, digest, json_text, projection_batch_check, projection_checks, projection_statements)
    from test_market_intelligence_repository import MemorySnapshot, SCOPE

    other = replace(SCOPE, owner_id='other-owner') if scope_change == 'owner' else replace(
        SCOPE, generation_id='00000000-0000-4000-8000-000000000002')
    journals, expected, expected_checks = [], [], {}
    for scope in (SCOPE, other):
        snapshot = MemorySnapshot()
        snapshot.scope = scope
        journal = Journal(snapshot, 0)
        journal.transact(lambda records, state: records.upsert('ai_mi_topics', {
            'topic_id': scope.key('projection-topic'),
            'canonical_scope_json': json_text({'entity': scope.owner_id, 'market': 'Colombia', 'objective': 'Scope fixture'})}))
        records = Records(scope, journal.read()[0]['tables'])
        writes = list(projection_statements(records, 'oci_medallion', 'oci_artifacts'))
        batch, check_count = projection_batch_check(list(projection_checks(records, 'oci_medallion', 'oci_artifacts')))
        journals.append(journal)
        expected.append([*writes, batch])
        expected_checks[batch] = check_count
    owners = {statement: index for index, statements in enumerate(expected) for statement in statements}
    registry, executed, names = {}, [[], []], [set(), set()]
    configs = ModuleType('aidputils.agents.toolkit.configs')
    helper = ModuleType('aidputils.agents.toolkit.tool_helper')
    configs.AIDPToolConf = lambda **values: SimpleNamespace(model_dump=lambda: values)

    def register(definition):
        name = definition['name']
        caller = owners[definition['conf']['query']]
        registry[name] = definition
        names[caller].add(name)
        async def invoke(arguments):
            assert arguments == {}
            # Oracle resolves the registered configuration by name after async dispatch.
            await asyncio.sleep(0)
            statement = registry[name]['conf']['query']
            executed[caller].append(statement)
            if statement in expected_checks:
                count = expected_checks[statement]
                return {'rows': [{'total_rows': count, 'distinct_keys': count, 'matched_rows': count}]}
            return {'rows': []}
        return SimpleNamespace(ainvoke=invoke)

    helper.create_langgraph_tool = register
    monkeypatch.setitem(sys.modules, configs.__name__, configs)
    monkeypatch.setitem(sys.modules, helper.__name__, helper)
    runtimes = [AgentRuntime(), AgentRuntime()]
    for runtime in runtimes:
        runtime.config = {'catalog': 'oci_medallion', 'framework_schema': 'oci_artifacts',
                          'gold_query_compute_id': 'synthetic-query-compute'}
    async def project_both():
        await asyncio.gather(*(runtime.project(journal) for runtime, journal in zip(runtimes, journals)))
    asyncio.run(project_both())
    assert executed == expected
    assert names[0].isdisjoint(names[1])
    for index, journal in enumerate(journals):
        assert names[index] == {'ai_mi_projection_' + digest([journal.scope.__dict__, slot])[:32]
                                for slot in (0, 'readback')}
        assert journal.read()[0]['projection_lease'] is None


@pytest.mark.parametrize('failure', ['rendering', 'omitted_check'])
def test_projection_rendering_failure_releases_its_lease_before_any_sql(monkeypatch, failure):
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
    def broken_statements(*args, **kwargs):
        yield from ()
        raise RuntimeError('Synthetic projection rendering failure')
    if failure == 'rendering':
        monkeypatch.setattr(native, 'projection_statements', broken_statements)
    else:
        monkeypatch.setattr(native, 'projection_checks', lambda *args, **kwargs: iter(()))
    runtime = AgentRuntime()
    runtime.config = {'catalog': 'oci_medallion', 'framework_schema': 'oci_artifacts',
                      'gold_query_compute_id': 'synthetic-query-compute'}
    with pytest.raises(RuntimeError, match='projection rendering failure|omitted a table verification'):
        asyncio.run(runtime.project(flow.journal))
    value, _ = flow.journal.read()
    assert value['projection_lease'] is None
    assert value['tables'] == before.tables
    assert not model.calls and not provider.starts


def test_empty_projection_needs_no_sql_and_releases_its_lease(monkeypatch):
    from test_market_intelligence_repository import MemorySnapshot
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Journal
    configs = ModuleType('aidputils.agents.toolkit.configs')
    helper = ModuleType('aidputils.agents.toolkit.tool_helper')
    def forbidden(*args, **kwargs):
        pytest.fail('An empty projection must not execute SQL')
    configs.AIDPToolConf = helper.create_langgraph_tool = forbidden
    monkeypatch.setitem(sys.modules, configs.__name__, configs)
    monkeypatch.setitem(sys.modules, helper.__name__, helper)
    runtime = AgentRuntime()
    runtime.config = {'catalog': 'oci_medallion', 'framework_schema': 'oci_artifacts',
                      'gold_query_compute_id': 'synthetic-query-compute'}
    journal = Journal(MemorySnapshot(), 0)
    asyncio.run(runtime.project(journal))
    value, _ = journal.read()
    assert value['projection_lease'] is None
    assert all(not rows for rows in value['tables'].values())


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
        assert tail[0].content == 'Evidencia contrastada.\n\n'
        assert tail[-1].content == 'Fixture validado.'
        assert tail[-1].additional_kwargs['ai_mi_response'] == {'markdown': 'Fixture validado.', 'chart': None}
        assert ''.join(message.content for message in [first, *tail]) == (
            'Revisando la solicitud.\n\nEvidencia contrastada.\n\nFixture validado.')
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
        assert result['messages'][0].content == 'Fixture validado.'
        assert result['messages'][0].additional_kwargs['ai_mi_response']['markdown'] == 'Fixture validado.'
        assert native_auth == ['start', 'end', 'start', 'end']
    asyncio.run(check())


@pytest.fixture
def verified_projection(monkeypatch):
    from types import SimpleNamespace

    flow, model, provider, _ = configured()
    runtime = AgentRuntime()
    runtime.config = {'catalog': 'oci_medallion', 'framework_schema': 'oci_artifacts',
                      'gold_query_compute_id': 'query-compute'}
    queries, phase = [], {'failure': None}
    configs = ModuleType('aidputils.agents.toolkit.configs')
    helper = ModuleType('aidputils.agents.toolkit.tool_helper')
    configs.AIDPToolConf = lambda **values: SimpleNamespace(model_dump=lambda: values)

    def change_source(records, state):
        source = records.rows('ai_mi_sources')[0]
        records.upsert('ai_mi_sources', {**source, 'description': (source['description'] or '') + ' revised'})

    def sql_tool(definition):
        statement = definition['conf']['query']
        async def invoke(arguments):
            assert arguments == {}
            value, _ = flow.journal.read()
            assert value.get('projection_lease') and not value.get('projection_receipts')
            queries.append(statement)
            if phase['failure'] == 'merge_failure' and statement.startswith('MERGE INTO'):
                raise RuntimeError('Synthetic interrupted MERGE')
            if phase['failure'] == 'cancelled':
                raise asyncio.CancelledError()
            if statement.startswith('SELECT COUNT(*) AS total_rows, COUNT(DISTINCT check_id)'):
                if phase['failure'] == 'snapshot_change':
                    flow.journal.transact(change_source)
                count = statement.count(' AS check_id')
                return {'rows': [{'total_rows': count, 'distinct_keys': count,
                                  'matched_rows': count - 1 if phase['failure'] == 'readback_failure' else count}]}
            return {'rows': []}
        return SimpleNamespace(ainvoke=invoke)

    helper.create_langgraph_tool = sql_tool
    monkeypatch.setitem(sys.modules, configs.__name__, configs)
    monkeypatch.setitem(sys.modules, helper.__name__, helper)
    asyncio.run(runtime.project(flow.journal))
    state, _ = flow.journal.read()
    assert state['projection_receipts']['version'] == 2
    assert len(state['projection_receipts']['rows']) == sum(len(rows) for rows in state['tables'].values())
    queries.clear()
    return SimpleNamespace(flow=flow, runtime=runtime, queries=queries, phase=phase, change_source=change_source,
                           model=model, provider=provider)


def assert_full_projection(projection):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import (
        projection_statements, projection_checks, projection_batch_check)
    runtime, records = projection.runtime, projection.flow.records()
    writes = list(projection_statements(records, runtime.config['catalog'], runtime.config['framework_schema']))
    checks = list(projection_checks(records, runtime.config['catalog'], runtime.config['framework_schema']))
    assert projection.queries == [*writes, projection_batch_check(checks)[0]]
    state, _ = projection.flow.journal.read()
    assert len(state['projection_receipts']['rows']) == sum(len(rows) for rows in state['tables'].values())
    assert state['projection_lease'] is None
    assert not projection.model.calls and not projection.provider.starts


@pytest.mark.parametrize('changed', [False, True], ids=['unchanged', 'one-table-changed'])
def test_verified_projection_merges_changed_rows_but_checks_the_complete_scope(verified_projection, changed):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import (
        projection_statements, projection_checks, projection_batch_check, projection_delta)
    p = verified_projection
    if changed:
        p.flow.journal.transact(p.change_source)
    previous = p.flow.journal.read()[0]['projection_receipts']
    row_keys, _ = projection_delta(p.flow.records(), 'oci_medallion', 'oci_artifacts', 'query-compute', previous)
    writes = list(projection_statements(p.flow.records(), 'oci_medallion', 'oci_artifacts', row_keys=row_keys))
    checks = list(projection_checks(p.flow.records(), 'oci_medallion', 'oci_artifacts'))
    asyncio.run(p.runtime.project(p.flow.journal))
    assert len(p.queries) == (2 if changed else 1)
    if changed:
        assert p.queries[0].startswith('MERGE INTO') and '`ai_mi_sources`' in p.queries[0]
    assert p.queries == [*writes, *([projection_batch_check(checks)[0]] if checks else [])]
    assert p.flow.journal.read()[0]['projection_lease'] is None


@pytest.mark.parametrize('key,value', [('catalog', 'other_medallion'), ('framework_schema', 'other_artifacts'),
                                      ('gold_query_compute_id', 'other-query-compute')])
def test_projection_receipts_cannot_skip_writes_to_another_destination(verified_projection, key, value):
    p = verified_projection
    p.runtime.config[key] = value
    asyncio.run(p.runtime.project(p.flow.journal))
    assert_full_projection(p)


@pytest.mark.parametrize('failure', ['readback_failure', 'merge_failure', 'cancelled'])
def test_projection_failure_invalidates_receipts_and_next_delivery_rewrites_all(verified_projection, failure):
    p = verified_projection
    p.flow.journal.transact(p.change_source)
    p.phase['failure'] = failure
    error = asyncio.CancelledError if failure == 'cancelled' else RuntimeError
    with pytest.raises(error):
        asyncio.run(p.runtime.project(p.flow.journal))
    state, _ = p.flow.journal.read()
    assert not state.get('projection_receipts') and state['projection_lease'] is None
    assert len(p.queries) == (1 if failure in {'merge_failure', 'cancelled'} else 2)
    p.queries.clear()
    p.phase['failure'] = None
    asyncio.run(p.runtime.project(p.flow.journal))
    assert_full_projection(p)


def test_orphan_writer_recovery_does_not_reuse_invalidated_receipts(verified_projection):
    p = verified_projection
    records, previous = p.flow.journal.claim_projection('terminated-writer')
    assert previous
    state, _ = p.flow.journal.read()
    assert not state.get('projection_receipts')
    p.flow.journal.recover_projection('terminated-writer', state['revision'], writer_stopped=True)
    asyncio.run(p.runtime.project(p.flow.journal))
    assert_full_projection(p)


@pytest.mark.parametrize('malformed', [None, {}, [{}], ['unexpected'] * 15])
def test_malformed_projection_receipts_fall_back_to_complete_writes(verified_projection, malformed):
    p = verified_projection
    p.flow.journal.transact(lambda records, state: state.update(projection_receipts=malformed))
    asyncio.run(p.runtime.project(p.flow.journal))
    assert_full_projection(p)


def test_another_lease_cannot_finish_or_restore_projection_receipts(verified_projection):
    p = verified_projection
    records, previous = p.flow.journal.claim_projection('writer-owner')
    with pytest.raises(RuntimeError, match='ownership changed'):
        p.flow.journal.finish_projection('other-writer', records, previous)
    state, _ = p.flow.journal.read()
    assert state['projection_lease'] == 'writer-owner' and not state.get('projection_receipts')
    p.flow.journal.finish_projection('writer-owner', records)


def test_projection_receipts_cannot_be_restored_from_another_scope(verified_projection):
    from dataclasses import replace
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Records
    p = verified_projection
    records, previous = p.flow.journal.claim_projection('writer-owner')
    other = Records(replace(records.scope, owner_id='different-owner'))
    with pytest.raises(ValueError, match='another scope'):
        p.flow.journal.finish_projection('writer-owner', other, previous)
    state, _ = p.flow.journal.read()
    assert state['projection_lease'] == 'writer-owner' and not state.get('projection_receipts')
    p.flow.journal.finish_projection('writer-owner', records)


def test_appending_a_row_does_not_rewrite_history_but_still_checks_it(verified_projection):
    p = verified_projection
    scope = p.flow.journal.scope
    def history(records, state):
        for index in range(20):
            records.upsert('ai_mi_questions', {'question_id': scope.key('historical-' + str(index)),
                'question_text': 'Existing historical payload ' + str(index) + ' content' * 50})
    p.flow.journal.transact(history)
    asyncio.run(p.runtime.project(p.flow.journal))
    previous = p.flow.journal.read()[0]['projection_receipts']
    p.queries.clear()
    new_key, new_text = scope.key('only-new-row'), 'Only this new question needs a readback'
    p.flow.journal.transact(lambda records, state: records.upsert('ai_mi_questions', {
        'question_id': new_key, 'question_text': new_text}))
    asyncio.run(p.runtime.project(p.flow.journal))
    assert len(p.queries) == 2 and p.queries[0].startswith('MERGE INTO')
    merge, readback = p.queries
    assert new_key.encode().hex() in merge and new_text.encode().hex() in merge
    assert scope.key('historical-0').encode().hex() not in merge
    assert '`ai_mi_questions`' in merge
    assert new_key.encode().hex() in readback and scope.key('historical-0').encode().hex() in readback
    current = p.flow.journal.read()[0]['projection_receipts']
    assert current['binding'] == previous['binding'] and len(current['rows']) == len(previous['rows']) + 1
    assert all(current['rows'][key] == fingerprint for key, fingerprint in previous['rows'].items())
    assert p.flow.journal.read()[0]['projection_lease'] is None
    assert not p.model.calls and not p.provider.starts


def test_legacy_matching_statement_receipts_require_a_full_verified_migration(verified_projection):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import digest, projection_statements
    p = verified_projection
    legacy = [digest([p.runtime.config['gold_query_compute_id'], statement])
              for statement in projection_statements(p.flow.records(), 'oci_medallion', 'oci_artifacts')]
    p.flow.journal.transact(lambda records, state: state.update(projection_receipts=legacy))
    asyncio.run(p.runtime.project(p.flow.journal))
    assert_full_projection(p)
    assert p.flow.journal.read()[0]['projection_receipts']['version'] == 2


def test_a_new_projection_contract_cannot_reuse_previous_schema_receipts(verified_projection, monkeypatch):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import repository
    p = verified_projection
    records = p.flow.records()
    previous = p.flow.journal.read()[0]['projection_receipts']
    expected_counts = {name: len(rows) for name, rows in records.tables.items()}
    original = repository.schema_sha256
    # Simulate a renderer/schema contract revision without changing persisted table rows.
    monkeypatch.setattr(repository, 'schema_sha256', lambda: 'new-schema-contract')
    row_keys, _ = repository.projection_delta(records, 'oci_medallion', 'oci_artifacts', 'query-compute', previous)
    monkeypatch.setattr(repository, 'schema_sha256', original)
    assert {name: len(keys) for name, keys in row_keys.items()} == expected_counts


def test_optional_receipts_do_not_overflow_the_snapshot_or_strand_its_lease(verified_projection):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import encoded, projection_statements
    p = verified_projection
    state, _ = p.flow.journal.read()
    state.pop('projection_receipts')
    p.flow.journal.snapshot.maximum_bytes = len(encoded(state)) + 100
    asyncio.run(p.runtime.project(p.flow.journal))
    after, _ = p.flow.journal.read()
    assert after['projection_lease'] is None and not after.get('projection_receipts')
    assert len(p.queries) == 1 and p.queries[0].startswith('SELECT')
    p.queries.clear()
    asyncio.run(p.runtime.project(p.flow.journal))
    writes = list(projection_statements(p.flow.records(), 'oci_medallion', 'oci_artifacts'))
    assert p.queries[:-1] == writes
    assert p.flow.journal.read()[0]['projection_lease'] is None
    assert not p.flow.journal.read()[0].get('projection_receipts')


@pytest.fixture
def projection_sqlite(monkeypatch):
    import sqlite3
    from types import SimpleNamespace
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Journal, Records
    from test_market_intelligence_repository import MemorySnapshot, SCOPE
    journal = Journal(MemorySnapshot(), 0)
    def seed(records, state):
        records.upsert('ai_mi_topics', {'topic_id': SCOPE.key('topic')})
        records.upsert('ai_mi_questions', {'question_id': SCOPE.key('question'),
            'topic_id': SCOPE.key('topic'), 'question_text': 'Verified historical content'})
    journal.transact(seed)
    specs = Records(SCOPE).specs
    database = sqlite3.connect(':memory:')
    database.create_function('unhex', 1, bytes.fromhex)
    database.create_function('decode', 2, lambda raw, encoding: raw.decode(encoding))
    tables = ('ai_mi_topics', 'ai_mi_questions')
    def create_table(name):
        database.execute('CREATE TABLE ' + name + ' (' + ','.join('"' + column['name'] + '" TEXT'
            for column in specs[name]['columns']) + ')')
    for table in tables:
        create_table(table)
    def sqlite_statement(statement):
        for table in tables:
            statement = statement.replace('`catalog`.`oci_artifacts`.`' + table + '`', table)
        return statement.replace('<=>', 'IS')
    queries = []
    configs = ModuleType('aidputils.agents.toolkit.configs')
    helper = ModuleType('aidputils.agents.toolkit.tool_helper')
    configs.AIDPToolConf = lambda **values: SimpleNamespace(model_dump=lambda: values)
    def sql_tool(definition):
        statement = definition['conf']['query']
        async def invoke(arguments):
            queries.append(statement)
            sql = sqlite_statement(statement)
            if not sql.startswith('MERGE INTO'):
                cursor = database.execute(sql)
                return {'rows': [dict(zip((column[0] for column in cursor.description), row)) for row in cursor.fetchall()]}
            target, source = sql[len('MERGE INTO '):].split(' t USING (', 1)
            select = source.rsplit(') s ON ', 1)[0]
            cursor = database.execute(select)
            columns = [column[0] for column in cursor.description]
            primary = next(column['name'] for column in specs[target]['columns'] if column.get('key') == 'pk')
            for values in cursor.fetchall():
                row = dict(zip(columns, values))
                database.execute('DELETE FROM ' + target + ' WHERE tenant_id=? AND owner_id=? AND ' + primary + '=?',
                    (row['tenant_id'], row['owner_id'], row[primary]))
                database.execute('INSERT INTO ' + target + ' VALUES (' + ','.join('?' for _ in columns) + ')', values)
            return {'rows': []}
        return SimpleNamespace(ainvoke=invoke)
    helper.create_langgraph_tool = sql_tool
    monkeypatch.setitem(sys.modules, configs.__name__, configs)
    monkeypatch.setitem(sys.modules, helper.__name__, helper)
    runtime = AgentRuntime()
    runtime.config = {'catalog': 'catalog', 'framework_schema': 'oci_artifacts', 'gold_query_compute_id': 'compute'}
    asyncio.run(runtime.project(journal))
    queries.clear()
    yield SimpleNamespace(runtime=runtime, journal=journal, database=database, queries=queries, create_table=create_table)
    database.close()


@pytest.mark.parametrize('drift', ['modified_history', 'recreated_empty_table'])
def test_full_readback_detects_historical_drift_then_full_replay_repairs_it(projection_sqlite, drift):
    p = projection_sqlite
    if drift == 'modified_history':
        p.database.execute("UPDATE ai_mi_questions SET question_text='Externally modified'")
    else:
        p.database.execute('DROP TABLE ai_mi_questions')
        p.create_table('ai_mi_questions')
    with pytest.raises(RuntimeError, match='does not match'):
        asyncio.run(p.runtime.project(p.journal))
    assert len(p.queries) == 1 and p.queries[0].startswith('SELECT')
    state, _ = p.journal.read()
    assert not state.get('projection_receipts') and state['projection_lease'] is None
    p.queries.clear()
    asyncio.run(p.runtime.project(p.journal))
    assert [query.startswith('MERGE INTO') for query in p.queries] == [True, True, False]
    assert p.database.execute('SELECT question_text FROM ai_mi_questions').fetchall() == [('Verified historical content',)]
    state, _ = p.journal.read()
    assert state['projection_receipts']['version'] == 2 and state['projection_lease'] is None
