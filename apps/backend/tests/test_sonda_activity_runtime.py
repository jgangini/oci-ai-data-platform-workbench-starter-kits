"""Inactive adapter checks with canonical static bundle and in-memory object IO."""
import asyncio
from datetime import datetime,timedelta,timezone
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import ModuleType,SimpleNamespace
from uuid import uuid4

import pytest

ROOT=Path(__file__).resolve().parents[3]
CONTRACT=ROOT/'contracts/sonda-activity/candidate-v1'


@pytest.fixture(scope='module')
def canonical():
    spec=importlib.util.spec_from_file_location('activity_runtime_bundle_builder',CONTRACT/'native_bundle.py')
    builder=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    source=builder.bundle_source(*[(CONTRACT/name).read_text(encoding='utf-8')
        for name in ('reference.py','producer.py','public_stream.py')],
        journal_source=(CONTRACT/'journal.py').read_text(encoding='utf-8'),
        results_source=(CONTRACT/'results.py').read_text(encoding='utf-8'))
    module=ModuleType('activity_runtime_canonical_test')
    sys.modules[module.__name__]=module
    exec(compile(source,'<reviewed-canonical-activity>','exec'),module.__dict__)
    yield module
    del sys.modules[module.__name__]


@pytest.fixture
def adapter(monkeypatch,canonical):
    monkeypatch.setitem(sys.modules,'app.sonda_activity_runtime',canonical)
    spec=importlib.util.spec_from_file_location('activity_adapter_test',ROOT/'apps/backend/app/sonda_activity.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Objects:
    def __init__(self):
        self.rows={}
        self.puts=[]
        self.fail=False
    def get(self,role,key):
        assert role=='artifacts'
        return self.rows.get(key)
    def put(self,role,key,raw,expected):
        assert role=='artifacts'
        if self.fail:
            raise RuntimeError('private object transport detail')
        previous=self.rows.get(key)
        if (previous.etag if previous else None)!=expected:
            raise RuntimeError('fake conditional-write conflict')
        self.puts.append((key,raw,expected))
        self.rows[key]=SimpleNamespace(data=raw,etag=str(len(self.puts)))
        return self.rows[key].etag


def seeded(canonical,autonomous=False):
    descriptor=json.loads((CONTRACT/'fixtures/producer-descriptor.json').read_bytes())
    now=datetime.now(timezone.utc)
    descriptor.update(admission_id=str(uuid4()),created_at=now.isoformat(),expires_at=(now+timedelta(minutes=30)).isoformat())
    descriptor['run'].update(run_id=str(uuid4()),request_id=str(uuid4()),attempt_id=str(uuid4()),created_at=now.isoformat())
    if autonomous:
        descriptor['run'].update(source='autonomous_study',work_id=str(uuid4()),investigation_id='b'*64)
        descriptor['work_input_sha256']='e'*64
    d=canonical.ActivityProducer_AdmissionDescriptor.model_validate(descriptor)
    c=canonical.ActivityProducer_CurrentPointer(schema_version=canonical.ActivityProducer_PRODUCER_VERSION,
        scope=d.scope,agent_id=d.run.agent_id,native_session_sha256=d.native_session_sha256,
        admission_id=d.admission_id,run_id=d.run.run_id,request_id=d.run.request_id,attempt_id=d.run.attempt_id,
        epoch=1,status='admitted',owner_nonce=None,expires_at=d.expires_at)
    root,current=canonical.ActivityJournal_admission_keys(d.scope,str(d.run.agent_id),d.native_session_sha256,str(d.admission_id))
    objects=Objects()
    for key,value in ((root+'descriptor.json',d),(current,c)):
        objects.rows[key]=SimpleNamespace(data=canonical.ActivityProducer_producer_encoded(value.model_dump(mode='json')),etag='seed')
    args=dict(scope=d.scope.model_dump(mode='json'),agent_id=str(d.run.agent_id),native_session_sha=d.native_session_sha256,
        input_sha=d.input_sha256,ref=str(d.admission_id),now=now)
    if autonomous:
        args['protected_work']=dict(work_id=str(d.run.work_id),work_input_sha256=d.work_input_sha256,
            investigation_id=d.run.investigation_id)
    return objects,args,d


def records(canonical,objects):
    rows=[]
    for key,raw,_ in objects.puts:
        if '/batches/' in key:
            rows.extend(canonical.ActivityProducer_parse_producer_document(raw).events)
    return rows


class Stream:
    def __init__(self,snapshots):
        self.snapshots=snapshots
        self.calls=0
    async def astream(self,messages):
        self.calls+=1
        for snapshot in self.snapshots:
            yield snapshot
    async def ainvoke(self,*args,**kwargs):
        raise AssertionError('No fallback/retry model invocation')


def test_one_stream_timer_flushes_buffer_during_model_pause(adapter,canonical):
    objects,args,_=seeded(canonical)
    class Paused(Stream):
        async def astream(self,messages):
            self.calls+=1
            yield {'answer':'A'}
            yield {'answer':'AB'}
            await asyncio.sleep(0.25)
            assert ''.join(event.public_text_delta for event in records(canonical,objects)
                if isinstance(event,canonical.ActivityProducer_ProducerTextDelta))=='AB'
            yield {'answer':'ABC'}
    stream=Paused([])
    async def run():
        assert adapter.current_activity() is None
        async with adapter.activity_invocation(objects,**args) as activity:
            assert adapter.current_activity() is activity
            assert await activity.stream_answer(stream,[])=={'answer':'ABC'}
        assert activity.closed and adapter.current_activity() is None
        assert await activity.stage('After closing') is False
    asyncio.run(run())
    assert stream.calls==1
    assert ''.join(event.public_text_delta for event in records(canonical,objects))=='ABC'


def test_failed_append_latches_observability_preserves_final_without_retry(adapter,canonical):
    objects,args,_=seeded(canonical)
    stream=Stream([{'answer':'A'},{'answer':'AB'}])
    async def run():
        async with adapter.activity_invocation(objects,**args) as activity:
            objects.fail=True
            result=await activity.stream_answer(stream,[])
            assert result=={'answer':'AB'} and activity.observability_failed
            objects.fail=False
            before=len(objects.puts)
            assert await activity.stage('No recovery writes') is False
            assert len(objects.puts)==before
    asyncio.run(run())
    assert stream.calls==1 and records(canonical,objects)==[]


def test_concurrent_invocations_keep_leases_contexts_and_sequences_separate(adapter,canonical):
    first=seeded(canonical);second=seeded(canonical)
    async def one(value,label):
        objects,args,descriptor=value
        async with adapter.activity_invocation(objects,**args) as activity:
            await asyncio.sleep(0)
            assert adapter.current_activity() is activity
            await activity.stage(label)
            await asyncio.sleep(0.02)
            assert adapter.current_activity() is activity
        return records(canonical,objects),descriptor
    async def run():
        return await asyncio.gather(one(first,'First'),one(second,'Second'))
    results=asyncio.run(run())
    for rows,descriptor in results:
        assert len(rows)==1 and rows[0].producer_sequence==1
        assert rows[0].run_id==descriptor.run.run_id and rows[0].producer_stream_id==descriptor.run.attempt_id
    assert results[0][0][0].run_id!=results[1][0][0].run_id
    assert adapter.current_activity() is None


def test_tools_wrap_actual_operation_finish_after_validation_and_hide_exception(adapter,canonical):
    objects,args,_=seeded(canonical)
    async def run():
        async with adapter.activity_invocation(objects,**args) as activity:
            async with activity.tool('query_read','approved_query'):
                assert [row.type for row in records(canonical,objects)]==['tool_start']
            with pytest.raises(RuntimeError,match='private credential detail'):
                async with activity.tool('research_preparation','prepare_research'):
                    raise RuntimeError('private credential detail')
            with pytest.raises(ValueError):
                async with activity.tool('provider_capture','not_native'):
                    pytest.fail('Provider capture cannot be entered')
    asyncio.run(run())
    rows=records(canonical,objects)
    assert [row.type for row in rows]==['tool_start','tool_finish','tool_start','tool_error']
    assert rows[1].effect=='executed' and rows[3].error_code=='tool_failed'
    assert 'private credential detail' not in ''.join(raw.decode() for _,raw,_ in objects.puts)
    assert rows[0].span_id==rows[1].span_id and rows[2].span_id==rows[3].span_id


def test_admission_denial_prevents_model_or_context_entry(adapter,canonical):
    objects,args,_=seeded(canonical)
    args['input_sha']='f'*64
    stream=Stream([{'answer':'Never run'}])
    async def run():
        with pytest.raises(canonical.ActivityJournal_DeliveryError):
            async with adapter.activity_invocation(objects,**args) as activity:
                await activity.stream_answer(stream,[])
        assert adapter.current_activity() is None
    asyncio.run(run())
    assert stream.calls==0 and objects.puts==[]


def test_cancellation_does_not_publish_fake_tool_error_or_completion(adapter,canonical):
    objects,args,_=seeded(canonical)
    entered=asyncio.Event()
    async def run():
        async def work():
            async with adapter.activity_invocation(objects,**args) as activity:
                async with activity.tool('query_read','approved_query'):
                    entered.set()
                    await asyncio.sleep(60)
        task=asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert [row.type for row in records(canonical,objects)]==['tool_start']


def test_cancelled_thread_io_is_finished_before_serial_lock_is_released(adapter):
    entered,release=threading.Event(),threading.Event()
    finished=[]
    def operation():
        entered.set()
        release.wait(2)
        finished.append(True)
    async def run():
        lock=asyncio.Lock()
        async def work():
            async with lock:
                await adapter._activity_io(operation)
        task=asyncio.create_task(work())
        await asyncio.to_thread(entered.wait,1)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0.02)
        assert lock.locked() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished==[True] and not lock.locked()
    asyncio.run(run())


