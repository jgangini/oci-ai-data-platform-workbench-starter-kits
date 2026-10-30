"""Native Sonda chat v2; all data comes from one committed, scoped Gold publication."""
import asyncio
from dataclasses import asdict
import json
from typing import TypedDict
from app.sonda_contract import ChatInput, encoded, no_duplicate_keys, sha256

from app.labs.ai_social_network.notebooks.sonda_io import generation_inference, runtime_storage
from app.labs.ai_social_network.notebooks.agent.sonda_memory import native_memory, root_graph_config
from app.labs.ai_social_network.notebooks.agent.sonda_chat import (
    BoundChat, SONDA_PROMPT, checked_binding, checked_plan_v2, configuration_sql,
    public_response, query_evidence, session_key, snapshot_from_row, snapshot_sql, tool_sql, validate_input)
from app.labs.ai_social_network.notebooks.agent.social_agent import (
    CLARIFICATION_ANSWER, conversation_context, plan_for_question, planning_error, planning_schema, previous_query, query, verified_plan)

CHAT_BINDING = {}


class ChatStateV2(TypedDict):
    messages: list
    raw_input: str
    bound: dict
    snapshot: dict
    plan: dict
    queries: list
    response: dict
    request_sha256: str
    planning_diagnostics: list
    plan_repairs: int


