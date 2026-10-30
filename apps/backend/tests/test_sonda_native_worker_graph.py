"""Real investigator StateGraph from trusted assembled source; external IO is fake.

The test-only exec exposes assembly collisions. Production source remains ordinary
Python without a loader. No OCI service, credential, model or container is used.
"""
import asyncio
from contextlib import contextmanager
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import types
from uuid import UUID, uuid5

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app import sonda_contract as contract
from app.sonda_runtime import investigator_source
from app.labs.ai_social_network.notebooks.agent import sonda_study as core
from app.labs.ai_social_network.notebooks.agent.sonda_memory import GenerationMemory

ROOT=Path(__file__).resolve().parents[3]


class ObjectError(RuntimeError):
    def __init__(self,status):
        self.status=status
        super().__init__('Synthetic object status '+str(status))


class Storage:
    def __init__(self):
        self.objects={}
        self.puts=[]
        self.failure=None
        self.conflicting_result=None

    def get_object(self,namespace,bucket,key):
        if key not in self.objects:
            raise ObjectError(404)
        raw=io.BytesIO(self.objects[key])
        return types.SimpleNamespace(data=types.SimpleNamespace(raw=raw,close=raw.close))

    def put_object(self,namespace,bucket,key,body,**kwargs):
        assert kwargs=={'if_none_match':'*','content_type':'application/json'}
        self.puts.append(key)
        if self.failure=='before':
            self.failure=None
            raise TimeoutError('Synthetic outcome unknown before write')
        if self.conflicting_result is not None:
            self.objects[key]=self.conflicting_result
            self.conflicting_result=None
        if key in self.objects:
            raise ObjectError(412)
        self.objects[key]=body
        if self.failure=='after':
            self.failure=None
            raise TimeoutError('Synthetic outcome unknown after committed write')
        return types.SimpleNamespace(headers={'etag':'synthetic-etag'})


