"""Execute trusted generated source with real LangGraph and fake external SDK IO.

exec is confined to this regression harness. The produced runtime has ordinary
readable source and no exec/loader. No model, credential or SQL service is called.
"""
import asyncio
from contextlib import contextmanager
from datetime import date, timedelta
from functools import partial
import json
from pathlib import Path
import re
import sys
import types
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.sonda_runtime import chat_source
from app.labs.ai_social_network.notebooks.agent import sonda_chat as core
from app.labs.ai_social_network.notebooks.agent.sonda_memory import GenerationMemory

ROOT=Path(__file__).resolve().parents[3]


class AuthenticatedSaver(InMemorySaver):
    """Simulate Oracle retaining the authenticated session despite supplied thread_id."""
    def forced(self,config):
        return {**config,'configurable':{**config.get('configurable',{}),'thread_id':'authenticated-native-session'}}

    async def aget_tuple(self,config):
        return await super().aget_tuple(self.forced(config))

    async def aput(self,config,checkpoint,metadata,new_versions):
        return await super().aput(self.forced(config),checkpoint,metadata,new_versions)

    async def aput_writes(self,config,writes,task_id,task_path=''):
        return await super().aput_writes(self.forced(config),writes,task_id,task_path=task_path)


class JsonMessageSaver(AuthenticatedSaver):
    """Oracle's checkpoint JSON transport restores message models as dictionaries."""
    async def aget_tuple(self, config):
        value = await super().aget_tuple(config)
        if value is None:
            return None
        def wire(messages):
            return [message.model_dump(mode='json') if hasattr(message, 'model_dump') else message
                    for message in messages]
        channels = dict(value.checkpoint['channel_values'])
        if 'messages' in channels:
            channels['messages'] = wire(channels['messages'])
        writes = [(task, channel, wire(data) if channel == 'messages' else data)
                  for task, channel, data in (value.pending_writes or [])]
        return value._replace(checkpoint={**value.checkpoint, 'channel_values': channels}, pending_writes=writes)


class FakeGenAI:
    """Injected provider with the structured-output interface used by native agents."""
    def __init__(self,owner,**kwargs):
        self.owner=owner
        assert kwargs['auth_type']=='API_KEY' and kwargs['client'] is not None

    def with_structured_output(self,schema,**kwargs):
        if schema['title']=='SondaQuery':
            self.owner.plan_schema=schema
        return FakeStructuredOutput(self.owner,schema['title'])


class FakeStructuredOutput:
    def __init__(self,owner,title):
        self.owner=owner
        self.role='plan' if title=='SondaQuery' else 'answer'

    async def ainvoke(self,messages):
        payload=json.loads(messages[-1].content)
        self.owner.model_calls.append((self.role,payload))
        return self.plan(payload) if self.role=='plan' else self.answer(payload)

    def plan(self,payload):
        owner=self.owner
        if owner.plan_parse_errors:
            owner.plan_parse_errors-=1
            raise ValueError('Synthetic structured-output parsing failure')
        if owner.bad_plan:
            return {'tool':'summary','args':{'sql':'DROP TABLE anything'},'presentation':'none','continue_query':False}
        if owner.plan_results:
            result=owner.plan_results.pop(0)
            if isinstance(result,Exception):
                raise result
            return result
        question=payload['question'].lower()
        if 'línea' in question or 'linea' in question:
            return {'tool':'trend','args':{'platforms':['instagram','facebook'],'limit':50},'presentation':'line','continue_query':False}
        return {'tool':'search','args':{},'presentation':'table','continue_query':False}

    def answer(self,payload):
        owner=self.owner
        if owner.answer_error:
            raise TimeoutError('Synthetic answer interruption after verified SQL')
        queries=payload['queries']
        last=queries[-1]
        rows=last['rows']
        refs=[row['post_key'] for row in rows if row.get('post_key')][:1]
        if owner.bad_ref:
            refs=['f'*64]
        if last['tool']=='summary' and last['args'].get('platform')=='youtube':
            answer='YouTube tiene '+str(rows[0]['value'])+' publicación disponible.'
        elif last['tool'] in ('ranking','summary'):
            answer='Facebook encabeza la comparación con las métricas informadas.'
        else:
            answer='La consulta muestra los datos disponibles en el período indicado.'
        return {'answer':answer,'post_keys':refs}


