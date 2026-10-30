"""Export the portable contract and synthetic interoperability fixtures. No network."""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps/backend'))
from app.sonda_contract import VERSION, ChatInput, catalog_fingerprint, chat_schema, encoded, evaluation_fingerprint, message_target, observation_fingerprints, parse, schema, sha256


def fixtures(contract_hash):
    uid = lambda number: str(UUID(int=number))
    at = '2026-01-01T00:00:00Z'
    common = {'contract_version':VERSION,'contract_sha256':contract_hash,'registry_id':uid(1),'generation_id':uid(2)}
    approved = {'status':'approved','decided_by':'fixture-operator','decided_at':at}
    managed = {'revision':1,'origin':'operator','author_id':'fixture-operator',
               'proposal_id':None,'approval':approved,'status':'active'}
    source = {**managed,'source_id':uid(3),'label':'Synthetic public pages',
              'connector_id':'fixture.pages','connector_revision':1,'category':'public_web','platform':None,
              'allowed_domains':['example.com']}
    queries = [{**managed,'query_id':uid(100+i),'source_id':uid(3),'label':'Fixture query '+str(i),
                'mode':'pages','target':'https://example.com/page/'+str(i),'parameters':{},
                'max_items':10,'max_charge_microusd':20000,'research_allowed':True} for i in range(6)]
    criterion = {**managed,'criterion_id':uid(4),'label':'Price announcements','source_ids':[uid(3)],
        'query_ids':[],'description':'Detect announcements about price changes, not unrelated sports news.',
        'threshold':0.8,'allow_research':True,'examples':[
            {'text':'Northstar announced a price reduction.','relevant':True},
            {'text':'Local sports results were published.','relevant':False}],
        'case_scope_key':'fixture-market','case_window_hours':24}
    config = {'revision':1,'previous_revision':0,'catalog_version':'fixture.1',
        'catalog_sha256':sha256(b'synthetic connector catalog'), 'connectors':[{
            'connector_id':'fixture.pages','revision':1,'actor_id':'fixture/public-pages',
            'actor_build':'fixture-only','operation':'pages','input_schema_sha256':sha256(b'synthetic input schema'),
            'output_mapping_version':'fixture.1','modes':['pages'],'research_fields':['target'],
            'reviewed_by':'fixture-operator','reviewed_at':at,'max_cost_microusd_per_item':1000}],
        'sources':[source],'queries':queries,'criteria':[criterion],
        'policy':{'interval_seconds':300,'max_concurrency':5,'max_items_per_round':60,
            'round_budget_microusd':200000,'research_reserve_microusd':50000,'max_research_requests_per_case':3}}
    config['catalog_sha256'] = catalog_fingerprint(config['connectors'])

    def message(number, kind, payload, direction='huginn_to_aidp'):
        return {**common,'kind':kind,'direction':direction,'message_id':uid(number),
                'created_at':at,'correlation_id':None,'payload':payload}

    observations = []
    for index, text in enumerate(('Northstar announced a price reduction.','Local sports results were published.')):
        row = {'document_type':'page','identity_version':1,'platform':None,'provider_id':None,
            'parent_document_key':None,'url':'https://example.com/evidence/'+str(index),
            'title':'Synthetic fixture '+str(index),'text':text,'author':None,'published_at':at,
            'observed_at':at,'metrics':{'likes':None,'comments':None,'shares':None,'views':None},
            'indicators':[],'capture_scope':'page_snapshot'}
        observations.append({**row,**observation_fingerprints(row)})
    evidence = [{'evidence_id':sha256(encoded([r['observation_key'],r['text']])),
        'observation_key':r['observation_key'],'document_key':r['document_key'],'url':r['url'],
        'quote':r['text'],'captured_at':at} for r in observations]
    evaluations = [{'evaluation_key':sha256(encoded([r['observation_key'],uid(4),1,'fixture-model',1])),
        'observation_key':r['observation_key'],'criterion_id':uid(4),'criterion_revision':1,'config_revision':1,
        'model_id':'fixture-model','model_revision':'fixture.1','prompt_version':'fixture.1',
        'rule_sha256':sha256(encoded(criterion)),'score':0.95 if i==0 else 0.05,'threshold':0.8,
        'decision':'relevant' if i==0 else 'not_relevant','reason':'Labelled synthetic example.',
        'evidence':[evidence[i]],'evaluated_at':at} for i,r in enumerate(observations)]
    evaluations = [{**row,'evaluation_key':evaluation_fingerprint(row)} for row in evaluations]
    case = sha256(encoded([uid(4),1,'fixture-market',at]))
    batch = {'config_revision':1,'source_id':uid(3),'source_revision':1,'query_id':uid(100),
        'query_revision':1,'execution_id':uid(10),'budget_round_id':uid(12),'provider_run_id':'fixture-no-paid-run',
        'initiated_by':'schedule','observations':observations}
    request = {'request_id':uid(11),'investigation_id':case,'config_revision':1,
        'criterion_id':uid(4),'criterion_revision':1,'query_id':uid(100),'query_revision':1,
        'budget_round_id':uid(12),'purpose':'Find supporting evidence using an approved query.',
        'target_override':'https://example.com/evidence/additional','max_items':5,'max_cost_microusd':10000,
        'expires_at':'2026-01-01T01:00:00Z','evidence_ids':[evidence[0]['evidence_id']]}
    execution = {'execution_id':uid(13),'budget_round_id':uid(12),'request_id':uid(11),
        'config_revision':1,'query_id':uid(100),'query_revision':1,'status':'budget_exhausted',
        'observation_message_ids':[],'record_count':0,'spent_microusd':0,'coverage':'unknown',
        'explanation':'Synthetic refusal fixture; no Actor was started and no charge was made.'}
    report = {'report_id':sha256(encoded([case,'fixture-report.1'])),'investigation_id':case,'revision':1,
        'config_revision':1,'criterion_id':uid(4),'criterion_revision':1,'status':'completed_with_limits',
        'title':'Synthetic price announcement','summary':'One relevant document was observed. Additional evidence was not requested because the fixture budget was exhausted.',
        'indicators':[{'key':'relevant_documents','label':'Relevant documents','value':'1','unit':'documents',
            'period_start':at,'period_end':at,'evidence_ids':[evidence[0]['evidence_id']],
            'method':'count_distinct_documents','population_count':2,'missing_count':0}],
        'evidence':evidence,'evaluations':evaluations,'coverage':{'distinct_documents':2,'distinct_observations':2,
            'query_ids':[uid(100)],'pending_requests':0,'failed_requests':0,'exhausted_budget':True,
            'limitations':['Synthetic fixture, not a real model evaluation or external capture.']},
        'model_id':'fixture-model','model_revision':'fixture.1','prompt_version':'fixture.1','created_at':at}
    proposed = copy.deepcopy(criterion)
    proposed.update(revision=2,origin='agent',author_id='fixture-model',proposal_id=uid(14),status='draft',
        approval={'status':'pending','decided_by':None,'decided_at':None},threshold=0.85)
    proposal = {'proposal_id':uid(14),'investigation_id':case,'base_config_revision':1,'operation':'update',
        'change':{'target_kind':'criterion','record':proposed},'state':'pending',
        'rationale':'Synthetic suggestion; operator approval is required.','evidence_ids':[evidence[0]['evidence_id']]}
    rejected = copy.deepcopy(config)
    rejected.update(revision=2,previous_revision=1,proposal_decisions=[{'proposal_id':uid(14),
        'decision':'rejected','operator_id':'fixture-operator','decided_at':at,'config_revision':2,
        'reason':'Synthetic operator refusal; keep the existing threshold.'}])
    documents = {
        '01-configuration.json':message(20,'configuration',config),
        '09-budget-round-open.json':message(28,'budget_round',{'budget_round_id':uid(12),
            'revision':1,'config_revision':1,'opened_at':at,'expires_at':'2026-01-01T01:00:00Z',
            'status':'open','remaining_microusd':200000,'research_remaining_microusd':50000,'remaining_items':60}),
        '02-observations.json':message(21,'observations',batch),
        '03-investigation-request.json':message(22,'investigation_request',request,'aidp_to_huginn'),
        '04-execution-result.json':message(23,'execution_result',execution),
        '10-budget-round-closed.json':message(29,'budget_round',{'budget_round_id':uid(12),
            'revision':2,'config_revision':1,'opened_at':at,'expires_at':'2026-01-01T01:00:00Z',
            'status':'closed','remaining_microusd':0,'research_remaining_microusd':0,'remaining_items':0}),
        '05-report.json':message(24,'report',report,'aidp_to_huginn'),
        '06-proposal.json':message(25,'proposal',proposal,'aidp_to_huginn'),
        '07-processing-receipt.json':message(26,'processing_receipt',{'source_sequence':1,
            'source_message_id':uid(20),'status':'applied','code':'configuration_applied',
            'explanation':'Synthetic configuration stored durably.'},'aidp_to_huginn'),
        '08-rejected-configuration.json':message(27,'configuration',rejected)}
    for direction in ('huginn_to_aidp','aidp_to_huginn'):
        previous, sequence = None, 0
        for name, value in list(documents.items()):
            if value['direction'] != direction:
                continue
            sequence += 1
            raw = encoded(value)
            entry = {**common,'kind':'stream_entry','direction':direction,'sequence':sequence,
                'message_id':value['message_id'],'message_kind':value['kind'],'message_sha256':sha256(raw),'message_bytes':len(raw),
                'previous_entry_sha256':previous}
            documents['entry-'+direction+'-'+str(sequence)+'.json'] = entry
            previous = sha256(encoded(entry))
        documents['head-'+direction+'.json'] = {**common,'kind':'stream_head','direction':direction,
            'last_sequence':sequence,'last_entry_sha256':previous,'updated_at':at}
        documents['ack-'+direction+'.json'] = {**common,'kind':'consumer_ack','direction':direction,
            'last_processed_sequence':sequence,'last_entry_sha256':previous,'updated_at':at}
    return documents


