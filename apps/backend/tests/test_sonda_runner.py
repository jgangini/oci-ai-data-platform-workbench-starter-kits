import json
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.labs.ai_social_network.notebooks import sonda_runner as runner
from app.sonda_exchange import Blob, RejectedMessage, StreamConflict
from app.sonda_runtime import ingestion_source


@pytest.mark.parametrize('fails',[False,True])
def test_phase_timings_are_monotonic_and_do_not_hide_failures(monkeypatch,capsys,fails):
    ticks=iter((10.0,10.125))
    monkeypatch.setattr(runner,'perf_counter',lambda:next(ticks))
    def work():
        if fails:
            raise RuntimeError('private provider response')
        return 'committed'
    if fails:
        with pytest.raises(RuntimeError,match='private provider response'):
            runner.timed_call('case_commit',work)
    else:
        assert runner.timed_call('case_commit',work)=='committed'
    output=capsys.readouterr().out
    assert 'private provider response' not in output
    assert output.startswith('SONDA_PHASE=')
    assert json.loads(output.split('=',1)[1])=={
        'phase':'case_commit','elapsed_ms':125.0,'status':'error' if fails else 'completed'}


@pytest.mark.parametrize('sink_error',[BrokenPipeError('pipe closed'),ValueError('stream closed')])
@pytest.mark.parametrize('fails',[False,True])
def test_phase_timing_sink_failure_preserves_work_outcome(monkeypatch,sink_error,fails):
    failure=RuntimeError('original failure')
    def broken_print(*args,**kwargs):
        raise sink_error
    def work():
        if fails:
            raise failure
        return 0
    monkeypatch.setattr(runner,'print',broken_print,raising=False)
    if fails:
        with pytest.raises(RuntimeError) as raised:
            runner.timed_call('case_commit',work)
        assert raised.value is failure
    else:
        assert runner.timed_call('case_commit',work)==0


def test_native_ingestion_is_readable_and_uses_case_pipeline():
    raw=ingestion_source()
    assert b'def run_sonda' in raw
    assert b'apply_case_study_results' in raw
    assert b'source_hashes=' in raw
    assert b'from app.' not in raw


@pytest.mark.parametrize('reject',[False,True])
def test_transport_cursor_follows_durable_apply_or_sanitized_rejection(monkeypatch,reject):
    events=[]
    message=RejectedMessage(UUID(int=1),'configuration') if reject else SimpleNamespace(
        message_id=UUID(int=1),model_dump=lambda **_: {'message_id':str(UUID(int=1))})
    stream=SimpleNamespace(pending=lambda **_: [(7,message)],verified_hash=lambda _: 'a'*64,
        acknowledge=lambda sequence:events.append(('ack',sequence)))
    def apply(*args,**kwargs):
        if reject:
            assert kwargs['source_sequence']==7 and kwargs['raw_sha256']=='a'*64
        else:
            assert kwargs['source_sequences']=={str(UUID(int=1)):7}
            assert kwargs['source_hashes']=={str(UUID(int=1)):'a'*64}
        events.append(('committed',7))
    monkeypatch.setattr(runner,'apply_messages',apply)
    monkeypatch.setattr(runner,'reject_transport_message',apply)
    assert runner.consume_batch(None,{},stream)==1
    assert events==[('committed',7),('ack',7)]


def test_failed_gold_commit_cannot_acknowledge(monkeypatch):
    acknowledged=[]
    message=SimpleNamespace(message_id=UUID(int=1),model_dump=lambda **_: {})
    stream=SimpleNamespace(pending=lambda **_: [(1,message)],verified_hash=lambda _: 'a'*64,
        acknowledge=acknowledged.append)
    def fail(*args,**kwargs):
        raise RuntimeError('interrupted Delta publication')
    monkeypatch.setattr(runner,'apply_messages',fail)
    with pytest.raises(RuntimeError):
        runner.consume_batch(None,{},stream)
    assert not acknowledged


def test_valid_messages_share_one_coherent_publication_before_any_cursor_advance(monkeypatch):
    events=[]
    rows=[(index,SimpleNamespace(message_id=UUID(int=index),
        model_dump=lambda index=index,**_: {'message_id':str(UUID(int=index))})) for index in (1,2)]
    stream=SimpleNamespace(pending=lambda **_:rows,verified_hash=lambda index:str(index)*64,
        acknowledge=lambda index:events.append(('ack',index)))
    def apply(spark,binding,messages,**kwargs):
        assert len(messages)==2
        assert kwargs['source_sequences']=={str(UUID(int=1)):1,str(UUID(int=2)):2}
        events.append(('publication',2))
    monkeypatch.setattr(runner,'apply_messages',apply)
    assert runner.consume_batch(None,{},stream)==2
    assert events==[('publication',2),('ack',1),('ack',2)]


class Objects:
    def __init__(self):
        self.rows={}

    def get(self,role,key):
        return self.rows.get((role,key))

    def put(self,role,key,data,expected):
        previous=self.rows.get((role,key))
        if (previous is None and expected is not None) or (previous is not None and previous.etag!=expected):
            raise StreamConflict('exists')
        etag=str(len(data))+'-etag'
        self.rows[role,key]=Blob(data,etag)
        return etag


def test_work_identity_is_stable_and_result_must_confirm_exact_input():
    binding={'registry_id':str(UUID(int=1)),'generation_id':str(UUID(int=2))}
    projection={'configuration':{},'work':{},'state':None,'execution_results':[]}
    objects=Objects()
    request=runner.create_case_work(objects,binding,projection)
    assert runner.create_case_work(objects,binding,projection)==request
    assert runner.create_case_work(objects,binding,{**projection,
        'publication_id':'another-case-published','publication_sha256':'c'*64})==request
    assert len(objects.rows)==1
    result={'work_id':request['work_id'],'input_sha256':request['sha256'],'result':{'state':{}}}
    assert runner.decode_case_result(result,request)=={'state':{}}
    with pytest.raises(ValueError):
        runner.decode_case_result({**result,'input_sha256':'b'*64},request)


