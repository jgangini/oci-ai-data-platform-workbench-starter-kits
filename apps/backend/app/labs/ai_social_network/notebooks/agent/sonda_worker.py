"""Native case investigator; only consumes immutable work published by the Gold runner."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import TypedDict
from uuid import UUID, uuid5

from app.sonda_contract import encoded, no_duplicate_keys, reject_secrets
from app.sonda_activity import current_activity
from app.sonda_activity_entry import activity_enabled, native_activity
from app.labs.ai_social_network.notebooks.sonda_io import (
    generation_inference, operational_prefix, runtime_storage, verified_control, worker_request)
from app.labs.ai_social_network.notebooks.agent.sonda_model import StudyModel, STUDY_PROMPT_VERSION
from app.labs.ai_social_network.notebooks.agent.sonda_study import study_case
from app.labs.ai_social_network.notebooks.agent.sonda_memory import native_memory, root_graph_config

WORKER_BINDING = {}


def worker_object(binding,request,suffix):
    if suffix not in ('input','result'):
        raise ValueError('Unknown work object')
    return operational_prefix(binding)+'work/'+request['work_id']+'/'+suffix+'.json'


def saved_work_result(storage,binding,request):
    try:
        raw=verified_control(storage,binding,worker_object(binding,request,'result'))
    except Exception as exc:
        if getattr(exc,'status',None)==404:
            return None
        raise
    value=json.loads(raw,object_pairs_hook=no_duplicate_keys)
    if (set(value)!={'work_id','input_sha256','result'} or value['work_id']!=request['work_id']
        or value['input_sha256']!=request['sha256']):
        raise RuntimeError('Committed work result does not match its input')
    reject_secrets(value)
    return value


def read_work_input(storage,binding,request):
    raw=verified_control(storage,binding,worker_object(binding,request,'input'))
    if hashlib.sha256(raw).hexdigest()!=request['sha256']:
        raise ValueError('Native work input hash does not match')
    work=json.loads(raw,object_pairs_hook=no_duplicate_keys)
    reject_secrets(work)
    if set(work)!={'configuration','work','state','execution_results'}:
        raise ValueError('Unexpected native work projection')
    scope=work['work']['scope']
    if any(scope.get(key)!=binding[key] for key in ('registry_id','generation_id','contract_sha256')):
        raise ValueError('Native work belongs to another generation')
    return work


def native_step_result(request,work,result):
    reports=result['reports']
    counts={name:len(result[key]) for name,key in (('evaluation_count','evaluations'),
        ('request_count','investigation_requests'),('proposal_count','proposals'))}
    phase='report_prepared' if reports else ('evaluated' if any(counts.values()) else 'no_change')
    return {'work_id':request['work_id'],'input_sha256':request['sha256'],
        'investigation_id':work['work']['investigation_id'],'phase':phase,
        'report_refs':[{'report_id':row['report_id'],'revision':row['revision']} for row in reports],
        **counts,'publication_status':'pending'}


class InvestigatorState(TypedDict):
    request: dict
    work: dict
    result: dict
    response: dict
    model_identity: dict
    started_at: str


class SondaInvestigator:
    def setup(self):
        from aidputils.agents.toolkit.agent_helper import GenAIChatInvoker, GenerativeAiInferenceV2Client
        from langgraph.func import task
        from langgraph.graph import START, END, StateGraph
        self.binding=dict(WORKER_BINDING)
        self.storage,self.signer=runtime_storage(self.binding)
        endpoint='https://inference.generativeai.'+self.binding['region']+'.oci.oraclecloud.com'
        invoker=GenAIChatInvoker(provider='generic',model_id=self.binding['model_id'],auth_type='API_KEY',
            compartment_id=self.binding['compartment_id'],service_endpoint=endpoint,
            client=GenerativeAiInferenceV2Client(endpoint=endpoint,signer=self.signer),is_stream=False,
            model_kwargs={'temperature':0,'max_tokens':4096},guardrails_config={'policies':[]})
        self.model=StudyModel(invoker,identity={'model_id':self.binding['model_id'],
            'model_revision':self.binding['model_revision'],'prompt_version':STUDY_PROMPT_VERSION})

        @task(name='sonda_case_inference')
        async def infer(role,payload):
            if not await asyncio.to_thread(generation_inference,self.storage,self.binding):
                raise RuntimeError('Sonda generation was paused before inference')
            activity=current_activity()
            if activity:
                await activity.stage({'evaluate':'Evaluando la relevancia de las observaciones.',
                    'plan':'Preparando una investigación con las herramientas autorizadas.',
                    'report':'Redactando el informe con la evidencia disponible.',
                    'propose':'Revisando recomendaciones de configuración.'}[role])
            return await self.model(role,payload)

        async def durable_model(role,payload):
            return await infer(role,payload)
        durable_model.identity=self.model.identity

        async def load(state):
            if not await asyncio.to_thread(generation_inference,self.storage,self.binding):
                raise RuntimeError('Sonda generation is paused or its reset is incomplete')
            request=state['request']
            work=await asyncio.to_thread(read_work_input,self.storage,self.binding,request)
            return {'work':work,'model_identity':dict(self.model.identity),
                'started_at':datetime.now(timezone.utc).isoformat()}

        async def investigate(state):
            if not await asyncio.to_thread(generation_inference,self.storage,self.binding):
                raise RuntimeError('Sonda generation was paused before inference')
            work=state['work']
            result=await study_case(work['configuration'],work['work'],work['state'],
                work['execution_results'],model=durable_model,
                now=datetime.fromisoformat(state['started_at']),within_parent_graph=True)
            return {'result':result}

        async def persist(state):
            request=state['request']
            # Re-check the fence after inference; a maintenance transition must not
            # publish more work. Gold still revalidates approval/budget/CAS on commit.
            if not await asyncio.to_thread(generation_inference,self.storage,self.binding):
                raise RuntimeError('Sonda generation was paused during inference')
            response={'work_id':request['work_id'],'input_sha256':request['sha256'],'result':state['result']}
            raw=encoded(response)
            if len(raw)>4194304:
                raise RuntimeError('Native case result exceeds the bounded work contract')
            try:
                await asyncio.to_thread(self.storage.put_object,self.binding['namespace'],
                    self.binding['artifacts_bucket'],worker_object(self.binding,request,'result'),raw,
                    if_none_match='*',content_type='application/json')
            except Exception as exc:
                if getattr(exc,'status',None)!=412:
                    raise
                response=await asyncio.to_thread(saved_work_result,self.storage,self.binding,request)
            activity=current_activity()
            if activity:
                await activity.complete_result('native_step',native_step_result(request,state['work'],response['result']))
            return {'response':{'work_id':request['work_id'],'input_sha256':request['sha256'],
                'committed':True},'work':{},'result':{}}

        graph=StateGraph(InvestigatorState)
        for name,node in (('load',load),('investigate',investigate),('persist',persist)):
            graph.add_node(name,node)
        graph.add_edge(START,'load')
        graph.add_edge('load','investigate')
        graph.add_edge('investigate','persist')
        graph.add_edge('persist',END)
        memory=native_memory(self.binding['generation_id'],'ai_social_network_investigator')
        self.graph=graph.compile(checkpointer=memory)

    async def _scoped_work(self,request,response,native_config):
        config=root_graph_config(native_config)
        config.setdefault('configurable',{})['thread_id']=str(uuid5(
            UUID(self.binding['generation_id']),request['work_id']))
        config['recursion_limit']=8
        previous=await self.graph.aget_state(config)
        values=previous.values or {}
        if values and values.get('request')!=request:
            raise ValueError('A work identity cannot be reused with another hash')
        if values.get('model_identity') not in (None,self.model.identity):
            raise ValueError('Pending work belongs to another model or prompt revision')
        if response is not None:
            response={'work_id':request['work_id'],'input_sha256':request['sha256'],'committed':True}
            if values and (previous.next or values.get('work') or values.get('result')):
                # A remote write may commit before its HTTP response times out.
                # Reconcile the known result without repeating inference.
                await self.graph.aupdate_state(config,{'response':response,'work':{},'result':{}},as_node='persist')
        else:
            result=await self.graph.ainvoke(None if previous.next else {'request':request},config=config)
            response=result['response']
        return response

    async def invoke(self,user_query,**kwargs):
        from aidputils.agents.toolkit.agent_helper import flow_setup
        from langchain_core.messages import AIMessage
        request=worker_request(user_query)
        if request['work_id']!=str(uuid5(UUID(self.binding['generation_id']),request['sha256'])):
            raise ValueError('Work UUID does not match its generation and input hash')
        if not await asyncio.to_thread(generation_inference,self.storage,self.binding):
            raise RuntimeError('Sonda generation is paused')
        response=await asyncio.to_thread(saved_work_result,self.storage,self.binding,request)
        with flow_setup(**kwargs) as native_config:
            if response is None and await asyncio.to_thread(activity_enabled,self.storage,self.binding):
                work=await asyncio.to_thread(read_work_input,self.storage,self.binding,request)
                facts={'work_id':request['work_id'],'work_input_sha256':request['sha256'],
                    'investigation_id':work['work']['investigation_id']}
                digest=hashlib.sha256(encoded(request)).hexdigest()
                async with native_activity(self.storage,self.binding,native_config,digest,protected_work=facts):
                    response=await self._scoped_work(request,response,native_config)
            else:
                response=await self._scoped_work(request,response,native_config)
        return {'messages':[AIMessage(content=json.dumps(response,ensure_ascii=False,separators=(',',':')))]}
