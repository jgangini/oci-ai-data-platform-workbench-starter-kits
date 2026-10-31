"""Real Spark/Delta migration and observed-comment regression; local fixtures only."""
import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile

from pyspark.sql import SparkSession

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('social_comments_ingest', root/'apps/backend/app/legacy_labs/ai_social_network/notebooks/social_ingest.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
agent_spec = importlib.util.spec_from_file_location('social_comments_agent', root/'apps/backend/app/legacy_labs/ai_social_network/notebooks/agent/social_agent.py')
agent = importlib.util.module_from_spec(agent_spec)
agent_spec.loader.exec_module(agent)


def main():
    scratch = Path(tempfile.mkdtemp(prefix='social-comments-'))
    spark = (SparkSession.builder.master('local[2]').appName('social-comment-migration')
        .config('spark.ui.enabled', 'false').config('spark.sql.shuffle.partitions', '2')
        .config('spark.databricks.delta.snapshotPartitions', '2')
        .config('spark.sql.extensions', 'io.delta.sql.DeltaSparkSessionExtension')
        .config('spark.sql.catalog.spark_catalog', 'org.apache.spark.sql.delta.catalog.DeltaCatalog')
        .config('spark.sql.warehouse.dir', str(scratch/'warehouse')).getOrCreate())
    spark.sparkContext.setLogLevel('ERROR')
    spark.conf.set('spark.sql.session.timeZone', 'UTC')
    config = {'catalog':'spark_catalog','gold_schema':'comment_gold','control_schema':'comment_control'}
    m.location = lambda _config, name: 'file:' + str(scratch/name)
    for schema in (config['gold_schema'],config['control_schema']):
        spark.sql('CREATE SCHEMA IF NOT EXISTS spark_catalog.' + schema)
    # Reproduce the six-table v2.7 deployment, including NULL derived Facebook metric.
    for name, sql in m.schema_statements(config):
        if name.startswith('ai_social_comment_'):
            continue
        sql = sql.replace(', comment_current_version BIGINT, comment_history_version BIGINT','').replace(', model_version INT','')
        spark.sql(sql)
    source = {'source_object':'events/network=facebook/date=2026-10-29/a.json','etag':'v1'}
    event = {'event_id':1,'created_at':'2026-10-29T10:00:00Z','payload':{'platform':'facebook','id':'post',
        'text':'Observed post','url':'https://www.facebook.com/example/posts/post',
        'published_at':'2026-10-28T12:00:00Z','raw':{'comments':6,'topComments':[
            {'commentId':'comment-1','id':'different-opaque-id','text':'Oracle is useful','date':'2026-10-28T13:00:00Z',
             'likesCount':'2','commentUrl':'https://www.facebook.com/example/posts/post?comment_id=comment-1','author':{'name':'Fixture author'}},
            {'commentId':'comment-2','text':'A second observed comment','date':None,'likesCount':None}]}}}
    row = m.normalize_event(event,source)
    row['comment_count'] = None
    def append(name, value):
        target = m.table(config,name)
        spark.createDataFrame([value],schema=spark.table(target).schema).write.format('delta').mode('append').saveAsTable(target)
    append('ai_social_current',row)
    append('ai_social_history',{**row,'observation_id':'legacy-observation','observation_month':'2026-10'})
    m.refresh_daily(spark,config,m.date_frame(spark,[{'platform':'facebook','published_date':'2026-10-28'}]),['facebook'])
    legacy_ids = spark.table(m.table(config,'ai_social_history')).select('observation_id','version_hash').collect()
    m.ensure_tables(spark,config)
    assert {'comment_current_version','comment_history_version'} <= set(spark.table(m.table(config,'ai_social_publication')).columns)
    assert 'model_version' in spark.table(m.table(config,'ai_social_ingest_state')).columns
    def count(name):
        return spark.table(m.table(config,'ai_social_'+name)).count()
    # Fail after derived post repairs but before comments publish: retry repairs daily again.
    original = m.write_comments
    def interrupted(*args, **kwargs):
        raise RuntimeError('Injected comment backfill interruption')
    m.write_comments = interrupted
    try:
        m.backfill_model(spark,config)
        raise AssertionError('Backfill interruption was not exercised')
    except RuntimeError as error:
        assert 'Injected' in str(error)
    finally:
        m.write_comments = original
    assert count('publication') == 0
    original_merge = m.merge_frame
    def interrupted_current(spark, target, *args, **kwargs):
        if target == m.table(config,'ai_social_comment_current'):
            raise RuntimeError('Injected comment current interruption after history commit')
        return original_merge(spark,target,*args,**kwargs)
    m.merge_frame = interrupted_current
    try:
        m.backfill_model(spark,config)
        raise AssertionError('The child-table interruption was not exercised')
    except RuntimeError as error:
        assert 'Injected' in str(error)
    finally:
        m.merge_frame = original_merge
    assert count('publication') == 0 and count('comment_current') == 0 and count('comment_history') == 2
    m.backfill_model(spark,config)
    assert spark.table(m.table(config,'ai_social_current')).first()['comment_count'] == 6
    assert spark.table(m.table(config,'ai_social_history')).first()['comment_count'] == 6
    assert spark.table(m.table(config,'ai_social_daily')).first()['comment_count'] == 6
    assert spark.table(m.table(config,'ai_social_history')).select('observation_id','version_hash').collect() == legacy_ids
    assert count('comment_current') == count('comment_history') == 2
    comments = {row['comment_id']:row.asDict() for row in spark.table(m.table(config,'ai_social_comment_current')).collect()}
    assert comments['comment-1']['like_count'] == 2 and comments['comment-2']['like_count'] is None
    assert comments['comment-2']['commented_at'] is None and comments['comment-2']['capture_scope'] == 'top_comments'
    m.backfill_model(spark,config)
    assert count('comment_current') == count('comment_history') == 2, 'Backfill retry must be idempotent'
    state = {'last_scan_completed_at':datetime.now(timezone.utc),'pending_files':0,'dirty_dates_json':None}
    m.publish_gold(spark,config,state,0)
    receipt = spark.table(m.table(config,'ai_social_publication')).first().asDict()
    assert all(type(receipt[key]) is int and receipt[key] >= 0 for key in ('comment_current_version','comment_history_version'))
    snapshot = agent.checked_snapshot([json.loads(spark.sql(agent.snapshot_sql(config)).first()['payload'])])
    def serving(args):
        return [json.loads(item['payload']) for item in spark.sql(agent.tool_sql(config,snapshot,'comments',args)).collect()]
    matched = serving({'entity':'Oracle','entity_scope':'mentions'})
    assert len(matched) == 1 and matched[0]['text'] == 'Oracle is useful'
    assert matched[0]['post_key'] == row['post_key'] and matched[0]['capture_scope'] == 'top_comments'
    available = serving({})
    assert len(available) == 2 and sum(item.get('commented_at') is None for item in available) == 1
    assert next(item for item in available if item['comment_id'] == 'comment-2').get('like_count') is None
    assert len(serving({'date_from':'2026-10-28','date_to':'2026-10-28'})) == 1
    assert agent.query_evidence(snapshot,'comments',{},available)['period_basis'] == 'comment'
    newer = copy.deepcopy(event)
    newer['created_at'] = '2026-10-29T10:05:00Z'
    newer['payload']['raw']['topComments'][0]['text'] = 'Changed observed comment'
    # The second comment is no longer in the top list; this is not a deletion event.
    newer['payload']['raw']['topComments'] = newer['payload']['raw']['topComments'][:1]
    assert m.write_gold(spark,config,[m.normalize_event(newer,source)],state,lambda:None) == 1
    assert count('comment_current') == 2 and count('comment_history') == 3
    assert spark.sql('SELECT text FROM ' + m.table(config,'ai_social_comment_current') + ' VERSION AS OF ' + str(receipt['comment_current_version']) + " WHERE comment_id='comment-1'").first()['text'] == 'Oracle is useful'
    assert m.write_gold(spark,config,[m.normalize_event(newer,source)],state,lambda:None) == 0
    assert count('comment_history') == 3
    # Empty fresh deployments create no synthetic child rows and have valid version receipts.
    empty_config = {**config,'gold_schema':'empty_gold','control_schema':'empty_control'}
    old_location = m.location
    m.location = lambda _config,name: 'file:' + str(scratch/'empty'/name)
    m.ensure_tables(spark,empty_config)
    m.backfill_model(spark,empty_config)
    m.publish_gold(spark,empty_config,state,0)
    assert spark.table(m.table(empty_config,'ai_social_comment_current')).count() == 0
    m.location = old_location
    print('PASS observed comments: legacy additive migration, metric repair without false observations, partial retry, child history, NULLs, no deletion, exact snapshot versions and empty deployment')
    spark.stop()


if __name__ == '__main__':
    main()
