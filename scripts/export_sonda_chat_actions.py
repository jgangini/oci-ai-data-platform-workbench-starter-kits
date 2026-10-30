"""Export the draft Chat Actions capability and synthetic fixtures; no network."""
import argparse
from pathlib import Path
from uuid import UUID

from app.sonda_contract import encoded, sha256
from app.sonda_chat_actions import (ACTIONS_VERSION, ACTION_ADAPTER, CaptureIntent,
    CaptureQuote, action_fingerprint, action_schema, capture_intent_id, parse_action)

ROOT=Path(__file__).resolve().parents[1]
TARGET=ROOT/'contracts/sonda-chat-actions/v1'


def action_fixtures(capability_hash, core_hash):
    uid=lambda number:str(UUID(int=number))
    pin={'capability_version':ACTIONS_VERSION,'capability_sha256':capability_hash,
        'core_contract_version':'1.0.2','core_contract_sha256':core_hash}
    scope={**pin,'registry_id':uid(1),'generation_id':uid(2)}
    context={'registry_id':uid(1),'generation_id':uid(2),'config_revision':1,'source_ids':[uid(4)],'query_ids':[uid(5)]}
    option={'registry_id':uid(1),'generation_id':uid(2),'turn_id':uid(3),'capture_config_revision':1,
        'source_id':uid(4),'source_revision':1,'query_id':uid(5),'query_revision':1,
        'label':'Página pública de ejemplo','mode':'pages','target':'https://example.com/reviewed-page',
        'target_override_allowed':True,'allowed_domains':['example.com'],'max_items':2,'max_cost_microusd':250000}
    option={**option,'option_id':sha256(encoded(option))}
    intent={**scope,'kind':'capture_intent','intent_id':str(capture_intent_id(uid(1),uid(2),uid(3))),
        'turn_id':uid(3),'capture_option_id':option['option_id'],'config_revision':1,'source_id':uid(4),'source_revision':1,'query_id':uid(5),
        'query_revision':1,'target_override':'https://example.com/reviewed-page','purpose':'Consultar una página pública aprobada.',
        'max_items':2,'max_cost_microusd':250000,'expires_at':'2026-01-01T00:15:00Z'}
    quote={**scope,'kind':'capture_quote','quote_id':uid(6),'intent_id':intent['intent_id'],
        'intent_sha256':action_fingerprint(CaptureIntent.model_validate(intent)),'revision':1,'config_revision':1,
        'source_revision':1,'query_revision':1,'connector_revision':1,'budget_round_id':uid(7),'budget_round_revision':1,
        'max_items':2,'max_cost_microusd':10000,'expires_at':'2026-01-01T00:12:00Z'}
    decision={**scope,'kind':'capture_decision','decision_id':uid(8),'intent_id':intent['intent_id'],'quote_id':uid(6),
        'quote_sha256':action_fingerprint(CaptureQuote.model_validate(quote)),'expected_operation_revision':3,'decision':'approve'}
    documents={'chat-input.json':{**pin,'kind':'chat_input','version':3,'turn_id':uid(3),
        'created_at':'2026-01-01T00:00:00Z','expires_at':'2026-01-01T00:15:00Z',
        'question':'Extrae información de la página aprobada.','search_context':context,
            'capture_config_revision':1,'capture_options_total':1,'capture_options':[option]},
        'intent.json':intent,'quote.json':quote,'decision-approve.json':decision,
        'decision-reject.json':{**decision,'decision':'reject'},
        'chat-response-action.json':{**scope,'kind':'chat_response','version':3,'turn_id':uid(3),
            'capture_config_revision':1,'answer':'Preparé una solicitud de extracción para revisar.',
            'gold':None,'gold_unavailable_reason':'not_requested','actions':[intent]}}
    no_capture={**documents['chat-input.json'],'capture_options_total':0,'capture_options':[]}
    documents['chat-input-no-capture-options.json']=no_capture
    proof=gold_evidence(core_hash)
    gold_publication={'module_id':'ai_social_network','publication_id':proof['publication_id'],
        'published_at':'2026-01-01T00:10:00Z','registry_id':uid(1),'generation_id':uid(2),
        'config_revision':1,'pending_files':None,'failed_files':None,'current_version':0,'history_version':0,'daily_version':0}
    documents['chat-response-gold.json']={**scope,'kind':'chat_response','version':3,'turn_id':uid(3),
        'capture_config_revision':1,'answer':'Hay una publicación en YouTube.','gold_unavailable_reason':None,'actions':[],
        'gold':{'sources':[],'publication':gold_publication,'search_context':context,
            'query_context':[{'tool':'summary','args':{'period':'all','metric':'posts','platform':'youtube','document_type':'post'}}],
            'visualization':{'version':1,'type':'bar','title':'Publicaciones','columns':['Red','Publicaciones'],
                'rows':[['youtube',1]],'caption':'Publicaciones disponibles.'},'visualization_error':None}}
    documents['chat-response-no-gold.json']={**documents['chat-response-action.json'],
        'answer':'Los datos todavía no están publicados.','actions':[],'gold_unavailable_reason':'no_publication'}
    for state in ('proposed','awaiting_confirmation','starting','running','captured','awaiting_gold',
            'ready','rejected','expired','cancelled','failed','uncertain'):
        started=state in ('starting','running','captured','awaiting_gold','ready','uncertain')
        proof=gold_evidence(core_hash)
        versions=proof['serving_versions']
        publication={**scope,'kind':'capture_publication',**proof['receipt_fields']}
        documents['status-'+state+'.json']={**scope,'kind':'capture_status','intent_id':intent['intent_id'],
            'revision':4,'updated_at':'2026-01-01T00:10:00Z','state':state,
            'quote':quote if state!='proposed' else None,'execution_id':uid(9) if started else None,
            'spent_microusd':None,'failure_code':'provider_outcome_unknown' if state=='uncertain'
                else 'provider_failed' if state=='failed' else None,
            'publication':publication if state=='ready' else None}
    zero={**documents['status-ready.json']}
    zero['publication']={**scope,'kind':'capture_publication',**gold_evidence(core_hash,zero=True)['receipt_fields']}
    documents['status-ready-zero.json']=zero
    documents['capture-publication.json']=documents['status-ready.json']['publication']
    documents['capture-publication-split.json']={**scope,'kind':'capture_publication',
        **gold_evidence(core_hash,split=True)['receipt_fields']}
    return documents


