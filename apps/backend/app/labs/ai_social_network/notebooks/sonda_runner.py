"""Five-minute single-writer Sonda orchestration; no Apify or arbitrary HTTP targets.

The installer owns exactly one job with maxConcurrentRuns=1 and queue disabled.
Only that job may call this entry for its bound generation. Administrative reset
first pauses the job and verifies no active runs. Native inference never writes
Delta: the job alone applies/replays and publishes business effects.
"""
import hashlib
import json
from uuid import UUID, uuid5

from app.sonda_contract import encoded, no_duplicate_keys
from app.sonda_exchange import Exchange, OciObjects, PreparedPublication, RejectedMessage, StreamConflict
from app.labs.ai_social_network.notebooks.sonda_io import (
    generation_enabled, generation_bootstrap, generation_inference, operational_prefix, runtime_storage)
from app.labs.ai_social_network.notebooks.sonda_delta import (
    _rows, _scope, _table, apply_messages, apply_case_study_results, checked_binding,
    definitions, ensure_tables, json_mode, list_pending_cases, load_case_work, publish, reject_transport_message)

RUNNER_BINDING = {}


def pending_outbox(spark,binding,limit=100):
    from pyspark.sql import functions as F
    frame=spark.table(_table(binding,'outbox')).where(F.col('status')=='ready')
    for key,value in _scope(binding).items():
        frame=frame.where(F.col(key)==value)
    return [row.asDict() for row in frame.orderBy('outbox_key').limit(limit).collect()]


def reserved_publication(objects,binding,stream,raw):
    message=json.loads(raw)
    key=operational_prefix(binding)+'publisher/'+message['message_id']+'.json'
    saved=objects.get('artifacts',key)
    if saved is None:
        reservation=stream.prepare(raw)
        value={'message':reservation.message.decode(),'entry':reservation.entry.decode(),
            'head':reservation.head.decode(),'previous_head_etag':reservation.previous_head_etag}
        data=encoded(value)
        try:
            objects.put('artifacts',key,data,None)
        except StreamConflict:
            saved=objects.get('artifacts',key)
            if saved is None or saved.data!=data:
                raise StreamConflict('Publisher reservation changed') from None
        return reservation
    value=json.loads(saved.data,object_pairs_hook=no_duplicate_keys)
    if set(value)!={'message','entry','head','previous_head_etag'} or value['message'].encode()!=raw:
        raise StreamConflict('Publisher reservation does not match its durable outbox')
    return PreparedPublication(value['message'].encode(),value['entry'].encode(),
        value['head'].encode(),value['previous_head_etag'])


def flush_outbox(spark,binding,objects,stream):
    count=0
    for row in pending_outbox(spark,binding):
        raw=row['payload_json'].encode('utf-8')
        stream.publish(reserved_publication(objects,binding,stream,raw))
        row['status']='sent'
        _rows(spark,binding,'outbox',[row],('outbox_key',),update=True)
        count+=1
    return count


def consume_batch(spark,binding,stream,*,configuration_only=False):
    count=0
    accepted=[]
    def commit_accepted():
        if not accepted:
            return
        sequences={str(message.message_id):sequence for sequence,message,_ in accepted}
        hashes={str(message.message_id):digest for _,message,digest in accepted}
        apply_messages(spark,binding,[message.model_dump(mode='json') for _,message,_ in accepted],
            source_sequences=sequences,source_hashes=hashes)
        for sequence,_,_ in accepted:
            stream.acknowledge(sequence)
        accepted.clear()
    for sequence,message in stream.pending(limit=25):
        if configuration_only and (isinstance(message,RejectedMessage) or message.kind!='configuration'):
            break  # Leave budgets/captures untouched until explicit activation.
        digest=stream.verified_hash(sequence)
        if isinstance(message,RejectedMessage):
            commit_accepted()
            reject_transport_message(spark,binding,message_id=str(message.message_id),raw_sha256=digest,
                kind=message.kind,code=message.code,source_sequence=sequence)
            stream.acknowledge(sequence)
        else:
            accepted.append((sequence,message,digest))
        # The functions above return only after publication and receipt outbox commit.
        count+=1
    commit_accepted()
    return count