def artifacts():
    schema_raw = encoded(schema())
    schema_hash = sha256(schema_raw)
    chat_raw = encoded(chat_schema())
    chat_hash = sha256(chat_raw)
    reference_raw = (ROOT/'apps/backend/app/sonda_contract.py').read_bytes().replace(b'\r\n',b'\n')
    reference_hash = sha256(reference_raw)
    contract_hash = sha256(encoded({'schema_sha256':schema_hash,'chat_schema_sha256':chat_hash,
                                    'reference_sha256':reference_hash}))
    files = {'schema.json':schema_raw,'chat.schema.json':chat_raw,'reference.py':reference_raw}
    examples = fixtures(contract_hash)
    for name, value in examples.items():
        raw = encoded(value)
        parse(raw,registry_id=UUID(int=1),generation_id=UUID(int=2),contract_sha256=contract_hash)
        files['fixtures/'+name] = raw
    chat = {'version':2,'question':'Compare the approved source evidence.', 'search_context':{
        'registry_id':str(UUID(int=1)),'generation_id':str(UUID(int=2)),'config_revision':1,
        'source_ids':[],'query_ids':[]}}
    chat_raw_fixture = encoded(chat)
    ChatInput.model_validate_json(chat_raw_fixture)
    files['fixtures/chat-v2.json'] = chat_raw_fixture
    baseline = examples['02-observations.json']['payload']['observations'][0]
    vectors = []
    for name, changes in [('human-nfc',{'text':'café'}),('human-nfd',{'text':'cafe\u0301'}),
        ('url-nfc',{'url':'https://example.com/café'}),('url-nfd',{'url':'https://example.com/cafe\u0301'}),
        ('provider-nfc',{'document_type':'post','platform':'instagram','provider_id':'café'}),
        ('provider-nfd',{'document_type':'post','platform':'instagram','provider_id':'cafe\u0301'})]:
        value = {**baseline,**changes}
        vectors.append({'name':name,'input':value,'expected':observation_fingerprints(value)})
    targets = [{'kind':value['kind'],'message_id':value['message_id'],'direction':value['direction'],
                'expected':message_target(value['kind'],value['message_id'],UUID(int=1),UUID(int=2),value['direction'],'aidp-social/')}
               for value in examples.values() if 'payload' in value]
    vector_raw = encoded({'observation_fingerprints':vectors,'message_targets':targets})
    files['conformance-vectors.json'] = vector_raw
    manifest = {'contract_version':VERSION,'status':'proposed','schema_file':'schema.json',
        'schema_sha256':schema_hash,'chat_schema_file':'chat.schema.json',
        'chat_schema_sha256':chat_hash,'reference_file':'reference.py','reference_sha256':reference_hash,
        'contract_sha256':contract_hash,'fixtures_are_synthetic':True,
        'conformance_vectors_file':'conformance-vectors.json','conformance_vectors_sha256':sha256(vector_raw),
        'fixtures':{name:sha256(raw) for name,raw in files.items() if name.startswith('fixtures/')}}
    files['manifest.json'] = encoded(manifest)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    args = parser.parse_args()
    destination = ROOT/'contracts/sonda/v1'
    files = artifacts()
    for name, raw in files.items():
        path = destination/name
        if args.check:
            if not path.exists() or path.read_bytes()!=raw:
                raise SystemExit('Contract artifact drift: '+name)
        else:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(raw)
    print('PASS Sonda contract '+VERSION+': '+str(len(files)-5)+' synthetic fixtures; manifest contracts/sonda/v1/manifest.json')


if __name__ == '__main__':
    main()
