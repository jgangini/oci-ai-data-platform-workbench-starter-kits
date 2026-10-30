"""Native Sonda chat v2; all data comes from one committed, scoped Gold publication."""
import asyncio
from dataclasses import asdict
import json
from typing import TypedDict

from app.labs.ai_social_network.notebooks.sonda_io import generation_inference, runtime_storage
from app.labs.ai_social_network.notebooks.agent.sonda_memory import native_memory, root_graph_config
from app.labs.ai_social_network.notebooks.agent.sonda_chat import (
    BoundChat, SONDA_PROMPT, checked_binding, checked_plan_v2, configuration_sql,
    public_response, query_evidence, session_key, snapshot_from_row, snapshot_sql, tool_sql, validate_input)
from app.labs.ai_social_network.notebooks.agent.social_agent import (
    TOOLS, NETWORKS, conversation_context, plan_for_question, previous_query, query, verified_plan)

CHAT_BINDING = {}


class ChatStateV2(TypedDict):
    messages: list
    raw_input: str
    bound: dict
    snapshot: dict
    plan: dict
    queries: list
    response: dict


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
        args_schema={'type':'object','additionalProperties':False,'properties':{
            **{key:{'type':'string'} for key in ('platform','date_from','date_to','text','post_key','period','entity','entity_scope','metric')},
            'document_type':{'type':'string','enum':['post','comment','page','indicator']},
            'platforms':{'type':'array','minItems':1,'maxItems':6,'items':{'type':'string','enum':list(NETWORKS)}},
            'limit':{'type':'integer','minimum':1,'maximum':50}}}
        planner=llm.with_structured_output({'title':'SondaQuery','type':'object','additionalProperties':False,
            'required':['tool','args','presentation','continue_query'],'properties':{
                'tool':{'type':'string','enum':[*TOOLS,'finish']},'args':args_schema,
                'presentation':{'type':'string','enum':['none','auto','table','bar','pie','line']},
                'continue_query':{'type':'boolean'}}},method='json_schema')

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
            return {'bound':asdict(bound),'snapshot':snapshot,'queries':[],'plan':{},'response':{},
                'messages':state['messages'][-8:]+[HumanMessage(content=bound.question)]}

        async def plan(state):
            bound=BoundChat(**state['bound'])
            payload={'question':bound.question,'conversation':conversation_context(state['messages']),
                'publication':state['snapshot'],'queries':state['queries'],'search_context':bound.search_context()}
            candidate=None if state['queries'] else verified_plan(bound.question,previous_query(state['messages']))
            for attempt in range(2):
                if candidate is None:
                    candidate=await planner.ainvoke([SystemMessage(content=SONDA_PROMPT),
                        HumanMessage(content=json.dumps(payload,ensure_ascii=False))])
                try:
                    if not state['queries']:
                        candidate=plan_for_question(state['snapshot'],candidate,bound.question)
                    candidate=checked_plan_v2(candidate,state['queries'],state['snapshot'])
                    if candidate['tool']!='finish':
                        tool_sql(self.binding,state['snapshot'],bound,candidate['tool'],candidate['args'])
                    return {'plan':candidate}
                except ValueError:
                    payload['validation_error']='Corrige el plan usando sólo las métricas, períodos y tipos de documento permitidos.'
                    candidate=None
            return {'plan':{'tool':'clarify','args':{}}}

        async def execute(state):
            bound=BoundChat(**state['bound'])
            plan=state['plan']
            rows=await asyncio.to_thread(query,self.binding,tool_sql(self.binding,state['snapshot'],bound,plan['tool'],plan['args']))
            item=query_evidence(state['snapshot'],bound,plan['tool'],plan['args'],rows)
            return {'queries':[*state['queries'],item]}

        async def answer(state):
            bound=BoundChat(**state['bound'])
            if state['plan']['tool']=='clarify':
                result={'answer':'Precisa las fuentes, la métrica y el período de la consulta. Para líneas, elige hasta 50 días.', 'post_keys':[]}
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
        with flow_setup(**kwargs) as native_config:
            config=root_graph_config(native_config)
            native=(config.get('configurable') or {}).get('thread_id')
            config.setdefault('configurable',{})['thread_id']=session_key(self.binding,native)
            config['recursion_limit']=18
            history=await self.graph.aget_state(config)
            previous=history.values or {}
            result=await self.graph.ainvoke({'raw_input':user_query,'messages':previous.get('messages',[])},config=config)
            return {'messages':[AIMessage(content=json.dumps(result['response'],ensure_ascii=False))]}