def create_case_work(objects,binding,projection):
    fields={'configuration','work','state','execution_results'}
    if not fields<=set(projection) or set(projection)-fields-{'publication_id','publication_sha256'}:
        raise ValueError('Unexpected Gold case projection')
    # Publication metadata proves the loader snapshot but is not case identity:
    # an unrelated case commit must not restart an uncertain native work item.
    raw=encoded({key:projection[key] for key in fields})
    if len(raw)>4194304:
        raise ValueError('Case work exceeds the bounded native input')
    digest=hashlib.sha256(raw).hexdigest()
    work_id=str(uuid5(UUID(binding['generation_id']),digest))
    key=operational_prefix(binding)+'work/'+work_id+'/input.json'
    try:
        objects.put('artifacts',key,raw,None)
    except StreamConflict:
        saved=objects.get('artifacts',key)
        if saved is None or saved.data!=raw:
            raise StreamConflict('Immutable native work changed') from None
    return {'version':1,'work_id':work_id,'sha256':digest}


def decode_case_result(value,request):
    if (not isinstance(value,dict) or set(value)!={'work_id','input_sha256','result'}
        or value['work_id']!=request['work_id'] or value['input_sha256']!=request['sha256']
        or not isinstance(value['result'],dict)):
        raise ValueError('Native case response does not confirm its immutable work')
    return value['result']


def invoke_case(binding,objects,signer,request):
    # Prefer a durable result after a previous unknown HTTP outcome.
    key=operational_prefix(binding)+'work/'+request['work_id']+'/result.json'
    saved=objects.get('artifacts',key)
    if saved:
        return decode_case_result(json.loads(saved.data,object_pairs_hook=no_duplicate_keys),request)
    import requests
    agent=str(UUID(binding['investigator_agent_key']))
    region=binding['region']
    if not isinstance(region,str) or not region or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in region):
        raise ValueError('Invalid bound OCI region')
    url=f'https://gateway.aidp.{region}.oci.oraclecloud.com/agentendpoint/{agent}/chat'
    try:
        response=requests.post(url,auth=signer,json={'sessionKey':request['work_id'],
            'isStreamEnabled':False,'trace':False,'input':[{'role':'User','content':[
                {'type':'INPUT_TEXT','text':json.dumps(request,separators=(',',':'))}]}]},
            headers={'x-session-id':request['work_id']},timeout=(10,240),allow_redirects=False)
    except requests.RequestException:
        raise RuntimeError('Native case inference outcome is not yet known') from None
    if response.status_code!=200:
        raise RuntimeError('Native case inference is not available')
    value=response.json()
    if not isinstance(value,dict) or (value.get('error') or {}).get('code'):
        raise RuntimeError('Native case inference failed')
    texts=[part['text'] for row in value.get('output',[]) if row.get('role')=='assistant'
        for part in row.get('content',[]) if part.get('type')=='output_text']
    if len(texts)!=1 or len(texts[0].encode('utf-8'))>4194304:
        raise RuntimeError('Native case inference returned an invalid response')
    # An HTTP response is insufficient: require the create-only result object.
    saved=objects.get('artifacts',key)
    if saved is None:
        raise RuntimeError('Native case inference has not committed its result')
    return decode_case_result(json.loads(saved.data,object_pairs_hook=no_duplicate_keys),request)


def process_case(spark,binding,objects,signer,case_key,identity):
    """Pin uncertain work before invoking; new same-case input cannot replace it."""
    if not isinstance(case_key,str) or len(case_key)!=64 or any(c not in '0123456789abcdef' for c in case_key):
        raise ValueError('Invalid investigation identity')
    key=operational_prefix(binding)+'pending_cases/'+case_key+'.json'
    saved=objects.get('artifacts',key)
    pointer=json.loads(saved.data,object_pairs_hook=no_duplicate_keys) if saved else None
    if pointer and (set(pointer)!={'investigation_id','request','status'}
        or pointer['investigation_id']!=case_key or pointer['status'] not in ('pending','applied')):
        raise StreamConflict('Pending case identity changed')
    if not pointer or pointer['status']=='applied':
        projection=load_case_work(spark,binding,case_key,model_identity=identity,batch_size=10)
        request=create_case_work(objects,binding,projection)
        pointer={'investigation_id':case_key,'request':request,'status':'pending'}
        etag=objects.put('artifacts',key,encoded(pointer),saved.etag if saved else None)
    else:
        request=pointer['request']
        etag=saved.etag
    result=invoke_case(binding,objects,signer,request)
    apply_case_study_results(spark,binding,result,result_id=request['work_id'],
        predecessor_revision=result['state']['predecessor_revision'])
    # A crash before this marker replays the same idempotent result, never inference.
    objects.put('artifacts',key,encoded({**pointer,'status':'applied'}),etag)


