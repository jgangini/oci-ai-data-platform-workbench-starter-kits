"""The native adapter runs the portable graph with scoped storage and content-carried events."""
import asyncio
import copy
import hashlib
import importlib.util
import io
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.modules.ai_market_intelligence.installation.agent_package import agent_package
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.wire import MarketWireDecoder, market_stream_graph, market_wire_line
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.services import build_market_services
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.runtime_core import Scope
from tests.modules.ai_market_intelligence.test_market_intelligence_graph import MarketFixtureModel, market_evidence, market_fixture
from tests.modules.ai_market_intelligence.test_market_intelligence_research_collector import MarketProviderFixture


class MarketStorageFixture:
    def __init__(self):
        self.objects={}
        self.revision=0

    def get_object(self,namespace,bucket,key):
        if key not in self.objects:
            error=RuntimeError('Not found')
            error.status=404
            raise error
        raw,etag=self.objects[key]
        data=SimpleNamespace(raw=io.BytesIO(raw),close=lambda:None)
        return SimpleNamespace(data=data,headers={'etag':etag})

    def put_object(self,namespace,bucket,key,raw,**options):
        previous=self.objects.get(key)
        if (options.get('if_none_match')=='*' and previous is not None
                or options.get('if_match') and (previous is None or previous[1]!=options['if_match'])):
            error=RuntimeError('Precondition failed')
            error.status=412
            raise error
        self.revision+=1
        self.objects[key]=(bytes(raw),str(self.revision))


class MarketNativeProviderFixture(MarketProviderFixture):
    async def observations(self,receipt,call,**kwargs):
        platform='instagram' if 'instagram' in json.dumps(call) else 'facebook'
        if 'scraperlink/' in call['actor']:
            row={'url':f'https://{platform}.com/fixturebrand','text':'Fixture brand public page',
                 'provenance':{'provider_run_id':receipt['runId'],'content_basis':'search_title_and_snippet'}}
        else:
            row={**market_evidence(platform),'url':f'https://{platform}.com/fixturebrand/posts/1',
                 'provenance':{'provider_run_id':receipt['runId'],'content_basis':'post_text'}}
        return SimpleNamespace(items=[row],coverage=None,cost=receipt['spent_microusd'])


def market_active_config():
    return {'schema_version':1,'research_enabled':True,'services':{
        'credential_name':'fixture-reference','identity_sha256':'a'*64,'candidate_revision':'b'*64,
        'region':'us-chicago-1','model_endpoint':'https://inference.example.com/',
        'model_id':'fixture-model','compartment_id':'fixture-compartment','guardrails_config':{},
        'tenant_id':'fixture-tenant','generation_id':'d27966a4-6b46-4734-a672-b648a3e7e375',
        'namespace':'fixture-namespace','bucket':'fixture-artifacts','enabled_sources':['facebook','instagram'],
        'search_context':{'country_code':'CO','language_code':'es'},'spending_mode':'unlimited',
        'runtime_profile':{'python':'3.11.13','packages':{name:'1.0.0' for name in
            ('langchain-core','langgraph','langgraph-checkpoint','langgraph-prebuilt','oci','pydantic','mcp','httpx','pypdf')}}}}


def market_native_fixture(tmp_path,monkeypatch):
    source,_=agent_package(market_active_config())
    path=tmp_path/'market_intelligence.py'
    path.write_bytes(source)
    name='market_active_native_fixture'
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules,name,module)
    spec.loader.exec_module(module)
    calls=[]
    helper=ModuleType('aidputils.agents.toolkit.agent_helper')
    def pre_invoke_setup(**kwargs):
        calls.append('pre_invoke_setup')
        return {'configurable':{'thread_id':kwargs.get('thread_id'),'checkpoint_ns':'default'}}
    def pre_tool_setup(**kwargs):
        calls.append(('pre_tool_setup',kwargs.get('thread_id')))
        return 'fixture-context-token'
    def post_tool_setup(token,**kwargs):
        calls.append(('post_tool_setup',token))
    helper.pre_invoke_setup=pre_invoke_setup
    helper.pre_tool_setup=pre_tool_setup
    helper.post_tool_setup=post_tool_setup
    monkeypatch.setitem(sys.modules,'aidputils.agents.toolkit.agent_helper',helper)
    monkeypatch.setattr(module,'native_memory',lambda *args,**kwargs:InMemorySaver())
    collector=module.MarketCollector
    monkeypatch.setattr(module,'MarketCollector',lambda *args,**kwargs:collector(*args,**kwargs,poll_attempts=1,poll_seconds=0))
    runtime=module.MarketIntelligenceAgent()
    runtime.setup()
    storage=MarketStorageFixture()
    model=MarketFixtureModel(('facebook','instagram'))
    provider=MarketNativeProviderFixture()
    runtime.services={'storage':storage,'model':model,'provider':provider,'mcp':None}
    return runtime,storage,model,provider,calls


