"""Five-minute incremental social ingestion. Gold is the only agent serving layer.

CONFIG is injected by the module installer; it contains identifiers, never secrets.
The source objects remain immutable inputs. Native execution supplies aidputils and Spark.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

CONFIG = {}
NETWORKS = frozenset({'instagram', 'facebook', 'tiktok', 'x', 'linkedin', 'youtube'})
GOLD_TABLES = ('ai_social_current', 'ai_social_history', 'ai_social_daily', 'ai_social_publication')
CONTROL_TABLES = ('ai_social_files', 'ai_social_ingest_state')
TABLE_DESCRIPTIONS = {
    'ai_social_current': 'Estado Gold más reciente por red e identificador original de publicación, con texto, métricas observadas y procedencia. El agente consulta las versiones Delta confirmadas por ai_social_publication.',
    'ai_social_history': 'Histórico Gold de cambios de publicaciones y métricas por instante de observación; conserva versiones distintas con procedencia y particiones mensuales UTC, sin duplicar capturas sin cambios.',
    'ai_social_daily': 'Agregados Gold por red y fecha UTC de publicación, calculados sobre el estado actual; los conteos y las métricas desconocidas se mantienen diferenciados.',
    'ai_social_publication': 'Recibo de publicación Gold consistente de AI Social Network: fija las versiones Delta de current, history y daily, la fecha del último recorrido completo y los archivos pendientes o fallidos. No contiene publicaciones sociales.',
    'ai_social_files': 'Control protegido de versiones de archivos JSON por nombre de objeto y ETag, con estado de procesamiento, conteos y errores sanitizados para reintentos incrementales.',
    'ai_social_ingest_state': 'Estado protegido de ingesta AI Social Network: cursor de descubrimiento, recibo de ejecución y fechas afectadas para recuperar escrituras interrumpidas sin duplicar datos.',
}
POST_COLUMNS = """post_key STRING NOT NULL COMMENT 'SHA256 of network and original post ID',
platform STRING NOT NULL COMMENT 'Source social network',
post_id STRING NOT NULL COMMENT 'Original provider post ID',
version_hash STRING NOT NULL COMMENT 'Canonical business payload SHA256; excludes capture run ID',
published_at TIMESTAMP COMMENT 'Publication timestamp reported by the source, nullable',
published_date DATE COMMENT 'UTC publication date, nullable',
text STRING COMMENT 'Observed post text',
url STRING COMMENT 'Original HTTP(S) publication URL',
author STRING COMMENT 'Observed public author name, nullable',
like_count BIGINT COMMENT 'Reported likes, nullable',
comment_count BIGINT COMMENT 'Reported comments, nullable',
share_count BIGINT COMMENT 'Reported shares, nullable',
view_count BIGINT COMMENT 'Reported views, nullable',
media_json STRING COMMENT 'Observed media links, not downloaded',
geography_json STRING COMMENT 'Reported location metadata; not inferred coordinates',
attributes_json STRING COMMENT 'Provider fields retained for schema evolution',
observed_at TIMESTAMP NOT NULL COMMENT 'UTC timestamp of the source observation',
source_object STRING NOT NULL COMMENT 'Object Storage source path',
source_etag STRING NOT NULL COMMENT 'Exact source object version read',
source_event_id STRING COMMENT 'Huginn event ID for provenance'"""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def timestamp(value, *, required=False):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('A timezone is required')
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        if required:
            raise ValueError('The source observation timestamp is invalid') from None
        return None


def validate_config(config):
    required = ('namespace', 'source_bucket', 'source_prefix', 'gold_bucket', 'artifacts_bucket',
                'catalog', 'gold_schema', 'control_schema', 'credential_name', 'identity_sha256')
    if any(not isinstance(config.get(key), str) or not config[key] for key in required):
        raise ValueError('Social ingestion configuration is incomplete')
    for key in ('catalog', 'gold_schema', 'control_schema'):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,99}', config[key]):
            raise ValueError('Invalid catalog or schema identifier')
    for key in ('source_bucket', 'gold_bucket', 'artifacts_bucket', 'namespace'):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}', config[key]):
            raise ValueError('Invalid Object Storage identifier')
    prefix = config['source_prefix']
    if prefix.startswith('/') or '\\' in prefix or any(part in {'.', '..', ''} for part in prefix.rstrip('/').split('/')):
        raise ValueError('Invalid source prefix')
    if any(ord(char) < 32 for char in prefix):
        raise ValueError('Invalid source prefix')
    if len({config[key] for key in ('source_bucket', 'gold_bucket', 'artifacts_bucket')}) != 3:
        raise ValueError('Source, Gold and control buckets must be distinct')
    for key, default, maximum in (('max_files', 25, 100), ('max_bytes', 32 * 1024 * 1024, 128 * 1024 * 1024),
                                  ('max_pages', 5, 20), ('max_records', 20000, 100000)):
        value = config.get(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError('Invalid ingestion batch limit')
    return config


def table(config, name):
    schema = config['gold_schema'] if name in GOLD_TABLES else config['control_schema']
    if name not in GOLD_TABLES + CONTROL_TABLES:
        raise ValueError('Unknown module table')
    return f"{config['catalog']}.{schema}.{name}"


def location(config, name):
    bucket = config['gold_bucket'] if name in GOLD_TABLES else config['artifacts_bucket']
    layer = 'gold' if name in GOLD_TABLES else 'control'
    return f"oci://{bucket}@{config['namespace']}/ai_social_network/{layer}/{name}"


def definitions():
    return {
        'ai_social_current': (POST_COLUMNS, 'platform'),
        'ai_social_history': (POST_COLUMNS + ", observation_id STRING NOT NULL COMMENT 'Idempotent observation identity', observation_month STRING NOT NULL COMMENT 'UTC observation month; physical partition'", 'observation_month'),
        'ai_social_daily': ("platform STRING NOT NULL, published_date DATE, published_month STRING NOT NULL, post_count BIGINT NOT NULL, like_count BIGINT, comment_count BIGINT, share_count BIGINT, view_count BIGINT, updated_at TIMESTAMP NOT NULL", 'published_month'),
        'ai_social_publication': ("module_id STRING NOT NULL, publication_id STRING NOT NULL, current_version BIGINT NOT NULL, history_version BIGINT NOT NULL, daily_version BIGINT NOT NULL, published_at TIMESTAMP NOT NULL, last_full_scan_at TIMESTAMP, pending_files BIGINT NOT NULL, failed_files BIGINT NOT NULL", ''),
        'ai_social_files': ("file_key STRING NOT NULL, source_object STRING NOT NULL, etag STRING NOT NULL, size_bytes BIGINT NOT NULL, last_modified TIMESTAMP NOT NULL, status STRING NOT NULL, discovered_at TIMESTAMP NOT NULL, processed_at TIMESTAMP, record_count BIGINT, error_code STRING", ''),
        'ai_social_ingest_state': ("module_id STRING NOT NULL, scan_cursor STRING, dirty_dates_json STRING, last_scan_completed_at TIMESTAMP, started_at TIMESTAMP, completed_at TIMESTAMP, status STRING, processed_files BIGINT, pending_files BIGINT, observed_records BIGINT, changed_records BIGINT, error_code STRING", ''),
    }


def schema_statements(config):
    for name, (columns, partition) in definitions().items():
        partition_sql = f' PARTITIONED BY ({partition})' if partition else ''
        yield name, f"CREATE TABLE IF NOT EXISTS {table(config, name)} ({columns}) USING DELTA COMMENT '{TABLE_DESCRIPTIONS[name]}'{partition_sql} LOCATION '{location(config, name)}' TBLPROPERTIES ('delta.autoOptimize.optimizeWrite'='true', 'delta.autoOptimize.autoCompact'='true', 'delta.dataSkippingNumIndexedCols'='6')"


def count_value(raw, *paths):
    for path in paths:
        value = raw
        for key in path.split('.'):
            value = value.get(key) if isinstance(value, dict) else None
        if type(value) is int and value >= 0:
            return value
    return None


def author_value(raw):
    for key in ('ownerUsername', 'channelName', 'pageName'):
        if isinstance(raw.get(key), str):
            return raw[key]
    for key in ('author', 'user'):
        value = raw.get(key)
        if isinstance(value, dict):
            for field in ('name', 'userName', 'username', 'displayName'):
                if isinstance(value.get(field), str):
                    return value[field]
    return None


def publication_url(url):
    from urllib.parse import urlsplit
    parsed = urlsplit(url) if isinstance(url, str) else None
    if parsed is None or any(ord(char) < 32 for char in url):
        raise ValueError('Invalid original publication URL')
    if url and (parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password):
        raise ValueError('Invalid original publication URL')
    return url


def normalize_event(event, source):
    if not isinstance(event, dict) or not isinstance(event.get('payload'), dict):
        raise ValueError('Invalid source event')
    payload = event['payload']
    platform, post_id = payload.get('platform'), payload.get('id')
    if platform not in NETWORKS or not isinstance(post_id, str) or not 1 <= len(post_id) <= 1024:
        raise ValueError('A supported network and original post ID are required')
    if f'network={platform}/' not in source['source_object']:
        raise ValueError('Source folder and publication network disagree')
    observed = timestamp(event.get('created_at'), required=True)
    published = timestamp(payload.get('published_at'))
    raw = payload.get('raw', {})
    if not isinstance(raw, dict) or not isinstance(payload.get('text', ''), str):
        raise ValueError('Invalid publication content')
    url = publication_url(payload.get('url', ''))
    # Capture IDs, search input and result ordering do not change the publication.
    attributes = {key: value for key, value in raw.items() if key not in {
        'input', 'inputUrl', 'query', 'order', 'run_id', 'scrapedAt', 'scraped_at', 'fetchedAt'}}
    business = {key: value for key, value in payload.items() if key not in {'run_id', 'raw'}}
    business['raw'] = attributes
    version_hash = digest(canonical(business))
    return {
        'post_key': digest(platform + '\0' + post_id), 'platform': platform, 'post_id': post_id,
        'version_hash': version_hash, 'published_at': published, 'published_date': published.date() if published else None,
        'text': payload.get('text', ''), 'url': url, 'author': author_value(raw),
        'like_count': count_value(raw, 'likesCount', 'likeCount', 'likes', 'engagement.likes', 'engagement.reactions'),
        'comment_count': count_value(raw, 'commentsCount', 'replyCount', 'engagement.comments'),
        'share_count': count_value(raw, 'shares', 'retweetCount', 'engagement.shares', 'engagement.reposts'),
        'view_count': count_value(raw, 'viewCount', 'viewsCount', 'views'),
        'media_json': canonical(payload.get('multimedia', [])), 'geography_json': canonical(payload.get('geography', {})),
        'attributes_json': canonical(attributes), 'observed_at': observed,
        'source_object': source['source_object'], 'source_etag': source['etag'],
        'source_event_id': str(event.get('event_id', '')),
    }


def credential_config(config):
    secrets_api = globals().get('aidputils')
    if secrets_api is None:
        import aidputils as secrets_api
    keys = ('user', 'tenancy', 'fingerprint', 'private_key', 'region')
    values = {key: secrets_api.secrets.get(name=config['credential_name'], key=key) for key in keys}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise RuntimeError('The shared OCI credential is incomplete')
    identity = [values[key] for key in ('tenancy', 'user', 'fingerprint')]
    if digest(json.dumps(identity, separators=(',', ':'))) != config['identity_sha256']:
        raise RuntimeError('The shared OCI identity does not match this deployment')
    import oci
    signer = oci.signer.Signer(values['tenancy'], values['user'], values['fingerprint'],
                              private_key_file_location=None, private_key_content=values['private_key'])
    sdk_config = {key: values[key] for key in ('tenancy', 'user', 'fingerprint', 'region')}
    sdk_config['key_content'] = values['private_key']
    return oci.object_storage.ObjectStorageClient(sdk_config, signer=signer, timeout=(10, 60))



def ensure_tables(spark, config):
    for schema in (config['gold_schema'], config['control_schema']):
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS {config['catalog']}.{schema}")
    for name, sql in schema_statements(config):
        spark.sql(sql)
        detail = spark.sql(f"DESCRIBE DETAIL {table(config, name)}").first().asDict()
        if str(detail.get('format', '')).lower() != 'delta' or str(detail.get('location', '')).rstrip('/') != location(config, name):
            raise RuntimeError('An existing module table has an incompatible format or location')
        expected_partition = [definitions()[name][1]] if definitions()[name][1] else []
        if list(detail.get('partitionColumns') or []) != expected_partition:
            raise RuntimeError('An existing module table has incompatible partitioning')
        expected = spark.createDataFrame([], schema=definitions()[name][0]).schema
        actual = spark.table(table(config, name)).schema
        if [(field.name, field.dataType) for field in actual] != [(field.name, field.dataType) for field in expected]:
            raise RuntimeError('An existing module table has incompatible columns or types')
        if not str(detail.get('description') or '').strip():
            spark.sql(f"ALTER TABLE {table(config, name)} SET TBLPROPERTIES ('comment'='{TABLE_DESCRIPTIONS[name]}')")


def merge_frame(spark, target, frame, key, *, update=True, condition=None):
    from delta.tables import DeltaTable
    # Delta 3.2 forName only parses two-part identifiers; AIDP uses three-part catalogs.
    path = spark.sql(f'DESCRIBE DETAIL {target}').first()['location']
    merge = DeltaTable.forPath(spark, path).alias('t').merge(frame.alias('s'), key)
    if update:
        merge = merge.whenMatchedUpdateAll(condition=condition)
    merge.whenNotMatchedInsertAll().execute()


def discovered_files(storage, config, cursor):
    rows, completed = [], False
    for _ in range(config.get('max_pages', 5)):
        result = storage.list_objects(config['namespace'], config['source_bucket'],
            prefix=config['source_prefix'].rstrip('/') + '/', start=cursor,
            fields='name,etag,size,timeModified', limit=1000).data
        for item in result.objects:
            if not item.name.endswith('.json'):
                continue
            if not item.etag or item.time_modified is None:
                raise RuntimeError('Object Storage did not return a versioned source object')
            rows.append({'file_key': digest(item.name + '\0' + item.etag),
                         'source_object': item.name, 'etag': item.etag, 'size_bytes': item.size,
                         'last_modified': item.time_modified, 'status': 'PENDING',
                         'discovered_at': datetime.now(timezone.utc), 'processed_at': None,
                         'record_count': None, 'error_code': None})
        cursor = result.next_start_with
        if not cursor:
            completed = True
            break
    return rows, cursor, completed


def record_discovery(spark, config, rows):
    if not rows:
        return
    target = table(config, 'ai_social_files')
    frame = spark.createDataFrame(rows, schema=spark.table(target).schema)
    merge_frame(spark, target, frame, 't.file_key=s.file_key', update=False)


def pending_files(spark, config):
    from pyspark.sql import functions as F
    candidates = (spark.table(table(config, 'ai_social_files'))
        .where(F.col('status').isin('PENDING', 'ERROR'))
        .orderBy(F.when(F.col('status') == 'PENDING', 0).otherwise(1), 'last_modified', 'source_object').limit(config.get('max_files', 25)).collect())
    total, selected = 0, []
    for candidate in candidates:
        source = candidate.asDict()
        # An oversized input is selected to record a visible error, never downloaded.
        size = int(source['size_bytes'])
        if selected and total + size > config.get('max_bytes', 32 * 1024 * 1024):
            break
        selected.append(source)
        total += size
    return selected


def read_source(storage, config, source):
    size = int(source['size_bytes'])
    if size > min(10 * 1024 * 1024, config.get('max_bytes', 32 * 1024 * 1024)):
        raise ValueError('The source file exceeds the accepted batch size')
    response = storage.get_object(config['namespace'], config['source_bucket'], source['source_object'], if_match=source['etag'])
    raw = response.data.raw.read(size + 1)
    if len(raw) != size:
        raise ValueError('The source file changed or exceeded its declared size')
    body = json.loads(raw)
    if not isinstance(body, dict) or not isinstance(body.get('events'), list):
        raise ValueError('The source must contain an events array')
    if len(body['events']) > config.get('max_records', 20000):
        raise ValueError('The source file exceeds the record limit')
    return [normalize_event(event, source) for event in body['events']]


def update_file_states(spark, config, files):
    if files:
        target = table(config, 'ai_social_files')
        merge_frame(spark, target, spark.createDataFrame(files, schema=spark.table(target).schema), 't.file_key=s.file_key')


def collect_sources(spark, storage, config):
    records, accepted, rejected = [], [], []
    for source in pending_files(spark, config):
        try:
            rows = read_source(storage, config, source)
            if len(records) + len(rows) > config.get('max_records', 20000):
                break
            records.extend(rows)
            accepted.append({**source, 'status': 'PROCESSED', 'processed_at': datetime.now(timezone.utc),
                             'record_count': len(rows), 'error_code': None})
        except Exception as exc:
            # A concurrently replaced object is rediscovered with its new ETag.
            state = 'SUPERSEDED' if getattr(exc, 'status', None) == 412 else 'ERROR'
            rejected.append({**source, 'status': state, 'error_code': digest(type(exc).__name__)[:16]})
    update_file_states(spark, config, rejected)
    return records, accepted, rejected


def change_frames(spark, config, records):
    from pyspark.sql import functions as F, Window
    current = table(config, 'ai_social_current')
    incoming = spark.createDataFrame(records, schema=spark.table(current).schema).dropDuplicates(['post_key', 'observed_at', 'version_hash'])
    keys = incoming.select('platform', 'post_key').distinct()
    networks = sorted({row['platform'] for row in records})
    previous = (spark.table(current).where(F.col('platform').isin(networks))
                .join(F.broadcast(keys), ['platform', 'post_key'], 'inner'))
    old_dates = previous.select('platform', 'published_date').distinct()
    combined = previous.withColumn('_incoming', F.lit(False)).unionByName(incoming.withColumn('_incoming', F.lit(True)))
    order = Window.partitionBy('platform', 'post_key').orderBy('observed_at', '_incoming', 'version_hash')
    baseline = Window.partitionBy('platform', 'post_key')
    compared = (combined.withColumn('_previous_hash', F.lag('version_hash').over(order))
        .withColumn('_baseline_hash', F.max(F.when(~F.col('_incoming'), F.col('version_hash'))).over(baseline))
        .withColumn('_baseline_at', F.max(F.when(~F.col('_incoming'), F.col('observed_at'))).over(baseline)))
    old_unchanged = (F.col('_baseline_hash').isNotNull() & (F.col('version_hash') == F.col('_baseline_hash'))
                     & (F.col('observed_at') <= F.col('_baseline_at')))
    changed = (compared.where(F.col('_incoming') & (F.col('_previous_hash').isNull() | (F.col('_previous_hash') != F.col('version_hash'))) & ~old_unchanged)
               .drop('_incoming', '_previous_hash', '_baseline_hash', '_baseline_at')
               .withColumn('observation_month', F.date_format('observed_at', 'yyyy-MM'))
               .withColumn('observation_id', F.sha2(F.concat_ws('\0', 'post_key', 'version_hash', F.date_format('observed_at', "yyyy-MM-dd'T'HH:mm:ss.SSSSSSXXX")), 256)))
    descending = Window.partitionBy('platform', 'post_key').orderBy(F.col('observed_at').desc(), F.col('_incoming').desc(), F.col('version_hash').desc())
    latest = (combined.withColumn('_rank', F.row_number().over(descending)).where(F.col('_rank') == 1)
              .drop('_rank', '_incoming'))
    dates = old_dates.unionByName(latest.select('platform', 'published_date')).distinct()
    return changed, latest, dates, networks


def write_gold(spark, config, records, state, checkpoint):
    from pyspark.sql import functions as F
    if not records:
        pending_dates = json.loads(state.get('dirty_dates_json') or '[]')
        if pending_dates:
            refresh_daily(spark, config, date_frame(spark, pending_dates), sorted({row['platform'] for row in pending_dates}))
        return 0
    changed, latest, dates, networks = change_frames(spark, config, records)
    changed = changed.cache()
    latest = latest.cache()
    dates = dates.cache()
    try:
        touched = {(row['platform'], row['published_date'].isoformat() if row['published_date'] else None) for row in dates.collect()}
        touched.update((row['platform'], row['published_date']) for row in json.loads(state.get('dirty_dates_json') or '[]'))
        dirty = [{'platform': platform, 'published_date': day} for platform, day in sorted(touched, key=lambda row: (row[0], row[1] or ''))]
        state['dirty_dates_json'] = canonical(dirty)
        checkpoint()
        count = changed.count()
        if count:
            months = [row[0] for row in changed.select('observation_month').distinct().collect()]
            # Values are generated UTC months, not caller/model SQL fragments.
            predicate = ','.join("'" + month + "'" for month in months)
            merge_frame(spark, table(config, 'ai_social_history'), changed,
                f't.observation_month IN ({predicate}) AND t.observation_id=s.observation_id', update=False)
        network_predicate = ','.join("'" + name + "'" for name in networks)
        merge_frame(spark, table(config, 'ai_social_current'), latest,
            f't.platform IN ({network_predicate}) AND t.platform=s.platform AND t.post_key=s.post_key',
            condition='s.observed_at > t.observed_at OR (s.observed_at=t.observed_at AND s.version_hash<>t.version_hash)')
        refresh_daily(spark, config, date_frame(spark, dirty), sorted({row['platform'] for row in dirty}))
        return count
    finally:
        changed.unpersist()
        latest.unpersist()
        dates.unpersist()


def date_frame(spark, dates):
    from datetime import date
    return spark.createDataFrame([
        (row['platform'], date.fromisoformat(row['published_date']) if row['published_date'] else None)
        for row in dates], schema='platform STRING, published_date DATE')


def refresh_daily(spark, config, dates, networks):
    from pyspark.sql import functions as F
    current = spark.table(table(config, 'ai_social_current')).where(F.col('platform').isin(networks)).alias('p')
    affected = dates.alias('d')
    selected = current.join(F.broadcast(affected), (F.col('p.platform') == F.col('d.platform')) & F.col('p.published_date').eqNullSafe(F.col('d.published_date')), 'inner')
    totals = selected.select('p.*').groupBy('platform', 'published_date').agg(
        F.count('*').alias('post_count'), *[F.sum(name).alias(name) for name in ('like_count', 'comment_count', 'share_count', 'view_count')])
    # Include dates emptied by a corrected source publication date: retry-safe zero counts.
    totals = (dates.join(totals, (dates.platform == totals.platform) & dates.published_date.eqNullSafe(totals.published_date), 'left')
              .select(dates.platform, dates.published_date, F.coalesce(totals.post_count, F.lit(0)).alias('post_count'),
                      *[totals[name].alias(name) for name in ('like_count', 'comment_count', 'share_count', 'view_count')])
              .withColumn('published_month', F.coalesce(F.date_format('published_date', 'yyyy-MM'), F.lit('unknown')))
              .withColumn('updated_at', F.current_timestamp()))
    months = [row[0] for row in totals.select('published_month').distinct().collect()]
    predicate = ','.join("'" + month + "'" for month in months)
    merge_frame(spark, table(config, 'ai_social_daily'), totals,
                f't.published_month IN ({predicate}) AND t.published_month=s.published_month AND t.platform=s.platform AND t.published_date <=> s.published_date')


def publish_gold(spark, config, state, failed_files):
    from uuid import uuid4
    versions = {}
    for name in ('current', 'history', 'daily'):
        result = spark.sql(f"DESCRIBE HISTORY {table(config, 'ai_social_' + name)} LIMIT 1").first()
        if result is None:
            raise RuntimeError('Gold table has no verifiable Delta snapshot')
        versions[name + '_version'] = int(result['version'])
    target = table(config, 'ai_social_publication')
    row = {'module_id': 'ai_social_network', 'publication_id': str(uuid4()),
           **versions, 'published_at': datetime.now(timezone.utc), 'last_full_scan_at': state['last_scan_completed_at'],
           'pending_files': state['pending_files'], 'failed_files': failed_files}
    merge_frame(spark, target, spark.createDataFrame([row], schema=spark.table(target).schema), 't.module_id=s.module_id')


def record_run(spark, config, state):
    target = table(config, 'ai_social_ingest_state')
    merge_frame(spark, target, spark.createDataFrame([state], schema=spark.table(target).schema), 't.module_id=s.module_id')


def run(config):
    from pyspark.sql import SparkSession, functions as F
    config = validate_config(config)
    spark = SparkSession.builder.getOrCreate()
    spark.conf.set('spark.sql.session.timeZone', 'UTC')
    ensure_tables(spark, config)
    states = spark.table(table(config, 'ai_social_ingest_state')).where(F.col('module_id') == 'ai_social_network').limit(2).collect()
    if len(states) > 1:
        raise RuntimeError('The social ingestion state singleton is ambiguous')
    old = states[0].asDict() if states else {}
    state = {'module_id': 'ai_social_network', 'scan_cursor': old.get('scan_cursor'),
             'dirty_dates_json': old.get('dirty_dates_json'), 'last_scan_completed_at': old.get('last_scan_completed_at'), 'started_at': datetime.now(timezone.utc),
             'completed_at': None, 'status': 'RUNNING', 'processed_files': 0, 'pending_files': None,
             'observed_records': 0, 'changed_records': 0, 'error_code': None}
    record_run(spark, config, state)
    try:
        storage = credential_config(config)
        metadata, cursor, completed = discovered_files(storage, config, state['scan_cursor'])
        record_discovery(spark, config, metadata)
        records, accepted, rejected = collect_sources(spark, storage, config)
        state['changed_records'] = write_gold(spark, config, records, state, lambda: record_run(spark, config, state))
        pending = spark.table(table(config, 'ai_social_files')).where(F.col('status').isin('PENDING', 'ERROR')).count()
        state['pending_files'] = max(0, pending - len(accepted))
        if completed:
            state['last_scan_completed_at'] = datetime.now(timezone.utc)
        failed = spark.table(table(config, 'ai_social_files')).where(F.col('status') == 'ERROR')
        if accepted:
            failed = failed.where(~F.col('file_key').isin([row['file_key'] for row in accepted]))
        publish_gold(spark, config, state, failed.count())
        # Cross-table commits are not atomic: history/current MERGE must be idempotent
        # before marking the exact source file versions as processed.
        update_file_states(spark, config, accepted)
        state.update(dirty_dates_json=None, scan_cursor=cursor, completed_at=datetime.now(timezone.utc), status='SUCCESS',
                     processed_files=len(accepted), observed_records=len(records))
        if completed:
            state['last_scan_completed_at'] = state['completed_at']
        state['pending_files'] = (spark.table(table(config, 'ai_social_files')).where(F.col('status').isin('PENDING', 'ERROR')).count())
        if any(row['status'] == 'ERROR' for row in rejected):
            raise RuntimeError('One or more source files could not be processed; inspect the protected file ledger')
        record_run(spark, config, state)
        print(canonical({key: value for key, value in state.items() if key not in {'scan_cursor', 'dirty_dates_json', 'started_at', 'completed_at', 'last_scan_completed_at'}}))
    except Exception as exc:
        state.update(status='ERROR', completed_at=datetime.now(timezone.utc), error_code=digest(type(exc).__name__)[:16])
        record_run(spark, config, state)
        raise


if __name__ == '__main__':
    run(CONFIG)