def record_runtime(spark,binding,objects,result):
    from datetime import datetime, timezone
    publication=publish(spark,binding)
    receipt={**result,**_scope(binding),'contract_sha256':binding['contract_sha256'],
        'table_count':len(definitions(binding)),'publication':json_mode(publication),
        'completed_at':datetime.now(timezone.utc).isoformat()}
    key=operational_prefix(binding)+'runtime.json'
    previous=objects.get('artifacts',key)
    objects.put('artifacts',key,encoded(receipt),previous.etag if previous else None)
    return result


def resume_pending_cases(spark,binding,objects,signer,identity):
    """Resolve uncertain work against its original input before advancing inbound."""
    resolved=0
    for item in list_pending_cases(spark,binding,limit=5)['cases']:
        key=item['investigation_id'] if isinstance(item,dict) else item
        pointer=objects.get('artifacts',operational_prefix(binding)+'pending_cases/'+key+'.json')
        if not pointer or json.loads(pointer.data,object_pairs_hook=no_duplicate_keys).get('status')!='pending':
            continue
        process_case(spark,binding,objects,signer,key,identity)
        resolved+=1
    return resolved


def run_sonda(binding,*,validation=False):
    from pyspark.sql import SparkSession
    binding=checked_binding(binding)
    storage,signer=runtime_storage(binding)
    active=generation_enabled(storage,binding)
    bootstrap=not active and generation_bootstrap(storage,binding)
    validating=validation is True and generation_inference(storage,binding)
    if not (active or bootstrap or validating):
        return {'status':'paused','processed_messages':0,'processed_case_phases':0}
    spark=SparkSession.builder.getOrCreate()
    spark.conf.set('spark.sql.session.timeZone','UTC')
    ensure_tables(spark,binding)
    objects=OciObjects(storage,binding['namespace'],{
        'source':binding['source_bucket'],'artifacts':binding['artifacts_bucket']})
    common={key:binding[key] for key in ('registry_id','generation_id','contract_sha256','source_prefix')}
    inbound=Exchange(objects,**common,direction='huginn_to_aidp')
    outbound=Exchange(objects,**common,direction='aidp_to_huginn')
    apply_messages(spark,binding,[])  # Repair interrupted inbound publication first.
    identity={key:binding[key] for key in ('model_id','model_revision','prompt_version')}
    if active or validating:
        try:
            resume_pending_cases(spark,binding,objects,signer,identity)
        except (RuntimeError,TimeoutError):
            published=flush_outbox(spark,binding,objects,outbound)
            return record_runtime(spark,binding,objects,{'status':'deferred','processed_messages':0,
                'processed_case_phases':0,'deferred_case_phases':1,'published_messages':published})
    processed=(consume_batch(spark,binding,inbound,configuration_only=True) if bootstrap and not validating
        else consume_batch(spark,binding,inbound))
    published=flush_outbox(spark,binding,objects,outbound)
    if bootstrap and not validating:
        return record_runtime(spark,binding,objects,{'status':'prepared','processed_messages':processed,
            'processed_case_phases':0,'published_messages':published})
    cases=0
    deferred=0
    pending_cases=list_pending_cases(spark,binding,limit=5)
    for item in pending_cases['cases']:
        if not generation_inference(storage,binding):
            break
        key=item['investigation_id'] if isinstance(item,dict) else item
        try:
            process_case(spark,binding,objects,signer,key,identity)
        except (RuntimeError,TimeoutError):
            # Keep the same immutable work/result identity for the next scheduled run.
            # No capture start or budget reservation occurs in this process.
            deferred+=1
            continue
        cases+=1
        published+=flush_outbox(spark,binding,objects,outbound)
    return record_runtime(spark,binding,objects,{'status':'success','processed_messages':processed,
        'processed_case_phases':cases,'deferred_case_phases':deferred,'published_messages':published})