class NativeFixture:
    def __init__(self,monkeypatch):
        self.config=json.loads((ROOT/'contracts/sonda/v1/fixtures/01-configuration.json').read_text())
        self.binding={'catalog':'spark_catalog','gold_schema':'sonda_gold','control_schema':'sonda_control',
            'registry_id':str(UUID(int=1)),'generation_id':str(UUID(int=2)),
            'contract_sha256':self.config['contract_sha256'],'region':'us-chicago-1',
            'model_id':'fixture-model','compartment_id':'fixture-compartment','gold_query_compute_id':'fixture-compute'}
        self.sql_calls,self.model_calls,self.flow_calls=[],[],[]
        self.paused,self.sql_error,self.bad_plan,self.bad_ref=False,False,False,False
        self.answer_error=False
        self.plan_parse_errors=0
        self.plan_results=[]
        self.plan_schema=None
        self.unsafe_value=False
        self.memory=AuthenticatedSaver()
        for name in ('aidputils','aidputils.agents','aidputils.agents.toolkit'):
            module=types.ModuleType(name)
            module.__path__=[]
            monkeypatch.setitem(sys.modules,name,module)
        helper=types.ModuleType('aidputils.agents.toolkit.agent_helper')
        owner=self
        helper.GenAIChatInvoker=partial(FakeGenAI,self)
        helper.GenerativeAiInferenceV2Client=lambda **kwargs:object()
        @contextmanager
        def flow_setup(**kwargs):
            owner.flow_calls.append(dict(kwargs))
            yield {'configurable':{'thread_id':kwargs.get('session_id','native-session'),'checkpoint_ns':'default'},
                'metadata':{'checkpoint_ns':'default','authorization_marker':'preserved'}}
        helper.flow_setup=flow_setup
        monkeypatch.setitem(sys.modules,helper.__name__,helper)
        configs=types.ModuleType('aidputils.agents.toolkit.configs')
        class ToolConf:
            def __init__(self,**kwargs):self.value=kwargs
            def model_dump(self):return self.value
        configs.AIDPToolConf=ToolConf
        monkeypatch.setitem(sys.modules,configs.__name__,configs)
        tools=types.ModuleType('aidputils.agents.toolkit.tool_helper')
        def create_langgraph_tool(value):
            assert value['tool_class']=='SQLTool'
            class SQL:
                def invoke(self,args):
                    assert args=={}
                    rows=owner.sql(value['conf']['query'])
                    return {'rows':[{'payload':json.dumps(row)} for row in rows]}
            return SQL()
        tools.create_langgraph_tool=create_langgraph_tool
        monkeypatch.setitem(sys.modules,tools.__name__,tools)
        self.module=types.ModuleType('trusted_generated_sonda_chat_graph')
        monkeypatch.setitem(sys.modules,self.module.__name__,self.module)
        exec(compile(chat_source(),'trusted-generated-sonda-chat.py','exec'),self.module.__dict__)
        self.module.runtime_storage=lambda binding:(object(),object())
        self.module.generation_inference=lambda storage,binding:not self.paused
        self.module.native_memory=lambda generation,agent:GenerationMemory(self.memory,generation)

    def agent(self,generation=None):
        self.module.CHAT_BINDING={**self.binding,'generation_id':generation or self.binding['generation_id']}
        agent=self.module.SondaChatAgent()
        agent.setup()
        return agent

    def envelope(self,question,generation=None,**context):
        return json.dumps({'version':2,'question':question,'search_context':{
            'registry_id':self.binding['registry_id'],'generation_id':generation or self.binding['generation_id'],
            'config_revision':1,'source_ids':[],'query_ids':[],**context}},ensure_ascii=False)

    def sql(self,statement):
        self.sql_calls.append(statement)
        if '.ai_social_publication ' in statement:
            versions={'ai_social_'+name:index+1 for index,name in enumerate(core.TABLES)}
            generation=self.module.CHAT_BINDING['generation_id']
            scope={'registry_id':self.binding['registry_id'],'generation_id':generation}
            return [{**scope,'config_revision':1,'versions_json':json.dumps(versions),'published_at':'2026-10-29T12:00:00Z',
                'publication_id':self.module.sha256(self.module.encoded([scope,1,versions]))}]
        if '.ai_social_registry ' in statement:
            value=json.loads(json.dumps(self.config))
            value['generation_id']=self.module.CHAT_BINDING['generation_id']
            return [value]
        if self.sql_error:
            raise RuntimeError('SQL fixture failed; not an empty population')
        if 'sequence(DATE ' in statement:
            first,last=re.search(r"sequence\(DATE '([^']+)',DATE '([^']+)'",statement).groups()
            start,end=date.fromisoformat(first),date.fromisoformat(last)
            return [{'period':str(start+timedelta(days=index)), 'instagram':1 if index==0 else 0,
                'facebook':2 if index==1 else 0} for index in range((end-start).days+1)]
        if 'GROUP BY platform ORDER BY platform' in statement or 'COUNT(*) AS value' in statement:
            if 'youtube'.encode().hex() in statement:
                return [{'platform':'youtube','value':1,'post_count':1,'reported_count':1,'undated_count':0}]
            return [{'platform':'facebook','value':9007199254740992 if self.unsafe_value else 12,'post_count':1,'reported_count':1},
                    {'platform':'instagram','value':7,'post_count':1,'reported_count':1}]
        return [{'post_key':'a'*64,'document_key':'b'*64,'parent_document_key':None,'platform':'facebook',
            'post_id':'fixture-post','version_hash':'c'*64,'published_at':'2026-10-29T00:00:00Z',
            'observed_at':'2026-10-29T01:00:00Z','text':'Oracle publication.',
            'url':'https://example.com/oracle/post','like_count':None,'comment_count':12,
            'share_count':None,'view_count':None,'value':12,'scope_posts':3,'reported_count':2,
            'document_type':'post','capture_scope':'requested_results'}]

    async def invoke(self,agent,question,**context):
        result=await agent.invoke(self.envelope(question,**context),session_id='native-session')
        return json.loads(result['messages'][0].content)