def test_rewritten_public_text_emits_only_neutral_progress_and_keeps_final(adapter,canonical):
    objects,args,_=seeded(canonical)
    stream=Stream([{'summary':'First'},{'summary':'Rewrite'}])
    async def run():
        async with adapter.activity_invocation(objects,**args) as activity:
            result=await activity.stream_answer(stream,[],field='summary')
            assert result=={'summary':'Rewrite'}
    asyncio.run(run())
    rows=records(canonical,objects)
    assert [row.type for row in rows]==['text_delta','stage']
    assert rows[1].status=='progress' and rows[1].effect is None
    assert stream.calls==1


def test_writer_initialization_failure_preserves_business_after_valid_admission(adapter,canonical,monkeypatch):
    objects,args,_=seeded(canonical)
    calls=[]
    def unavailable(*args):
        calls.append(True)
        raise RuntimeError('private read failure')
    monkeypatch.setattr(adapter,'ActivityJournal_JournalWriter',unavailable)
    stream=Stream([{'answer':'Valid final'}])
    async def run():
        async with adapter.activity_invocation(objects,**args) as activity:
            assert activity.observability_failed
            assert await activity.stream_answer(stream,[])=={'answer':'Valid final'}
    asyncio.run(run())
    assert calls==[True] and stream.calls==1
    assert not records(canonical,objects)


