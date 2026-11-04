import asyncio
from contextlib import suppress
import inspect
import sys
from types import ModuleType,SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from app.ai_data_governance.governance_source import governance_entry_source


def entry(monkeypatch,runtime):
    setup=Mock()
    monkeypatch.setitem(sys.modules,'governance_runtime',SimpleNamespace(DataGovernanceAgent=lambda:runtime))
    monkeypatch.setitem(sys.modules,'aidputils.agents.toolkit.agent_helper',SimpleNamespace(pre_invoke_setup=setup))
    module=ModuleType('isolated_stream_entry')
    exec(compile(governance_entry_source(),'governance_agent.py','exec'),module.__dict__)
    return module.DataGovernanceAgent(),setup


@pytest.mark.parametrize('message_count',[1,2])
def test_stream_entry_returns_before_runtime_io_and_only_yields_validated_final_messages(monkeypatch,message_count):
    async def run():
        release=asyncio.Event();started=asyncio.Event()
        final=AIMessage(content='Respuesta comprobada',additional_kwargs={'component_revision':'a'*64,
            'governance':{'action_receipts':[{'verified':True}]}})
        messages=[final] if message_count==1 else [final,AIMessage(content='Fuente comprobada')]
        async def invoke(question,**kwargs):
            started.set();await release.wait()
            return {'messages':messages}
        agent,setup=entry(monkeypatch,SimpleNamespace(invoke=invoke))
        task=asyncio.create_task(agent.invoke('consulta',stream=True,session_id='owned'))
        try:
            await asyncio.sleep(0)
            assert task.done(),'The native stream must be returned before waiting on metadata/model I/O'
            stream=task.result();assert inspect.isasyncgen(stream)
            setup.assert_called_once_with(stream=True,session_id='owned')
            assert not started.is_set()
            heartbeat=await anext(stream)
            assert heartbeat.content=='' and heartbeat.additional_kwargs=={'governance_stream_event':'keepalive'}
            pending=asyncio.create_task(anext(stream))
            await started.wait();assert not pending.done()
            release.set();assert await pending is final
            for message in messages[1:]:assert await anext(stream) is message
            with pytest.raises(StopAsyncIteration):await anext(stream)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):await task
    asyncio.run(run())


@pytest.mark.parametrize('options',[{},dict(stream=False)])
def test_nonstream_mode_retains_the_existing_result_and_kwargs(monkeypatch,options):
    seen=[];result={'messages':[AIMessage(content='comprobado')]}
    async def invoke(question,**kwargs):seen.append((question,kwargs));return result
    agent,setup=entry(monkeypatch,SimpleNamespace(invoke=invoke))
    assert asyncio.run(agent.invoke('consulta',session_id='owned',**options)) is result
    assert seen==[('consulta',dict(session_id='owned',**options))]
    setup.assert_called_once()


@pytest.mark.parametrize('flag',[1,'true',None,[]])
def test_invalid_stream_flag_cannot_start_runtime_work(monkeypatch,flag):
    called=[]
    async def invoke(*a,**k):called.append(True);return {'messages':[]}
    agent,_=entry(monkeypatch,SimpleNamespace(invoke=invoke))
    with pytest.raises(ValueError):asyncio.run(agent.invoke('consulta',stream=flag))
    assert called==[]


def test_cancelled_stream_keeps_runtime_cleanup_and_never_yields_a_fabricated_result(monkeypatch):
    async def run():
        started=asyncio.Event();cleaned=[]
        async def invoke(*args,**kwargs):
            started.set()
            try:await asyncio.Event().wait()
            finally:cleaned.append(True)
        agent,_=entry(monkeypatch,SimpleNamespace(invoke=invoke))
        stream=await agent.invoke('consulta',stream=True)
        assert (await anext(stream)).content==''
        task=asyncio.create_task(anext(stream))
        await started.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert cleaned==[True]
        with pytest.raises(StopAsyncIteration):await anext(stream)
    asyncio.run(run())