class WorkerFixture:
    def __init__(self,monkeypatch):
        self.storage=Storage()
        self.memory=InMemorySaver()
        self.enabled=True
        self.pause_after_model=False
        self.model_calls=[]
        self.cancel_evaluation_call=None
        self.flow_calls=[]
        config=json.loads((ROOT/'contracts/sonda/v1/fixtures/01-configuration.json').read_text())
        captured=json.loads((ROOT/'contracts/sonda/v1/fixtures/02-observations.json').read_text())['payload']
        self.binding={'registry_id':config['registry_id'],'generation_id':config['generation_id'],
            'contract_sha256':config['contract_sha256'],'credential_name':'existing-fixture-credential',
            'namespace':'synthetic','artifacts_bucket':'synthetic-artifacts','region':'us-chicago-1',
            'model_id':'fixture-selected-model','model_revision':'fixture-real-revision','compartment_id':'fixture-compartment'}
        criterion=config['payload']['criteria'][0]
        epoch=datetime(1970,1,1,tzinfo=timezone.utc)
        seconds=criterion['case_window_hours']*3600
        observed=core.instant(captured['observations'][0]['observed_at'])
        start=epoch+timedelta(seconds=((observed-epoch)//timedelta(seconds=seconds))*seconds)
        self.projection={'configuration':config,'state':None,'execution_results':[], 'work':{
            'scope':{key:config[key] for key in ('registry_id','generation_id','contract_version','contract_sha256')},
            'investigation_id':core.case_key(criterion,start),'criterion':criterion,'config_revision':1,
            'window_start':core.canonical_utc(start.isoformat()),'phase':'evaluate','input_revision':core.digest(['worker-fixture',1]),
            'requests_complete':True,'pending_inputs':[{'observation':row,'config_revision':1,
                'source_id':captured['source_id'],'query_id':captured['query_id']} for row in captured['observations']]}}
        for name in ('aidputils','aidputils.agents','aidputils.agents.toolkit'):
            package=types.ModuleType(name)
            package.__path__=[]
            monkeypatch.setitem(sys.modules,name,package)
        sdk=types.ModuleType('aidputils.agents.toolkit.agent_helper')
        owner=self
        class Invoker:
            def __init__(self,**kwargs):
                assert kwargs['model_id']==owner.binding['model_id']
                assert kwargs['auth_type']=='API_KEY' and kwargs['client'] is not None
            def with_structured_output(self,schema,**kwargs):
                role={'Verdict':'evaluate','Plan':'plan','Prose':'report','Drafts':'propose'}[schema['title']]
                class Inference:
                    async def astream(self,messages):
                        assert role=='report'
                        payload=json.loads(messages[-1].content)
                        owner.model_calls.append(('report_stream',payload))
                        yield {'summary':'La evidencia','title':'Informe'}
                        owner.stream_paused.set()
                        await owner.release_stream.wait()
                        yield {'title':'Informe','summary':'La evidencia disponible respalda este informe.'}

                    async def ainvoke(self,messages):
                        payload=json.loads(messages[-1].content)
                        owner.model_calls.append((role,payload))
                        if role=='evaluate' and len([entry for entry in owner.model_calls if entry[0]=='evaluate'])==owner.cancel_evaluation_call:
                            raise asyncio.CancelledError('Synthetic process interruption between evaluations')
                        if owner.pause_after_model:
                            owner.enabled=False
                        if role=='evaluate':
                            text=payload['observation']['text']
                            return {'score':0.95 if 'price' in text else 0.05,'reason':'Synthetic semantic fixture.','quotes':[text]}
                        if role=='plan':return {'requests':[],'propose':False}
                        if role=='report':return {'title':'Informe','summary':'La evidencia disponible respalda este informe.'}
                        return {'changes':[]}
                return Inference()
        sdk.GenAIChatInvoker=Invoker
        sdk.GenerativeAiInferenceV2Client=lambda **kwargs:object()
        @contextmanager
        def flow_setup(**kwargs):
            owner.flow_calls.append(kwargs)
            yield {'configurable':{'thread_id':kwargs.get('session_id','native-worker-session'),'checkpoint_ns':'default'},
                'metadata':{'checkpoint_ns':'default','authorization_marker':'preserved'}}
        sdk.flow_setup=flow_setup
        monkeypatch.setitem(sys.modules,sdk.__name__,sdk)
        self.module=types.ModuleType('trusted_generated_sonda_worker_graph')
        monkeypatch.setitem(sys.modules,self.module.__name__,self.module)
        exec(compile(investigator_source(),'trusted-generated-sonda-investigator.py','exec'),self.module.__dict__)
        self.module.runtime_storage=lambda binding:(self.storage,object())
        self.module.generation_inference=lambda storage,binding:self.enabled
        self.module.generation_control=lambda storage,binding:{}  # Legacy fixture has Activity disabled.
        self.module.native_memory=lambda generation,agent:GenerationMemory(self.memory,generation)
        self.module.WORKER_BINDING=dict(self.binding)
        self.agent=self.module.SondaInvestigator()
        self.agent.setup()
        self.request=self.store_projection(self.projection)

    def store_projection(self,projection):
        raw=contract.encoded(projection)
        digest=hashlib.sha256(raw).hexdigest()
        request={'version':1,'work_id':str(uuid5(UUID(self.binding['generation_id']),digest)),'sha256':digest}
        self.storage.objects[self.object(request,'input')]=raw
        return request

    def object(self,request,suffix):
        return self.module.worker_object(self.binding,request,suffix)

    def checkpoint_config(self,request=None):
        request=request or self.request
        return {'configurable':{'thread_id':str(uuid5(UUID(self.binding['generation_id']),request['work_id']))}}

    async def invoke(self,request=None):
        response=await self.agent.invoke(json.dumps(request or self.request),session_id='native-worker-session')
        return json.loads(response['messages'][0].content)

    def stored_result(self,request=None):
        return json.loads(self.storage.objects[self.object(request or self.request,'result')])


def test_resume_partial_evaluation_batch_reuses_completed_inference_tasks(worker):
    async def check():
        assert len(worker.projection['work']['pending_inputs'])>=2
        worker.cancel_evaluation_call=2
        with pytest.raises(asyncio.CancelledError):
            await worker.invoke()
        checkpoint=(await worker.agent.graph.aget_state(worker.checkpoint_config())).values
        started_at=contract.canonical_utc(checkpoint['started_at'])
        worker.cancel_evaluation_call=None
        response=await worker.invoke()
        assert response['committed'] is True
        keys=[payload['observation']['observation_key'] for role,payload in worker.model_calls if role=='evaluate']
        first=worker.projection['work']['pending_inputs'][0]['observation']['observation_key']
        assert keys.count(first)==1
        assert len(worker.stored_result()['result']['evaluations'])==2
        assert {row['evaluated_at'] for row in worker.stored_result()['result']['evaluations']}=={started_at}
    asyncio.run(check())


def test_partial_work_cannot_reuse_cached_inference_after_model_revision_changes(worker):
    async def check():
        worker.cancel_evaluation_call=2
        with pytest.raises(asyncio.CancelledError):
            await worker.invoke()
        calls=len(worker.model_calls)
        worker.cancel_evaluation_call=None
        worker.agent.model.identity={**worker.agent.model.identity,'model_revision':'another-revision'}
        with pytest.raises(ValueError,match='another model or prompt'):
            await worker.invoke()
        assert len(worker.model_calls)==calls
    asyncio.run(check())


@pytest.fixture
def worker(monkeypatch):
    return WorkerFixture(monkeypatch)


def test_investigator_commits_exact_evidence_result_then_keeps_only_compact_terminal_state(worker):
    async def check():
        ack=await worker.invoke()
        assert ack=={'work_id':worker.request['work_id'],'input_sha256':worker.request['sha256'],'committed':True}
        stored=worker.stored_result()
        assert stored['work_id']==worker.request['work_id'] and stored['input_sha256']==worker.request['sha256']
        assert len(stored['result']['evaluations'])==2 and not stored['result']['reports']
        assert {row['decision'] for row in stored['result']['evaluations']}=={'relevant','not_relevant'}
        assert all(row['model_revision']==worker.binding['model_revision'] for row in stored['result']['evaluations'])
        state=await worker.agent.graph.aget_state(worker.checkpoint_config())
        assert not state.next and state.values['work']=={} and state.values['result']=={}
        assert state.values['response']==ack
        assert len(contract.encoded(state.values))<1500
        assert len(worker.model_calls)==2 and all(role=='evaluate' for role,_ in worker.model_calls)
    asyncio.run(check())


def test_cached_result_replay_preserves_create_only_bytes_and_compact_ack_without_model(worker):
    async def check():
        ack=await worker.invoke()
        key=worker.object(worker.request,'result')
        raw=worker.storage.objects[key]
        calls,puts=len(worker.model_calls),len(worker.storage.puts)
        replay=await worker.invoke()
        assert replay==ack
        assert worker.storage.objects[key]==raw and len(worker.storage.puts)==puts
        assert len(worker.model_calls)==calls
    asyncio.run(check())


@pytest.mark.parametrize('bad',['hash','scope','criterion'])
def test_invalid_work_identity_or_scope_blocks_model_and_result_publication(worker,bad):
    async def check():
        request=dict(worker.request)
        if bad=='hash':
            request['sha256']='f'*64
        else:
            projection=copy.deepcopy(worker.projection)
            if bad=='scope':projection['work']['scope']['generation_id']=str(UUID(int=999))
            else:projection['work']['investigation_id']='e'*64
            request=worker.store_projection(projection)
        with pytest.raises((ValueError,RuntimeError)):
            await worker.invoke(request)
        assert not worker.model_calls and worker.object(request,'result') not in worker.storage.objects
    asyncio.run(check())


def test_pause_before_invoke_performs_no_inference_or_result_write(worker):
    async def check():
        worker.enabled=False
        with pytest.raises(RuntimeError,match='paused'):
            await worker.invoke()
        assert not worker.model_calls and not worker.storage.puts
    asyncio.run(check())


def test_pause_after_model_stops_publication_then_resumes_persist_without_reinference(worker):
    async def check():
        worker.pause_after_model=True
        with pytest.raises(RuntimeError,match='paused during inference'):
            await worker.invoke()
        assert worker.object(worker.request,'result') not in worker.storage.objects
        count=len(worker.model_calls)
        interrupted=await worker.agent.graph.aget_state(worker.checkpoint_config())
        assert interrupted.next==('persist',)
        worker.enabled=True
        worker.pause_after_model=False
        ack=await worker.invoke()
        assert ack['committed'] and len(worker.model_calls)==count
        terminal=await worker.agent.graph.aget_state(worker.checkpoint_config())
        assert not terminal.next and terminal.values['work']==terminal.values['result']=={}
    asyncio.run(check())


@pytest.mark.parametrize('failure',['before','after'])
def test_unknown_write_outcome_reconciles_same_work_without_repeating_model_and_cleans_state(worker,failure):
    async def check():
        worker.storage.failure=failure
        with pytest.raises(TimeoutError,match='outcome unknown'):
            await worker.invoke()
        calls=len(worker.model_calls)
        ack=await worker.invoke()
        assert ack=={'work_id':worker.request['work_id'],'input_sha256':worker.request['sha256'],'committed':True}
        assert len(worker.model_calls)==calls and len(worker.stored_result()['result']['evaluations'])==2
        terminal=await worker.agent.graph.aget_state(worker.checkpoint_config())
        assert not terminal.next and terminal.values['work']==terminal.values['result']=={}
    asyncio.run(check())


def test_wrong_hash_does_not_poison_the_correct_immutable_work_in_the_same_session(worker):
    async def check():
        bad={**worker.request,'sha256':'f'*64}
        with pytest.raises(ValueError):await worker.invoke(bad)
        assert not worker.model_calls
        ack=await worker.invoke()
        assert ack['committed'] and len(worker.model_calls)==2
    asyncio.run(check())


def test_create_only_conflict_accepts_first_durable_result_without_overwrite_or_reinference(worker):
    async def check():
        worker.storage.failure='before'
        with pytest.raises(TimeoutError):await worker.invoke()
        pending=await worker.agent.graph.aget_state(worker.checkpoint_config())
        winner=contract.encoded({'work_id':worker.request['work_id'],'input_sha256':worker.request['sha256'],
            'result':pending.values['result']})
        worker.storage.conflicting_result=winner
        calls=len(worker.model_calls)
        ack=await worker.invoke()
        assert ack['committed'] and len(worker.model_calls)==calls
        assert worker.storage.objects[worker.object(worker.request,'result')]==winner
        terminal=await worker.agent.graph.aget_state(worker.checkpoint_config())
        assert not terminal.next and terminal.values['work']==terminal.values['result']=={}
    asyncio.run(check())


def test_cached_object_must_bind_input_hash_before_any_model_or_result_overwrite(worker):
    async def check():
        key=worker.object(worker.request,'result')
        invalid=contract.encoded({'work_id':worker.request['work_id'],'input_sha256':'e'*64,'result':{}})
        worker.storage.objects[key]=invalid
        with pytest.raises(RuntimeError,match='does not match its input'):
            await worker.invoke()
        assert not worker.model_calls and not worker.storage.puts
        assert worker.storage.objects[key]==invalid
    asyncio.run(check())


class ActivityObjects:
    """Conditional Activity IO only; private work remains in the storage fixture."""
    def __init__(self):
        self.rows={}
        self.puts=[]

    def get(self,role,key):
        assert role=='artifacts'
        return self.rows.get(key)

    def put(self,role,key,raw,expected):
        assert role=='artifacts'
        prior=self.rows.get(key)
        if (prior.etag if prior else None)!=expected:
            raise RuntimeError('Synthetic conditional-write conflict')
        self.puts.append((key,raw,expected))
        self.rows[key]=types.SimpleNamespace(data=raw,etag=str(len(self.puts)))
        return self.rows[key].etag


def enable_worker_activity(worker,monkeypatch):
    module=worker.module
    objects=ActivityObjects()
    now=datetime.now(timezone.utc)-timedelta(seconds=1)
    agent_id,ref=str(UUID(int=777)),str(UUID(int=778))
    worker.binding['activity_agent_id']=agent_id
    worker.agent.binding['activity_agent_id']=agent_id
    digest=hashlib.sha256(contract.encoded(worker.request)).hexdigest()
    session_sha=hashlib.sha256(worker.request['work_id'].encode()).hexdigest()
    descriptor=module.ActivityProducer_AdmissionDescriptor.model_validate(dict(
        schema_version=module.ActivityProducer_PRODUCER_VERSION,admission_id=ref,
        scope={key:worker.binding[key] for key in ('registry_id','generation_id')},
        run=dict(run_id=str(UUID(int=779)),request_id=str(UUID(int=780)),attempt_id=str(UUID(int=781)),
            work_id=worker.request['work_id'],investigation_id=worker.projection['work']['investigation_id'],
            execution_kind='native_invocation',component='native_agent',agent_id=agent_id,
            agent_name='ai_social_network_investigator',source='autonomous_study',created_at=now.isoformat(),synthetic=True),
        native_session_sha256=session_sha,input_sha256=digest,config_revision=1,tool_catalog_sha256='a'*64,
        created_at=now.isoformat(),expires_at=(now+timedelta(minutes=30)).isoformat(),work_input_sha256=worker.request['sha256']))
    current=module.ActivityProducer_CurrentPointer(schema_version=descriptor.schema_version,scope=descriptor.scope,
        agent_id=agent_id,native_session_sha256=session_sha,admission_id=ref,run_id=descriptor.run.run_id,
        request_id=descriptor.run.request_id,attempt_id=descriptor.run.attempt_id,epoch=1,status='admitted',
        owner_nonce=None,expires_at=descriptor.expires_at)
    root,key=module.ActivityJournal_admission_keys(descriptor.scope,agent_id,session_sha,ref)
    objects.rows[root+'descriptor.json']=types.SimpleNamespace(data=module.ActivityProducer_producer_encoded(descriptor.model_dump(mode='json')),etag='descriptor')
    objects.rows[key]=types.SimpleNamespace(data=module.ActivityProducer_producer_encoded(current.model_dump(mode='json')),etag='current')
    module.generation_control=lambda storage,binding:{'activity':{'enabled':True,'manifest_sha256':module.ACTIVITY_MANIFEST_SHA256}}
    module.OciObjects=lambda *args:objects
    context=types.ModuleType('aidputils.agents.toolkit.chat_context')
    context.session_context_var=types.SimpleNamespace(get=lambda:{'sessionvariables.sondaActivityRef':{'value':ref}})
    monkeypatch.setitem(sys.modules,context.__name__,context)
    worker.stream_paused,worker.release_stream=asyncio.Event(),asyncio.Event()
    return objects,root


def activity_events(objects):
    return [event for key,blob in objects.rows.items() if '/batches/' in key for event in json.loads(blob.data)['events']]


def report_projection(worker):
    projection=copy.deepcopy(worker.projection)
    projection['work'].update(phase='report',pending_inputs=[],
        facts={'distinct_documents':0,'distinct_observations':0,'relevant_documents':0,'missing_evaluations':0})
    worker.projection=projection
    worker.request=worker.store_projection(projection)


async def invoke_activity_worker(worker):
    return await worker.agent.invoke(json.dumps(worker.request),session_id=worker.request['work_id'])


def test_enabled_worker_real_graph_streams_only_public_report_summary_and_returns_pending_step(worker,monkeypatch):
    async def check():
        report_projection(worker)
        objects,root=enable_worker_activity(worker,monkeypatch)
        task=asyncio.create_task(invoke_activity_worker(worker))
        await asyncio.wait_for(worker.stream_paused.wait(),3)
        assert not task.done()
        events=activity_events(objects)
        assert [event['public_text_delta'] for event in events if event.get('type')=='text_delta']==['La evidencia']
        assert any(key.endswith('/head.json') for key in objects.rows)
        assert not any(event.get('kind')=='execution_completion' for event in events)
        assert not any('/results/' in key for key in objects.rows)
        assert worker.object(worker.request,'result') not in worker.storage.objects
        worker.release_stream.set()
        response=await asyncio.wait_for(task,3)
        assert json.loads(response['messages'][0].content)['committed'] is True
        stored=worker.stored_result()['result']
        artifact=[json.loads(blob.data) for key,blob in objects.rows.items() if '/results/' in key]
        assert len(artifact)==1 and artifact[0]['kind']=='native_step'
        payload=artifact[0]['payload']
        assert payload=={'work_id':worker.request['work_id'],'input_sha256':worker.request['sha256'],
            'investigation_id':worker.projection['work']['investigation_id'],'phase':'report_prepared',
            'report_refs':[{'report_id':stored['reports'][0]['report_id'],'revision':stored['reports'][0]['revision']}],
            'evaluation_count':0,'request_count':0,'proposal_count':0,'publication_status':'pending'}
        events=activity_events(objects)
        assert len([event for event in events if event.get('kind')=='execution_completion'])==1
        text=''.join(event['public_text_delta'] for event in events if event.get('type')=='text_delta')
        assert text=='La evidencia disponible respalda este informe.'
        assert 'Informe' not in text and 'reason' not in text and 'requests' not in text
        assert [role for role,_ in worker.model_calls]==['report_stream']
        puts=len(objects.puts)
        private_puts=len(worker.storage.puts)
        replay=await invoke_activity_worker(worker)
        assert replay==response and len(objects.puts)==puts and len(worker.storage.puts)==private_puts
        assert [role for role,_ in worker.model_calls]==['report_stream']
    asyncio.run(check())


def test_enabled_worker_evaluation_emits_no_reason_or_model_json_and_reports_pending(worker,monkeypatch):
    async def check():
        objects,root=enable_worker_activity(worker,monkeypatch)
        await invoke_activity_worker(worker)
        events=activity_events(objects)
        assert not any(event.get('type')=='text_delta' for event in events)
        artifact=next(json.loads(blob.data) for key,blob in objects.rows.items() if '/results/' in key)
        assert artifact['kind']=='native_step'
        assert artifact['payload']['phase']=='evaluated'
        assert artifact['payload']['evaluation_count']==2
        assert artifact['payload']['publication_status']=='pending'
        assert artifact['payload']['report_refs']==[]
        assert all(role=='evaluate' for role,_ in worker.model_calls)
        public=json.dumps(events)+json.dumps(artifact)
        assert 'Synthetic semantic fixture.' not in public
        assert 'score' not in public and 'observation' not in public
    asyncio.run(check())


def test_cached_large_private_work_result_never_becomes_public_artifact_or_new_admission(worker,monkeypatch):
    async def check():
        await worker.invoke()
        saved=worker.stored_result()
        now=datetime.now(timezone.utc).isoformat()
        evidence=[]
        for index in range(500):
            quote='Synthetic private supporting evidence '+('x'*3900)+str(index)
            observation='a'*64
            evidence.append({'evidence_id':contract.sha256(contract.encoded([observation,quote])),
                'observation_key':observation,'document_key':'b'*64,'url':'https://example.com/evidence',
                'quote':quote,'captured_at':now})
        criterion=worker.projection['work']['criterion']
        report=contract.Report.model_validate(dict(report_id='c'*64,investigation_id=worker.projection['work']['investigation_id'],
            revision=1,config_revision=1,criterion_id=criterion['criterion_id'],criterion_revision=criterion['revision'],
            status='completed_with_limits',title='Informe privado sintético',summary='Evidencia privada acotada.',
            indicators=[],evidence=evidence,evaluations=[],coverage=dict(distinct_documents=500,distinct_observations=500,
                query_ids=[],pending_requests=0,failed_requests=0,exhausted_budget=False,limitations=['Synthetic fixture']),
            model_id=worker.binding['model_id'],model_revision=worker.binding['model_revision'],prompt_version=worker.agent.model.identity['prompt_version'],
            created_at=now)).model_dump(mode='json')
        saved['result']['reports']=[report]
        raw=contract.encoded(saved)
        assert 1048576<len(raw)<=4194304
        worker.storage.objects[worker.object(worker.request,'result')]=raw
        objects,root=enable_worker_activity(worker,monkeypatch)
        calls=len(worker.model_calls)
        response=await invoke_activity_worker(worker)
        assert json.loads(response['messages'][0].content)['committed'] is True
        assert not objects.puts and not activity_events(objects)
        assert not any('/results/' in key for key in objects.rows)
        assert len(worker.model_calls)==calls
        assert worker.storage.objects[worker.object(worker.request,'result')]==raw
    asyncio.run(check())
