"""Real LangGraph regression; model, SQL and AI Lakehouse transports are offline stubs.
This verifies routing/evidence contracts, not a live model's language understanding.
"""
import asyncio
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from langgraph.checkpoint.memory import InMemorySaver

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('social_agent', root/'apps/backend/app/labs/ai_social_network/notebooks/agent/social_agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
receipt = {'publication_id':'receipt-1','published_at':'2026-10-29T10:00:00Z','current_version':1,'history_version':2,'daily_version':3}
queries, plans, active = [], [], []
POST_KEY = 'a' * 64


class Model:
    def __init__(self, **kwargs):
        pass

    def with_structured_output(self, schema, **kwargs):
        if schema['title'] == 'SocialQuery':
            assert {'presentation','continue_query'} <= set(schema['required'])
        return SimpleNamespace(ainvoke=self.answer if schema['title']=='SocialAnswer' else self.plan)

    async def plan(self, messages, **kwargs):
        value = json.loads(messages[-1].content)
        question = value['conversation'][-1]['content']
        args, tool, presentation, again = {}, 'summary', 'auto', False
        if question == 'cuantos post hay en yotube':
            args, presentation = {'platform':'yotube','metric':'posts'}, 'none'
        elif question == 'realiza un ranking de los post mas comentados':
            tool, args = 'ranking', {'metric':'comments'}
        elif question == 'en que red social hay mas comentarios de oracle':
            args = {'entity':'Oracle','entity_scope':'any','metric':'comments','date_from':'2026-10-01','date_to':'2026-10-28'}
            presentation = 'bar'
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
        keys = [row['post_key'] for item in value['queries'] for row in item['rows'] if row.get('post_key')]
        if value['question'] == 'REFERENCIA INVALIDA':
            keys = ['f'*64]
        return {'answer':'Datos Gold actuales','post_keys':list(dict.fromkeys(keys))}


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
        assert plans[-1]['input']['search_context'] == context, 'Sonda context must reach the planner without excluding YouTube'
        assert plans[-1]['input']['conversation'][-1]['content'] == 'cuantos post hay en yotube'
        ranking, own = await invoke('realiza un ranking de los post mas comentados','ranking')
        assert ranking['visualization']['type'] == 'table' and len(own) == 2
        assert ranking['sources'][0]['post_key'] == POST_KEY
        assert 'ORDER BY value DESC NULLS LAST, post_key LIMIT 10' in own[-1]['sql']
        oracle, own = await invoke('en que red social hay mas comentarios de oracle','oracle')
        assert oracle['visualization']['type'] == 'bar' and len(own) == 2
        original_args = oracle['query_context'][-1]['args']
        assert original_args['metric'] == 'comments' and original_args['entity'] == 'Oracle'
        assert original_args['period'] == 'range' and original_args['date_to'] == '2026-10-28'
        receipt.update(publication_id='receipt-2',current_version=4,daily_version=6)
        followup, own = await invoke('Ahora en pastel','oracle')
        assert followup['visualization']['type'] == 'pie'
        assert followup['query_context'][-1]['args'] == original_args
        assert followup['visualization']['rows'] == [['facebook',15],['instagram',5]]
        assert oracle['visualization']['rows'] == [['facebook',12],['instagram',5]]
        assert followup['publication']['publication_id'] == 'receipt-2' and 'VERSION AS OF 4' in own[-1]['sql']
        assert plans[-1]['input']['search_context'] == {}, 'A missing envelope must not retain stale Sonda context'
        prior = [json.loads(item['content']) for item in plans[-1]['input']['conversation'] if item['role']=='ai'][-1]
        assert set(prior) == {'answer','query_context'}, 'Do not replay chart rows/receipts into planner memory'
        isolated, own = await invoke('Ahora en pastel','isolated')
        assert 'entity' not in isolated['query_context'][-1]['args'] and isolated['query_context'][-1]['args']['period'] == 'all'
        assert 'ai_social_daily VERSION AS OF 6' in own[-1]['sql']
        history, own = await invoke('Historial del primer post','history')
        assert len(own) == 3 and [item['tool'] for item in history['query_context']] == ['search','history']
        assert history['query_context'][-1]['args']['date_from'] == '2026-09-30'
        assert 'ai_social_history VERSION AS OF 2' in own[-1]['sql']
        repeated, own = await invoke('REPETIR','repeat')
        assert len(own) == 2 and len(repeated['query_context']) == 1
        bounded, own = await invoke('LIMITE','bounded')
        assert len(own) == 5 and len(bounded['query_context']) == 4
        for question, message in [('FALLO SQL','Offline Gold query failure'),('REFERENCIA INVALIDA','unqueried evidence')]:
            try:
                await instance.invoke(question,thread_id='failure')
                raise AssertionError('Invalid evidence must not become empty successful data')
            except RuntimeError as error:
                assert message in str(error)
            assert not active
        recovered, own = await invoke('cuantos post hay en yotube','failure')
        assert len(own) == 2 and len(recovered['query_context']) == 1
        try:
            await instance.invoke('Sin sesion')
            raise AssertionError('Stateless fallback must not be allowed')
        except ValueError:
            pass
        assert not active
    print('PASS LangGraph: three user queries, typed presentation, Sonda context, fresh/isolated follow-ups, dependent tools, repeat/four-query guards and failure cleanup (offline transports/model)')


if __name__=='__main__':
    asyncio.run(main())
