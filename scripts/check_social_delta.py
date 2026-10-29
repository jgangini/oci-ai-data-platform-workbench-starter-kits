"""Executable Delta regression, run in Spark 3.5 + Delta 3.2 (no OCI credentials)."""
import copy
import importlib.util
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from pyspark.sql import SparkSession

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('social_ingest', root / 'apps/backend/app/labs/ai_social_network/notebooks/social_ingest.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def main():
    scratch = Path(tempfile.mkdtemp(prefix='social-delta-'))
    spark = (SparkSession.builder.master('local[2]').appName('social-delta-regression')
        .config('spark.ui.enabled', 'false').config('spark.sql.shuffle.partitions', '2')
        .config('spark.databricks.delta.snapshotPartitions', '2')
        .config('spark.sql.extensions', 'io.delta.sql.DeltaSparkSessionExtension')
        .config('spark.sql.catalog.spark_catalog', 'org.apache.spark.sql.delta.catalog.DeltaCatalog')
        .config('spark.sql.warehouse.dir', str(scratch / 'warehouse')).getOrCreate())
    spark.sparkContext.setLogLevel('ERROR')
    spark.conf.set('spark.sql.session.timeZone', 'UTC')
    config = {'catalog': 'spark_catalog', 'gold_schema': 'social_gold', 'control_schema': 'social_control'}
    m.location = lambda _config, name: 'file:' + str(scratch / name)
    m.ensure_tables(spark, config)
    for name in m.GOLD_TABLES + m.CONTROL_TABLES:
        assert spark.sql(f'DESCRIBE DETAIL {m.table(config, name)}').first()['description'] == m.TABLE_DESCRIPTIONS[name]
    current_table = m.table(config, 'ai_social_current')
    spark.sql(f"ALTER TABLE {current_table} SET TBLPROPERTIES ('comment'='')")
    m.ensure_tables(spark, config)
    assert spark.sql(f'DESCRIBE DETAIL {current_table}').first()['description'] == m.TABLE_DESCRIPTIONS['ai_social_current'], 'Redeployment must restore missing descriptions'
    spark.sql(f"ALTER TABLE {current_table} SET TBLPROPERTIES ('comment'='Operator description')")
    m.ensure_tables(spark, config)
    assert spark.sql(f'DESCRIBE DETAIL {current_table}').first()['description'] == 'Operator description', 'User descriptions must be preserved'
    source = {'source_object': 'events/network=instagram/date=2026-10-28/a.json', 'etag': 'one'}
    event = {'event_id': 1, 'created_at': '2026-10-29T10:00:00Z', 'payload': {
        'platform': 'instagram', 'id': 'one', 'text': 'Observed publication', 'url': 'https://example.com/one',
        'published_at': '2026-10-28T09:00:00Z', 'raw': {'likesCount': 2}}}
    state = {'dirty_dates_json': None}
    checkpoints = []
    def checkpoint():
        checkpoints.append(copy.deepcopy(state))
    def write(events):
        return m.write_gold(spark, config, [m.normalize_event(item, source) for item in events], state, checkpoint)
    def count(name):
        return spark.table(m.table(config, name)).count()
    assert write([event]) == 1
    assert count('ai_social_current') == count('ai_social_history') == 1
    later = copy.deepcopy(event)
    later['created_at'] = '2026-10-29T10:05:00Z'
    later['payload']['run_id'] = 'different-capture'
    assert write([later]) == 0
    assert count('ai_social_history') == 1
    assert spark.table(m.table(config, 'ai_social_current')).first()['observed_at'] == datetime(2026, 10, 29, 10, 5)
    late = copy.deepcopy(event)
    late['created_at'] = '2026-10-29T09:55:00Z'
    assert write([late]) == 0
    assert count('ai_social_history') == 1, 'A late unchanged capture must not become a historical change'
    assert spark.table(m.table(config, 'ai_social_current')).first()['observed_at'] == datetime(2026, 10, 29, 10, 5)
    state.update(last_scan_completed_at=datetime.now(timezone.utc), pending_files=0)
    m.publish_gold(spark, config, state, 0)
    receipt = spark.table(m.table(config, 'ai_social_publication')).first().asDict()
    changed = copy.deepcopy(later)
    changed['created_at'] = '2026-10-29T10:10:00Z'
    changed['payload'].update(text='Changed publication', published_at='2026-09-15T09:00:00Z')
    changed['payload']['raw']['likesCount'] = 4
    original_merge = m.merge_frame
    def fail_before_daily(spark, target, *args, **kwargs):
        if target == m.table(config, 'ai_social_daily'):
            raise RuntimeError('Injected interruption after current commit')
        return original_merge(spark, target, *args, **kwargs)
    m.merge_frame = fail_before_daily
    try:
        write([changed])
        raise AssertionError('The interruption did not execute')
    except RuntimeError as exc:
        assert 'Injected interruption' in str(exc)
    finally:
        m.merge_frame = original_merge
    assert count('ai_social_history') == 2
    assert any(item['published_date'] == '2026-10-28' for item in json.loads(checkpoints[-1]['dirty_dates_json']))
    assert spark.table(m.table(config, 'ai_social_publication')).first()['publication_id'] == receipt['publication_id']
    # The published snapshot continues serving the previous state during an interrupted batch.
    previous = spark.sql(f"SELECT text FROM {m.table(config, 'ai_social_current')} VERSION AS OF {receipt['current_version']}").first()
    assert previous['text'] == 'Observed publication'
    assert write([changed]) == 0
    assert count('ai_social_history') == 2 and count('ai_social_current') == 1
    daily = {str(row['published_date']): row['post_count'] for row in spark.table(m.table(config, 'ai_social_daily')).collect()}
    assert daily == {'2026-10-28': 0, '2026-09-15': 1}
    unknown = copy.deepcopy(event)
    unknown['payload'].update(id='unknown-date', published_at=None)
    assert write([unknown]) == 1
    missing = spark.table(m.table(config, 'ai_social_daily')).where('published_date IS NULL').first()
    assert missing['post_count'] == 1 and missing['published_month'] == 'unknown'
    assert write([]) == 0
    assert count('ai_social_current') == 2 and count('ai_social_history') == 3
    print('PASS Delta: retries, repeated captures, changed records, date corrections, NULL dates, coherent publication snapshots')
    spark.stop()


if __name__ == '__main__':
    main()