class SondaChatAgent:
    def setup(self):
        from aidputils.agents.toolkit.agent_helper import GenAIChatInvoker, GenerativeAiInferenceV2Client
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
        from langgraph.graph import END, START, StateGraph
        self.binding=dict(CHAT_BINDING)
        checked_binding(self.binding)
        self.storage,signer=runtime_storage(self.binding)
        endpoint='https://inference.generativeai.'+self.binding['region']+'.oci.oraclecloud.com'
        llm=GenAIChatInvoker(provider='generic',model_id=self.binding['model_id'],auth_type='API_KEY',
            compartment_id=self.binding['compartment_id'],service_endpoint=endpoint,
            client=GenerativeAiInferenceV2Client(endpoint=endpoint,signer=signer),is_stream=False,
            model_kwargs={'temperature':0,'max_tokens':2048},guardrails_config={'policies':[]})
        planner=llm.with_structured_output(planning_schema('SondaQuery',('post','comment','page','indicator')),
            method='json_schema')

        async def prepare(state):
            if not await asyncio.to_thread(generation_inference,self.storage,self.binding):
                raise RuntimeError('Sonda is paused or its generation is not ready')
            rows=await asyncio.to_thread(query,self.binding,snapshot_sql(self.binding))
            if len(rows)!=1:
                raise RuntimeError('A committed Sonda publication is not available')
            snapshot=snapshot_from_row(self.binding,rows[0])
            configurations=await asyncio.to_thread(query,self.binding,configuration_sql(self.binding,snapshot))
            if len(configurations)!=1:
                raise RuntimeError('Approved Sonda configuration is not available')
            bound=validate_input(state['raw_input'],self.binding,snapshot,configurations[0])
            return {'bound':asdict(bound),'snapshot':snapshot,'queries':[],'plan':{},'response':{},'planning_diagnostics':[],
                'plan_repairs':0,
                'messages':state['messages'][-8:]+[HumanMessage(content=bound.question)]}

        async def plan(state):
            bound=BoundChat(**state['bound'])
            payload={'question':bound.question,'conversation':conversation_context(state['messages']),
                'publication':state['snapshot'],'queries':state['queries'],'search_context':bound.search_context()}
            candidate=None if state['queries'] else verified_plan(bound.question,previous_query(state['messages']))
            diagnostics=list(state.get('planning_diagnostics',[]))
            repairs=state.get('plan_repairs',0)
            attempts=2 if repairs==0 else 1
            for attempt in range(attempts):
                stage='structured_output'
                try:
                    if candidate is None:
                        candidate=await planner.ainvoke([SystemMessage(content=SONDA_PROMPT),
                            HumanMessage(content=json.dumps(payload,ensure_ascii=False))])
                    if not state['queries']:
                        stage='request_normalization'
                        candidate=plan_for_question(state['snapshot'],candidate,bound.question)
                    stage='plan_validation'
                    candidate=checked_plan_v2(candidate,state['queries'],state['snapshot'])
                    if candidate['tool']!='finish':
                        stage='query_validation'
                        tool_sql(self.binding,state['snapshot'],bound,candidate['tool'],candidate['args'])
                    return {'plan':candidate,'planning_diagnostics':diagnostics,'plan_repairs':repairs}
                except ValueError as error:
                    diagnostic=planning_error(stage,error)
                    diagnostics=[*diagnostics,diagnostic][-2:]
                    repairs=1
                    payload['validation_error']=diagnostic
                    candidate=None
            return {'plan':{'tool':'clarify','args':{}},'planning_diagnostics':diagnostics,'plan_repairs':repairs}

        async def execute(state):
            bound=BoundChat(**state['bound'])
            plan=state['plan']
            rows=await asyncio.to_thread(query,self.binding,tool_sql(self.binding,state['snapshot'],bound,plan['tool'],plan['args']))
            item=query_evidence(state['snapshot'],bound,plan['tool'],plan['args'],rows)
            return {'queries':[*state['queries'],item]}

        async def answer(state):
            bound=BoundChat(**state['bound'])
            if state['plan']['tool']=='clarify':
                result={'answer':CLARIFICATION_ANSWER,'post_keys':[]}
            else:
                schema={'title':'SondaAnswer','type':'object','additionalProperties':False,
                    'required':['answer','post_keys'],'properties':{'answer':{'type':'string','maxLength':1800},
                        'post_keys':{'type':'array','maxItems':8,'items':{'type':'string'}}}}
                result=await llm.with_structured_output(schema,method='json_schema').ainvoke([
                    SystemMessage(content=SONDA_PROMPT+'\nResponde claro y brevemente en español. Usa exclusivamente '
                        'las consultas ejecutadas en este turno; cita sólo post_keys presentes. No muestres código, JSON, SQL '
                        'ni repitas las filas del gráfico. Una métrica desconocida no equivale a cero.'),
                    HumanMessage(content=json.dumps({'question':bound.question,'queries':state['queries']},ensure_ascii=False))])
            if not isinstance(result,dict) or set(result)!={'answer','post_keys'} or '```' in result['answer']:
                raise RuntimeError('The answer is not a verified plain-language response')
            response=public_response(state['snapshot'],bound,result['answer'],state['queries'],
                state['plan'].get('presentation','auto'),result['post_keys'])
            compact=AIMessage(content=json.dumps({key:response[key] for key in ('answer','query_context','search_context')},ensure_ascii=False))
            return {'response':response,'messages':[*state['messages'],compact][-8:],
                'queries':[],'raw_input':''}

        graph=StateGraph(ChatStateV2)
        for name,node in (('prepare',prepare),('plan',plan),('execute',execute),('answer',answer)):
            graph.add_node(name,node)
        graph.add_edge(START,'prepare')
        graph.add_edge('prepare','plan')
        graph.add_conditional_edges('plan',lambda state:'answer' if state['plan']['tool'] in ('finish','clarify') else 'execute')
        graph.add_conditional_edges('execute',lambda state:'plan' if state['plan'].get('continue_query') and len(state['queries'])<4 else 'answer')
        graph.add_edge('answer',END)
        self.graph=graph.compile(checkpointer=native_memory(self.binding['generation_id'],'ai_social_network'))

    async def invoke(self,user_query,**kwargs):
        from aidputils.agents.toolkit.agent_helper import flow_setup
        from langchain_core.messages import AIMessage
        request=ChatInput.model_validate(json.loads(user_query,object_pairs_hook=no_duplicate_keys))
        digest=sha256(encoded(request.model_dump(mode='json')))
        if not await asyncio.to_thread(generation_inference,self.storage,self.binding):
            raise RuntimeError('Sonda is paused or its generation is not ready')
        with flow_setup(**kwargs) as native_config:
            config=root_graph_config(native_config)
            native=(config.get('configurable') or {}).get('thread_id')
            config.setdefault('configurable',{})['thread_id']=session_key(self.binding,native)
            config['recursion_limit']=18
            history=await self.graph.aget_state(config)
            previous=history.values or {}
            if history.next and previous.get('request_sha256')==digest:
                if 'prepare' not in history.next:
                    rows=await asyncio.to_thread(query,self.binding,snapshot_sql(self.binding))
                    if len(rows)!=1 or snapshot_from_row(self.binding,rows[0])['config_revision']!=previous.get('snapshot',{}).get('config_revision'):
                        raise RuntimeError('Approved configuration changed during this interrupted turn; start a new question')
                result=await self.graph.ainvoke(None,config=config)
            else:
                messages=previous.get('messages',[])
                if history.next:
                    if messages and messages[-1].type=='human':
                        messages=messages[:-1]
                    await self.graph.aupdate_state(config,{'queries':[],'raw_input':'','response':{},
                        'messages':messages},as_node='answer')
                result=await self.graph.ainvoke({'raw_input':user_query,'request_sha256':digest,
                    'messages':messages},config=config)
            return {'messages':[AIMessage(content=json.dumps(result['response'],ensure_ascii=False))]}
