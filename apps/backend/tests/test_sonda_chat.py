"""Pure chat-v2 scope, fixed-SQL and existing visualization regressions; no Spark/cloud."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import UUID

import pytest

from app.labs.ai_social_network.notebooks.agent import sonda_chat as chat
from app.labs.ai_social_network.notebooks import sonda_delta as gold

ROOT=Path(__file__).resolve().parents[3]


def fixture(name='01-configuration.json'):
    return json.loads((ROOT/'contracts/sonda/v1/fixtures'/name).read_text(encoding='utf-8'))


def binding():
    return {'catalog':'spark_catalog','gold_schema':'sonda_gold','control_schema':'sonda_control',
        'namespace':'synthetic','source_bucket':'fixture_source','gold_bucket':'fixture_gold',
        'artifacts_bucket':'fixture_artifacts','registry_id':str(UUID(int=1)),
        'generation_id':str(UUID(int=2)),'contract_sha256':gold.CONTRACT_SHA256}


def publication(**changes):
    bound=binding()
    versions={'ai_social_'+name:number+1 for number,name in enumerate(chat.TABLES)}
    row={key:bound[key] for key in ('registry_id','generation_id')}
    row.update(config_revision=1,versions_json=gold.canonical(versions),published_at='2026-10-29T10:00:00Z')
    row['publication_id']=gold.fingerprint([{key:bound[key] for key in ('registry_id','generation_id')},1,versions])
    return {**row,**changes}


def envelope(**context):
    return {'version':2,'question':'cuantos post hay en yotube','search_context':{
        'registry_id':str(UUID(int=1)),'generation_id':str(UUID(int=2)),
        'config_revision':1,'source_ids':[],'query_ids':[],**context}}


def setup(value=None,config=None):
    snapshot=chat.snapshot_from_row(binding(),publication())
    bound=chat.validate_input(value or envelope(),binding(),snapshot,config or fixture())
    return snapshot,bound


def sql(name,args=None,**context):
    snapshot,bound=setup(envelope(**context))
    return chat.tool_sql(binding(),snapshot,bound,name,args or {})


def test_single_network_count_retains_empty_population_without_coalescing_unknown_metrics():
    count=sql('summary',{'platform':'youtube','metric':'posts'})
    assert 'COUNT(*) AS value' in count and 'GROUP BY platform' not in count
    assert "AS platform" in count and 'COUNT(CASE WHEN published_date IS NULL' in count
    metric=sql('summary',{'platform':'youtube','metric':'comments'})
    assert 'SUM(comments) AS value' in metric and 'COALESCE(SUM(comments)' not in metric
    assert 'GROUP BY platform' in sql('summary',{'metric':'posts'})


@pytest.mark.parametrize('change',[
    {'generation_id':str(UUID(int=50))},{'registry_id':str(UUID(int=50))},{'config_revision':2},
    {'source_ids':[str(UUID(int=50))]},{'query_ids':[str(UUID(int=50))]}])
def test_chat_rejects_cross_generation_stale_configuration_and_unknown_selection(change):
    with pytest.raises(ValueError):setup(envelope(**change))


@pytest.mark.parametrize('value',[
    {'version':1,'question':'hello','search_context':{}},
    {'version':2.0,'question':'hello','search_context':{}},
    {**envelope(),'extra':True},{**envelope(),'question':' '},
    {**envelope(),'question':'x'*501}])
def test_input_is_exact_chat_v2_not_a_legacy_or_extended_envelope(value):
    with pytest.raises(ValueError):setup(value)


def test_ids_resolve_current_approval_and_selections_are_returned_unchanged():
    config=fixture()
    source=config['payload']['sources'][0]['source_id']
    query=config['payload']['queries'][0]['query_id']
    snapshot,bound=setup(envelope(source_ids=[source],query_ids=[query]))
    assert bound.search_context()==envelope(source_ids=[source],query_ids=[query])['search_context']
    statement=chat.tool_sql(binding(),snapshot,bound,'summary',{})
    assert source.encode().hex() in statement and query.encode().hex() in statement
    config['payload']['queries'][0]['status']='paused'
    _,paused=setup(envelope(query_ids=[query]),config)
    assert paused.query_ids==(query,)
    config['payload']['queries'][0]['approval']={'status':'rejected','decided_by':'operator','decided_at':'2026-01-01T00:00:00Z'}
    with pytest.raises(ValueError,match='unapproved'):
        setup(envelope(query_ids=[query]),config)


def test_duplicate_ids_and_query_from_another_selected_source_are_rejected():
    config=fixture()
    query=config['payload']['queries'][0]['query_id']
    with pytest.raises(ValueError):setup(envelope(query_ids=[query,query]))
    sources=config['payload']['sources']
    if len(sources)>1:
        unrelated=next(row['source_id'] for row in sources if row['source_id']!=config['payload']['queries'][0]['source_id'])
        with pytest.raises(ValueError):setup(envelope(source_ids=[unrelated],query_ids=[query]))


@pytest.mark.parametrize('field,value',[
    ('versions_json','{}'),('publication_id','a'*64),('config_revision',True),
    ('registry_id',str(UUID(int=88))),('published_at',None),('pending_files',False),('failed_files',-1)])
def test_publication_requires_scope_and_hash_bound_exact_versions(field,value):
    with pytest.raises(ValueError):chat.snapshot_from_row(binding(),publication(**{field:value}))


def test_snapshot_preserves_real_timestamp_counters_and_uses_control_namespace():
    row=publication(published_at=datetime(2026,10,29,10,tzinfo=timezone.utc),pending_files=4)
    snapshot=chat.snapshot_from_row(binding(),row)
    assert snapshot['pending_files']==4 and snapshot['failed_files'] is None
    assert snapshot['module_id']=='ai_social_network'
    assert snapshot['published_at']=='2026-10-29T10:00:00.000000Z'
    assert 'sonda_control.ai_social_publication' in chat.snapshot_sql(binding())
    assert 'VERSION AS OF 1' in chat.configuration_sql(binding(),snapshot)
    assert (snapshot['current_version'],snapshot['history_version'],snapshot['daily_version'])==(4,5,9)
    with pytest.raises(ValueError):
        chat.configuration_sql(binding(),{**snapshot,'config_revision':'1 OR TRUE'})


def test_uuid_session_is_stable_and_isolated_by_registry_generation_and_session():
    first=chat.session_key(binding(),'native-session-a')
    assert first==chat.session_key(binding(),'native-session-a')
    assert UUID(first).version==5
    assert first!=chat.session_key({**binding(),'generation_id':str(UUID(int=80))},'native-session-a')
    assert first!=chat.session_key({**binding(),'registry_id':str(UUID(int=80))},'native-session-a')
    assert first!=chat.session_key(binding(),'native-session-b')
    with pytest.raises(ValueError):chat.session_key(binding(),'')


def test_selected_discoveries_use_exists_not_a_join_that_inflates_document_counts():
    statement=sql('summary',{'metric':'posts'})
    assert 'COUNT(*) AS value' in statement
    assert 'EXISTS (SELECT 1 FROM' in statement
    assert 's.observation_key=d.observation_key' in statement
    assert ' JOIN ' not in statement
    assert 'c.approval_status=\'approved\'' in statement and "c.status IN ('active','paused')" in statement
    assert 'ai_social_sources VERSION AS OF 2' in statement and 'ai_social_queries VERSION AS OF 3' in statement
    assert statement.count('.registry_id=')>=4 and statement.count('.generation_id=')>=4


def test_count_youtube_typo_and_all_dates_use_current_post_documents_not_comment_rows():
    plan=chat.verified_plan('cuantos post hay en yotube')
    assert plan['tool']=='summary'
    statement=sql(plan['tool'],plan['args'])
    assert 'youtube'.encode().hex() in statement
    assert 'post'.encode().hex() in statement
    assert 'd.document_type=' in statement and 'd.published_date BETWEEN' not in statement
    assert 'COALESCE(SUM' not in statement


def test_global_most_commented_ranking_uses_reported_metrics_before_limit_not_recent_sample():
    plan=chat.verified_plan('realiza un ranking de los post mas comentados')
    statement=sql(plan['tool'],plan['args'])
    assert 'comments AS value' in statement and 'comments AS comment_count' in statement
    assert 'COUNT(comments) OVER() AS reported_count' in statement
    assert 'COUNT(*) OVER() AS scope_posts' in statement
    assert statement.index('WHERE value IS NOT NULL')<statement.index('ORDER BY value DESC NULLS LAST,post_key LIMIT 10')
    assert statement.count('LIMIT ')==1 and 'ORDER BY observed_at' not in statement


def test_oracle_comment_counts_and_comment_body_search_have_distinct_population_and_semantics():
    plan=chat.verified_plan('en que red social hay mas comentarios de oracle')
    totals=sql(plan['tool'],plan['args'])
    assert 'SUM(comments) AS value' in totals and 'COUNT(comments) AS reported_count' in totals
    assert "concat_ws(' ',d.author,d.text,d.url)" in totals
    captured=sql('comments',{'entity':'Oracle'})
    assert 'comment'.encode().hex() in captured and 'd.document_type=' in captured
    assert "concat_ws(' ',d.author,d.text)" in captured
    assert "concat_ws(' ',d.author,d.text,d.url)" not in captured
    assert 'observation_key AS comment_key' in captured and 'observation_key AS post_key' in captured
    assert 'SUM(comments)' not in captured


def test_dates_partition_history_and_filter_comment_publication_time_not_parent_post():
    args={'post_key':'a'*64,'date_from':'2026-09-30','date_to':'2026-10-29'}
    history=sql('history',args)
    assert 'ai_social_observations VERSION AS OF 5' in history
    assert "observation_month BETWEEN '2026-09' AND '2026-10'" in history
    assert "d.observed_at < TIMESTAMP '2026-10-30 00:00:00'" in history
    comments=sql('comments',args)
    assert 'd.parent_document_key IN (' in comments
    assert "d.published_date BETWEEN DATE '2026-09-30' AND DATE '2026-10-29'" in comments
    assert 'published_at AS commented_at' in comments


@pytest.mark.parametrize('name,args',[
    ('summary',{'sql':'DROP TABLE x'}),('ranking',{'metric':'posts'}),('ranking',{'limit':51}),
    ('details',{}),('comments',{'metric':'comments'}),('comments',{'document_type':'post'}),
    ('trend',{'document_type':'page'}),
    ('trend',{'date_from':'2026-01-01','date_to':'2026-02-20'}),('search',{'document_type':'unknown'})])
def test_fixed_tools_reject_unsafe_or_semantically_invalid_arguments(name,args):
    with pytest.raises(ValueError):sql(name,args)


def test_free_text_is_hex_encoded_and_cannot_change_the_fixed_select():
    hostile="Oracle'); DROP TABLE x; --"
    statement=sql('search',{'text':hostile})
    assert statement.startswith('SELECT ') and hostile not in statement and ';' not in statement
    assert hostile.encode().hex() in statement
    snapshot,bound=setup()
    with pytest.raises(ValueError):chat.relation(binding(),snapshot,'outbox')
    with pytest.raises(ValueError):chat.relation({**binding(),'gold_schema':'x`;DROP'},snapshot,'document_current')
    with pytest.raises(ValueError):chat.tool_sql(binding(),{**snapshot,'generation_id':str(UUID(int=9))},bound,'summary',{})


def evidence(name,args,rows):
    snapshot,bound=setup()
    query=chat.query_evidence(snapshot,bound,name,args,rows)
    return snapshot,bound,query


@pytest.mark.parametrize('kind',['bar','pie','table'])
def test_summary_reuses_existing_visualization_for_real_returned_rows(kind):
    rows=[{'platform':'instagram','value':12,'post_count':3,'reported_count':2},
          {'platform':'facebook','value':None,'post_count':2,'reported_count':0}]
    snapshot,bound,query=evidence('summary',{'metric':'comments'},rows)
    result=chat.public_response(snapshot,bound,'Instagram presenta la cifra informada; Facebook no tiene una métrica conocida.',[query],kind)
    assert result['visualization']['type']==kind
    assert result['visualization']['rows']==[['instagram',12],['facebook',None]]
    assert result['publication']['failed_files'] is None and result['publication']['pending_files'] is None
    assert result['search_context']==envelope()['search_context'] and 'versions' not in result['publication']


def test_multiseries_line_uses_complete_calendar_and_preserves_unknown_values():
    args={'metric':'posts','platforms':['instagram','facebook'],'date_from':'2026-10-28','date_to':'2026-10-29'}
    rows=[{'period':'2026-10-28','instagram':3,'facebook':0}, {'period':'2026-10-29','instagram':0,'facebook':4}]
    snapshot,bound,query=evidence('trend',args,rows)
    result=chat.public_response(snapshot,bound,'La comparación cubre dos fechas UTC.',[query],'line')
    assert result['visualization']['columns']==['Fecha','Instagram','Facebook']
    assert result['visualization']['rows']==[['2026-10-28',3,0],['2026-10-29',0,4]]
    statement=chat.tool_sql(binding(),snapshot,bound,'trend',args)
    assert 'sequence(DATE' in statement and 'OR d.published_date IS NULL' in statement
    assert 'COALESCE(MAX(CASE WHEN daily.' in statement
    metric_statement=sql('trend',{**args,'metric':'comments'})
    assert 'SUM(comments)' in metric_statement and 'COUNT(comments)' in metric_statement
    assert 'THEN daily.value END),0)' not in metric_statement


def test_comment_sources_are_queried_own_urls_and_partial_capture_metadata_is_retained():
    row={'post_key':'b'*64,'comment_key':'b'*64,'document_key':'c'*64,'parent_document_key':'a'*64,'platform':'facebook','text':'Oracle is useful',
        'url':'https://example.com/comment','like_count':None,'capture_scope':'top_comments','document_type':'comment'}
    snapshot,bound,query=evidence('comments',{'entity':'Oracle'},[row])
    result=chat.public_response(snapshot,bound,'El comentario capturado menciona Oracle.',[query],'table',[row['comment_key']])
    assert result['sources'][0]['comment_key']==row['comment_key']
    assert result['sources'][0]['post_key']==row['comment_key'] and result['sources'][0]['parent_document_key']=='a'*64
    assert result['sources'][0]['url']==row['url'] and result['sources'][0]['capture_scope']=='top_comments'
    assert result['visualization']['rows'][0][3] is None
    with pytest.raises(ValueError):chat.public_response(snapshot,bound,'Respuesta.',[query],'none',['c'*64])


def test_public_response_rejects_cross_publication_evidence_and_preserves_text_on_chart_failure():
    snapshot,bound,query=evidence('summary',{},[{'platform':'facebook','value':9007199254740992}])
    result=chat.public_response(snapshot,bound,'La cifra excede la representación gráfica segura.',[query],'bar')
    assert result['answer'] and result['visualization'] is None and result['visualization_error']
    wrong=copy.deepcopy(query)
    wrong['publication_id']='e'*64
    with pytest.raises(ValueError):chat.public_response(snapshot,bound,'Respuesta.',[wrong])


def test_query_guard_preserves_document_type_and_bounds_dependent_queries():
    snapshot,_=setup()
    first=chat.checked_plan_v2({'tool':'summary','args':{}},[],snapshot)
    previous={'tool':'summary','args':first['args'],'rows':[]}
    assert chat.checked_plan_v2(first,[previous],snapshot)['tool']=='finish'
    assert chat.checked_plan_v2({'tool':'summary','args':{'document_type':'comment'}},[previous],snapshot)['tool']=='summary'
    assert chat.checked_plan_v2({'tool':'search','args':{}},[previous]*4,snapshot)['tool']=='finish'


def test_versioned_public_reference_resolves_details_exactly_and_history_by_document():
    reference='b'*64
    details=sql('details',{'post_key':reference})
    assert 'ai_social_observations VERSION AS OF 5' in details
    assert 'd.observation_key=' in details and reference.encode().hex() in details
    assert 'observation_key AS post_key,document_key,parent_document_key' in details
    history=sql('history',{'post_key':reference})
    assert 'd.document_key IN (SELECT p.document_key' in history
    assert 'p.observation_key=' in history


def test_web_documents_preserve_absent_platform_and_are_not_labelled_social_posts():
    row={'post_key':'d'*64,'document_key':'e'*64,'platform':None,'value':3,'post_count':3,'reported_count':3}
    snapshot,bound,query=evidence('summary',{'document_type':'page'},[row])
    result=chat.public_response(snapshot,bound,'Hay tres páginas en el alcance aprobado.',[query],'table')
    assert query['rows'][0]['platform'] is None
    assert result['visualization']['title']=='Páginas web'
    assert result['visualization']['columns']==['Red','Páginas web']
    assert 'Tipo de documento: Páginas web' in result['visualization']['caption']