def test_real_runtime_restores_each_caller_when_generators_are_consumed_in_foreign_tasks(monkeypatch):
    from contextlib import contextmanager
    from contextvars import ContextVar
    from unittest.mock import AsyncMock
    from app.ai_data_governance import native_governance
    from test_ai_dg_governance_source import binding
    context=ContextVar('stream-caller');seen=[];callers=[]
    def setup(**kwargs):
        context.set(SimpleNamespace(dh_user_principal='opaque',subject_id=kwargs['subject'],session_id=kwargs['session']))
        return {}
    @contextmanager
    def flow_setup(**kwargs):
        token=context.set(SimpleNamespace(dh_user_principal='opaque',subject_id=kwargs['subject'],session_id=kwargs['session']))
        try:yield
        finally:context.reset(token)
    def transport(config):
        own=context.get()
        def check():
            if context.get()!=own:raise PermissionError('Caller context crossed tasks')
        caller=SimpleNamespace(check_identity=check,session=SimpleNamespace(close=Mock()))
        callers.append(caller);return caller
    hooks=SimpleNamespace(authorize=AsyncMock(),resolve_assets=AsyncMock(),_read_admitted=AsyncMock(),
        capabilities=(),_validate_admitted_evidence=AsyncMock(),_reobserve_admitted_evidence=AsyncMock())
    monkeypatch.setattr(native_governance,'native_central_transport',transport)
    monkeypatch.setattr(native_governance,'ConsultationHooks',lambda *a,**k:hooks)
    monkeypatch.setattr(native_governance,'render_governance_response',lambda state:'validated')
    monkeypatch.setattr(native_governance,'governance_visual_response',lambda *a:('validated',{}))
    async def run():
        gate=asyncio.Event()
        async def invoke_graph(state,config):
            own=context.get();seen.append((own.subject_id,own.session_id,config['configurable']['thread_id']))
            if len(seen)==2:gate.set()
            await gate.wait()
            assert context.get()==own
            return {'response':{'answer_markdown':'validated'}}
        monkeypatch.setattr(native_governance,'build_governance_graph',lambda **k:SimpleNamespace(ainvoke=invoke_graph))
        runtime=native_governance.DataGovernanceAgent();runtime.binding={**binding(),'component_revision':'a'*64};runtime.memory=object()
        agent,_=entry(monkeypatch,runtime)
        monkeypatch.setitem(sys.modules,'aidputils.agents.toolkit.agent_helper',SimpleNamespace(pre_invoke_setup=setup,flow_setup=flow_setup))
        monkeypatch.setitem(sys.modules,'aidputils.agents.auth.util',SimpleNamespace(auth_utils=SimpleNamespace(get_auth_context=context.get)))
        a=await agent.invoke('a',stream=True,subject='user-a',session='session-a')
        b=await agent.invoke('b',stream=True,subject='user-b',session='session-b')
        context.set(SimpleNamespace(dh_user_principal='opaque',subject_id='foreign',session_id='foreign'))
        async def consume(stream):return [message.content async for message in stream if message.content]
        assert await asyncio.gather(consume(a),consume(b))==[['validated'],['validated']]
    asyncio.run(run())
    assert {(subject,session) for subject,session,_ in seen}=={('user-a','session-a'),('user-b','session-b')}
    assert len({thread for _,_,thread in seen})==2
    assert len(callers)==2 and all(c.session.close.call_count==1 for c in callers)


def test_periodic_keepalives_have_no_answer_or_evidence_and_preserve_the_final_message(monkeypatch):
    async def run():
        release=asyncio.Event();ticks=[];calls=[]
        final=AIMessage(content='Respuesta con fuente [1]',additional_kwargs={'component_revision':'a'*64})
        async def invoke(*args,**kwargs):
            calls.append(True)
            await release.wait()
            return {'messages':[final]}
        agent,_=entry(monkeypatch,SimpleNamespace(invoke=invoke))
        async def advance(tasks,*,timeout):
            ticks.append(timeout)
            await asyncio.sleep(0)
            done={task for task in tasks if task.done()}
            return done,set(tasks)-done
        monkeypatch.setattr(asyncio,'wait',advance)
        stream=await agent.invoke('consulta',stream=True)
        for _ in range(3):
            chunk=await anext(stream)
            assert chunk.content=='' and chunk.additional_kwargs=={'governance_stream_event':'keepalive'}
        assert ticks==[15,15]
        release.set()
        assert await anext(stream) is final
        with pytest.raises(StopAsyncIteration):await anext(stream)
        assert calls==[True]
    asyncio.run(run())


def test_runtime_failure_after_keepalive_cannot_become_a_successful_answer(monkeypatch):
    async def invoke(*args,**kwargs):raise ValueError('runtime failed')
    agent,_=entry(monkeypatch,SimpleNamespace(invoke=invoke))
    async def run():
        stream=await agent.invoke('consulta',stream=True)
        assert (await anext(stream)).content==''
        with pytest.raises(ValueError,match='runtime failed'):await anext(stream)
        with pytest.raises(StopAsyncIteration):await anext(stream)
    asyncio.run(run())


def test_closing_stream_joins_runtime_cleanup_without_a_late_message(monkeypatch):
    async def run():
        started=asyncio.Event();cleaning=asyncio.Event();release=asyncio.Event();closed=[]
        async def invoke(*args,**kwargs):
            started.set()
            try:await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                closed.append(True)
        agent,_=entry(monkeypatch,SimpleNamespace(invoke=invoke))
        stream=await agent.invoke('consulta',stream=True)
        assert (await anext(stream)).content==''
        await started.wait()
        close=asyncio.create_task(stream.aclose())
        try:
            await cleaning.wait()
            assert not close.done() and closed==[]
        finally:
            release.set()
            await close
        assert closed==[True]
        with pytest.raises(StopAsyncIteration):await anext(stream)
    asyncio.run(run())


def test_second_cancellation_during_stream_shutdown_keeps_the_worker_owned(monkeypatch):
    async def run():
        started=asyncio.Event();cleaning=asyncio.Event();release=asyncio.Event();closed=[]
        async def invoke(*args,**kwargs):
            started.set()
            try:await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                closed.append(True)
        agent,_=entry(monkeypatch,SimpleNamespace(invoke=invoke))
        stream=await agent.invoke('consulta',stream=True)
        assert (await anext(stream)).content==''
        await started.wait()
        close=asyncio.create_task(stream.aclose())
        try:
            await cleaning.wait();close.cancel();await asyncio.sleep(0)
            assert not close.done() and closed==[]
        finally:
            release.set();await close
        assert closed==[True]
        with pytest.raises(StopAsyncIteration):await anext(stream)
    asyncio.run(run())