def test_tool_preparation_effect_and_stream_payload_allowlist(adapter,canonical):
    objects,args,_=seeded(canonical)
    stream=Stream([{'answer':'Public answer','private_reasoning':'never journal this'}])
    async def run():
        async with adapter.activity_invocation(objects,**args) as activity:
            async with activity.tool('research_preparation','prepare_research'):
                pass  # Caller has validated its prepared request; no provider capture occurred.
            final=await activity.stream_answer(stream,[])
            assert final['private_reasoning']=='never journal this'
    asyncio.run(run())
    rows=records(canonical,objects)
    assert rows[1].type=='tool_finish' and rows[1].effect=='prepared'
    assert all(not ({'authority','received_at','ingestion_sequence'} & set(row.model_dump())) for row in rows)
    assert 'private_reasoning' not in ''.join(raw.decode() for _,raw,_ in objects.puts)
    assert 'never journal this' not in ''.join(raw.decode() for _,raw,_ in objects.puts)


def test_repeated_cancellation_keeps_serial_owner_until_actual_thread_finishes(adapter):
    entered,release=threading.Event(),threading.Event()
    order=[]
    def blocked_append():
        order.append('append_entered')
        entered.set()
        release.wait(2)
        order.append('append_finished')
    async def run():
        lock=asyncio.Lock()
        async def append():
            async with lock:
                await adapter._activity_io(blocked_append)
        async def timer_flush():
            async with lock:
                order.append('flush_entered')
        task=asyncio.create_task(append())
        await asyncio.to_thread(entered.wait,1)
        assert entered.is_set()
        task.cancel('original cancellation')
        await asyncio.sleep(0.01)
        waiting_flush=asyncio.create_task(timer_flush())
        task.cancel('second cancellation')
        await asyncio.sleep(0.02)
        assert lock.locked() and not task.done() and not waiting_flush.done()
        assert order==['append_entered']
        release.set()
        with pytest.raises(asyncio.CancelledError,match='original cancellation'):
            await task
        await waiting_flush
        assert order==['append_entered','append_finished','flush_entered']
    asyncio.run(run())