@pytest.fixture
def native(monkeypatch):
    return NativeFixture(monkeypatch)


def test_generated_native_graph_answers_three_reported_failures_and_echoes_exact_v2_scope(native):
    async def check():
        agent=native.agent()
        count=await native.invoke(agent,'cuantos post hay en yotube')
        assert count['answer']=='YouTube tiene 1 publicación disponible.' and count['visualization'] is None
        ranking=await native.invoke(agent,'realiza un ranking de los post mas comentados')
        assert ranking['visualization']['type']=='table' and ranking['visualization']['rows'][0][1]==12
        assert ranking['sources'][0]['post_key']=='a'*64 and ranking['sources'][0]['document_key']=='b'*64
        comparison=await native.invoke(agent,'en que red social hay mas comentarios de oracle')
        assert comparison['visualization']['type']=='bar'
        assert comparison['visualization']['rows']==[['facebook',12],['instagram',7]]
        assert comparison['search_context']==json.loads(native.envelope('ignored'))['search_context']
        assert comparison['publication']['pending_files'] is None and comparison['publication']['failed_files'] is None
        assert not [role for role,_ in native.model_calls if role=='plan']
        data=[sql for sql in native.sql_calls if '.ai_social_registry ' not in sql and '.ai_social_publication ' not in sql]
        assert len(data)==3 and all('EXISTS (SELECT 1 FROM' in sql for sql in data)
    asyncio.run(check())


@pytest.mark.parametrize('interrupted', [None, 'same_question', 'new_question'])
def test_native_json_memory_restores_history_after_refresh_and_interruption(native, interrupted):
    async def check():
        native.memory = JsonMessageSaver()
        agent = native.agent()
        question = 'en que red social hay mas comentarios de oracle'
        if interrupted:
            native.answer_error = True
            with pytest.raises(TimeoutError):
                await native.invoke(agent, question)
            native.answer_error = False
        else:
            await native.invoke(agent, question)
        refreshed = native.agent()
        next_question = question if interrupted == 'same_question' else 'cuantos post hay en yotube'
        response = await native.invoke(refreshed, next_question)
        assert response['answer']
        if interrupted != 'same_question':
            assert response['answer'] == 'YouTube tiene 1 publicación disponible.'
            assert response['query_context'][0]['args']['period'] == 'all'
        pie = await native.invoke(native.agent(), 'en pastel')
        assert pie['visualization']['type'] == 'pie'
        assert pie['query_context'][0]['args'] == response['query_context'][0]['args']
        config = {'configurable': {'thread_id': core.session_key(native.binding, 'native-session')}}
        state = await refreshed.graph.aget_state(config)
        assert not state.next
        assert all(message.type in ('human', 'ai') for message in state.values['messages'])
        assert len(state.values['messages']) <= 8
    asyncio.run(check())