def gold_evidence(core_hash,*,zero=False,split=False):
    from app.sonda_contract import StreamEntry, observation_fingerprints
    uid=lambda number:str(UUID(int=number))
    scope={'contract_version':'1.0.2','contract_sha256':core_hash,'registry_id':uid(1),'generation_id':uid(2)}
    items=[]
    for number in ([] if zero else [1,2]):
        item={'document_type':'page','platform':None,'provider_id':None,'parent_document_key':None,
            'url':'https://example.com/page-'+str(number),'title':'Synthetic page','text':'Synthetic public fact '+str(number),
            'author':None,'published_at':None,'observed_at':'2026-01-01T00:08:00Z',
            'metrics':{'likes':None,'comments':None,'shares':None,'views':None},'indicators':[],'capture_scope':'page_snapshot'}
        items.append({**item,**observation_fingerprints(item)})
    observations=[] if zero else [{**scope,'kind':'observations','direction':'huginn_to_aidp','message_id':uid(11),
        'created_at':'2026-01-01T00:08:00Z','correlation_id':None,'payload':{'config_revision':1,
            'source_id':uid(4),'source_revision':1,'query_id':uid(5),'query_revision':1,
            'execution_id':uid(9),'budget_round_id':uid(7),'provider_run_id':'synthetic-provider-run',
            'initiated_by':'operator','observations':items}}]
    ids=[] if zero else [uid(11)]
    execution={**scope,'kind':'execution_result','direction':'huginn_to_aidp','message_id':uid(10),
        'created_at':'2026-01-01T00:09:00Z','correlation_id':None,'payload':{
            'execution_id':uid(9),'budget_round_id':uid(7),'request_id':None,'config_revision':1,
            'query_id':uid(5),'query_revision':1,'status':'completed','observation_message_ids':ids,
            'record_count':len(items),'spent_microusd':10000,'coverage':'requested_scope','explanation':'Synthetic capture.'}}
    sequence=26 if split else 2
    entry={**scope,'kind':'stream_entry','direction':'huginn_to_aidp','sequence':sequence,'message_id':uid(10),
        'message_kind':'execution_result','message_sha256':sha256(encoded(execution)),
        'message_bytes':len(encoded(execution)),'previous_entry_sha256':'d'*64}
    receipt_ids=[uid(12)] if zero else [uid(12),uid(13)]
    receipts=[{**scope,'kind':'processing_receipt','direction':'aidp_to_huginn','message_id':uid(number),
        'created_at':'2026-01-01T00:10:00Z','correlation_id':None,'payload':{'source_sequence':source_sequence,
            'source_message_id':uid(message),'status':'applied','code':'applied','explanation':'Synthetic receipt.'}}
        for number,message,source_sequence in ([(12,10,sequence)] if zero else [(12,10,sequence),(13,11,25 if split else 1)])]
    versions={'ai_social_'+name:0 for name in ('registry','sources','queries','document_current','observations',
        'discoveries','evaluation_results','reports','daily','executions')}
    publication_id=sha256(encoded([{key:scope[key] for key in ('registry_id','generation_id')},1,versions]))
    members=[{key:item[key] for key in ('observation_key','document_key','content_sha256')} for item in items]
    payload_hash=sha256(encoded(execution['payload']))
    snapshot={'registry_id':uid(1),'generation_id':uid(2),'execution_id':uid(9),'publication_id':publication_id,
        'serving_versions':versions,'execution_payload_sha256':payload_hash,'observations':members}
    fields={'publication_id':publication_id,'published_at':'2026-01-01T00:10:00Z',
        'capture_config_revision':1,'publication_config_revision':1,'serving_versions':versions,
        'publication_message_ids':[uid(10)] if split or zero else [uid(10),uid(11)],
        'execution_id':uid(9),'intent_id':None,'execution_message_id':uid(10),
        'execution_payload_sha256':payload_hash,'execution_source_sequence':sequence,
        'execution_entry_sha256':sha256(encoded(StreamEntry.model_validate(entry).model_dump(mode='json'))),
        'execution_previous_entry_sha256':'d'*64,'observation_message_ids':ids,
        'processing_receipt_ids':receipt_ids,'record_count':len(items),
        'members':[{**row,'observation_message_ids':ids} for row in members]}
    return {'execution':execution,'entry':entry,'receipts':receipts,'observations':observations,
        'snapshot':snapshot,'serving_versions':versions,'entry_sha256':fields['execution_entry_sha256'],
        'publication_id':publication_id,'receipt_fields':fields}