@pytest.mark.parametrize('effect',['committed','queued','requested','prepared','executed'])
def test_progress_stages_cannot_claim_business_effects(adapter,canonical,effect):
    objects,args,_=seeded(canonical)
    async def run():
        async with adapter.activity_invocation(objects,**args) as activity:
            with pytest.raises(ValueError,match='business effects'):
                await activity.stage('Progreso',effect=effect)
            assert await activity.stage('Consultando informaciÃ³n.') is True
    asyncio.run(run())
    rows=records(canonical,objects)
    assert len(rows)==1 and rows[0].effect is None and rows[0].status=='progress'


def result_objects(objects):
    return {key:blob for key,blob in objects.rows.items() if '/results/' in key}


def test_chat_result_semantics_artifact_before_private_completion_and_no_late_events(adapter,canonical):
    objects,args,descriptor=seeded(canonical)
    approved=[]
    payload={'answer':'Respuesta validada.','charts':[]}
    def validator(value,*,scope,run):
        approved.append((value,scope,run))
        return value==payload
    stream=Stream([{'answer':'Respuesta validada.'}])
    async def run():
        async with adapter.activity_invocation(objects,**args) as activity:
            await activity.stream_answer(stream,[])
            assert await activity.complete_result('chat_response',payload,chat_validator=validator) is True
            before=list(objects.puts)
            assert activity.completed
            assert await activity.stage('Tardío') is False
            assert await activity.complete_result('chat_response',payload,chat_validator=validator) is False
            assert objects.puts==before
    asyncio.run(run())
    rows=records(canonical,objects)
    assert len(approved)==1 and stream.calls==1
    assert approved[0][1]==descriptor.scope.model_dump(mode='json')
    assert approved[0][2]==descriptor.run.model_dump(mode='json')
    receipt=rows[-1]
    assert isinstance(receipt,canonical.ActivityProducer_NativeCompletionReceipt)
    assert receipt.kind=='execution_completion' and receipt.outcome=='completed' and not hasattr(receipt,'authority')
    key=canonical.ActivityJournal_admission_keys(descriptor.scope,str(descriptor.run.agent_id),
        descriptor.native_session_sha256,str(descriptor.admission_id))[0]+'results/'+str(descriptor.admission_id)+'.json'
    artifact=result_objects(objects)
    assert set(artifact)=={key}
    parsed=canonical.ActivityResult_parse_result(artifact[key].data,expected_scope=descriptor.scope,
        expected_run=descriptor.run,expected_ref=receipt.final_ref,chat_validator=lambda value,**kwargs:value==payload)
    assert parsed.payload==payload and parsed.result_id==descriptor.admission_id
    artifact_write=next(index for index,(path,_,_) in enumerate(objects.puts) if path==key)
    completion_batch=next(index for index,(path,raw,_) in enumerate(objects.puts)
        if '/batches/' in path and b'execution_completion' in raw)
    assert artifact_write<completion_batch