def test_native_followup_pie_refreshes_gold_and_explicit_lines_bind_dates_without_model_values(native):
    async def check():
        agent=native.agent()
        await native.invoke(agent,'en que red social hay mas comentarios de oracle')
        queries=len(native.sql_calls)
        pie=await native.invoke(agent,'Muestra esa misma comparación en un gráfico de pastel')
        assert len(native.sql_calls)==queries+3 and pie['visualization']['type']=='pie'
        assert pie['query_context'][0]['args']['entity'].lower()=='oracle'
        line=await native.invoke(agent,'Compara las publicaciones de Instagram y Facebook del 2026-10-23 al 2026-10-29 con un gráfico de líneas.')
        assert line['visualization']['type']=='line' and len(line['visualization']['rows'])==7
        assert line['visualization']['columns']==['Fecha','Instagram','Facebook']
        assert line['query_context'][0]['args']['date_from']=='2026-10-23'
        assert 'limit' not in line['query_context'][0]['args']
    asyncio.run(check())


def test_f5_recreates_native_agent_with_bounded_history_and_generation_memory_isolation(native):
    async def check():
        agent=native.agent()
        for _ in range(5):await native.invoke(agent,'en que red social hay mas comentarios de oracle')
        refreshed=native.agent()
        pie=await native.invoke(refreshed,'en pastel')
        assert pie['visualization']['type']=='pie' and pie['query_context'][0]['args']['entity'].lower()=='oracle'
        config={'configurable':{'thread_id':core.session_key(native.binding,'native-session')}}
        state=(await refreshed.graph.aget_state(config)).values
        assert len(state['messages'])==8 and not state['queries'] and not state['raw_input']
        ai=[json.loads(message.content) for message in state['messages'] if message.type=='ai']
        assert all(set(message)=={'answer','query_context','search_context'} for message in ai)
        generation=str(UUID(int=999))
        isolated=native.agent(generation)
        other_config={'configurable':{'thread_id':core.session_key({**native.binding,'generation_id':generation},'native-session')}}
        assert not (await isolated.graph.aget_state(other_config)).values
        await native.invoke(isolated,'cuantos post hay en yotube',generation=generation)
        assert (await refreshed.graph.aget_state(config)).values['messages']==state['messages']
        namespaces=native.memory.storage['authenticated-native-session']
        assert 'sonda/'+native.binding['generation_id']+'/' in namespaces
        assert 'sonda/'+generation+'/' in namespaces
        assert all(call['session_id']=='native-session' for call in native.flow_calls)
    asyncio.run(check())


@pytest.mark.parametrize('failure',['paused','wrong_generation','stale','sql','reference'])
def test_native_graph_never_converts_guard_or_query_failure_into_empty_success(native,failure):
    async def check():
        agent=native.agent()
        if failure=='paused':native.paused=True
        if failure=='sql':native.sql_error=True
        if failure=='reference':native.bad_ref=True
        context={'generation':str(UUID(int=888))} if failure=='wrong_generation' else {'config_revision':2} if failure=='stale' else {}
        with pytest.raises((RuntimeError,ValueError)):
            await native.invoke(agent,'realiza un ranking de los post mas comentados',**context)
        if failure in ('paused','wrong_generation','stale','sql'):
            assert not native.model_calls
    asyncio.run(check())


def test_native_plan_repair_is_bounded_and_chart_failure_preserves_plain_answer(native):
    async def check():
        agent=native.agent()
        native.bad_plan=True
        clarify=await native.invoke(agent,'Consulta algo con un esquema imposible')
        assert clarify['visualization'] is None and not clarify['query_context']
        assert len([role for role,_ in native.model_calls if role=='plan'])==2
        native.bad_plan=False
        native.unsafe_value=True
        response=await native.invoke(agent,'en que red social hay mas comentarios de oracle')
        assert response['answer'] and response['visualization'] is None and response['visualization_error']
    asyncio.run(check())


