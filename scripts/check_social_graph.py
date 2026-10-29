"""Real LangGraph regression; model, native SQL and AI Lakehouse transport are offline stubs."""
import asyncio
from contextlib import contextmanager
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
receipt = {'module_id':'ai_social_network','publication_id':'receipt','published_at':'2026-10-29T10:00:00Z',
           'current_version':1,'history_version':2,'daily_version':3,'pending_files':0,'failed_files':0}
queries, plans, active = [], [], []


class Model:
    def __init__(self, **kwargs):
        pass

    def with_structured_output(self, schema, **kwargs):
        return SimpleNamespace(ainvoke=self.answer if schema['title']=='SocialAnswer' else self.plan)

    async def plan(self, messages, **kwargs):
        value = json.loads(messages[-1].content)
        if value['queries']:
            # Reproduce the native model repeating a successful query instead of finishing.
            return {key: value['queries'][-1][key] for key in ('tool', 'args')}
        own_context = any('CONTEXTO_A' in item['content'] for item in value['conversation'])
        choice = {'tool':'search','args':{'text':'Oracle'}} if own_context else {'tool':'summary','args':{}}
        plans.append(choice)
        return choice

    async def answer(self, messages, **kwargs):
        value = json.loads(messages[-1].content)
        keys = [row['post_key'] for item in value['queries'] for row in item['rows'] if row.get('post_key')]
        return {'answer':'Datos de la consulta Gold actual', 'post_keys':keys}


@contextmanager
def flow_setup(**kwargs):
    active.append(kwargs.get('thread_id'))
    try:
        yield {'configurable': {'thread_id': kwargs.get('thread_id')}}
    finally:
        active.pop()


def query(config, sql):
    assert active, 'SQL must run inside the native invocation context'
    queries.append(sql)
    if 'ai_social_publication' in sql:
        return [receipt]
    if 'ai_social_daily' in sql:
        return [{'platform':'instagram','post_count':1}]
    return [{'post_key':'a'*64,'platform':'instagram','url':'https://example.invalid/post','published_at':None,'observed_at':'2026-10-29'}]


def module(name, **members):
    value = ModuleType(name)
    value.__dict__.update(members)
    return value


async def main():
    import hashlib
    values = {'tenancy':'tenant','user':'user','fingerprint':'fingerprint','private_key':'offline-only'}
    identity = hashlib.sha256(json.dumps(['tenant','user','fingerprint'],separators=(',',':')).encode()).hexdigest()
    agent.CONFIG = {'catalog':'oci_medallion','gold_schema':'oci_gold','gold_query_compute_id':'query',
        'region':'us-chicago-1','compartment_id':'compartment','model_id':'selected','credential_name':'AidpRuntime','identity_sha256':identity}
    modules = {name: module(name) for name in ('aidputils','aidputils.agents','aidputils.agents.toolkit','oracle_memory_clients')}
    modules['aidputils'].secrets = SimpleNamespace(get=lambda name,key: values[key])
    modules['aidputils.agents.toolkit.agent_helper'] = module('aidputils.agents.toolkit.agent_helper',
        GenAIChatInvoker=Model, GenerativeAiInferenceV2Client=lambda **kwargs:None, flow_setup=flow_setup)
    modules['oracle_memory_clients.client'] = module('oracle_memory_clients.client', AsyncProxyCheckpointClient=lambda **kwargs: InMemorySaver())
    import oci
    with patch.dict(sys.modules,modules), patch.object(oci.signer,'Signer',return_value='offline'), patch.object(agent,'query',query):
        instance = agent.SocialNetworkAgent()
        instance.setup()
        first = await instance.invoke('Busca Oracle. CONTEXTO_A.',thread_id='conversation-A')
        assert json.loads(first['messages'][-1].content)['sources'][0]['post_key']=='a'*64
        await instance.invoke('¿Y esa misma búsqueda?',thread_id='conversation-A')
        await instance.invoke('¿Y esa misma búsqueda?',thread_id='conversation-B')
        assert [item['tool'] for item in plans] == ['search','search','summary']
        assert len([sql for sql in queries if 'ai_social_publication' in sql]) == 3, 'Every turn must refresh the committed Gold snapshot'
        assert len(queries) == 6, 'Every turn must execute a fresh scoped Gold data query'
        assert not active, 'Invocation context must be cleaned after a response'
        try:
            await instance.invoke('Sin sesión')
            raise AssertionError('Stateless fallback must not be allowed')
        except ValueError:
            pass
        assert not active
    print('PASS LangGraph: repeated plans finalize without duplicate queries, compiles, same-session follow-up, isolated sessions, fresh Gold queries and mandatory native memory context')


if __name__=='__main__':
    asyncio.run(main())
