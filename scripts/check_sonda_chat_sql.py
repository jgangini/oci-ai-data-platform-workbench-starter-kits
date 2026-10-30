"""Prepared SQL acceptance for the existing disposable Sonda Delta runner.

prepare_chat_sql_checks runs on host Python with Pydantic. run_chat_sql_checks
uses only stdlib and the caller's existing Spark session: no new session, cluster,
source or credential. Synthetic social rows live in temporary views, never Gold.
"""
import copy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sys
from uuid import UUID


def prepare_chat_sql_checks(messages, binding=None):
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/'apps/backend'))
    from app.labs.ai_social_network.notebooks.agent import sonda_chat as chat
    from app.labs.ai_social_network.notebooks import sonda_delta as delta
    from app import sonda_contract as contract
    binding=binding or {'catalog':'spark_catalog','gold_schema':'sonda_gold','control_schema':'sonda_control',
        'namespace':'synthetic','source_bucket':'fixture_source','gold_bucket':'fixture_gold','artifacts_bucket':'fixture_artifacts',
        'registry_id':str(UUID(int=1)),'generation_id':str(UUID(int=2)),'contract_sha256':delta.CONTRACT_SHA256}
    config=messages['approved']
    revision=config['payload']['revision']
    versions={'ai_social_'+name:700001+index for index,name in enumerate(chat.TABLES)}
    scope={key:binding[key] for key in ('registry_id','generation_id')}
    row={**scope,'config_revision':revision,'versions_json':delta.canonical(versions),'published_at':'2026-01-02T12:00:00Z',
        'publication_id':delta.fingerprint([scope,revision,versions])}
    snapshot=chat.snapshot_from_row(binding,row)
    def bound(query_ids=()):
        return chat.validate_input({'version':2,'question':'Synthetic SQL acceptance','search_context':{
            **scope,'config_revision':revision,'source_ids':[],'query_ids':list(query_ids)}},binding,snapshot,config)
    checks=[]
    def check(name,tool,args,expected,fields,*,synthetic=False,presentation=None,query_ids=()):
        selected=bound(query_ids)
        item={'name':name,'sql':chat.tool_sql(binding,snapshot,selected,tool,args),'synthetic':synthetic,
            'fields':fields,'expected':[{key:value.get(key) for key in fields} for value in expected]}
        if presentation:
            evidence=chat.query_evidence(snapshot,selected,tool,args,expected)
            visual=chat.public_response(snapshot,selected,'Synthetic SQL visualization acceptance.',[evidence],presentation)['visualization']
            assert visual is not None
            item['visualization']={'type':visual['type'],'rows':visual['rows'],'columns':visual['columns']}
        checks.append(item)
    changed=messages['changed']['payload']['observations'][0]
    original=messages['observations']['payload']['observations'][0]
    check('existing-distinct-pages-multiquery','summary',{'document_type':'page'},[{'platform':None,'value':2,'post_count':2}],['platform','value','post_count'])
    check('existing-reported-metric-not-unknown-zero','summary',{'document_type':'page','metric':'comments'},
        [{'platform':None,'value':5,'post_count':2,'reported_count':1}],['platform','value','post_count','reported_count'])
    check('existing-global-ranking','ranking',{'document_type':'page','metric':'comments'},
        [{'post_key':changed['observation_key'],'value':5,'scope_posts':2,'reported_count':1}],['post_key','value','scope_posts','reported_count'])
    check('existing-empty-network-count','summary',{'platform':'youtube','metric':'posts'},
        [{'platform':'youtube','value':0,'post_count':0,'reported_count':0,'undated_count':0}],
        ['platform','value','post_count','reported_count','undated_count'])
    check('existing-empty-network-metric-unknown','summary',{'platform':'youtube','metric':'comments'},
        [{'platform':'youtube','value':None,'post_count':0,'reported_count':0}],
        ['platform','value','post_count','reported_count'])
    check('existing-text-search','search',{'document_type':'page','text':'price'},
        [{'post_key':changed['observation_key'],'comment_count':5}],['post_key','comment_count'])
    check('existing-version-details','details',{'document_type':'page','post_key':original['observation_key']},
        [{'post_key':original['observation_key'],'comment_count':None}],['post_key','comment_count'])
    check('existing-observation-history','history',{'document_type':'page','post_key':changed['observation_key'],
        'date_from':'2026-01-01','date_to':'2026-01-02'},
        [{'post_key':changed['observation_key'],'comment_count':5},{'post_key':original['observation_key'],'comment_count':None}],['post_key','comment_count'])
    source=config['payload']['queries'][0]['source_id']
    query1,query2=[value['query_id'] for value in config['payload']['queries'][:2]]
    documents=[]
    def observation_identity(value):
        observation={key:value.get(key) for key in ('document_type','platform','provider_id','parent_document_key','url','title','text','author','published_at','observed_at','capture_scope')}
        observation.update(metrics={key:value.get(key) for key in ('likes','comments','shares','views')},indicators=[])
        observation.update(contract.observation_fingerprints(observation))
        checked=contract.Observation.model_validate(observation).model_dump(mode='json')
        return {key:checked[key] for key in ('document_key','observation_key','content_sha256')}
    def document(tag,platform,text,comments,published,*,kind='post',parent=None,author='Oracle',observed='2026-01-02T01:00:00Z'):
        value={**scope,'document_type':kind,'platform':platform,'parent_document_key':parent,'provider_id':tag,
            'url':'https://example.com/oracle/'+tag,'title':None,'text':text,'author':author,'published_at':published,
            'published_date':published[:10] if published else None,'observed_at':observed,'likes':None,
            'comments':comments,'shares':None,'views':None,'capture_scope':'top_comments' if kind=='comment' else 'requested_results',
            'indicators_json':'[]','last_seen_at':observed}
        value.update(observation_identity(value))
        documents.append(value)
        return value
    instagram=document('instagram-post','instagram','Oracle launches a platform.',7,'2026-01-01T00:00:00Z')
    facebook=document('facebook-post','facebook','Oracle customer update.',12,'2026-01-02T00:00:00Z')
    youtube=document('youtube-post','youtube','Other tutorial.',None,None,author='Other')
    youtube['url']='https://example.com/other/youtube-post'
    youtube.update(observation_identity(youtube))
    comment=document('facebook-comment','facebook','Oracle is useful.',None,'2026-01-02T00:00:00Z',kind='comment',parent=facebook['document_key'],author='Reader')
    document('facebook-comment-no-mention','facebook','Thanks for sharing.',None,'2026-01-02T00:00:00Z',kind='comment',parent=facebook['document_key'],author='Reader')
    forbidden=document('unapproved-query','facebook','Oracle invisible query.',999,'2026-01-02T00:00:00Z')
    old=copy.deepcopy(instagram)
    old.update(comments=3,observed_at='2026-01-01T00:00:00Z')
    old.update(observation_identity(old))
    discoveries=[]
    def discovery(value,query):
        query_source=next((row['source_id'] for row in config['payload']['queries'] if row['query_id']==query),source)
        discoveries.append({**scope,'discovery_key':contract.sha256(contract.encoded([value['observation_key'],query])),
            'observation_key':value['observation_key'],'document_key':value['document_key'],'config_revision':1,
            'source_id':query_source,'source_revision':1,'query_id':query,'query_revision':1,'execution_id':str(UUID(int=8800)),
            'budget_round_id':messages['opened']['payload']['budget_round_id'],'provider_run_id':'synthetic-sql-only',
            'message_id':str(UUID(int=8801)),'observed_at':value['observed_at'],'observation_month':value['observed_at'][:7]})
    for value in documents:
        discovery(value,str(UUID(int=8899)) if value is forbidden else query2 if value is youtube else query1)
    discovery(instagram,query2)  # Same version in two queries must remain one post.
    discovery(old,query1)
    other=copy.deepcopy(instagram)
    other.update(generation_id=str(UUID(int=8898)))
    observations=[{**value,'observation_month':value['observed_at'][:7]} for value in documents+[old,other]]
    check('youtube-common-typo','summary',{'platform':'yotube'},[{'platform':'youtube','value':1,'post_count':1,'undated_count':1}],
        ['platform','value','post_count','undated_count'],synthetic=True)
    compare=[{'platform':'facebook','value':12,'post_count':1,'reported_count':1},
             {'platform':'instagram','value':7,'post_count':1,'reported_count':1}]
    for kind in ('bar','pie','table'):
        check('oracle-comments-'+kind,'summary',{'metric':'comments','entity':'Oracle'},compare,
            ['platform','value','post_count','reported_count'],synthetic=True,presentation=kind)
    check('global-post-ranking','ranking',{'metric':'comments'},[
        {'post_key':facebook['observation_key'],'text':facebook['text'],'platform':'facebook','value':12,'scope_posts':3,'reported_count':2},
        {'post_key':instagram['observation_key'],'text':instagram['text'],'platform':'instagram','value':7,'scope_posts':3,'reported_count':2}],
        ['post_key','value','scope_posts','reported_count'],synthetic=True,presentation='table')
    check('selected-query-not-multiplied','summary',{},[{'platform':'instagram','value':1},{'platform':'youtube','value':1}],
        ['platform','value'],synthetic=True,query_ids=[query2])
    check('real-comment-body-not-parent-or-url','comments',{'entity':'Oracle'},[
        {'post_key':comment['observation_key'],'comment_key':comment['observation_key'],'text':comment['text'],'platform':'facebook','like_count':None}],
        ['post_key','comment_key','like_count'],synthetic=True,presentation='table')
    check('publication-date-filter','summary',{'date_from':'2026-01-01','date_to':'2026-01-01'},
        [{'platform':'instagram','value':1}],['platform','value'],synthetic=True)
    check('daily-complete-line','trend',{'platforms':['instagram','facebook'],'date_from':'2026-01-01','date_to':'2026-01-02'},
        [{'period':'2026-01-01','instagram':1,'facebook':0},{'period':'2026-01-02','instagram':0,'facebook':1}],
        ['period','instagram','facebook'],synthetic=True,presentation='line')
    check('daily-unknown-metric-line','trend',{'metric':'likes','platforms':['instagram','facebook'],'date_from':'2026-01-01','date_to':'2026-01-02'},
        [{'period':'2026-01-01','instagram':None,'facebook':None},{'period':'2026-01-02','instagram':None,'facebook':None}],
        ['period','instagram','facebook'],synthetic=True,presentation='line')
    return {'binding':binding,'versions':versions,'checks':checks,
        'synthetic':{'document_current':documents+[other],'observations':observations,'discoveries':discoveries}}