def test_retry_after_answer_failure_resumes_without_repeating_gold_data_query_or_user_message(native):
    async def check():
        agent=native.agent()
        native.answer_error=True
        question='cuantos post hay en yotube'
        with pytest.raises(TimeoutError):
            await native.invoke(agent,question)
        data_queries=[s for s in native.sql_calls if '.ai_social_document_current ' in s]
        assert len(data_queries)==1
        native.answer_error=False
        response=await native.invoke(native.agent(),question)
        assert response['answer']=='YouTube tiene 1 publicación disponible.'
        assert len([s for s in native.sql_calls if '.ai_social_document_current ' in s])==1
        config={'configurable':{'thread_id':core.session_key(native.binding,'native-session')}}
        state=(await agent.graph.aget_state(config)).values
        assert [m.content for m in state['messages'] if m.type=='human']==[question]
    asyncio.run(check())


def test_new_question_discards_interrupted_readonly_turn_instead_of_reusing_its_rows(native):
    async def check():
        agent=native.agent()
        native.answer_error=True
        with pytest.raises(TimeoutError):
            await native.invoke(agent,'cuantos post hay en yotube')
        native.answer_error=False
        response=await native.invoke(agent,'realiza un ranking de los post mas comentados')
        assert response['query_context'][0]['tool']=='ranking'
        config={'configurable':{'thread_id':core.session_key(native.binding,'native-session')}}
        state=(await agent.graph.aget_state(config)).values
        assert [m.content for m in state['messages'] if m.type=='human']==['realiza un ranking de los post mas comentados']
    asyncio.run(check())


def test_structured_plan_parser_errors_use_only_two_repair_attempts(native):
    async def check():
        native.plan_parse_errors=2
        response=await native.invoke(native.agent(),'Busca documentos sobre un producto')
        assert response['query_context']==[] and response['visualization'] is None
        assert len([role for role,_ in native.model_calls if role=='plan'])==2
        repair=[payload for role,payload in native.model_calls if role=='plan'][-1]['validation_error']
        assert repair['stage']=='structured_output' and repair['code']=='invalid_structured_output'
        assert 'Synthetic' not in json.dumps(repair)
    asyncio.run(check())


def test_native_planner_uses_closed_filter_enums(native):
    native.agent()
    schema=native.plan_schema['properties']['args']['properties']
    assert schema['period']['enum']==['all','range']
    assert schema['metric']['enum']==['posts','comments','likes','shares','views']
    assert 'all' not in schema['platform']['enum']


def test_gold_network_coverage_question_repairs_before_any_data_sql(native):
    async def check():
        question='¿Qué redes tienen publicaciones Gold disponibles en los últimos 30 días? Indica cobertura.'
        invalid={'tool':'summary','args':{'platform':'all'},'presentation':'table','continue_query':False}
        valid={'tool':'summary','args':{},'presentation':'table','continue_query':False}
        native.plan_results=[invalid,valid]
        agent=native.agent()
        response=await native.invoke(agent,question)
        plans=[payload for role,payload in native.model_calls if role=='plan']
        assert len(plans)==2
        assert plans[0]['question']==question
        repair=plans[1]['validation_error']
        assert repair['stage']=='plan_validation'
        assert repair['code']=='unsupported_platform'
        assert set(repair)=={'stage','code','instruction'}
        assert 'platform' in repair['instruction']
        assert 'all' not in json.dumps(repair)
        data=[sql for sql in native.sql_calls if '.ai_social_registry ' not in sql and '.ai_social_publication ' not in sql]
        assert len(data)==1
        assert 'GROUP BY platform' in data[0]
        assert 'COUNT(*) AS value' in data[0]
        assert "d.published_date BETWEEN DATE '2026-09-30' AND DATE '2026-10-29'" in data[0]
        assert 'EXISTS (SELECT 1 FROM' in data[0]
        args=response['query_context'][0]['args']
        assert args=={'period':'range','date_from':'2026-09-30','date_to':'2026-10-29','metric':'posts','document_type':'post'}
        assert response['visualization']['type']=='table'
        config={'configurable':{'thread_id':core.session_key(native.binding,'native-session')}}
        diagnostics=(await agent.graph.aget_state(config)).values['planning_diagnostics']
        assert len(diagnostics)==1
        assert diagnostics[0]['code']=='unsupported_platform'
    asyncio.run(check())


