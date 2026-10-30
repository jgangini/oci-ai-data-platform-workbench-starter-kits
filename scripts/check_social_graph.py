"""Real LangGraph regression; model, SQL and AI Lakehouse transports are offline stubs.
This verifies routing/evidence contracts, not a live model's language understanding.
"""
import asyncio
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from datetime import date, timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from langgraph.checkpoint.memory import InMemorySaver

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('social_agent', root/'apps/backend/app/labs/ai_social_network/notebooks/agent/social_agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
receipt = {'publication_id':'receipt-1','published_at':'2026-10-29T10:00:00Z','current_version':1,'history_version':2,'daily_version':3,
           'comment_current_version':14,'comment_history_version':15}
queries, plans, active, answers = [], [], [], []
POST_KEY = 'a' * 64
LINE_QUESTION = 'Compara las publicaciones de Instagram y Facebook del 2026-10-23 al 2026-10-29 con un gráfico de líneas.'


class Model:
    def __init__(self, **kwargs):
        pass

    def with_structured_output(self, schema, **kwargs):
        if schema['title'] == 'SocialQuery':
            assert {'presentation','continue_query'} <= set(schema['required'])
        else:
            assert schema['properties']['answer']['maxLength'] == 1800
        return SimpleNamespace(ainvoke=self.answer if schema['title']=='SocialAnswer' else self.plan)

    async def plan(self, messages, **kwargs):
        value = json.loads(messages[-1].content)
        question = value['conversation'][-1]['content']
        args, tool, presentation, again = {}, 'summary', 'auto', False
        if question in ('cuantos post hay en yotube', 'realiza un ranking de los post mas comentados',
                        'en que red social hay mas comentarios de oracle', 'Busca la palabra Oracle en los comentarios capturados'):
            raise AssertionError('The three verified questions must bypass the model planner')
        if question == 'VALOR GRANDE':
            presentation = 'bar'
        elif question == LINE_QUESTION:
            # Captured native output: omitted both requested dates and supplied a generic row limit.
            tool, presentation = 'trend', 'line'
            args = {'platforms':['instagram','facebook'],'limit':50}
        elif question in ('RECUPERA PLAN','ACLARA PLAN'):
            if value.get('validation_error'):
                assert set(value) == {'conversation','publication','queries','search_context','validation_error'}
                assert value['validation_error']['stage']=='plan_validation'
                assert value['validation_error']['code']=='unsupported_platform'
            args = {'platform':'youtube','metric':'posts'} if question == 'RECUPERA PLAN' and value.get('validation_error') else {'platform':'unsupported'}
        elif question == 'Compara las búsquedas guardadas y explica su cobertura':
            args = {'entity':'Oracle','entity_scope':'any','metric':'comments'}
        elif question == 'Compara los comentarios de Oracle del 2026-10-01 al 2026-10-28':
            args = {'entity':'Oracle','entity_scope':'any','metric':'comments','date_from':'2026-10-01','date_to':'2026-10-28'}
            presentation = 'bar'
        elif question == 'Compara por día los comentarios de Instagram y Facebook del 2026-10-27 al 2026-10-29 en líneas':
            tool, presentation = 'trend', 'line'
            args = {'platforms':['instagram','facebook'],'metric':'comments','date_from':'2026-10-27','date_to':'2026-10-29'}
        elif question == 'Ahora en líneas':
            previous = [json.loads(item['content']) for item in value['conversation'] if item['role']=='ai'][-1]['query_context'][-1]
            tool, presentation = 'trend', 'line'
            args = {key: value for key, value in previous['args'].items() if key not in ('period','limit')}
        elif question == 'Busca los comentarios que mencionen Oracle':
            tool, args, presentation = 'comments', {'entity':'Oracle','entity_scope':'mentions'}, 'table'
        elif question == 'Ahora en pastel':
            previous = [json.loads(item['content']) for item in value['conversation'] if item['role']=='ai']
            if previous:
                previous_query = previous[-1]['query_context'][-1]
                tool, args = previous_query['tool'], previous_query['args']
            presentation = 'pie'
        elif question == 'Historial del primer post':
            if value['queries']:
                tool, args = 'history', {'post_key':value['queries'][0]['rows'][0]['post_key']}
            else:
                tool, args, again = 'search', {'text':'Oracle'}, True
        elif question in ('REPETIR','LIMITE'):
            tool, again = 'search', True
            args = {'text':'Oracle' if question == 'REPETIR' else 'paso' + str(len(value['queries']))}
        elif question in ('FALLO SQL','REFERENCIA INVALIDA'):
            tool, args = 'search', {'text':question}
        result = {'tool':tool,'args':args,'presentation':presentation,'continue_query':again}
        plans.append({'input':value,'result':result})
        return result

    async def answer(self, messages, **kwargs):
        value = json.loads(messages[-1].content)
        answers.append(value['question'])
        keys = [(row.get('comment_key') or row['post_key']) for item in value['queries'] for row in item['rows'] if row.get('post_key')]
        if value['question'] == 'REFERENCIA INVALIDA':
            keys = ['f'*64]
        return {'answer':'x'*1801 if value['question'] == 'RESPUESTA LARGA' else 'Datos Gold actuales','post_keys':list(dict.fromkeys(keys))}


@contextmanager
def flow_setup(**kwargs):
    active.append(kwargs.get('thread_id'))
    try:
        yield {'configurable':{'thread_id':kwargs.get('thread_id')}}
    finally:
        active.pop()


def query(config, sql):
    assert active, 'SQL must run inside native invocation context'
    queries.append({'thread':active[-1],'sql':sql})
    if 'ai_social_publication' in sql:
        return [dict(receipt)]
    if 'FALLO SQL'.encode().hex() in sql:
        raise RuntimeError('Offline Gold query failure')
    if active[-1] == 'unsafe-chart':
        return [{'platform':'youtube','value':9007199254740992,'post_count':9007199254740992}]
    if 'ai_social_comment_current' in sql:
        return [{'comment_key':letter*64,'platform':'facebook','post_key':POST_KEY,'comment_id':letter,
                 'text':'Oracle comment '+letter,'author':None,'url':'https://example.invalid/comments/'+letter,
                 'like_count':None,'commented_at':None,'observed_at':'2026-10-29','capture_scope':'top_comments'} for letter in ('c','d')]
    if 'calendar.day' in sql:
        calendar = re.search(r"sequence\(DATE '([^']+)',DATE '([^']+)'",sql)
        start, end = [date.fromisoformat(value) for value in calendar.groups()]
        posts = 'SUM(post_count)' in sql
        return [{'period':str(start+timedelta(days=index)), 'instagram':receipt['current_version']+index,
                 'facebook':(0 if posts else None) if index == 1 else 2} for index in range((end-start).days+1)]
    if 'ai_social_daily' in sql:
        return [{'platform':'youtube','value':7,'post_count':7,'undated_count':2}]
    if 'SUM(comment_count)' in sql:
        return [{'platform':'facebook','value':11+receipt['current_version'],'reported_count':2,'post_count':3,'undated_count':1},
                {'platform':'instagram','value':5,'reported_count':1,'post_count':1,'undated_count':0}]
    return [{'post_key':POST_KEY,'platform':'instagram','text':'Fixture Oracle post','value':42,'comment_count':42,
             'url':'https://example.invalid/post','published_at':None,'observed_at':'2026-10-29','scope_posts':3,'reported_count':2}]


def module(name, **members):
    value = ModuleType(name)
    value.__dict__.update(members)
    return value


async def main():
    values = {'tenancy':'tenant','user':'user','fingerprint':'fingerprint','private_key':'offline-only'}
    identity = hashlib.sha256(json.dumps(['tenant','user','fingerprint'],separators=(',',':')).encode()).hexdigest()
    agent.CONFIG = {'catalog':'oci_medallion','gold_schema':'oci_gold','gold_query_compute_id':'query','region':'us-chicago-1',
        'compartment_id':'compartment','model_id':'selected','credential_name':'AidpRuntime','identity_sha256':identity}
    modules = {name:module(name) for name in ('aidputils','aidputils.agents','aidputils.agents.toolkit','oracle_memory_clients')}
    modules['aidputils'].secrets = SimpleNamespace(get=lambda name,key:values[key])
    modules['aidputils.agents.toolkit.agent_helper'] = module('aidputils.agents.toolkit.agent_helper',
        GenAIChatInvoker=Model,GenerativeAiInferenceV2Client=lambda **kwargs:None,flow_setup=flow_setup)
    modules['oracle_memory_clients.client'] = module('oracle_memory_clients.client',AsyncProxyCheckpointClient=lambda **kwargs:InMemorySaver())
    import oci
    with patch.dict(sys.modules,modules), patch.object(oci.signer,'Signer',return_value='offline'), patch.object(agent,'query',query):
        instance = agent.SocialNetworkAgent()
        instance.setup()

        async def invoke(question, thread):
            start = len(queries)
            result = await instance.invoke(question,thread_id=thread)
            own = queries[start:]
            assert sum('ai_social_publication' in item['sql'] for item in own) == 1
            assert all(item['thread'] == thread for item in own) and not active
            return json.loads(result['messages'][-1].content), own

        context = {'networks':[{'platform':'instagram','selected':True,'filters':['@oracle']},
                              {'platform':'facebook','selected':True,'filters':['https://www.facebook.com/Oracle/']},
                              {'platform':'youtube','selected':False,'filters':['https://www.youtube.com/user/Oracle/videos']}]}
        count, own = await invoke(json.dumps({'version':1,'question':'cuantos post hay en yotube','search_context':context}),'count')
        assert count['visualization'] is None and len(own) == 2
        assert count['query_context'][0]['args']['platform'] == 'youtube'
        assert count['query_context'][0]['args']['period'] == 'all'
        assert not count['query_context'][0]['args'].get('entity') and not count['query_context'][0]['args'].get('text')
        assert not plans, 'A verified YouTube count must not call the model planner or inherit Sonda exclusions'
        ranking, own = await invoke('realiza un ranking de los post mas comentados','ranking')
        assert ranking['visualization']['type'] == 'table' and len(own) == 2
        assert ranking['sources'][0]['post_key'] == POST_KEY
        assert 'ORDER BY value DESC NULLS LAST, post_key LIMIT 10' in own[-1]['sql']
        oracle, own = await invoke('en que red social hay mas comentarios de oracle','oracle')
        assert oracle['visualization']['type'] == 'bar' and len(own) == 2
        original_args = oracle['query_context'][-1]['args']
        assert original_args['metric'] == 'comments' and original_args['entity'].casefold() == 'oracle'
        assert original_args['period'] == 'all' and not original_args.get('date_from')
        assert not plans, 'All three frequent verified questions must bypass the model planner'
        complex_question = 'Compara las búsquedas guardadas y explica su cobertura'
        await invoke(json.dumps({'version':1,'question':complex_question,'search_context':context}),'saved-search-context')
        assert plans[-1]['input']['search_context'] == context, 'A complex question must deliver Sonda context to the planner'
        assert plans[-1]['input']['conversation'][-1]['content'] == complex_question
        await invoke(complex_question,'saved-search-context')
        assert plans[-1]['input']['search_context'] == {}, 'A missing envelope must not retain stale Sonda context'
        prior = [json.loads(item['content']) for item in plans[-1]['input']['conversation'] if item['role']=='ai'][-1]
        assert set(prior) == {'answer','query_context'}, 'Do not replay chart rows/receipts into planner context'
        receipt.update(publication_id='receipt-2',current_version=4,daily_version=6)
        planner_calls = len(plans)
        followup, own = await invoke('Ahora en pastel','oracle')
        assert followup['visualization']['type'] == 'pie'
        assert followup['query_context'][-1]['args'] == original_args
        assert followup['visualization']['rows'] == [['facebook',15],['instagram',5]]
        assert oracle['visualization']['rows'] == [['facebook',12],['instagram',5]]
        assert followup['publication']['publication_id'] == 'receipt-2' and 'VERSION AS OF 4' in own[-1]['sql']
        assert len(plans) == planner_calls, 'A verified format follow-up reuses resolved scope without a new model plan'
        isolated, own = await invoke('Ahora en pastel','isolated')
        assert 'entity' not in isolated['query_context'][-1]['args'] and isolated['query_context'][-1]['args']['period'] == 'all'
        assert 'ai_social_daily VERSION AS OF 6' in own[-1]['sql']
        dated, own = await invoke('Compara los comentarios de Oracle del 2026-10-01 al 2026-10-28','dated')
        assert dated['query_context'][-1]['args']['period'] == 'range'
        assert dated['query_context'][-1]['args']['date_from'] == '2026-10-01'
        assert dated['query_context'][-1]['args']['date_to'] == '2026-10-28'
        assert "DATE '2026-10-01'" in own[-1]['sql'] and "DATE '2026-10-28'" in own[-1]['sql']
        line, own = await invoke('Compara por día los comentarios de Instagram y Facebook del 2026-10-27 al 2026-10-29 en líneas','line')
        assert line['visualization']['type'] == 'line' and line['visualization']['columns'] == ['Fecha','Instagram','Facebook']
        assert line['visualization']['rows'][1] == ['2026-10-28',5,None]
        planner_calls = len(plans)
        receipt.update(publication_id='receipt-3',current_version=7)
        repeated_line, own = await invoke('Ahora en líneas','line')
        assert repeated_line['visualization']['rows'][1] == ['2026-10-28',8,None]
        assert 'VERSION AS OF 7' in own[-1]['sql'] and len(plans) == planner_calls
        summary_to_line, own = await invoke('Ahora en líneas','oracle')
        assert summary_to_line['query_context'][-1]['tool'] == 'trend'
        assert summary_to_line['query_context'][-1]['args']['entity'].casefold() == 'oracle'
        assert len(summary_to_line['visualization']['rows']) == 30 and len(plans) == planner_calls+1
        captured_line, own = await invoke(LINE_QUESTION,'captured-native-line')
        assert len(captured_line['visualization']['rows']) == 7
        assert captured_line['visualization']['rows'][0][0] == '2026-10-23'
        assert captured_line['visualization']['rows'][-1][0] == '2026-10-29'
        assert captured_line['query_context'][-1]['args']['metric'] == 'posts'
        assert 'limit' not in captured_line['query_context'][-1]['args']
        assert plans[-1]['result']['args'] == {'platforms':['instagram','facebook'],'limit':50}
        assert len(own) == 2, 'The malformed native plan must be normalized without retrying an executed query'
        planner_calls = len(plans)
        comment_word, own = await invoke('Busca la palabra Oracle en los comentarios capturados','verified-comment-word')
        assert len(plans) == planner_calls and comment_word['query_context'][-1]['tool'] == 'comments'
        assert comment_word['query_context'][-1]['args']['text'] == 'Oracle'
        assert 'ai_social_comment_current VERSION AS OF 14' in own[-1]['sql']
        comments, own = await invoke('Busca los comentarios que mencionen Oracle','comments')
        assert 'ai_social_comment_current VERSION AS OF 14' in own[-1]['sql']
        assert len(comments['sources']) == 2 and all(source['post_key'] == POST_KEY for source in comments['sources'])
        assert {source['url'] for source in comments['sources']} == {'https://example.invalid/comments/c','https://example.invalid/comments/d'}
        assert comments['visualization']['columns'][0] == 'Comentario' and 'parcial' in comments['visualization']['caption']
        history, own = await invoke('Historial del primer post','history')
        assert len(own) == 3 and [item['tool'] for item in history['query_context']] == ['search','history']
        assert history['query_context'][-1]['args']['date_from'] == '2026-09-30'
        assert 'ai_social_history VERSION AS OF 2' in own[-1]['sql']
        repeated, own = await invoke('REPETIR','repeat')
        assert len(own) == 2 and len(repeated['query_context']) == 1
        bounded, own = await invoke('LIMITE','bounded')
        assert len(own) == 5 and len(bounded['query_context']) == 4
        planner_calls = len(plans)
        repaired, own = await invoke('RECUPERA PLAN','repair-once')
        assert len(plans) == planner_calls+2 and len(own) == 2
        assert repaired['query_context'][-1]['args']['platform'] == 'youtube'
        planner_calls, answer_calls = len(plans), len(answers)
        clarification, own = await invoke('ACLARA PLAN','clarification')
        assert len(plans) == planner_calls+2 and len(answers) == answer_calls
        assert len(own) == 1, 'An invalid plan and its failed repair must never execute data SQL'
        assert clarification['sources'] == [] and clarification['query_context'] == [] and clarification['visualization'] is None
        assert clarification['publication']['publication_id'] == receipt['publication_id']
        assert clarification['answer']=='No pude completar esta consulta. Inténtalo de nuevo o reformula la pregunta.'
        planner_calls = len(plans)
        after_clarification, own = await invoke('RECUPERA PLAN','clarification')
        assert len(plans) == planner_calls+2 and len(own) == 2
        assert len(after_clarification['query_context']) == 1, 'Each turn resets repair budget and transient evidence'
        for question, message in [('FALLO SQL','Offline Gold query failure'),('REFERENCIA INVALIDA','unqueried evidence'),
                                  ('RESPUESTA LARGA','unqueried evidence')]:
            try:
                await instance.invoke(question,thread_id='failure')
                raise AssertionError('Invalid evidence must not become empty successful data')
            except RuntimeError as error:
                assert message in str(error)
            assert not active
        recovered, own = await invoke('cuantos post hay en yotube','failure')
        assert len(own) == 2 and len(recovered['query_context']) == 1
        unsafe, own = await invoke('VALOR GRANDE','unsafe-chart')
        assert unsafe['visualization'] is None and unsafe['visualization_error']
        assert unsafe['answer'] == 'Datos Gold actuales' and unsafe['sources'] == []
        try:
            await instance.invoke('Sin sesion')
            raise AssertionError('Stateless fallback must not be allowed')
        except ValueError:
            pass
        assert not active
    print('PASS LangGraph: verified queries bypass planner; structured complex/date plans, Sonda context, fresh/isolated follow-ups, dependent tools, guards and failure cleanup (offline transports/model)')


if __name__=='__main__':
    asyncio.run(main())