def artifacts():
    import json
    core_manifest=json.loads((ROOT/'contracts/sonda/v1/manifest.json').read_text(encoding='utf-8'))
    core_raw=(ROOT/'apps/backend/app/sonda_contract.py').read_bytes().replace(b'\r\n',b'\n')
    if sha256(core_raw)!=core_manifest['reference_sha256']:
        raise ValueError('The published core reference must remain byte-exact')
    reference=(ROOT/'apps/backend/app/sonda_chat_actions.py').read_bytes().replace(b'\r\n',b'\n')
    if reference.count(b'from app.sonda_contract import ')!=1:
        raise ValueError('The portable reference import boundary changed')
    reference=reference.replace(b'from app.sonda_contract import ',b'from sonda_core_1_0_2 import ')
    schema_raw=encoded(action_schema())
    core_hash=core_manifest['contract_sha256']
    capability_hash=sha256(encoded({'schema_sha256':sha256(schema_raw),
        'reference_sha256':sha256(reference),'core_contract_sha256':core_hash}))
    files={'schema.json':schema_raw,'reference.py':reference}
    for name,value in action_fixtures(capability_hash,core_hash).items():
        raw=encoded(value)
        parse_action(raw,capability_sha256=capability_hash,core_contract_sha256=core_hash,
            registry_id=UUID(int=1),generation_id=UUID(int=2))
        files['fixtures/'+name]=raw
    manifest={'capability_version':ACTIONS_VERSION,'status':'draft_not_accepted_or_activated',
        'capability_sha256':capability_hash,'schema_file':'schema.json','schema_sha256':sha256(schema_raw),
        'reference_file':'reference.py','reference_sha256':sha256(reference),'reference_import_rewrite':'app.sonda_contract -> sonda_core_1_0_2',
        'core_contract_version':'1.0.2','core_contract_sha256':core_hash,
        'core_reference_file':'../../sonda/v1/reference.py','core_reference_module':'sonda_core_1_0_2','core_reference_sha256':sha256(core_raw),
        'fixtures_are_synthetic':True,'fixtures':{name:sha256(raw) for name,raw in files.items() if name.startswith('fixtures/')}}
    files['gold-evidence.json']=encoded(gold_evidence(core_hash))
    files['gold-evidence-split.json']=encoded(gold_evidence(core_hash,split=True))
    files['gold-evidence-zero.json']=encoded(gold_evidence(core_hash,zero=True))
    manifest['gold_evidence_file']='gold-evidence.json'
    manifest['gold_evidence_sha256']=sha256(files['gold-evidence.json'])
    manifest['evidence_vectors']={name:sha256(raw) for name,raw in files.items() if name.startswith('gold-evidence')}
    manifest['max_document_bytes']=1048576
    files['manifest.json']=encoded(manifest)
    return files


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    files=artifacts()
    for name,raw in files.items():
        path=TARGET/name
        if args.check:
            if not path.is_file() or path.read_bytes()!=raw:
                raise SystemExit('Draft capability drift: '+name)
        else:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(raw)
    print('PASS draft Chat Actions '+ACTIONS_VERSION+'; '+str(sum(name.startswith('fixtures/') for name in files))+' synthetic fixtures; not activated')


if __name__=='__main__':
    main()