def market_native_turn(runtime,question='¿Qué reclamaciones aparecen en la muestra?',request_id='request-1',subject='fixture-user',session='fixture-session',command=None):
    async def run():
        messages=await runtime.invoke(json.dumps(command if command is not None else {'request_id':request_id,'question':question}),
            subject_id=subject,thread_id=session,trace_id='fixture-trace',stream=True)
        decoder=MarketWireDecoder()
        packets=[]
        async for message in messages:
            for character in message.content:
                packets.extend(decoder.feed(character))
        decoder.finish()
        return packets
    return asyncio.run(run())


def test_native_replay_restores_public_reference_manifests_without_model_or_capture_calls(tmp_path,monkeypatch):
    runtime,_,model,provider,_=market_native_fixture(tmp_path,monkeypatch)
    first=market_native_turn(runtime)
    before=(len(model.calls),len(provider.starts))
    replay=market_native_turn(runtime,command={'action':'replay','request_id':'request-1','after_sequence':0})
    packet=replay[-1]
    assert packet['kind']=='snapshot' and packet['references']
    assert (len(model.calls),len(provider.starts))==before
    expected={row['evidence_id']:row['url'] for row in first[-1]['response']['references']}
    assert all(expected[row['evidence_id']]==row['url'] for row in packet['references'])
    assert all('source' in row and not {'text','owner_id','scope','receipt_id'}&row.keys() for row in packet['references'])


def test_native_market_full_graph_preserves_source_role_artifacts_and_cached_delivery(tmp_path,monkeypatch):
    runtime,storage,model,provider,calls=market_native_fixture(tmp_path,monkeypatch)
    packets=market_native_turn(runtime)
    drafts=[packet['event'] for packet in packets if packet['kind']=='event' and packet['event']['type']=='draft.upsert']
    assert [event['source_key'] for event in drafts]==['facebook','instagram']
    assert all(event['role_key']=='market_analysis' for event in drafts)
    assert packets[-1]['response']['status']=='complete'
    assert packets[-1]['response']['artifacts'][0]['kind']=='bar'
    assert len(provider.starts)==2 and calls[0]=='pre_invoke_setup'
    assert [call['actor'] for call in provider.starts]==['apify/facebook-search-scraper','apify/instagram-search-scraper']
    assert calls[-1]==('post_tool_setup','fixture-context-token')
    previous_calls=list(model.calls)
    repeated=market_native_turn(runtime)
    assert len(provider.starts)==2 and model.calls==previous_calls
    assert repeated[-1]['response']==packets[-1]['response']
    binding = market_active_config()['services']
    owner = hashlib.sha256(b'fixture-user').hexdigest()
    prefix = Scope(binding['tenant_id'], owner, binding['generation_id']).prefix
    assert all(key.startswith(prefix) and key.removeprefix(prefix).startswith(('research/', 'current/sessions/'))
               for key in storage.objects)
    assert any(key.startswith(prefix + 'research/') for key in storage.objects)
    assert any(key.startswith(prefix + 'current/sessions/') for key in storage.objects)


def test_native_market_clarification_context_is_retained_without_any_capture(tmp_path,monkeypatch):
    runtime,storage,model,provider,calls=market_native_fixture(tmp_path,monkeypatch)
    model.clarification='¿En qué fuentes quieres buscar?'
    first=market_native_turn(runtime,question='¿Qué se dice de la marca?')
    assert first[-1]['response']['status']=='needs_clarification' and provider.starts==[]
    original=model.ask
    context=[]
    async def observe_context(name,schema,payload):
        if name=='market_understand':
            context.append(copy.deepcopy(payload['conversation_context']))
        return await original(name,schema,payload)
    model.ask=observe_context
    model.clarification=None
    result=market_native_turn(runtime,question='En ambas redes.',request_id='request-2')
    assert result[-1]['response']['status']=='complete'
    assert context[0]['question']=='¿Qué se dice de la marca?'
    assert context[0]['brief']['clarification']=='¿En qué fuentes quieres buscar?'


def test_native_market_two_authenticated_owners_never_share_snapshots(tmp_path,monkeypatch):
    runtime,storage,model,provider,calls=market_native_fixture(tmp_path,monkeypatch)
    first=market_native_turn(runtime,subject='first-user')
    second=market_native_turn(runtime,subject='second-user')
    assert len(provider.starts)==4
    assert first[-1]['response']['references']!=second[-1]['response']['references']
    sessions=[key for key in storage.objects if '/sessions/' in key]
    assert len(sessions)==2


def test_native_market_same_owner_and_explicit_request_id_are_isolated_by_native_session(tmp_path,monkeypatch):
    runtime,storage,model,provider,calls=market_native_fixture(tmp_path,monkeypatch)
    first=market_native_turn(runtime,session='first-session')
    second=market_native_turn(runtime,session='second-session')
    assert len(provider.starts)==4
    assert first[-1]['response']['references']!=second[-1]['response']['references']
    events=[packet['event'] for packet in first+second if packet['kind']=='event' and packet['event']['type']=='run.completed']
    assert len({event['run_id'] for event in events})==2