def run_chat_sql_checks(spark,delta,binding,prepared):
    """Called by the existing disposable Delta runner; no Pydantic imported here."""
    assert prepared['binding']==binding,'Prepare SQL with the same disposable binding'
    publication=delta._find(spark,binding,'publication')
    assert len(publication)==1
    versions=json.loads(publication[0]['versions_json'])
    token_to_table={str(value):name for name,value in prepared['versions'].items()}
    views={}
    def decoded(rows,schema):
        result=[]
        for value in rows:
            row={field.name:value.get(field.name) for field in schema.fields}
            for field in schema.fields:
                value=row[field.name]
                if value is not None and field.dataType.typeName()=='timestamp':
                    row[field.name]=datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc).replace(tzinfo=None)
                elif value is not None and field.dataType.typeName()=='date':
                    row[field.name]=date.fromisoformat(value)
            result.append(row)
        return result
    for name,rows in prepared['synthetic'].items():
        schema=spark.table(delta._table(binding,name)).schema
        view='sonda_chat_fixture_'+name
        spark.createDataFrame(decoded(rows,schema),schema).createOrReplaceTempView(view)
        views['ai_social_'+name]=view
    try:
        for check in prepared['checks']:
            def replace(match):
                table,token=match.group(1),match.group(2)
                name=token_to_table[token]
                if check['synthetic'] and name in views:
                    return views[name]
                return table+' VERSION AS OF '+str(versions[name])
            statement=re.sub(r'([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*) VERSION AS OF (700\d+)',replace,check['sql'])
            assert not re.search(r'VERSION AS OF 700\d+',statement)
            rows=[json.loads(row['payload']) for row in spark.sql(statement).collect()]
            actual=[{key:row.get(key) for key in check['fields']} for row in rows]
            assert actual==check['expected'],(check['name'],actual,check['expected'])
            if check.get('visualization'):
                assert check['visualization']['type'] in ('bar','pie','table','line')
                assert len(check['visualization']['rows'])==len(rows)
        print('PASS Sonda chat SQL: '+str(len(prepared['checks']))+' real SELECT checks; fixed snapshots, scopes, query dedup, NULL metrics, versioned refs and graph-ready rows (synthetic views)')
    finally:
        for view in views.values():
            spark.catalog.dropTempView(view)