@pytest.mark.parametrize('mode',['missing','reject','mutate','gold_kind'])
def test_invalid_result_semantics_never_write_artifact_or_completion(adapter,canonical,mode):
    objects,args,_=seeded(canonical)
    def invalid(value,**kwargs):
        if mode=='mutate':
            value['answer']='changed'
            return True
        return False
    async def run():
        async with adapter.activity_invocation(objects,**args) as activity:
            kwargs={} if mode=='missing' else {'chat_validator':invalid}
            kind='gold_report' if mode=='gold_kind' else 'chat_response'
            assert await activity.complete_result(kind,{'answer':'Original'},**kwargs) is False
            assert activity.observability_failed and not activity.completed
    asyncio.run(run())
    assert not result_objects(objects) and not records(canonical,objects)


def native_payload(descriptor):
    payload=json.loads((CONTRACT/'fixtures/result-native-step.json').read_bytes())['payload']
    payload.update(work_id=str(descriptor.run.work_id),investigation_id=descriptor.run.investigation_id,
        input_sha256=descriptor.work_input_sha256)
    return payload


def test_native_step_requires_protected_work_and_keeps_publication_pending(adapter,canonical):
    objects,args,descriptor=seeded(canonical,autonomous=True)
    payload=native_payload(descriptor)
    async def run():
        async with adapter.activity_invocation(objects,**args) as activity:
            assert await activity.complete_result('native_step',payload) is True
    asyncio.run(run())
    receipt=records(canonical,objects)[-1]
    raw=next(iter(result_objects(objects).values())).data
    result=canonical.ActivityResult_parse_result(raw,expected_scope=descriptor.scope,expected_run=descriptor.run,
        expected_ref=receipt.final_ref,expected_work_input_sha256=descriptor.work_input_sha256)
    assert result.payload.publication_status=='pending' and result.payload.phase=='evaluated'
    assert result.payload.input_sha256==descriptor.work_input_sha256


@pytest.mark.parametrize('field',['input_sha256','work_id','investigation_id','publication_status'])
def test_wrong_work_or_claimed_gold_commit_never_produces_native_result(adapter,canonical,field):
    objects,args,descriptor=seeded(canonical,autonomous=True)
    payload=native_payload(descriptor)
    payload[field]='committed' if field=='publication_status' else str(uuid4()) if field=='work_id' else 'f'*64
    async def run():
        async with adapter.activity_invocation(objects,**args) as activity:
            assert await activity.complete_result('native_step',payload) is False
    asyncio.run(run())
    assert not result_objects(objects) and not records(canonical,objects)


def test_result_put_lost_ack_reconciles_exact_bytes_without_second_put_or_business_retry(adapter,canonical):
    objects,args,_=seeded(canonical)
    original=objects.put
    lost=[]
    def put(role,key,raw,expected):
        etag=original(role,key,raw,expected)
        if '/results/' in key:
            lost.append(key)
            raise TimeoutError('private response lost after write')
        return etag
    objects.put=put
    stream=Stream([{'answer':'Validated'}])
    async def run():
        async with adapter.activity_invocation(objects,**args) as activity:
            await activity.stream_answer(stream,[])
            assert await activity.complete_result('chat_response',{'answer':'Validated'},
                chat_validator=lambda value,**kwargs:True) is True
            assert not activity.observability_failed
    asyncio.run(run())
    assert stream.calls==1 and len(lost)==1 and len(result_objects(objects))==1
    assert isinstance(records(canonical,objects)[-1],canonical.ActivityProducer_NativeCompletionReceipt)


def test_result_write_failure_emits_no_completion_and_cannot_retry_completion(adapter,canonical):
    objects,args,_=seeded(canonical)
    async def run():
        async with adapter.activity_invocation(objects,**args) as activity:
            objects.fail=True
            assert await activity.complete_result('chat_response',{'answer':'Validated'},
                chat_validator=lambda value,**kwargs:True) is False
            objects.fail=False
            before=list(objects.puts)
            assert await activity.complete_result('chat_response',{'answer':'Validated'},
                chat_validator=lambda value,**kwargs:True) is False
            assert objects.puts==before and activity.observability_failed
    asyncio.run(run())
    assert not result_objects(objects) and not records(canonical,objects)