def test_twice_rejected_coverage_plan_never_queries_or_answers_from_data(native):
    async def check():
        question='¿Qué redes tienen publicaciones Gold disponibles en los últimos 30 días? Indica cobertura.'
        invalid={'tool':'summary','args':{'platform':'all'},'presentation':'table','continue_query':False}
        native.plan_results=[invalid,invalid]
        agent=native.agent()
        response=await native.invoke(agent,question)
        plans=[payload for role,payload in native.model_calls if role=='plan']
        assert len(plans)==2
        assert plans[0]['question']==question
        repair=plans[1]['validation_error']
        assert repair['stage']=='plan_validation'
        assert repair['code']=='unsupported_platform'
        assert set(repair)=={'stage','code','instruction'}
        assert 'platform' in repair['instruction']
        assert 'all' not in json.dumps(repair)
        data=[sql for sql in native.sql_calls if '.ai_social_registry ' not in sql and '.ai_social_publication ' not in sql]
        assert data==[]
        assert response['query_context']==[]
        assert response['visualization'] is None
        assert response['answer']=='No pude completar esta consulta. Inténtalo de nuevo o reformula la pregunta.'
        assert not [payload for role,payload in native.model_calls if role=='answer']
        config={'configurable':{'thread_id':core.session_key(native.binding,'native-session')}}
        diagnostics=(await agent.graph.aget_state(config)).values['planning_diagnostics']
        assert len(diagnostics)==2
        assert all(item['code']=='unsupported_platform' for item in diagnostics)
    asyncio.run(check())


@pytest.mark.parametrize('repair_valid',[True,False])
def test_new_turn_clears_repaired_or_terminal_planning_diagnostics(native,repair_valid):
    async def check():
        invalid={'tool':'summary','args':{'platform':'all'},'presentation':'table','continue_query':False}
        valid={'tool':'summary','args':{},'presentation':'table','continue_query':False}
        native.plan_results=[invalid,valid if repair_valid else invalid]
        agent=native.agent()
        await native.invoke(agent,'¿Qué redes tienen publicaciones Gold disponibles en los últimos 30 días? Indica cobertura.')
        config={'configurable':{'thread_id':core.session_key(native.binding,'native-session')}}
        diagnostics=(await agent.graph.aget_state(config)).values['planning_diagnostics']
        assert len(diagnostics)==(1 if repair_valid else 2)
        assert all(item['code']=='unsupported_platform' for item in diagnostics)
        await native.invoke(agent,'cuantos post hay en yotube')
        assert (await agent.graph.aget_state(config)).values['planning_diagnostics']==[]
        assert (await agent.graph.aget_state(config)).values['plan_repairs']==0
    asyncio.run(check())


def test_dependent_queries_share_one_repair_budget_per_turn(native):
    async def check():
        invalid={'tool':'search','args':{'platform':'all'},'presentation':'table','continue_query':False}
        search={'tool':'search','args':{'text':'Oracle'},'presentation':'table','continue_query':True}
        missing_reference={'tool':'details','args':{},'presentation':'table','continue_query':False}
        native.plan_results=[invalid,search,missing_reference]
        agent=native.agent()
        response=await native.invoke(agent,'Busca una publicación de Oracle y después consulta sus detalles.')
        plans=[payload for role,payload in native.model_calls if role=='plan']
        assert len(plans)==3
        assert plans[1]['validation_error']['code']=='unsupported_platform'
        assert len(plans[2]['queries'])==1
        assert 'validation_error' not in plans[2]
        data=[sql for sql in native.sql_calls if '.ai_social_registry ' not in sql and '.ai_social_publication ' not in sql]
        assert len(data)==1
        assert response['answer']=='No pude completar esta consulta. Inténtalo de nuevo o reformula la pregunta.'
        config={'configurable':{'thread_id':core.session_key(native.binding,'native-session')}}
        state=(await agent.graph.aget_state(config)).values
        assert state['plan']['tool']=='clarify'
        assert state['plan_repairs']==1
        assert [item['code'] for item in state['planning_diagnostics']]==['unsupported_platform','missing_post_reference']
        native.plan_results=[invalid,{**search,'continue_query':False}]
        await native.invoke(agent,'Busca otra publicación disponible de Oracle.')
        assert len([role for role,_ in native.model_calls if role=='plan'])==5
        state=(await agent.graph.aget_state(config)).values
        assert state['plan']['tool']=='search'
        assert state['plan_repairs']==1
        assert [item['code'] for item in state['planning_diagnostics']]==['unsupported_platform']
    asyncio.run(check())


