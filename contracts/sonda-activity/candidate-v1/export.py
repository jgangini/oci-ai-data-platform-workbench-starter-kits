"""Export/check the unaccepted synthetic candidate; never contacts a runtime."""
import argparse
import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parents[2]/'apps/backend'))
from app.sonda_contract import ChatInput, encoded as core_encoded, no_duplicate_keys
spec = importlib.util.spec_from_file_location('activity_candidate', ROOT/'reference.py')
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)
producer_spec = importlib.util.spec_from_file_location('activity_producer_candidate', ROOT/'producer.py')
producer = importlib.util.module_from_spec(producer_spec)
producer_spec.loader.exec_module(producer)
result_spec = importlib.util.spec_from_file_location('activity_result_candidate', ROOT/'results.py')
result = importlib.util.module_from_spec(result_spec)
result_spec.loader.exec_module(result)


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))+'\n').encode()


def producer_bytes(value):
    return producer.producer_encoded(producer.PRODUCER_DOCUMENTS.validate_python(value).model_dump(mode='json'))


def artifacts():
    uid = lambda n: str(UUID(int=n))
    now = '2026-10-30T00:00:00Z'
    scope = {'registry_id':uid(1), 'generation_id':uid(2)}
    wrapper = {'schema_version':'activity.v1.candidate.4', 'scope':scope, 'next_cursor':None}
    run = {'run_id':uid(3), 'request_id':uid(4), 'attempt_id':uid(5), 'work_id':None, 'investigation_id':None,
        'execution_kind':'native_invocation','component':'native_agent',
        'agent_id':uid(6), 'agent_name':'Synthetic chat fixture', 'source':'huginn_chat',
        'created_at':now, 'synthetic':True}
    base = {'run_id':uid(3), 'attempt_id':uid(5), 'producer_stream_id':uid(7),
        'authority':'native_agent', 'emitted_at':now, 'received_at':now,
        'span_id':uid(8), 'parent_span_id':None, 'causation_id':None}
    events = [{**base, 'event_id':uid(10), 'producer_sequence':1, 'ingestion_sequence':1,
        'type':'tool_start', 'tool_name':'gold_query', 'domain':'query_read', 'summary':'Synthetic query started'},
        {**base, 'event_id':uid(11), 'producer_sequence':2, 'ingestion_sequence':2,
        'type':'tool_finish', 'effect':'executed', 'summary':'Synthetic query completed'},
        {**base, 'event_id':uid(12), 'producer_sequence':3, 'ingestion_sequence':3,
        'type':'text_delta', 'public_text_delta':'Respuesta sintética parcial.'},
        {**base, 'event_id':uid(13), 'producer_sequence':4, 'ingestion_sequence':4,
        'type':'run_terminal', 'authority':'activity_broker', 'outcome':'completed', 'summary':'Synthetic completion',
        'final_ref':{'result_id':uid(14), 'sha256':'a'*64, 'byte_length':100}}]
    valid = {'runs':{**wrapper, 'items':[run]},
        'events':{**wrapper, 'run_id':uid(3), 'attempt_id':uid(5), 'events':events,
            'resume_cursor':'synthetic-opaque-checkpoint-not-a-valid-token'}}
    valid['events-at-tail'] = {**valid['events'], 'events':[]}
    valid['runs-autonomous'] = {**wrapper, 'items':[{**run, 'source':'autonomous_study',
        'work_id':uid(20), 'investigation_id':'b'*64, 'agent_name':'Synthetic study fixture'}]}
    parent={'run_id':uid(3),'attempt_id':uid(5),'span_id':uid(8),'event_id':uid(11)}
    valid['runs-provider']={**wrapper,'items':[{**run,'run_id':uid(40),'attempt_id':uid(41),
        'execution_kind':'provider_execution','component':'huginn_provider_broker',
        'agent_id':None,'agent_name':None,'execution_ref':'synthetic-provider-execution',
        'causal_parent':parent,'core_request_id':uid(42),'core_message_ids':[uid(43)]}]}
    valid['runs-publication']={**wrapper,'items':[{**run,'run_id':uid(44),'attempt_id':uid(45),
        'execution_kind':'gold_publication','component':'aidp_gold_runner',
        'agent_id':None,'agent_name':None,'execution_ref':uid(46),
        'causal_parent':parent,'core_request_id':None,'core_message_ids':[uid(43)]}]}
    stage = {**base, 'event_id':uid(30), 'producer_sequence':5, 'ingestion_sequence':5,
        'type':'stage', 'authority':'activity_broker', 'summary':'Synthetic observation uncertainty', 'effect':None}
    valid['events-uncertain'] = {**valid['events'], 'events':[{**stage, 'status':'uncertain'}]}
    valid['events-degraded'] = {**valid['events'], 'events':[events[2],
        {**stage, 'status':'degraded', 'summary':'Synthetic public stream interrupted'}]}
    valid['events-progress-default'] = {**valid['events'], 'events':[stage]}
    valid['events-finish-before-start'] = {**valid['events'], 'events':[events[1],events[0]]}
    outputs = {'schema.json':encoded(reference.RESPONSES.json_schema()),
        'producer-schema.json':encoded(producer.producer_schema()),
        'result-schema.json':encoded(result.RESULTS.json_schema())}
    for source_name in ('reference.py','producer.py','public_stream.py','journal.py','native_bundle.py','results.py'):
        outputs[source_name]=(ROOT/source_name).read_bytes()
    chat_input={'version':2,'question':'¿Cuántos posts de Cafe\u0301 y Coca-Cola? 🌎',
        'search_context':{**scope,'config_revision':23,'source_ids':[uid(32),uid(31)],
            'query_ids':[uid(34),uid(33)]}}
    raw_input=json.dumps(chat_input,ensure_ascii=True,indent=2)
    validated=ChatInput.model_validate(json.loads(raw_input,object_pairs_hook=no_duplicate_keys)).model_dump(mode='json')
    canonical=core_encoded(validated)
    outputs['fixtures/chat-admission-input.json']=encoded({'synthetic':True,'raw_input':raw_input,
        'validated_input':validated,'canonical_utf8':canonical.decode('utf-8'),
        'input_sha256':hashlib.sha256(canonical).hexdigest()})
    for name, value in valid.items():
        reference.parse(encoded(value))
        outputs['fixtures/'+name+'.json'] = encoded(value)
    bad = copy.deepcopy(valid['events'])
    bad['events'][0]['run_id'] = uid(99)
    outputs['fixtures/reject-cross-run.json'] = encoded(bad)
    bad = copy.deepcopy(valid['events'])
    bad['events'][0]['private_prompt'] = 'synthetic forbidden field'
    outputs['fixtures/reject-private-field.json'] = encoded(bad)
    bad = copy.deepcopy(valid['events'])
    bad['events'][0]['attempt_id'] = uid(99)
    outputs['fixtures/reject-cross-attempt.json'] = encoded(bad)
    bad = copy.deepcopy(valid['events'])
    bad['events'][-1]['authority'] = 'native_agent'
    outputs['fixtures/reject-native-terminal.json'] = encoded(bad)
    bad = copy.deepcopy(valid['events'])
    bad['events'][0]['domain'] = 'provider_capture'
    outputs['fixtures/reject-provider-claim.json'] = encoded(bad)
    bad = copy.deepcopy(valid['events-degraded'])
    bad['events'][-1]['authority'] = 'native_agent'
    outputs['fixtures/reject-native-degradation.json'] = encoded(bad)
    bad=copy.deepcopy(valid['runs-provider'])
    bad['items'][0]['agent_id']=uid(6)
    outputs['fixtures/reject-provider-agent-identity.json']=encoded(bad)
    bad=copy.deepcopy(valid['runs-provider'])
    bad['items'][0]['component']='native_agent'
    outputs['fixtures/reject-execution-component.json']=encoded(bad)
    outputs['fixtures/replay-scenario.json'] = encoded({'synthetic':True,
        'reads':[valid['events'], valid['events-at-tail'], valid['events']],
        'expected_run_id':uid(3), 'expected_attempt_id':uid(5)})
    outputs['fixtures/span-policy-scenarios.json']=encoded({'synthetic':True,'schema_version':'activity.v1.candidate.4',
        'cases':[{'name':'finish_before_start','reads':[events[1],events[0]],
            'expected':'hold_finish_until_registered_start_and_authorized_receipt'},
            {'name':'domain_cannot_change','reads':[events[0],{**events[0],'domain':'research_preparation','event_id':uid(47)}],
             'expected':'reject_conflicting_span_registration'},
            {'name':'post_terminal_provider_is_separate','parent_run':run,'child_run':valid['runs-provider']['items'][0],
             'expected':'link_authorized_parent_without_reopening_or_mutating_parent'}]})
    version=producer.PRODUCER_VERSION
    descriptor={'schema_version':version,'admission_id':uid(50),'scope':scope,'run':run,
        'native_session_sha256':'c'*64,'input_sha256':'d'*64,'config_revision':1,'tool_catalog_sha256':'e'*64,
        'created_at':now,'expires_at':'2026-10-30T01:00:00Z','work_input_sha256':None}
    current={'schema_version':version,'scope':scope,'agent_id':run['agent_id'],
        'native_session_sha256':descriptor['native_session_sha256'],'admission_id':uid(50),
        'run_id':run['run_id'],'request_id':run['request_id'],'attempt_id':run['attempt_id'],
        'epoch':1,'status':'admitted','owner_nonce':None,'expires_at':descriptor['expires_at']}
    claim={'schema_version':version,'scope':scope,'admission_id':uid(50),'run_id':run['run_id'],
        'attempt_id':run['attempt_id'],'owner_nonce':uid(51),'claimed_at':now}
    private_events=[{**{key:value for key,value in event.items() if key not in ('authority','received_at','ingestion_sequence')},
        'producer_stream_id':uid(5)}
        for event in events[:3]]
    private_events.append({**{key:value for key,value in private_events[-1].items()
        if key!='public_text_delta' and key!='type'},'event_id':uid(52),'producer_sequence':4,
        'kind':'execution_completion','outcome':'completed','summary':'Synthetic native execution finished',
        'final_ref':events[-1]['final_ref']})
    journal={'schema_version':version,'scope':scope,'admission_id':uid(50),'run_id':run['run_id'],
        'attempt_id':run['attempt_id'],'producer_stream_id':uid(5)}
    batch={**journal,'batch_id':uid(53),'first_sequence':1,'last_sequence':4,'events':private_events}
    batch_raw=producer_bytes(batch)
    index={**journal,'index_id':uid(54),'index_sequence':1,'first_sequence':1,'last_sequence':4,
        'batch':{'batch_id':uid(53),'sha256':hashlib.sha256(batch_raw).hexdigest(),'byte_length':len(batch_raw)},
        'previous_index_sha256':None}
    index_raw=producer_bytes(index)
    head={**journal,'index_sequence':1,'index_id':uid(54),'index_sha256':hashlib.sha256(index_raw).hexdigest(),
        'producer_sequence':4}
    for name,value in (('descriptor',descriptor),('current',current),('claim',claim),('batch',batch),('index',index),('head',head)):
        raw=producer_bytes(value)
        producer.parse_producer_document(raw)
        outputs['fixtures/producer-'+name+'.json']=raw
    step={'schema_version':result.RESULT_VERSION,'scope':scope,'result_id':uid(60),
        'run_id':uid(3),'attempt_id':uid(5),'kind':'native_step','payload':{
            'work_id':uid(20),'input_sha256':'d'*64,'investigation_id':'b'*64,'phase':'evaluated',
            'report_refs':[],'evaluation_count':2,'request_count':0,'proposal_count':0,'publication_status':'pending'}}
    step_raw=result.result_bytes(step)
    result.parse_result(step_raw,expected_scope=scope,expected_run=valid['runs-autonomous']['items'][0],
        expected_ref={'result_id':uid(60),'sha256':hashlib.sha256(step_raw).hexdigest(),'byte_length':len(step_raw)},
        expected_work_input_sha256='d'*64)
    outputs['fixtures/result-native-step.json']=step_raw
    producer.validate_batch_binding(batch,descriptor)
    producer.validate_index_batch(index,batch_raw)
    producer.validate_head_successor(None,head,index_raw)
    for name, raw in outputs.items():
        if '/reject-' in name:
            try:
                reference.parse(raw)
            except ValueError:
                continue
            raise AssertionError('Invalid fixture was accepted: '+name)
    outputs['candidate.json'] = encoded({'status':'proposal_not_accepted_not_active',
        'schema_version':'activity.v1.candidate.4',
        'producer_schema_version':version,
        'result_schema_version':result.RESULT_VERSION,
        'files':{name:hashlib.sha256(raw).hexdigest() for name, raw in sorted(outputs.items())}})
    return outputs


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    for name, raw in artifacts().items():
        target = ROOT/name
        if args.check:
            assert target.read_bytes() == raw, 'Candidate artifact drift: '+name
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
    print('Activity candidate artifacts PASS; no runtime acceptance or activation')