def test_market_wire_rejects_incomplete_or_foreign_protocol_messages():
    decoder=MarketWireDecoder()
    decoder.feed('{"protocol":')
    with pytest.raises(ValueError,match='ended inside'):
        decoder.finish()
    with pytest.raises(ValueError,match='Unexpected native wire'):
        MarketWireDecoder().feed('{"protocol":"other","version":"2.0","kind":"response"}\n')


def test_market_wire_accepts_grouped_packets_with_individual_size_bounds():
    line=market_wire_line('response',response={'markdown':'x'*1100000})
    decoder=MarketWireDecoder()
    assert len(decoder.feed(line+line))==2
    decoder.finish()


def test_native_stream_can_close_at_response_without_leaving_a_reserved_session(tmp_path,monkeypatch):
    runtime,storage,model,provider,calls=market_native_fixture(tmp_path,monkeypatch)
    async def close_at_response():
        messages=await runtime.invoke(json.dumps({'request_id':'request-1','question':'First question'}),
            subject_id='fixture-user',thread_id='fixture-session',stream=True)
        async for message in messages:
            if json.loads(message.content)['kind']=='response':
                await messages.aclose()
                return
    asyncio.run(close_at_response())
    result=market_native_turn(runtime,question='Another question',request_id='request-2')
    assert result[-1]['response']['status']=='complete' and len(provider.starts)==4


def test_native_cancel_closes_pending_request_and_releases_session_preserving_receipts(tmp_path,monkeypatch):
    runtime,storage,model,provider,calls=market_native_fixture(tmp_path,monkeypatch)
    provider.pending=True
    pending=market_native_turn(runtime)
    assert pending[-1]['response']['status']=='pending'
    async def cancel():
        messages=await runtime.invoke(json.dumps({'action':'cancel','request_id':'request-1'}),
            subject_id='fixture-user',thread_id='fixture-session',stream=True)
        return [json.loads(message.content) async for message in messages]
    cancelled=asyncio.run(cancel())
    assert cancelled[-1]['response']['status']=='cancelled'
    old_starts=len(provider.starts)
    provider.pending=False
    result=market_native_turn(runtime,question='Another question',request_id='request-2')
    assert result[-1]['response']['status']=='complete' and len(provider.starts)>old_starts
    states=[json.loads(raw) for raw,_ in storage.objects.values()]
    old=[state for state in states if state.get('request_id')=='request-1']
    assert old[0]['status']=='cancelled' and old[0]['collections']
    operations=[state for state in states if 'operations' in state]
    assert any(row['receipt'] for state in operations for row in state['operations'].values() if row['kind']=='apify')


def test_market_runtime_mismatch_fails_before_credentials_or_remote_services():
    config=market_active_config()['services']
    with pytest.raises(ValueError,match='native runtime changed'):
        build_market_services(config)


def test_market_wire_heartbeats_do_not_publish_private_model_analysis():
    store,model,collector,graph=market_fixture()
    original=collector.collect
    async def delayed(**kwargs):
        await asyncio.sleep(0.025)
        return await original(**kwargs)
    collector.collect=delayed
    async def run():
        stream=market_stream_graph(graph,store,{'request_id':'request','question':'A fixture question'},
                              {'configurable':{'thread_id':'request'}},heartbeat_seconds=0.005)
        return [json.loads(line) async for line in stream]
    packets=asyncio.run(run())
    assert any(packet['kind']=='heartbeat' for packet in packets)
    assert all(set(packet)=={'protocol','version','kind','request_id'}
               for packet in packets if packet['kind']=='heartbeat')
    assert packets[-1]['kind']=='response'


def test_completed_invalid_decision_releases_the_session_for_a_new_request(tmp_path,monkeypatch):
    runtime,storage,model,provider,_=market_native_fixture(tmp_path,monkeypatch)
    original=model.ask
    async def invalid(name,schema,payload):
        result=await original(name,schema,payload)
        if name=='market_analyze':
            result['findings'][0]['supports']=['unowned-evidence']
        return result
    model.ask=invalid
    first=market_native_turn(runtime)
    assert first[-1]['response']['status']=='failed'
    model.ask=original
    second=market_native_turn(runtime,question='Otra investigación.',request_id='second')
    assert second[-1]['response']['status'] in {'complete','partial'}
    session=next(json.loads(raw) for key,(raw,_) in storage.objects.items() if '/sessions/' in key)
    assert session['requests']['request-1']['finished'] and session['active'] is None


def test_uncertain_model_failure_does_not_release_the_session_or_start_another_request(tmp_path,monkeypatch):
    runtime,storage,model,provider,_=market_native_fixture(tmp_path,monkeypatch)
    original=model.ask
    async def timeout(name,schema,payload):
        if name=='market_analyze':
            raise TimeoutError('Simulated uncertain submitted inference')
        return await original(name,schema,payload)
    model.ask=timeout
    market_native_turn(runtime)
    before=list(model.calls)
    second=market_native_turn(runtime,question='Otra investigación.',request_id='second')
    assert second[-1]['response']['status']=='pending' and model.calls==before
    session=next(json.loads(raw) for key,(raw,_) in storage.objects.items() if '/sessions/' in key)
    assert session['active']['request_id']=='request-1'