def test_repair_budget_does_not_reduce_four_valid_dependent_queries(native):
    async def check():
        native.plan_results=[{'tool':'search','args':{'text':word},'presentation':'table','continue_query':True}
            for word in ('Oracle','cloud','database','lakehouse')]
        agent=native.agent()
        response=await native.invoke(agent,'Busca las publicaciones necesarias para explicar los temas disponibles.')
        assert len(response['query_context'])==4
        assert len([role for role,_ in native.model_calls if role=='plan'])==4
        data=[sql for sql in native.sql_calls if '.ai_social_registry ' not in sql and '.ai_social_publication ' not in sql]
        assert len(data)==4
        config={'configurable':{'thread_id':core.session_key(native.binding,'native-session')}}
        state=(await agent.graph.aget_state(config)).values
        assert state['plan_repairs']==0 and state['planning_diagnostics']==[]
    asyncio.run(check())


@pytest.mark.parametrize('repair_started',[False,True])
def test_model_service_failure_propagates_without_an_automatic_planning_retry(native,repair_started):
    async def check():
        invalid={'tool':'search','args':{'platform':'all'},'presentation':'table','continue_query':False}
        valid={'tool':'search','args':{},'presentation':'table','continue_query':False}
        native.plan_results=([invalid] if repair_started else [])+[TimeoutError('Synthetic model-service timeout'),valid]
        with pytest.raises(TimeoutError,match='model-service timeout'):
            await native.invoke(native.agent(),'Busca publicaciones disponibles sobre Oracle.')
        assert len([role for role,_ in native.model_calls if role=='plan'])==(2 if repair_started else 1)
        assert native.plan_results==[valid]
        data=[sql for sql in native.sql_calls if '.ai_social_registry ' not in sql and '.ai_social_publication ' not in sql]
        assert data==[]
        assert not [role for role,_ in native.model_calls if role=='answer']
    asyncio.run(check())


def test_resume_rechecks_pause_and_rejects_changed_approved_configuration(native):
    async def check():
        agent=native.agent()
        question='cuantos post hay en yotube'
        native.answer_error=True
        with pytest.raises(TimeoutError):
            await native.invoke(agent,question)
        native.answer_error=False
        native.paused=True
        calls=len(native.model_calls)
        with pytest.raises(RuntimeError,match='paused'):
            await native.invoke(agent,question)
        assert len(native.model_calls)==calls
        native.paused=False
        original=native.module.query
        def changed(binding,statement):
            rows=original(binding,statement)
            if '.ai_social_publication ' in statement:
                rows[0]['config_revision']=2
                scope={key:binding[key] for key in ('registry_id','generation_id')}
                rows[0]['publication_id']=native.module.sha256(native.module.encoded([
                    scope,2,json.loads(rows[0]['versions_json'])]))
            return rows
        native.module.query=changed
        with pytest.raises(RuntimeError,match='configuration changed'):
            await native.invoke(agent,question)
        assert len(native.model_calls)==calls
    asyncio.run(check())


def test_prepare_query_failure_can_resume_before_a_snapshot_exists(native):
    async def check():
        agent=native.agent()
        original=native.module.query
        def unavailable(binding,statement):
            raise RuntimeError('Synthetic snapshot query unavailable')
        native.module.query=unavailable
        with pytest.raises(RuntimeError):
            await native.invoke(agent,'cuantos post hay en yotube')
        native.module.query=original
        assert (await native.invoke(agent,'cuantos post hay en yotube'))['answer']
    asyncio.run(check())