@pytest.mark.parametrize('uncertain',[False,True])
@pytest.mark.parametrize('limits_pending',[False,True])
def test_production_loop_resumes_uncertainty_before_consuming_and_keeps_noop_predecessor(monkeypatch,uncertain,limits_pending):
    binding={'registry_id':str(UUID(int=1)),'generation_id':str(UUID(int=2)),
        'contract_sha256':'a'*64,'source_prefix':'aidp-social/','namespace':'test',
        'source_bucket':'source','artifacts_bucket':'artifacts',
        'model_id':'model','model_revision':'1','prompt_version':'prompt'}
    spark=SimpleNamespace(conf=SimpleNamespace(set=lambda *args:None))
    monkeypatch.setitem(sys.modules,'pyspark.sql',SimpleNamespace(SparkSession=
        SimpleNamespace(builder=SimpleNamespace(getOrCreate=lambda:spark))))
    monkeypatch.setattr(runner,'checked_binding',lambda value:value)
    monkeypatch.setattr(runner,'runtime_storage',lambda _: (None,None))
    monkeypatch.setattr(runner,'generation_enabled',lambda *args:True)
    monkeypatch.setattr(runner,'generation_inference',lambda *args:True)
    monkeypatch.setattr(runner,'ensure_tables',lambda *args:None)
    monkeypatch.setattr(runner,'OciObjects',lambda *args:Objects())
    monkeypatch.setattr(runner,'Exchange',lambda *args,**kwargs:None)
    monkeypatch.setattr(runner,'apply_messages',lambda *args:None)
    monkeypatch.setattr(runner,'consume_batch',lambda *args:0)
    monkeypatch.setattr(runner,'flush_outbox',lambda *args:0)
    monkeypatch.setattr(runner,'consume_source_limits',lambda *args:{'state':'pending' if limits_pending else 'applied'})
    monkeypatch.setattr(runner,'record_runtime',lambda spark,binding,objects,result:result)
    monkeypatch.setattr(runner,'list_pending_cases',lambda *args,**kwargs:{'cases':[{'investigation_id':'b'*64}]})
    def load(*args,**kwargs):
        assert args[2]=='b'*64 and kwargs['batch_size']==10
        return {'state':{'version':5},'work':{},'configuration':{},'execution_results':[]}
    monkeypatch.setattr(runner,'load_case_work',load)
    monkeypatch.setattr(runner,'create_case_work',lambda *args:{'work_id':str(UUID(int=3))})
    monkeypatch.setattr(runner,'invoke_case',lambda *args:{'state':{'version':5,'predecessor_revision':4}})
    committed=[]
    monkeypatch.setattr(runner,'apply_case_study_results',lambda *args,**kwargs:committed.append(kwargs))
    if uncertain:
        def fail(*args):
            raise RuntimeError('Native result is still uncertain')
        def forbidden(*args,**kwargs):
            pytest.fail('Inbound must not invalidate the pending native work')
        monkeypatch.setattr(runner,'resume_pending_cases',fail)
        monkeypatch.setattr(runner,'consume_batch',forbidden)
        assert runner.run_sonda(binding)['status']=='deferred'
        assert not committed
        return
    result=runner.run_sonda(binding)
    if limits_pending:
        assert result['status']=='deferred' and result['processed_case_phases']==0
        assert result['source_limits_state']=='pending' and not committed
        return
    assert result['processed_case_phases']==1
    assert committed[0]['predecessor_revision']==4


def test_uncertain_case_reuses_pending_work_even_when_new_evidence_changes_projection(monkeypatch):
    binding={'registry_id':str(UUID(int=1)),'generation_id':str(UUID(int=2))}
    objects=Objects()
    loaded=[]
    def load(*args,**kwargs):
        loaded.append(True)
        return {'configuration':{},'work':{'input_revision':len(loaded)},'state':None,'execution_results':[]}
    monkeypatch.setattr(runner,'load_case_work',load)
    requests=[]
    def invoke(binding,objects,signer,request):
        requests.append(request)
        if len(requests)==1:
            raise RuntimeError('unknown HTTP outcome')
        return {'state':{'predecessor_revision':0}}
    monkeypatch.setattr(runner,'invoke_case',invoke)
    applied=[]
    monkeypatch.setattr(runner,'apply_case_study_results',lambda *args,**kwargs:applied.append(kwargs))
    with pytest.raises(RuntimeError):
        runner.process_case(None,binding,objects,None,'b'*64,{})
    runner.process_case(None,binding,objects,None,'b'*64,{})
    assert len(loaded)==1 and requests[0]==requests[1]
    assert applied[0]['result_id']==requests[0]['work_id']


def test_bootstrap_stops_before_capture_without_acknowledging_it(monkeypatch):
    events=[]
    config=SimpleNamespace(kind='configuration',message_id=UUID(int=1),model_dump=lambda **_: {})
    capture=SimpleNamespace(kind='observations',message_id=UUID(int=2))
    stream=SimpleNamespace(pending=lambda **_:[(1,config),(2,capture)],verified_hash=lambda _:'a'*64,
        acknowledge=lambda seq:events.append(('ack',seq)))
    monkeypatch.setattr(runner,'apply_messages',lambda *args,**kwargs:events.append(('commit',1)))
    assert runner.consume_batch(None,{},stream,configuration_only=True)==1
    assert events==[('commit',1),('ack',1)]
