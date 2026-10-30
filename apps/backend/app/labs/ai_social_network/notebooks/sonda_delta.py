"""Sonda 1.0.2 persistence, independent of the legacy social ingestion runtime.

Inputs are JSON-mode dictionaries ALREADY checked by sonda_contract.parse against
the transport's raw-byte ledger. This module does not replace that validation,
fetch objects, acknowledge streams, spend budget, or invoke models. The caller
must hold one generation-scoped writer lease across apply_messages/publish.

Delta commits are per table. A durable APPLYING/READY journal repairs interrupted
writes before another message is accepted; readers must use the exact table
versions in ai_social_publication, never uncommitted latest tables. Transport may
ack only returned APPLIED/REJECTED receipts. apply_study_results persists typed
model outputs and their checkpoint state; it never invokes a model itself.
"""
import hashlib
import json
import re
from datetime import date, datetime, timezone
from uuid import UUID, uuid5
from app.labs.ai_social_network.notebooks.sonda_tools import tool_catalog_definition, tool_catalog_rows

CONTRACT_SHA256 = '6e2613ac0c28e863fdf2fc02d69f92b92dd56003edcc242c6b41bbe218924483'
SCOPE = 'registry_id STRING NOT NULL, generation_id STRING NOT NULL, '
SNAPSHOT = ('snapshot_key STRING NOT NULL, config_revision BIGINT NOT NULL, '
            'record_id STRING NOT NULL, record_revision BIGINT NOT NULL, '
            'status STRING NOT NULL, approval_status STRING NOT NULL, '
            'record_sha256 STRING NOT NULL, payload_json STRING NOT NULL')
DOCUMENT = ('document_key STRING NOT NULL, observation_key STRING NOT NULL, '
            'content_sha256 STRING NOT NULL, document_type STRING NOT NULL, '
            'platform STRING, published_date DATE, published_at TIMESTAMP, observed_at TIMESTAMP NOT NULL, '
            'likes BIGINT, comments BIGINT, shares BIGINT, views BIGINT, '
            'provider_id STRING, parent_document_key STRING, '
            'url STRING NOT NULL, title STRING, text STRING NOT NULL, author STRING, '
            'capture_scope STRING NOT NULL, indicators_json STRING NOT NULL')
BUDGET = ('budget_round_id STRING NOT NULL, revision BIGINT NOT NULL, config_revision BIGINT NOT NULL, '
          'opened_at TIMESTAMP NOT NULL, expires_at TIMESTAMP NOT NULL, status STRING NOT NULL, '
          'remaining_microusd BIGINT, research_remaining_microusd BIGINT, remaining_items BIGINT, '
          'payload_sha256 STRING NOT NULL, payload_json STRING NOT NULL, message_json STRING NOT NULL')
TABLE_PURPOSES = {
    'registry':'Approved configuration revisions and their immutable source messages',
    'sources':'Versioned approved connector and source references',
    'queries':'Versioned operator queries, capture limits and approval decisions',
    'tool_catalog':'Reviewed Huginn query tools and static invocation ceilings; execution remains broker controlled',
    'source_limits':'Immutable source policy snapshots bound to applied Huginn configuration revisions',
    'criteria':'Versioned relevance rules, thresholds and labelled examples',
    'budget_rounds':'Current confirmed capture budget from the Huginn ledger',
    'budget_round_revisions':'Immutable budget revisions for execution verification',
    'document_current':'Latest observed version of each logical post, comment, page or indicator',
    'observations':'Distinct content and metric versions, partitioned by observation month',
    'discoveries':'Query and execution provenance for each observed document version',
    'evaluations':'Pending and completed relevance work keyed by case and observation',
    'evaluation_results':'Model and prompt versioned relevance decisions with exact evidence',
    'investigations':'Stable research cases bound to the original criterion revision',
    'evidence':'Versioned source references and exact quotations supporting research',
    'reports':'Committed research report revisions and their measured coverage',
    'proposals':'Agent suggestions awaiting an explicit operator decision',
    'daily':'Daily distinct-document totals and known metric sums with missing coverage',
    'executions':'Confirmed capture outcomes and actual spending from Huginn',
    'requests':'Bounded research requests using approved queries and confirmed budget',
    'study_results':'Legacy offline study result replay journal',
    'study_state':'Legacy offline study checkpoint; unused by the production case worker',
    'study_case_state':'Compact per-case operational state with predecessor revision checks',
    'case_work':'Dirty case signals and immutable input revision fingerprints',
    'case_study_results':'Durable per-case result replay and publication journal',
    'receipts':'Inbound message validation, rejection and effect commit journal',
    'outbox':'Durable outbound messages awaiting immutable stream publication',
    'publication':'Coherent serving snapshot pinning the exact committed Delta versions',
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n'


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def json_mode(value):
    if isinstance(value, datetime):
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return value.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_mode(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_mode(child) for child in value]
    return value


def utc(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('Timestamp must include a timezone')
    return parsed.astimezone(timezone.utc)


def checked_binding(binding):
    result = dict(binding)
    for key in ('registry_id', 'generation_id'):
        if not isinstance(result.get(key), str) or str(UUID(result[key])) != result[key]:
            raise ValueError('Binding requires canonical UUIDs')
    for key in ('catalog', 'gold_schema', 'control_schema', 'namespace',
                'source_bucket', 'gold_bucket', 'artifacts_bucket'):
        if not isinstance(result.get(key), str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,254}', result[key]):
            raise ValueError('Invalid bound identifier: ' + key)
    for key in ('catalog','gold_schema','control_schema'):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,254}',result[key]):
            raise ValueError('SQL namespace requires a plain identifier: '+key)
    if result['gold_schema'] == result['control_schema']:
        raise ValueError('Gold and control namespaces must be distinct')
    if len({result[k] for k in ('source_bucket', 'gold_bucket', 'artifacts_bucket')}) != 3:
        raise ValueError('Source, Gold and artifacts buckets must be distinct')
    if result.get('contract_sha256') != CONTRACT_SHA256:
        raise ValueError('Unsupported pinned contract')
    if 'local_root' in result or 'location_root' in result:
        raise ValueError('Storage locations cannot be overridden by binding')
    return result


def location(binding, name, layer):
    """Only operator-bound OCI storage; tests may replace this function locally."""
    bucket = binding['gold_bucket' if layer == 'gold' else 'artifacts_bucket']
    return ('oci://{bucket}@{namespace}/ai_social_network/registries/{registry_id}/'
            'generations/{generation_id}/{layer}/{name}').format(bucket=bucket, layer=layer, name=name, **binding)


def definitions(binding):
    """Typed table definitions and dedicated bound locations; no DDL side effects."""
    b = checked_binding(binding)
    shapes = {
        'registry': ('config_revision BIGINT NOT NULL, payload_sha256 STRING NOT NULL, payload_json STRING NOT NULL, message_json STRING NOT NULL', (), 'gold'),
        'sources': (SNAPSHOT, (), 'gold'),
        'queries': (SNAPSHOT, (), 'gold'),
        'tool_catalog': (tool_catalog_definition()['columns'], (), 'gold'),
        'source_limits': ('catalog_revision BIGINT NOT NULL, config_revision BIGINT NOT NULL, snapshot_sha256 STRING NOT NULL, configuration_payload_sha256 STRING NOT NULL, payload_json STRING NOT NULL, core_configuration_message_id STRING NOT NULL, core_configuration_source_sequence BIGINT NOT NULL, core_configuration_raw_sha256 STRING NOT NULL', (), 'gold'),
        'criteria': (SNAPSHOT, (), 'gold'),
        'budget_rounds': (BUDGET, (), 'gold'),
        'budget_round_revisions': (BUDGET, (), 'control'),
        'document_current': (DOCUMENT + ', last_seen_at TIMESTAMP NOT NULL', ('document_type', 'platform'), 'gold'),
        'observations': (DOCUMENT + ', observation_month STRING NOT NULL', ('observation_month',), 'gold'),
        'discoveries': ('discovery_key STRING NOT NULL, observation_key STRING NOT NULL, document_key STRING NOT NULL, config_revision BIGINT NOT NULL, source_id STRING NOT NULL, source_revision BIGINT NOT NULL, query_id STRING NOT NULL, query_revision BIGINT NOT NULL, execution_id STRING NOT NULL, budget_round_id STRING NOT NULL, provider_run_id STRING NOT NULL, message_id STRING NOT NULL, observed_at TIMESTAMP NOT NULL, observation_month STRING NOT NULL', ('observation_month',), 'gold'),
        'evaluations': ('investigation_id STRING NOT NULL, work_key STRING NOT NULL, observation_key STRING NOT NULL, criterion_id STRING NOT NULL, criterion_revision BIGINT NOT NULL, config_revision BIGINT NOT NULL, rule_sha256 STRING NOT NULL, state STRING NOT NULL, evaluation_key STRING, model_id STRING, model_revision STRING, prompt_version STRING, score DOUBLE, decision STRING, payload_json STRING, observation_month STRING NOT NULL', ('observation_month',), 'gold'),
        'evaluation_results': ('investigation_id STRING NOT NULL, evaluation_key STRING NOT NULL, observation_key STRING NOT NULL, criterion_id STRING NOT NULL, criterion_revision BIGINT NOT NULL, config_revision BIGINT NOT NULL, rule_sha256 STRING NOT NULL, model_id STRING NOT NULL, model_revision STRING NOT NULL, prompt_version STRING NOT NULL, score DOUBLE, decision STRING NOT NULL, evaluated_at TIMESTAMP NOT NULL, payload_sha256 STRING NOT NULL, payload_json STRING NOT NULL, observation_month STRING NOT NULL', ('observation_month',), 'gold'),
        'investigations': ('investigation_id STRING NOT NULL, criterion_id STRING NOT NULL, criterion_revision BIGINT NOT NULL, config_revision BIGINT NOT NULL, case_scope_key STRING NOT NULL, window_start TIMESTAMP NOT NULL, state STRING NOT NULL', (), 'gold'),
        'evidence': ('evidence_id STRING NOT NULL, observation_key STRING NOT NULL, document_key STRING NOT NULL, url STRING NOT NULL, quote STRING NOT NULL, captured_at TIMESTAMP NOT NULL', (), 'gold'),
        'reports': ('report_id STRING NOT NULL, investigation_id STRING NOT NULL, revision BIGINT NOT NULL, config_revision BIGINT NOT NULL, status STRING NOT NULL, payload_json STRING NOT NULL', (), 'gold'),
        'proposals': ('proposal_id STRING NOT NULL, base_config_revision BIGINT, state STRING NOT NULL, payload_json STRING, decision_json STRING', (), 'gold'),
        'daily': ('day_key STRING NOT NULL, document_type STRING NOT NULL, platform STRING, published_date DATE, document_count BIGINT NOT NULL, likes BIGINT, comments BIGINT, shares BIGINT, views BIGINT, missing_likes BIGINT NOT NULL, missing_comments BIGINT NOT NULL, missing_shares BIGINT NOT NULL, missing_views BIGINT NOT NULL, published_month STRING NOT NULL', ('published_month',), 'gold'),
        'executions': ('execution_id STRING NOT NULL, status STRING NOT NULL, config_revision BIGINT NOT NULL, query_id STRING NOT NULL, query_revision BIGINT NOT NULL, record_count BIGINT NOT NULL, spent_microusd BIGINT, payload_json STRING NOT NULL, message_json STRING NOT NULL', (), 'control'),
        'requests': ('request_id STRING NOT NULL, investigation_id STRING NOT NULL, config_revision BIGINT NOT NULL, query_id STRING NOT NULL, query_revision BIGINT NOT NULL, budget_round_id STRING NOT NULL, state STRING NOT NULL, expires_at TIMESTAMP NOT NULL, payload_sha256 STRING NOT NULL, payload_json STRING NOT NULL', (), 'gold'),
        'study_results': ('result_id STRING NOT NULL, result_sha256 STRING NOT NULL, result_json STRING NOT NULL, state STRING NOT NULL, created_at TIMESTAMP NOT NULL, sequence BIGINT NOT NULL', (), 'control'),
        'study_state': ('result_id STRING NOT NULL, sequence BIGINT NOT NULL, state_json STRING NOT NULL', (), 'control'),
        'study_case_state': ('investigation_id STRING NOT NULL, version BIGINT NOT NULL, predecessor_revision BIGINT NOT NULL, result_id STRING NOT NULL, state_json STRING NOT NULL, write_status STRING NOT NULL, updated_at TIMESTAMP NOT NULL', (), 'control'),
        'case_work': ('investigation_id STRING NOT NULL, input_revision STRING NOT NULL, dirty BOOLEAN NOT NULL, updated_at TIMESTAMP NOT NULL', (), 'control'),
        'case_study_results': ('result_id STRING NOT NULL, investigation_id STRING NOT NULL, predecessor_revision BIGINT NOT NULL, version BIGINT NOT NULL, result_sha256 STRING NOT NULL, result_json STRING NOT NULL, state STRING NOT NULL, created_at TIMESTAMP NOT NULL, batch_size INT NOT NULL', (), 'control'),
        'receipts': ('message_id STRING NOT NULL, message_sha256 STRING NOT NULL, kind STRING NOT NULL, state STRING NOT NULL, code STRING NOT NULL, explanation STRING NOT NULL, message_json STRING NOT NULL, dirty_dates_json STRING NOT NULL, source_sequence BIGINT, raw_sha256 STRING, dirty_cases_json STRING, received_at TIMESTAMP', (), 'control'),
        'outbox': ('outbox_key STRING NOT NULL, source_message_id STRING NOT NULL, kind STRING NOT NULL, status STRING NOT NULL, payload_json STRING NOT NULL', (), 'control'),
        'publication': ('publication_id STRING NOT NULL, config_revision BIGINT NOT NULL, versions_json STRING NOT NULL, message_ids_json STRING NOT NULL, published_at TIMESTAMP NOT NULL', (), 'control'),
    }
    result = {}
    for suffix, (columns, partitions, layer) in shapes.items():
        name = 'ai_social_' + suffix
        schema = b['gold_schema' if layer == 'gold' else 'control_schema']
        result[name] = {'columns': SCOPE + columns, 'partitions': partitions, 'layer': layer,
                        'description': TABLE_PURPOSES[suffix]+'; generation scoped; unknown metrics remain NULL',
                        'table': '.'.join((b['catalog'], schema, name)),
                        'location': location(b, name, layer), 'indexed_columns': {
                            'document_current': 16, 'observations': 16,
                            'evidence': 5, 'reports': 7, 'study_case_state': 6, 'case_study_results': 7,
                            'study_results': 4, 'discoveries': 12}.get(suffix, 8)}
    return result


def ensure_tables(spark, binding):
    """Create fresh Sonda tables; refuse adoption/evolution of another runtime."""
    b = checked_binding(binding)
    for schema in (b['gold_schema'], b['control_schema']):
        spark.sql('CREATE SCHEMA IF NOT EXISTS ' + b['catalog'] + '.' + schema)
    for name, d in definitions(b).items():
        partition = ' PARTITIONED BY (' + ','.join(d['partitions']) + ')' if d['partitions'] else ''
        spark.sql("CREATE TABLE IF NOT EXISTS " + d['table'] + ' (' + d['columns'] + ") USING DELTA" + partition +
                  " LOCATION '" + d['location'] + "' COMMENT '" + d['description'] +
                  "' TBLPROPERTIES ('delta.dataSkippingNumIndexedCols'='" + str(d['indexed_columns']) + "')")
        detail = spark.sql('DESCRIBE DETAIL ' + d['table']).first().asDict()
        expected = spark.createDataFrame([], d['columns']).schema
        actual = spark.table(d['table']).schema
        checks = {'format': detail['format'].lower() == 'delta',
                  'location': same_location(detail['location'], d['location']),
                  'partitions': list(detail['partitionColumns']) == list(d['partitions']),
                  'columns': [(f.name, f.dataType) for f in actual] == [(f.name, f.dataType) for f in expected]}
        if not all(checks.values()):
            raise RuntimeError('Incompatible Sonda table: ' + name + ' (' + ','.join(k for k, ok in checks.items() if not ok) + ')')


def same_location(actual, expected):
    """Hadoop renders local file:/// fixtures as file:/; OCI stays exact."""
    from urllib.parse import urlsplit
    a, b = urlsplit(actual), urlsplit(expected)
    if a.scheme == b.scheme == 'file' and not (a.netloc or b.netloc or a.query or b.query or a.fragment or b.fragment):
        return a.path.rstrip('/') == b.path.rstrip('/')
    return actual.rstrip('/') == expected.rstrip('/')


def _table(binding, suffix):
    return definitions(binding)['ai_social_' + suffix]['table']


def _scope(binding):
    return {key: binding[key] for key in ('registry_id', 'generation_id')}


def _rows(spark, binding, suffix, rows, keys, condition=None, update=False):
    if not rows:
        return
    definition = definitions(binding)['ai_social_' + suffix]
    frame = spark.createDataFrame([{**_scope(binding), **row} for row in rows], spark.table(definition['table']).schema)
    _merge(spark, definition['location'], frame, keys, condition, update)


def _merge(spark, path, frame, keys, condition=None, update=False):
    from delta.tables import DeltaTable
    on = ' AND '.join('t.' + key + ' <=> s.' + key for key in ('registry_id', 'generation_id') + tuple(keys))
    operation = DeltaTable.forPath(spark, path).alias('t').merge(frame.alias('s'), on)
    if update:
        operation = operation.whenMatchedUpdateAll(condition=condition)
    operation.whenNotMatchedInsertAll().execute()


def _find(spark, binding, suffix, **where):
    from pyspark.sql import functions as F
    frame = spark.table(_table(binding, suffix))
    for key, value in {**_scope(binding), **where}.items():
        frame = frame.where(F.col(key).eqNullSafe(F.lit(value)))
    return [row.asDict() for row in frame.collect()]


def _pending(spark, binding, states):
    from pyspark.sql import functions as F
    frame = spark.table(_table(binding, 'receipts'))
    for key, value in _scope(binding).items():
        frame = frame.where(F.col(key) == value)
    rows = frame.where(F.col('state').isin(states)).limit(101).collect()
    if len(rows) > 100:
        raise RuntimeError('Pending journal exceeds one bounded batch')
    return [row.asDict() for row in rows]


def _keys(spark, binding, suffix, key, values):
    from pyspark.sql import functions as F
    if not values:
        return []
    frame = spark.table(_table(binding, suffix)).where(F.col(key).isin(list(values)))
    for field, value in _scope(binding).items():
        frame = frame.where(F.col(field) == value)
    return [row.asDict() for row in frame.collect()]


def capture_configuration(payload, config):
    """Historical capture validation; the latest snapshot cannot reinterpret it."""
    if config['revision'] != payload['config_revision']:
        raise ValueError('Capture configuration revision is unavailable')
    source = next((r for r in config['sources'] if r['source_id'] == payload['source_id']), None)
    query = next((r for r in config['queries'] if r['query_id'] == payload['query_id']), None)
    if (source is None or query is None or query['source_id'] != source['source_id']
            or source['revision'] != payload['source_revision'] or query['revision'] != payload['query_revision']
            or any(r['status'] != 'active' or r['approval']['status'] != 'approved' for r in (source, query))):
        raise ValueError('Capture source/query is not approved at its capture revision')
    allowed = {'posts': {'post'}, 'comments': {'comment'}, 'pages': {'page'}, 'indicators': {'indicator'}}[query['mode']]
    for obs in payload['observations']:
        if obs['document_type'] not in allowed or (source['platform'] is not None and obs['platform'] != source['platform']):
            raise ValueError('Observation does not match its reviewed source/query')
        if source['category'] != 'social':
            from urllib.parse import urlsplit
            if urlsplit(obs['url']).hostname.encode('idna').decode().lower() not in source['allowed_domains']:
                raise ValueError('Observation host is outside its reviewed source')
    return source, query


def observation_rows(message, config):
    """Bounded DTO projection; discovery never becomes a new logical document."""
    batch = message['payload']
    capture_configuration(batch, config)
    current, observations, discoveries, evaluations, cases = {}, {}, {}, {}, {}
    for obs in batch['observations']:
        observed = utc(obs['observed_at'])
        published = utc(obs['published_at']) if obs['published_at'] else None
        row = {key: obs.get(key) for key in ('document_key', 'observation_key', 'content_sha256', 'document_type', 'platform', 'parent_document_key', 'provider_id', 'url', 'title', 'text', 'author', 'capture_scope')}
        row.update(published_at=published, published_date=published.date() if published else None,
                   observed_at=observed, indicators_json=canonical(obs['indicators']), **obs['metrics'])
        old = current.get(obs['document_key'])
        if old is None or (observed, obs['observation_key']) > (old['observed_at'], old['observation_key']):
            current[obs['document_key']] = {**row, 'last_seen_at': observed}
        observations[obs['observation_key']] = {**row, 'observation_month': observed.strftime('%Y-%m')}
        discovery_key = fingerprint([batch['query_id'], batch['query_revision'], batch['execution_id'], obs['observation_key']])
        discoveries[discovery_key] = {'discovery_key': discovery_key, 'observation_key': obs['observation_key'], 'document_key': obs['document_key'],
            **{key: batch[key] for key in ('config_revision', 'source_id', 'source_revision', 'query_id', 'query_revision', 'execution_id', 'budget_round_id', 'provider_run_id')},
            'message_id': message['message_id'], 'observed_at': observed, 'observation_month': observed.strftime('%Y-%m')}
        for criterion in config['criteria']:
            if (criterion['status'] != 'active' or criterion['approval']['status'] != 'approved'
                    or batch['source_id'] not in criterion['source_ids']
                    or (criterion['query_ids'] and batch['query_id'] not in criterion['query_ids'])):
                continue
            hours = criterion['case_window_hours']
            start = datetime.fromtimestamp((int(observed.timestamp()) // (hours * 3600)) * hours * 3600, timezone.utc)
            case_id = fingerprint([criterion['criterion_id'], criterion['revision'], criterion['case_scope_key'], start.isoformat(timespec='microseconds').replace('+00:00', 'Z')])
            rule_hash = fingerprint(criterion)
            work_key = fingerprint([obs['observation_key'], criterion['criterion_id'], criterion['revision'], rule_hash])
            evaluations[work_key] = {'work_key': work_key, 'observation_key': obs['observation_key'], 'criterion_id': criterion['criterion_id'],
                'criterion_revision': criterion['revision'], 'config_revision': config['revision'], 'rule_sha256': rule_hash,
                'investigation_id': case_id, 'state': 'pending', 'evaluation_key': None, 'model_id': None,
                'model_revision': None, 'prompt_version': None, 'score': None, 'decision': None, 'payload_json': None,
                'observation_month': observed.strftime('%Y-%m')}
            if len(evaluations) > 10000:
                raise ValueError('Capture exceeds the bounded pending evaluation batch')
            cases[case_id] = {'investigation_id': case_id, 'criterion_id': criterion['criterion_id'], 'criterion_revision': criterion['revision'],
                'config_revision': config['revision'], 'case_scope_key': criterion['case_scope_key'], 'window_start': start, 'state': 'awaiting_evaluation'}
    return {'document_current': list(current.values()), 'observations': list(observations.values()),
            'discoveries': list(discoveries.values()), 'evaluations': list(evaluations.values()), 'investigations': list(cases.values())}


def _configuration(spark, binding, revision):
    rows = _find(spark, binding, 'registry', config_revision=revision)
    if len(rows) != 1:
        raise ValueError('Referenced configuration snapshot is unavailable')
    return json.loads(rows[0]['payload_json'])


def _latest_revision(spark, binding):
    from pyspark.sql import functions as F
    return int(spark.table(_table(binding, 'registry')).agg(F.max('config_revision')).first()[0] or 0)


def _check_record_successors(previous, current):
    for domain, key in (('sources', 'source_id'), ('queries', 'query_id'), ('criteria', 'criterion_id')):
        old = {row[key]: row for row in previous[domain]}
        for row in current[domain]:
            prior = old.get(row[key])
            if prior and (row['revision'] < prior['revision'] or (row['revision'] == prior['revision'] and row != prior)):
                raise ValueError('Managed record revision changed without a new revision')


def _check_managed_history(spark, binding, payload):
    for domain, key in (('sources', 'source_id'), ('queries', 'query_id'), ('criteria', 'criterion_id')):
        for row in payload[domain]:
            prior = _find(spark, binding, domain, record_id=row[key], record_revision=row['revision'])
            if any(r['record_sha256'] != fingerprint(row) for r in prior):
                raise ValueError('Managed revision is immutable across configuration snapshots')


def _prepare_configuration(spark, binding, message):
    p = message['payload']
    latest = _latest_revision(spark, binding)
    existing = _find(spark, binding, 'registry', config_revision=p['revision'])
    if existing:
        if existing[0]['payload_sha256'] != fingerprint(p):
            raise ValueError('Conflicting immutable configuration revision')
    elif p['previous_revision'] != latest or p['revision'] != latest + 1:
        raise ValueError('Configuration does not extend the current revision')
    if p['previous_revision']:
        previous = _configuration(spark, binding, p['previous_revision'])
        _check_record_successors(previous, p)
    for decision in p.get('proposal_decisions', []):
        prior = _find(spark, binding, 'proposals', proposal_id=decision['proposal_id'])
        if prior and prior[0]['decision_json'] and prior[0]['decision_json'] != canonical(decision):
            raise ValueError('Proposal already has a different terminal decision')
    _check_managed_history(spark, binding, p)
    return {'tool_catalog': tool_catalog_rows(p, validated=True)}


def _prepare_budget_round(spark, binding, message):
    p = message['payload']
    _configuration(spark, binding, p['config_revision'])
    prior = _find(spark, binding, 'budget_round_revisions', budget_round_id=p['budget_round_id'], revision=p['revision'])
    if prior:
        if prior[0]['payload_sha256'] != fingerprint(p):
            raise ValueError('Conflicting immutable budget revision')
        return {}
    current = _find(spark, binding, 'budget_rounds', budget_round_id=p['budget_round_id'])
    if current:
        last = json.loads(current[0]['payload_json'])
        if p['revision'] <= last['revision']:
            raise ValueError('Unrecognized stale budget revision')
        if any(p[key] != last[key] for key in ('config_revision', 'opened_at', 'expires_at')):
            raise ValueError('Budget round identity/lifetime is immutable')
        if last['status'] == 'closed' and p['status'] != 'closed':
            raise ValueError('A closed budget round cannot reopen')
    elif p['revision'] != 1:
        raise ValueError('A new budget round must begin at revision one')
    return {}


def _prepare_observations(spark, binding, message):
    p = message['payload']
    config = _configuration(spark, binding, p['config_revision'])
    rounds = _find(spark, binding, 'budget_rounds', budget_round_id=p['budget_round_id'])
    if not rounds or rounds[0]['config_revision'] != p['config_revision']:
        raise ValueError('Capture budget round is unavailable at its configuration')
    provenance = ('config_revision', 'source_id', 'source_revision', 'query_id', 'query_revision', 'budget_round_id', 'provider_run_id')
    if any(any(row[key] != p[key] for key in provenance)
           for row in _find(spark, binding, 'discoveries', execution_id=p['execution_id'])):
        raise ValueError('Capture execution identity has conflicting provenance')
    plan = observation_rows(message, config)
    observed_months = {r['observation_key']: r['observation_month'] for r in
        _keys(spark, binding, 'observations', 'observation_key', {r['observation_key'] for r in plan['evaluations']})}
    for evaluation in plan['evaluations']:
        evaluation['observation_month'] = observed_months.get(evaluation['observation_key'], evaluation['observation_month'])
    # An unchanged capture can add provenance, not another semantic work item
    # or an empty case merely because it was captured in a later window.
    existing_work = {r['work_key'] for r in _keys(spark, binding, 'evaluations', 'work_key', {r['work_key'] for r in plan['evaluations']})}
    plan['evaluations'] = [row for row in plan['evaluations'] if row['work_key'] not in existing_work]
    active_cases = {row['investigation_id'] for row in plan['evaluations']}
    plan['investigations'] = [row for row in plan['investigations'] if row['investigation_id'] in active_cases]
    incoming = {r['document_key']: r for r in plan['document_current']}
    parent_keys = {r['parent_document_key'] for r in plan['observations'] if r['parent_document_key']}
    parents = {r['document_key']: r for r in _keys(spark, binding, 'document_current', 'document_key', parent_keys)}
    for row in plan['observations']:
        parent = row['parent_document_key']
        if parent:
            candidate = incoming.get(parent, parents.get(parent))
            if not candidate or candidate['document_type'] != 'post' or candidate['platform'] != row['platform']:
                raise ValueError('Comment parent is absent or belongs to another platform')
    return plan


def _check_execution_captures(spark, binding, p):
    for message_id in p['observation_message_ids']:
        rows = _find(spark, binding, 'receipts', message_id=message_id)
        if len(rows) != 1 or rows[0]['kind'] != 'observations' or rows[0]['state'] not in ('APPLIED', 'READY'):
            raise ValueError('Execution references an uncommitted observation message')
        capture = json.loads(rows[0]['message_json'])['payload']
        if any(capture[key] != p[key] for key in ('execution_id', 'budget_round_id', 'config_revision', 'query_id', 'query_revision')):
            raise ValueError('Execution provenance does not match observations')


def _prepare_execution(spark, binding, message):
    p = message['payload']
    config = _configuration(spark, binding, p['config_revision'])
    rounds = _find(spark, binding, 'budget_rounds', budget_round_id=p['budget_round_id'])
    if not rounds or rounds[0]['config_revision'] != p['config_revision']:
        raise ValueError('Execution budget round is unavailable')
    query = next((q for q in config['queries'] if q['query_id'] == p['query_id'] and q['revision'] == p['query_revision']), None)
    if query is None:
        raise ValueError('Execution query revision is unavailable')
    if p['request_id'] is not None:
        requests = _find(spark, binding, 'requests', request_id=p['request_id'])
        if len(requests) != 1 or any(requests[0][key] != p[key] for key in ('config_revision', 'query_id', 'query_revision', 'budget_round_id')):
            raise ValueError('Execution references an unavailable or mismatched investigation request')
    prior = _find(spark, binding, 'executions', execution_id=p['execution_id'])
    if prior and prior[0]['status'] != 'uncertain' and prior[0]['payload_json'] != canonical(p):
        raise ValueError('Terminal execution result is immutable')
    _check_execution_captures(spark, binding, p)
    return {}


def _prepare(spark, binding, message):
    """Validate business references before any business MERGE."""
    handlers = {'configuration': _prepare_configuration, 'budget_round': _prepare_budget_round,
                'observations': _prepare_observations, 'execution_result': _prepare_execution}
    handler = handlers.get(message['kind'])
    if handler is None:
        raise NotImplementedError('Sonda result application is not implemented for ' + message['kind'])
    return handler(spark, binding, message)


def _effect(spark, binding, message, plan):
    kind, p = message['kind'], message['payload']
    if kind == 'configuration':
        _rows(spark, binding, 'registry', [{'config_revision': p['revision'], 'payload_sha256': fingerprint(p), 'payload_json': canonical(p), 'message_json': canonical(message)}], ('config_revision',))
        _rows(spark, binding, 'tool_catalog', plan['tool_catalog'], ('snapshot_key',))
        for domain, key in (('sources', 'source_id'), ('queries', 'query_id'), ('criteria', 'criterion_id')):
            rows = [{'snapshot_key': fingerprint([p['revision'], r[key]]), 'config_revision': p['revision'], 'record_id': r[key],
                     'record_revision': r['revision'], 'status': r['status'], 'approval_status': r['approval']['status'],
                     'record_sha256': fingerprint(r), 'payload_json': canonical(r)} for r in p[domain]]
            _rows(spark, binding, domain, rows, ('snapshot_key',))
        decisions = []
        for decision in p.get('proposal_decisions', []):
            prior = _find(spark, binding, 'proposals', proposal_id=decision['proposal_id'])
            decisions.append({'proposal_id': decision['proposal_id'], 'base_config_revision': prior[0]['base_config_revision'] if prior else None,
                'state': decision['decision'], 'payload_json': prior[0]['payload_json'] if prior else None, 'decision_json': canonical(decision)})
        _rows(spark, binding, 'proposals', decisions, ('proposal_id',), update=True)
    elif kind == 'observations':
        for suffix, keys in (('observations', ('observation_key',)), ('discoveries', ('discovery_key',)),
                             ('investigations', ('investigation_id',)), ('evaluations', ('work_key',))):
            _rows(spark, binding, suffix, plan[suffix], keys)
        _rows(spark, binding, 'document_current', plan['document_current'], ('document_key',), update=True,
              condition='s.observed_at > t.observed_at OR (s.observed_at = t.observed_at AND s.observation_key >= t.observation_key)')
        # A late capture must not replace current content, but still advances last seen.
        from delta.tables import DeltaTable
        target = _table(binding, 'document_current')
        frame = spark.createDataFrame([{**_scope(binding), **r} for r in plan['document_current']], spark.table(target).schema)
        path = definitions(binding)['ai_social_document_current']['location']
        (DeltaTable.forPath(spark, path).alias('t').merge(frame.alias('s'), 't.document_key=s.document_key AND t.registry_id=s.registry_id AND t.generation_id=s.generation_id')
            .whenMatchedUpdate(set={'last_seen_at': 'greatest(t.last_seen_at, s.last_seen_at)'}).execute())
        _mark_case_work(spark, binding, plan.get('_case_ids', {r['investigation_id'] for r in plan['evaluations']}), fingerprint(['capture', message['message_id']]))
    elif kind == 'budget_round':
        row = {key: p[key] for key in ('budget_round_id', 'revision', 'config_revision', 'status',
            'remaining_microusd', 'research_remaining_microusd', 'remaining_items')}
        row.update(opened_at=utc(p['opened_at']), expires_at=utc(p['expires_at']),
                   payload_sha256=fingerprint(p), payload_json=canonical(p), message_json=canonical(message))
        _rows(spark, binding, 'budget_round_revisions', [row], ('budget_round_id', 'revision'))
        _rows(spark, binding, 'budget_rounds', [row], ('budget_round_id',), update=True, condition='s.revision > t.revision')
    elif kind == 'execution_result':
        row = {key: p[key] for key in ('execution_id', 'status', 'config_revision', 'query_id', 'query_revision', 'record_count', 'spent_microusd')}
        row['payload_json'] = canonical(p)
        row['message_json'] = canonical(message)
        _rows(spark, binding, 'executions', [row], ('execution_id',), update=True)
        if p['request_id'] is not None:
            request = _find(spark, binding, 'requests', request_id=p['request_id'])[0]
            request['state'] = p['status']
            _rows(spark, binding, 'requests', [request], ('request_id',), update=True)
            _mark_case_work(spark, binding, {request['investigation_id']}, fingerprint(['execution', message['message_id']]))


def _dirty_dates(spark, binding, plan):
    dates = {r['published_date'].isoformat() if r['published_date'] else None for r in plan.get('document_current', [])}
    for old in _keys(spark, binding, 'document_current', 'document_key', {r['document_key'] for r in plan.get('document_current', [])}):
        dates.add(old['published_date'].isoformat() if old['published_date'] else None)
    return sorted(dates, key=lambda value: value or '')


def _refresh_daily(spark, binding, dates):
    if not dates:
        return
    from pyspark.sql import functions as F
    predicate = F.col('published_date').isin([d for d in dates if d is not None])
    if None in dates:
        predicate = predicate | F.col('published_date').isNull()
    source = spark.table(_table(binding, 'document_current')).where(predicate)
    dims = ['registry_id', 'generation_id', 'document_type', 'platform', 'published_date']
    metrics = ('likes', 'comments', 'shares', 'views')
    aggregate = source.groupBy(*dims).agg(F.count('*').alias('document_count'),
        *[F.sum(k).alias(k) for k in metrics],
        *[F.sum(F.when(F.col(k).isNull(), 1).otherwise(0)).alias('missing_' + k) for k in metrics])
    # Retain zero-count tombstone groups when a current document moves date/type.
    previous = spark.table(_table(binding, 'daily')).where(predicate).select(*dims)
    dimensions = aggregate.select(*dims).union(previous).distinct()
    joined = dimensions.alias('d').join(aggregate.alias('a'), [F.col('d.' + k).eqNullSafe(F.col('a.' + k)) for k in dims], 'left')
    frame = joined.select(*[F.col('d.' + k).alias(k) for k in dims], F.coalesce(F.col('a.document_count'), F.lit(0)).alias('document_count'),
        *[F.col('a.' + k).alias(k) for k in metrics],
        *[F.coalesce(F.col('a.missing_' + k), F.lit(0)).alias('missing_' + k) for k in metrics])
    frame = frame.withColumn('published_month', F.coalesce(F.date_format('published_date', 'yyyy-MM'), F.lit('undated')))
    frame = frame.withColumn('day_key', F.sha2(F.to_json(F.struct(*dims), {'ignoreNullFields': 'false'}), 256))
    _merge(spark, definitions(binding)['ai_social_daily']['location'],
        frame.select(*spark.table(_table(binding, 'daily')).columns), ('day_key',), update=True)


def _publish_receipt(spark, binding, receipt):
    receipt['state'] = 'APPLIED' if receipt['code'] == 'applied' else 'REJECTED'
    payload = {'source_message_id': receipt['message_id'], 'status': 'applied' if receipt['state'] == 'APPLIED' else 'rejected',
               'code': receipt['code'], 'explanation': receipt['explanation']}
    key = fingerprint(['processing_receipt', receipt['message_id']])
    if receipt.get('source_sequence') is not None:
        payload['source_sequence'] = receipt['source_sequence']
        payload = _wire_message(binding, 'processing_receipt', payload, key, receipt.get('received_at') or clock())
    _rows(spark, binding, 'outbox', [{'outbox_key': fingerprint(['processing_receipt', receipt['message_id']]),
        'source_message_id': receipt['message_id'], 'kind': 'processing_receipt',
        'status': 'ready' if receipt.get('source_sequence') is not None else 'awaiting_transport_sequence',
        'payload_json': canonical(payload)}], ('outbox_key',), update=True,
        condition="t.status='awaiting_transport_sequence' AND s.status='ready'")
    _rows(spark, binding, 'receipts', [receipt], ('message_id',), update=True)


def _publish_study_outputs(spark, binding, studies, table):
    for study in studies:
        for output in _study_outbox(binding, json.loads(study['result_json']), study):
            _rows(spark, binding, 'outbox', [output], ('outbox_key',))
        study['state'] = 'APPLIED'
        _rows(spark, binding, table, [study], ('result_id',), update=True)


def sync_tool_catalog(spark, binding):
    """Recover the current derived snapshot without resetting configuration/history.

    Called under the writer lease after interrupted inbound effects are repaired.
    Earlier snapshots remain immutable; partial inserts replay the same keys.
    """
    revision = _latest_revision(spark, binding)
    if not revision:
        return False
    expected = {row['snapshot_key']: row for row in
        tool_catalog_rows(_configuration(spark, binding, revision), validated=True)}
    existing = _find(spark, binding, 'tool_catalog', config_revision=revision)
    for row in existing:
        actual = {key: value for key, value in row.items() if key not in _scope(binding)}
        if actual != expected.get(row['snapshot_key']):
            raise ValueError('Conflicting immutable tool catalog snapshot')
    present = {row['snapshot_key'] for row in existing}
    missing = [row for key, row in expected.items() if key not in present]
    _rows(spark, binding, 'tool_catalog', missing, ('snapshot_key',))
    return bool(missing)


def _reusable_publication(existing, pending, study_pending, case_pending, catalog_changed):
    """A migration must publish the new metadata table even with no new messages."""
    return (bool(existing) and not (pending or study_pending or case_pending or catalog_changed)
        and {'ai_social_tool_catalog', 'ai_social_source_limits'} <= set(json.loads(existing[0]['versions_json'])))


def publish(spark, binding, *, metadata_changed=False):
    """Publish committed versions, then terminal receipts. Never writes stream ack."""
    from pyspark.sql import functions as F
    checked_binding(binding)
    journals = _pending(spark, binding, ['APPLYING', 'READY'])
    studies = _study_pending(spark, binding)
    case_studies = _case_study_pending(spark, binding)
    if any(row['state'] == 'APPLYING' for row in journals + studies + case_studies):
        raise RuntimeError('An interrupted message must be replayed before publication')
    pending = [r for r in journals if r['state'] == 'READY']
    study_pending = [r for r in studies if r['state'] == 'READY']
    case_pending = [r for r in case_studies if r['state'] == 'READY']
    existing = _find(spark, binding, 'publication')
    # Recheck the references used by all pending operations before serving them.
    for receipt in pending:
        if receipt['code'] == 'applied':
            _prepare(spark, binding, json.loads(receipt['message_json']))
    for study in study_pending:
        _prepare_study(spark, binding, json.loads(study['result_json']))
    for study in case_pending:
        _prepare_case_result(spark, binding, json.loads(study['result_json']), study['predecessor_revision'], replay=study, batch_size=study['batch_size'])
    catalog_changed = sync_tool_catalog(spark, binding)
    if _reusable_publication(existing, pending, study_pending, case_pending, catalog_changed or metadata_changed):
        return json_mode(existing[0])
    dates = {date for row in pending for date in json.loads(row['dirty_dates_json'])}
    _refresh_daily(spark, binding, sorted(dates, key=lambda d: d or ''))
    versions = {}
    for name, definition in definitions(binding).items():
        if name not in ('ai_social_publication', 'ai_social_receipts', 'ai_social_outbox', 'ai_social_study_results', 'ai_social_case_study_results'):
            versions[name] = int(spark.sql('DESCRIBE HISTORY ' + definition['table'] + ' LIMIT 1').first()['version'])
    revision = _latest_revision(spark, binding)
    ids = sorted([row['message_id'] for row in pending] + ['study:' + r['result_id'] for r in study_pending] + ['case:' + r['result_id'] for r in case_pending])
    publication_id = fingerprint([_scope(binding), revision, versions])
    row = {'publication_id': publication_id, 'config_revision': revision, 'versions_json': canonical(versions),
           'message_ids_json': canonical(ids), 'published_at': datetime.now(timezone.utc)}
    _rows(spark, binding, 'publication', [row], (), update=True)
    for receipt in pending:
        _publish_receipt(spark, binding, receipt)
    _publish_study_outputs(spark, binding, study_pending, 'study_results')
    _publish_study_outputs(spark, binding, case_pending, 'case_study_results')
    return json_mode({**_scope(binding), **row})


def _source_limits_core_receipt(spark, binding, snapshot, message_id):
    configuration = _configuration(spark, binding, snapshot['config_revision'])
    if fingerprint(configuration) != snapshot['configuration_payload_sha256']:
        raise ValueError('Source limits require their exact historical configuration')
    registry = _find(spark, binding, 'registry', config_revision=snapshot['config_revision'])[0]
    if json.loads(registry['message_json'])['message_id'] != message_id:
        raise ValueError('Source limits configuration message identity mismatch')
    receipts = _find(spark, binding, 'receipts', message_id=message_id)
    if len(receipts) != 1 or receipts[0]['state'] != 'APPLIED' or receipts[0]['code'] != 'applied':
        raise ValueError('Source limits require an APPLIED core configuration')
    receipt = receipts[0]
    if receipt.get('source_sequence') is None or receipt.get('raw_sha256') is None:
        raise ValueError('Applied configuration lacks protected transport identity')
    return receipt


def _source_limits_successor(spark, binding, snapshot, row):
    from pyspark.sql import functions as F
    existing = _find(spark, binding, 'source_limits', catalog_revision=row['catalog_revision'])
    if existing:
        if len(existing) != 1 or {key: existing[0][key] for key in row} != row:
            raise ValueError('Conflicting immutable source limits snapshot')
        return False
    revision = int(spark.table(_table(binding, 'source_limits')).agg(F.max('catalog_revision')).first()[0] or 0)
    previous = _find(spark, binding, 'source_limits', catalog_revision=revision)[0] if revision else None
    digest = previous['snapshot_sha256'] if previous else None
    if snapshot['catalog_revision'] != revision + 1 or snapshot['previous_catalog_sha256'] != digest:
        raise ValueError('Source limits must extend their applied predecessor')
    return True


def apply_source_limits(spark, binding, snapshot, *, snapshot_sha256, core_message_id):
    """Commit an already-validated sidecar under the generation writer lease.

    The protected transport adapter validates policies and all raw proofs first.
    This function enforces historical core application and immutable Delta replay;
    it neither creates a storage ACK nor grants broker execution.
    """
    checked_binding(binding)
    if any(snapshot.get(key) != value for key, value in _scope(binding).items()):
        raise ValueError('Source limits snapshot scope mismatch')
    if fingerprint(snapshot) != snapshot_sha256:
        raise ValueError('Source limits snapshot fingerprint mismatch')
    receipt = _source_limits_core_receipt(spark, binding, snapshot, core_message_id)
    row = {key: snapshot[key] for key in ('catalog_revision', 'config_revision', 'configuration_payload_sha256')}
    row.update(snapshot_sha256=snapshot_sha256, payload_json=canonical(snapshot),
        core_configuration_message_id=core_message_id,
        core_configuration_source_sequence=receipt['source_sequence'], core_configuration_raw_sha256=receipt['raw_sha256'])
    changed = _source_limits_successor(spark, binding, snapshot, row)
    if changed:
        _rows(spark, binding, 'source_limits', [row], ('catalog_revision',))
    publication = publish(spark, binding, metadata_changed=True)
    return {'publication': publication, 'snapshot_row': {**_scope(binding), **row}}


def _check_inbound_message(binding, message, source_sequences, source_hashes):
    if any(message.get(key) != value for key, value in _scope(binding).items()) or message.get('contract_sha256') != CONTRACT_SHA256 or message.get('contract_version') != '1.0.2':
        raise ValueError('Message scope or pinned contract mismatch')
    if message.get('kind') not in ('configuration', 'budget_round', 'observations', 'execution_result') or message.get('direction') != 'huginn_to_aidp':
        raise NotImplementedError('Only inbound Sonda persistence is implemented')
    sequence = (source_sequences or {}).get(message['message_id'])
    raw_hash = (source_hashes or {}).get(message['message_id'])
    if sequence is not None and (type(sequence) is not int or not 1 <= sequence <= 9007199254740991):
        raise ValueError('Invalid committed source sequence')
    if raw_hash is not None and (not isinstance(raw_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', raw_hash)):
        raise ValueError('Invalid committed source body hash')


def _bind_receipt_transport(spark, binding, found, sequence, raw_hash):
    if not found:
        return found
    prior = found[0]
    metadata = {'source_sequence': sequence, 'raw_sha256': raw_hash}
    for key, value in metadata.items():
        if value is not None and prior.get(key) not in (None, value):
            raise ValueError('Conflicting committed transport identity')
    if any(value is not None and prior.get(key) is None for key, value in metadata.items()):
        prior = dict(prior)
        prior.update({key: value for key, value in metadata.items() if value is not None})
        if prior['state'] in ('APPLIED', 'REJECTED'):
            prior['state'] = 'READY'
        _rows(spark, binding, 'receipts', [prior], ('message_id',), update=True)
        return [prior]
    return found


def _commit_inbound_effects(spark, binding, message, receipt, found):
    try:
        plan = _prepare(spark, binding, message)
    except ValueError as exc:
        if found and found[0]['state'] == 'APPLYING':
            raise RuntimeError('Interrupted business effects require reconciliation; journal remains APPLYING') from exc
        receipt.update(state='READY', code='invalid_reference', explanation='Business references or immutable revision conflict')
        _rows(spark, binding, 'receipts', [receipt], ('message_id',), update=True)
    else:
        dates = _dirty_dates(spark, binding, plan)
        if found:
            dates = sorted(set(dates + json.loads(found[0]['dirty_dates_json'])), key=lambda d: d or '')
        receipt['dirty_dates_json'] = canonical(dates)
        cases = {r['investigation_id'] for r in plan.get('evaluations', [])}
        if found:
            cases.update(json.loads(found[0].get('dirty_cases_json') or '[]'))
        plan['_case_ids'] = cases
        receipt['dirty_cases_json'] = canonical(sorted(cases))
        _rows(spark, binding, 'receipts', [receipt], ('message_id',), update=True)
        _effect(spark, binding, message, plan)
        receipt['state'] = 'READY'
        _rows(spark, binding, 'receipts', [receipt], ('message_id',), update=True)


def _apply_inbound_message(spark, binding, message, source_sequences, source_hashes):
    sequence = (source_sequences or {}).get(message['message_id'])
    raw_hash = (source_hashes or {}).get(message['message_id'])
    found = _find(spark, binding, 'receipts', message_id=message['message_id'])
    digest = fingerprint(message)
    if found and found[0]['message_sha256'] != digest:
        raise ValueError('Conflicting message identity')
    found = _bind_receipt_transport(spark, binding, found, sequence, raw_hash)
    if found and found[0]['state'] in ('APPLIED', 'REJECTED', 'READY'):
        return
    receipt = {'message_id': message['message_id'], 'message_sha256': digest, 'kind': message['kind'],
        'state': 'APPLYING', 'code': 'applied', 'explanation': 'Business effects committed',
        'message_json': canonical(message), 'dirty_dates_json': '[]', 'dirty_cases_json': '[]',
        'source_sequence': sequence, 'raw_sha256': raw_hash, 'received_at': clock()}
    if found:
        receipt['source_sequence'] = found[0].get('source_sequence') or receipt['source_sequence']
        receipt['raw_sha256'] = found[0].get('raw_sha256') or receipt['raw_sha256']
        receipt['received_at'] = found[0].get('received_at') or receipt['received_at']
    _commit_inbound_effects(spark, binding, message, receipt, found)


def apply_messages(spark, binding, messages, *, source_sequences=None, source_hashes=None):
    """Apply a bounded ordered batch and recover dirty work; external lease required.

    Raises on scope/identity corruption (never acknowledges it). A valid scoped
    business message with invalid references receives a durable rejection. The
    transport must supply source_sequence when wrapping receipt outbox records;
    these are receipt payloads awaiting that metadata, not fabricated wire DTOs.
    """
    checked_binding(binding)
    if not isinstance(messages, list) or len(messages) > 100:
        raise ValueError('Expected at most 100 validated messages')
    for message in messages:
        _check_inbound_message(binding, message, source_sequences, source_hashes)
    _recover_studies(spark, binding)
    _recover_case_studies(spark, binding)
    dirty = _pending(spark, binding, ['APPLYING'])
    if len(dirty) > 1:
        raise RuntimeError('Multiple interrupted writers require operator reconciliation')
    incoming = [json.loads(r['message_json']) for r in dirty] + messages
    results = []
    for message in incoming:
        _apply_inbound_message(spark, binding, message, source_sequences, source_hashes)
    publication = publish(spark, binding)
    for message in messages:
        results.append(_find(spark, binding, 'receipts', message_id=message['message_id'])[0])
    return json_mode({'publication': publication, 'receipts': results})


def clock():
    return datetime.now(timezone.utc)


def _study_pending(spark, binding):
    from pyspark.sql import functions as F
    frame = spark.table(_table(binding, 'study_results'))
    for key, value in _scope(binding).items():
        frame = frame.where(F.col(key) == value)
    rows = frame.where(F.col('state').isin('APPLYING', 'READY')).limit(2).collect()
    if len(rows) > 1:
        raise RuntimeError('Multiple study writers require reconciliation')
    return [r.asDict() for r in rows]


def _record_config(spark, binding, config_revision, domain, key, identifier, revision):
    config = _configuration(spark, binding, config_revision)
    record = next((r for r in config[domain] if r[key] == identifier and r['revision'] == revision), None)
    if record is None:
        raise ValueError('Study references an unavailable managed revision')
    return config, record


def _checked_study_scope(binding, result):
    state = result.get('state', {})
    expected = {**_scope(binding), 'contract_version': '1.0.2', 'contract_sha256': CONTRACT_SHA256}
    if state.get('scope') != expected:
        raise ValueError('Study result scope or pinned contract mismatch')
    if set(result) != {'state', 'evaluations', 'investigation_requests', 'reports', 'proposals'}:
        raise ValueError('Unsupported study result shape')
    if any(not isinstance(result[key], list) or len(result[key]) > 1000
           for key in ('evaluations', 'investigation_requests', 'reports', 'proposals')):
        raise ValueError('Study result exceeds its bounded batch')
    return state


def _study_reference(spark, binding, plan, domain, identity):
    table, key, error = {
        'observations': ('observations', 'observation_key', 'Study observation is not committed'),
        'cases': ('investigations', 'investigation_id', 'Study investigation is not committed')}[domain]
    cache = plan[domain]
    if identity not in cache:
        rows = _find(spark, binding, table, **{key: identity})
        if len(rows) != 1:
            raise ValueError(error)
        cache[identity] = rows[0]
    return cache[identity]


def _study_quote(spark, binding, plan, item):
    evidence = plan['evidence']
    observed = _study_reference(spark, binding, plan, 'observations', item['observation_key'])
    if (item['document_key'] != observed['document_key'] or item['url'] != observed['url']
            or not any(item['quote'] in (observed[field] or '') for field in ('text', 'title'))
            or item['evidence_id'] != fingerprint([item['observation_key'], item['quote']])):
        raise ValueError('Study evidence does not quote the committed observation')
    if item['evidence_id'] in evidence and evidence[item['evidence_id']] != item:
        raise ValueError('Conflicting evidence identity')
    prior = _find(spark, binding, 'evidence', evidence_id=item['evidence_id'])
    if prior and any(prior[0][key] != item[key] for key in ('observation_key', 'document_key', 'url', 'quote')):
        raise ValueError('Immutable evidence identity conflict')
    evidence[item['evidence_id']] = item


def _study_evidence_exists(spark, binding, plan, key):
    return key in plan['evidence'] or bool(_find(spark, binding, 'evidence', evidence_id=key))


def _check_evaluation_work(spark, binding, evaluation, parent):
    work_key = fingerprint([evaluation['observation_key'], evaluation['criterion_id'], evaluation['criterion_revision'], evaluation['rule_sha256']])
    jobs = _find(spark, binding, 'evaluations', work_key=work_key)
    resumed = False
    if len(jobs) == 1 and jobs[0]['investigation_id'] != parent['investigation_id']:
        for discovery in _find(spark, binding, 'discoveries', observation_key=evaluation['observation_key']):
            for execution in _find(spark, binding, 'executions', execution_id=discovery['execution_id']):
                payload = json.loads(execution['payload_json'])
                if payload.get('request_id'):
                    requests = _find(spark, binding, 'requests', request_id=payload['request_id'])
                    resumed = resumed or (len(requests) == 1 and requests[0]['investigation_id'] == parent['investigation_id'])
    if len(jobs) != 1 or (jobs[0]['investigation_id'] != parent['investigation_id'] and not resumed):
        raise ValueError('Evaluation has no matching committed work/case identity')


def _prepare_study_evaluation(spark, binding, state, plan, evaluation):
    evaluations = plan['evaluations']
    observed = _study_reference(spark, binding, plan, 'observations', evaluation['observation_key'])
    _, criterion = _record_config(spark, binding, evaluation['config_revision'], 'criteria', 'criterion_id',
                                 evaluation['criterion_id'], evaluation['criterion_revision'])
    if evaluation['rule_sha256'] != fingerprint(criterion) or evaluation['threshold'] != criterion['threshold']:
        raise ValueError('Evaluation rule fingerprint or threshold differs from captured criterion')
    case_id = state.get('evaluation_cases', {}).get(evaluation['evaluation_key'])
    parent = _study_reference(spark, binding, plan, 'cases', case_id)
    if parent['criterion_id'] != criterion['criterion_id'] or parent['criterion_revision'] != criterion['revision']:
        raise ValueError('Evaluation references a different case criterion')
    expected_key = fingerprint([1, evaluation['observation_key'], evaluation['criterion_id'],
        evaluation['criterion_revision'], evaluation['rule_sha256'], evaluation['model_id'], evaluation['model_revision'], evaluation['prompt_version']])
    if evaluation['evaluation_key'] != expected_key:
        raise ValueError('Evaluation does not bind its model and prompt revisions')
    _check_evaluation_work(spark, binding, evaluation, parent)
    prior = _find(spark, binding, 'evaluation_results', evaluation_key=evaluation['evaluation_key'])
    if prior and prior[0]['payload_sha256'] != fingerprint(evaluation):
        raise ValueError('Immutable evaluation result conflict')
    for item in evaluation['evidence']:
        _study_quote(spark, binding, plan, item)
    evaluations[evaluation['evaluation_key']] = (evaluation, parent, observed)


def _prepare_study_report(spark, binding, plan, report):
    evaluations = plan['evaluations']
    parent = _study_reference(spark, binding, plan, 'cases', report['investigation_id'])
    if any(report[key] != parent[key] for key in ('config_revision', 'criterion_id', 'criterion_revision')):
        raise ValueError('Report references a different case revision')
    prior = _find(spark, binding, 'reports', report_id=report['report_id'])
    same = [r for r in prior if r['revision'] == report['revision']]
    if same and same[0]['payload_json'] != canonical(report):
        raise ValueError('Immutable report revision conflict')
    if not same and report['revision'] != max([r['revision'] for r in prior] or [0]) + 1:
        raise ValueError('Report does not extend its predecessor')
    for item in report['evidence']:
        _study_quote(spark, binding, plan, item)
    for evaluation in report['evaluations']:
        entry = evaluations.get(evaluation['evaluation_key'])
        stored = _find(spark, binding, 'evaluation_results', evaluation_key=evaluation['evaluation_key']) if entry is None else []
        if entry:
            valid = entry[0] == evaluation and entry[1]['investigation_id'] == parent['investigation_id']
        else:
            valid = len(stored) == 1 and stored[0]['investigation_id'] == parent['investigation_id'] and stored[0]['payload_json'] == canonical(evaluation)
        if not valid:
            raise ValueError('Report evaluation is absent or belongs to a different case')


def _prepare_study_proposal(spark, binding, plan, proposal):
    _study_reference(spark, binding, plan, 'cases', proposal['investigation_id'])
    config = _configuration(spark, binding, proposal['base_config_revision'])
    if any(not _study_evidence_exists(spark, binding, plan, key) for key in proposal['evidence_ids']):
        raise ValueError('Proposal references uncommitted evidence')
    record = proposal['change']['record']
    source_ids = {r['source_id'] for r in config['sources']}
    query_ids = {r['query_id'] for r in config['queries']}
    if record['origin'] != 'agent' or record['approval']['status'] != 'pending' or record['status'] == 'active':
        raise ValueError('An agent proposal cannot approve itself')
    if proposal['change']['target_kind'] == 'criterion':
        valid = set(record['source_ids']) <= source_ids and set(record['query_ids']) <= query_ids
    else:
        valid = record['source_id'] in source_ids
    if not valid:
        raise ValueError('Proposed record has unavailable source/query references')
    prior = _find(spark, binding, 'proposals', proposal_id=proposal['proposal_id'])
    if prior and prior[0]['payload_json'] not in (None, canonical(proposal)):
        raise ValueError('Immutable proposal identity conflict')


def _approved_study_request(spark, binding, request, parent):
    config, criterion = _record_config(spark, binding, request['config_revision'], 'criteria', 'criterion_id', request['criterion_id'], request['criterion_revision'])
    _, query = _record_config(spark, binding, request['config_revision'], 'queries', 'query_id', request['query_id'], request['query_revision'])
    source = next(r for r in config['sources'] if r['source_id'] == query['source_id'])
    connector = next(r for r in config['connectors'] if r['connector_id'] == source['connector_id'])
    if (request['config_revision'] != _latest_revision(spark, binding)
            or any(r['status'] != 'active' or r['approval']['status'] != 'approved' for r in (criterion, query, source))
            or not criterion['allow_research'] or not query['research_allowed']
            or parent['criterion_revision'] != criterion['revision'] or parent['criterion_id'] != criterion['criterion_id']
            or query['source_id'] not in criterion['source_ids'] or (criterion['query_ids'] and query['query_id'] not in criterion['query_ids'])):
        raise ValueError('Additional work is not approved by the current configuration')
    return query, connector


def _request_budget(spark, binding, request):
    rounds = _find(spark, binding, 'budget_rounds', budget_round_id=request['budget_round_id'])
    budget = rounds[0] if len(rounds) == 1 else None
    if (budget is None or budget['config_revision'] != request['config_revision'] or budget['status'] != 'open'
            or budget['expires_at'].replace(tzinfo=timezone.utc) <= clock()
            or utc(request['expires_at']) <= clock() or utc(request['expires_at']) > budget['expires_at'].replace(tzinfo=timezone.utc)
            or any(budget[key] is None for key in ('remaining_microusd', 'research_remaining_microusd', 'remaining_items'))):
        raise ValueError('Investigation budget is unavailable, unknown, closed or expired')
    return budget


def _prepare_study_request(spark, binding, plan, reserved, request):
    parent = _study_reference(spark, binding, plan, 'cases', request['investigation_id'])
    query, connector = _approved_study_request(spark, binding, request, parent)
    if any(not _study_evidence_exists(spark, binding, plan, key) for key in request['evidence_ids']):
        raise ValueError('Investigation request references uncommitted evidence')
    prior = _find(spark, binding, 'requests', request_id=request['request_id'])
    if prior and prior[0]['payload_sha256'] != fingerprint(request):
        raise ValueError('Immutable investigation request conflict')
    if prior:
        return  # replay cannot reserve or start the same request again
    budget = _request_budget(spark, binding, request)
    cost, items = reserved.get(request['budget_round_id'], (0, 0))
    cost += request['max_cost_microusd']
    items += request['max_items']
    if (cost > min(budget['remaining_microusd'], budget['research_remaining_microusd']) or items > budget['remaining_items']
            or request['max_cost_microusd'] > query['max_charge_microusd'] or request['max_items'] > query['max_items']
            or connector['max_cost_microusd_per_item'] is None
            or request['max_cost_microusd'] < connector['max_cost_microusd_per_item'] * request['max_items']):
        raise ValueError('Investigation exceeds its approved limits')
    reserved[request['budget_round_id']] = (cost, items)


def _prepare_study(spark, binding, result):
    """Check previously typed study DTOs against committed business references."""
    state = _checked_study_scope(binding, result)
    plan = {'observations': {}, 'cases': {}, 'evidence': {}, 'evaluations': {}}
    for evaluation in result['evaluations']:
        _prepare_study_evaluation(spark, binding, state, plan, evaluation)
    for report in result['reports']:
        _prepare_study_report(spark, binding, plan, report)
    for proposal in result['proposals']:
        _prepare_study_proposal(spark, binding, plan, proposal)
    reserved = {}
    for request in result['investigation_requests']:
        _prepare_study_request(spark, binding, plan, reserved, request)
    return plan


def _effect_study(spark, binding, result, journal, plan, *, persist_generation_state=True):
    for item in plan['evidence'].values():
        row = dict(item, captured_at=utc(item['captured_at']))
        _rows(spark, binding, 'evidence', [row], ('evidence_id',))
    for evaluation, parent, observed in plan['evaluations'].values():
        row = {key: evaluation[key] for key in ('evaluation_key', 'observation_key', 'criterion_id', 'criterion_revision',
            'config_revision', 'rule_sha256', 'model_id', 'model_revision', 'prompt_version', 'score', 'decision')}
        row.update(investigation_id=parent['investigation_id'], evaluated_at=utc(evaluation['evaluated_at']),
            payload_sha256=fingerprint(evaluation), payload_json=canonical(evaluation), observation_month=observed['observation_month'])
        _rows(spark, binding, 'evaluation_results', [row], ('evaluation_key',))
        work_key = fingerprint([evaluation['observation_key'], evaluation['criterion_id'], evaluation['criterion_revision'], evaluation['rule_sha256']])
        jobs = _find(spark, binding, 'evaluations', work_key=work_key)
        if len(jobs) != 1:
            raise RuntimeError('Committed evaluation has no pending work identity')
        job = jobs[0]
        job.update(state='completed' if evaluation['decision'] != 'error' else 'error',
            investigation_id=parent['investigation_id'],
            **{k: evaluation[k] for k in ('evaluation_key', 'model_id', 'model_revision', 'prompt_version', 'score', 'decision')},
            payload_json=canonical(evaluation))
        _rows(spark, binding, 'evaluations', [job], ('work_key',), update=True)
    for report in result['reports']:
        row = {k: report[k] for k in ('report_id', 'investigation_id', 'revision', 'config_revision', 'status')}
        row['payload_json'] = canonical(report)
        _rows(spark, binding, 'reports', [row], ('report_id', 'revision'))
        parent = dict(plan['cases'][report['investigation_id']], state=report['status'])
        _rows(spark, binding, 'investigations', [parent], ('investigation_id',), update=True)
    for proposal in result['proposals']:
        prior = _find(spark, binding, 'proposals', proposal_id=proposal['proposal_id'])
        row = {'proposal_id': proposal['proposal_id'], 'base_config_revision': proposal['base_config_revision'],
               'state': prior[0]['state'] if prior and prior[0]['decision_json'] else 'pending',
               'payload_json': canonical(proposal), 'decision_json': prior[0]['decision_json'] if prior else None}
        _rows(spark, binding, 'proposals', [row], ('proposal_id',), update=True)
    for request in result['investigation_requests']:
        row = {k: request[k] for k in ('request_id', 'investigation_id', 'config_revision', 'query_id', 'query_revision', 'budget_round_id')}
        row.update(state='pending', expires_at=utc(request['expires_at']), payload_sha256=fingerprint(request), payload_json=canonical(request))
        _rows(spark, binding, 'requests', [row], ('request_id',))
    if persist_generation_state:
        _rows(spark, binding, 'study_state', [{'result_id': journal['result_id'], 'sequence': journal['sequence'],
            'state_json': canonical(result['state'])}], (), update=True, condition='s.sequence >= t.sequence')


def _study_outbox(binding, result, journal):
    for kind, plural, key in (('report', 'reports', 'report_id'), ('proposal', 'proposals', 'proposal_id'),
                              ('investigation_request', 'investigation_requests', 'request_id')):
        for payload in result[plural]:
            identity = [kind, payload[key], payload.get('revision')]
            outbox_key = fingerprint(identity)
            envelope = _wire_message(binding, kind, payload, outbox_key, journal['created_at'])
            yield {'outbox_key': outbox_key, 'source_message_id': envelope['message_id'], 'kind': kind,
                   'status': 'ready', 'payload_json': canonical(envelope)}


def _recover_studies(spark, binding):
    for journal in _study_pending(spark, binding):
        if journal['state'] != 'APPLYING':
            continue
        result = json.loads(journal['result_json'])
        try:
            plan = _prepare_study(spark, binding, result)
        except ValueError as exc:
            raise RuntimeError('Interrupted study requires reconciliation; journal remains APPLYING') from exc
        _effect_study(spark, binding, result, journal, plan)
        journal['state'] = 'READY'
        _rows(spark, binding, 'study_results', [journal], ('result_id',), update=True)


def apply_study_results(spark, binding, result, *, result_id):
    """Offline compatibility only: persist the original all-generation study output.

    A caller must validate Evaluation/Report/Proposal/InvestigationRequest DTOs
    first. This performs relational/evidence/approval checks, not LLM execution.
    A study is committed before its ready wire DTOs enter the transport outbox.
    """
    checked_binding(binding)
    _checked_study_scope(binding, result)
    if not isinstance(result_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', result_id):
        raise ValueError('A stable bounded study result identity is required')
    if _pending(spark, binding, ['APPLYING']):
        raise RuntimeError('Recover inbound messages with apply_messages before study results')
    _recover_studies(spark, binding)
    prior = _find(spark, binding, 'study_results', result_id=result_id)
    digest = fingerprint(result)
    if prior:
        if prior[0]['result_sha256'] != digest:
            raise ValueError('Conflicting study result identity')
        return {'publication': publish(spark, binding), 'result_id': result_id, 'state': 'APPLIED'}
    plan = _prepare_study(spark, binding, result)
    current = _find(spark, binding, 'study_state')
    journal = {'result_id': result_id, 'result_sha256': digest, 'result_json': canonical(result),
               'state': 'APPLYING', 'created_at': clock(), 'sequence': (current[0]['sequence'] if current else 0) + 1}
    _rows(spark, binding, 'study_results', [journal], ('result_id',))
    _effect_study(spark, binding, result, journal, plan)
    journal['state'] = 'READY'
    _rows(spark, binding, 'study_results', [journal], ('result_id',), update=True)
    return {'publication': publish(spark, binding), 'result_id': result_id, 'state': 'APPLIED'}


def _wire_message(binding, kind, payload, key, created_at):
    return {**_scope(binding), 'contract_version': '1.0.2', 'contract_sha256': CONTRACT_SHA256,
        'message_id': str(uuid5(UUID(binding['generation_id']), key)),
        'created_at': created_at.replace(tzinfo=timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z'),
        'correlation_id': None, 'kind': kind, 'direction': 'aidp_to_huginn', 'payload': payload}


def _mark_case_work(spark, binding, cases, revision):
    _rows(spark, binding, 'case_work', [{'investigation_id': key, 'input_revision': revision,
        'dirty': True, 'updated_at': clock()} for key in sorted(cases)], ('investigation_id',), update=True)


def _case_study_pending(spark, binding):
    from pyspark.sql import functions as F
    frame = spark.table(_table(binding, 'case_study_results'))
    for key, value in _scope(binding).items():
        frame = frame.where(F.col(key) == value)
    rows = frame.where(F.col('state').isin('APPLYING', 'READY')).limit(101).collect()
    if len(rows) > 100:
        raise RuntimeError('Case result recovery exceeds a bounded batch')
    return [r.asDict() for r in rows]


def _case_result(result):
    """Adapter for shared relational validation; never accumulates generation state."""
    return {**result, 'state': {'scope': result['state']['scope'], 'evaluation_cases':
        {e['evaluation_key']: result['state']['investigation_id'] for e in result['evaluations']}}}


def _check_case_predecessor(state, predecessor_revision):
    if (type(predecessor_revision) is not int or not 0 <= predecessor_revision < 9007199254740991
            or type(state['predecessor_revision']) is not int or type(state['version']) is not int
            or state['predecessor_revision'] != predecessor_revision or state['version'] != predecessor_revision + 1):
        raise ValueError('Case state must extend its predecessor revision')


def _check_case_references(state):
    for key, maximum in (('request_ids', 20), ('proposal_ids', 5)):
        if not isinstance(state[key], list) or len(state[key]) > maximum or len(set(state[key])) != len(state[key]):
            raise ValueError('Case operational references exceed their bounds')
        if any(not isinstance(value, str) or str(UUID(value)) != value for value in state[key]):
            raise ValueError('Case operational references require canonical UUIDs')


def _check_case_outputs(state, result):
    if (len(result['evaluations']) > 100 or sum(len(result[k]) for k in ('evaluations', 'reports', 'proposals', 'investigation_requests')) > 1000
            or len(canonical(result).encode('utf-8')) > 16 * 1024 * 1024):
        raise ValueError('Case result journal exceeds its bounded batch')
    if state['phase'] == 'evaluate' and any(result[k] for k in ('reports', 'proposals', 'investigation_requests')):
        raise ValueError('Evaluate results must commit before reporting')
    if state['phase'] == 'report' and result['evaluations']:
        raise ValueError('Report results cannot introduce unaggregated evaluations')
    if len(result['reports']) > 1 or any(row['investigation_id'] != state['investigation_id']
            for name in ('reports', 'proposals', 'investigation_requests') for row in result[name]):
        raise ValueError('Case outputs belong to another investigation')
    if result['reports'] and result['reports'][0]['revision'] != state['report_revision']:
        raise ValueError('Case state and report revision differ')


def _checked_case_result(binding, result, predecessor_revision):
    _checked_study_scope(binding, result)
    state = result['state']
    fields = {'scope', 'investigation_id', 'version', 'predecessor_revision', 'phase', 'input_revision',
              'last_input_fingerprint', 'report_revision', 'request_ids', 'proposal_ids'}
    if set(state) != fields or len(canonical(state).encode('utf-8')) > 32768:
        raise ValueError('Case state must contain only bounded operational references')
    _check_case_predecessor(state, predecessor_revision)
    for key in ('investigation_id', 'input_revision', 'last_input_fingerprint'):
        if not isinstance(state[key], str) or not re.fullmatch(r'[0-9a-f]{64}', state[key]):
            raise ValueError('Case identity/fingerprint is invalid')
    if state['phase'] not in ('evaluate', 'report') or type(state['report_revision']) is not int or state['report_revision'] < 0:
        raise ValueError('Case phase or report revision is invalid')
    _check_case_references(state)
    _check_case_outputs(state, result)
    return state


def _case_input_revision(signal, phase, keys):
    return fingerprint([signal, phase, list(keys)])


def _case_pending_keys(spark, binding, case_id, batch_size):
    from pyspark.sql import functions as F
    frame = spark.table(_table(binding, 'evaluations'))
    for key, value in _scope(binding).items():
        frame = frame.where(F.col(key) == value)
    return [r['observation_key'] for r in frame.where((F.col('investigation_id') == case_id) & (F.col('state') == 'pending'))
        .orderBy('observation_month', 'observation_key').select('observation_key').limit(batch_size).collect()]


def _check_case_report(spark, binding, state, result):
    if state['phase'] == 'report':
        if not result['reports']:
            raise ValueError('A changed report phase must produce a report, not a successful empty result')
        work = load_case_work(spark, binding, state['investigation_id'])['work']
        if work['phase'] != 'report':
            raise ValueError('Pending evaluations must be committed before report facts')
        for report in result['reports']:
            if (report['coverage']['distinct_documents'] != work['facts']['distinct_documents']
                    or report['coverage']['distinct_observations'] != work['facts']['distinct_observations']
                    or report['coverage']['query_ids'] != work['query_ids'] or report['indicators'] != work['indicators']):
                raise ValueError('Report figures differ from full-population published Gold facts')


def _prepare_case_result(spark, binding, result, predecessor_revision, replay=None, batch_size=10):
    state = _checked_case_result(binding, result, predecessor_revision)
    current = _find(spark, binding, 'study_case_state', investigation_id=state['investigation_id'])
    own_claim = bool(replay and current and current[0]['result_id'] == replay['result_id'] and current[0]['version'] == state['version'])
    if not own_claim and (current[0]['version'] if current else 0) != predecessor_revision:
        raise ValueError('Stale case predecessor revision')
    signals = _find(spark, binding, 'case_work', investigation_id=state['investigation_id'])
    if not own_claim:
        if len(signals) != 1:
            raise ValueError('Case input changed before its result was committed')
        keys = _case_pending_keys(spark, binding, state['investigation_id'], batch_size) if state['phase'] == 'evaluate' else []
        if _case_input_revision(signals[0]['input_revision'], state['phase'], keys) != state['input_revision']:
            raise ValueError('Case input changed before its result was committed')
        if state['phase'] == 'evaluate' and not keys:
            raise ValueError('The evaluation phase has no pending inputs')
    _check_case_report(spark, binding, state, result)
    return _prepare_study(spark, binding, _case_result(result))


def _claim_case_state(spark, binding, result, journal):
    from delta.tables import DeltaTable
    state = result['state']
    row = {**_scope(binding), 'investigation_id': state['investigation_id'], 'version': state['version'],
        'predecessor_revision': state['predecessor_revision'], 'result_id': journal['result_id'],
        'state_json': canonical(state), 'write_status': 'APPLYING', 'updated_at': journal['created_at']}
    target = _table(binding, 'study_case_state')
    frame = spark.createDataFrame([row], spark.table(target).schema)
    path = definitions(binding)['ai_social_study_case_state']['location']
    (DeltaTable.forPath(spark, path).alias('t').merge(frame.alias('s'),
        't.registry_id=s.registry_id AND t.generation_id=s.generation_id AND t.investigation_id=s.investigation_id')
        .whenMatchedUpdateAll(condition='t.version=s.predecessor_revision')
        .whenNotMatchedInsertAll(condition='s.predecessor_revision=0').execute())
    actual = _find(spark, binding, 'study_case_state', investigation_id=state['investigation_id'])
    if len(actual) != 1 or actual[0]['version'] != state['version'] or actual[0]['result_id'] != journal['result_id']:
        raise ValueError('Case predecessor compare-and-swap failed')


def _effect_case_study(spark, binding, result, journal, plan):
    state = result['state']
    _effect_study(spark, binding, _case_result(result), journal, plan, persist_generation_state=False)
    row = _find(spark, binding, 'study_case_state', investigation_id=state['investigation_id'])[0]
    if row['result_id'] != journal['result_id'] or row['version'] != state['version']:
        raise RuntimeError('Case claim changed during business writes')
    row['write_status'] = 'READY'
    _rows(spark, binding, 'study_case_state', [row], ('investigation_id',), update=True,
          condition='t.result_id=s.result_id AND t.version=s.version')
    if state['phase'] == 'evaluate':
        _mark_case_work(spark, binding, {state['investigation_id']}, fingerprint(['evaluation', journal['result_id']]))
    else:
        signals = _find(spark, binding, 'case_work', investigation_id=state['investigation_id'])
        if signals and _case_input_revision(signals[0]['input_revision'], 'report', []) == state['input_revision']:
            signal = dict(signals[0], dirty=False)
            _rows(spark, binding, 'case_work', [signal], ('investigation_id',), update=True,
                  condition='t.input_revision=s.input_revision')


def _recover_case_studies(spark, binding):
    for journal in _case_study_pending(spark, binding):
        if journal['state'] != 'APPLYING':
            continue
        result = json.loads(journal['result_json'])
        try:
            plan = _prepare_case_result(spark, binding, result, journal['predecessor_revision'], replay=journal, batch_size=journal['batch_size'])
            _claim_case_state(spark, binding, result, journal)
        except ValueError as exc:
            raise RuntimeError('Interrupted case requires reconciliation; journal remains APPLYING') from exc
        _effect_case_study(spark, binding, result, journal, plan)
        journal['state'] = 'READY'
        _rows(spark, binding, 'case_study_results', [journal], ('result_id',), update=True)


def apply_case_study_results(spark, binding, result, *, result_id, predecessor_revision, batch_size=10):
    """Production path: one compact case state with optimistic predecessor CAS.

    The caller still owns the generation writer/publisher lease. A conflicting
    case writer cannot replace state. Dirty claims block publication until replay.
    No generation-wide observations/configuration are copied into this journal.
    """
    checked_binding(binding)
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError('Case evaluation batch_size must be between one and 100')
    state = _checked_case_result(binding, result, predecessor_revision)
    if not isinstance(result_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', result_id):
        raise ValueError('A stable bounded case result identity is required')
    if not any(result[key] for key in ('evaluations', 'reports', 'proposals', 'investigation_requests')):
        current = _find(spark, binding, 'study_case_state', investigation_id=state['investigation_id'])
        if len(current) == 1 and current[0]['state_json'] == canonical(state):
            return {'publication': publish(spark, binding), 'result_id': result_id, 'state': 'APPLIED', 'no_op': True}
    if _pending(spark, binding, ['APPLYING']) or _study_pending(spark, binding):
        raise RuntimeError('Recover prior business operations with apply_messages first')
    _recover_case_studies(spark, binding)
    prior = _find(spark, binding, 'case_study_results', result_id=result_id)
    if prior:
        if prior[0]['result_sha256'] != fingerprint(result) or prior[0]['predecessor_revision'] != predecessor_revision:
            raise ValueError('Conflicting case result identity')
        return {'publication': publish(spark, binding), 'result_id': result_id, 'state': 'APPLIED'}
    plan = _prepare_case_result(spark, binding, result, predecessor_revision, batch_size=batch_size)
    journal = {'result_id': result_id, 'investigation_id': state['investigation_id'], 'predecessor_revision': predecessor_revision,
        'version': state['version'], 'result_sha256': fingerprint(result), 'result_json': canonical(result),
        'state': 'APPLYING', 'created_at': clock(), 'batch_size': batch_size}
    _rows(spark, binding, 'case_study_results', [journal], ('result_id',))
    try:
        _claim_case_state(spark, binding, result, journal)
    except ValueError:
        journal['state'] = 'CONFLICT'
        _rows(spark, binding, 'case_study_results', [journal], ('result_id',), update=True)
        raise
    _effect_case_study(spark, binding, result, journal, plan)
    journal['state'] = 'READY'
    _rows(spark, binding, 'case_study_results', [journal], ('result_id',), update=True)
    return {'publication': publish(spark, binding), 'result_id': result_id, 'state': 'APPLIED'}


def reject_transport_message(spark, binding, *, message_id, raw_sha256, kind, code, source_sequence):
    """Durable rejection of a valid scoped ledger entry; never stores its body."""
    checked_binding(binding)
    if str(UUID(message_id)) != message_id or not re.fullmatch(r'[0-9a-f]{64}', raw_sha256):
        raise ValueError('Invalid transport rejection identity')
    if kind not in ('configuration', 'budget_round', 'observations', 'execution_result') or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,99}', code):
        raise ValueError('Invalid rejection kind/code')
    if type(source_sequence) is not int or not 1 <= source_sequence <= 9007199254740991:
        raise ValueError('Rejection requires its real source sequence')
    _recover_studies(spark, binding)
    _recover_case_studies(spark, binding)
    if _pending(spark, binding, ['APPLYING']):
        raise RuntimeError('Recover prior inbound messages before rejection')
    prior = _find(spark, binding, 'receipts', message_id=message_id)
    if prior:
        row = prior[0]
        if row.get('raw_sha256') != raw_sha256 or row.get('source_sequence') != source_sequence or row['code'] != code:
            raise ValueError('Conflicting transport rejection identity')
    else:
        row = {'message_id': message_id, 'message_sha256': raw_sha256, 'raw_sha256': raw_sha256,
            'kind': kind, 'state': 'READY', 'code': code, 'explanation': 'The scoped message failed business validation',
            'message_json': 'null', 'dirty_dates_json': '[]', 'dirty_cases_json': '[]',
            'source_sequence': source_sequence, 'received_at': clock()}
        _rows(spark, binding, 'receipts', [row], ('message_id',))
    publication = publish(spark, binding)
    return json_mode({'publication': publication, 'receipt': _find(spark, binding, 'receipts', message_id=message_id)[0]})


def _publication_reader(spark, binding):
    """Pin one coherent snapshot; callers supply keys, never SQL or object paths."""
    from pyspark.sql import functions as F
    checked_binding(binding)
    rows = _find(spark, binding, 'publication')
    if len(rows) != 1:
        raise ValueError('Case work requires one committed publication')
    publication = rows[0]
    versions = json.loads(publication['versions_json'])
    def read(suffix):
        name = 'ai_social_' + suffix
        version = versions.get(name)
        if type(version) is not int or version < 0:
            raise ValueError('Publication lacks the requested Sonda table version')
        frame = spark.sql('SELECT * FROM ' + _table(binding, suffix) + ' VERSION AS OF ' + str(version))
        for key, value in _scope(binding).items():
            frame = frame.where(F.col(key) == value)
        return frame
    return publication, read


def list_pending_cases(spark, binding, *, limit=100):
    """Bounded dirty-case selection; no all-generation state or text is collected."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Case selection limit must be between one and 100')
    from pyspark.sql import functions as F
    publication, read = _publication_reader(spark, binding)
    dirty = read('case_work').where('dirty = true').join(read('investigations').select('investigation_id'), 'investigation_id', 'left_semi').alias('w')
    states = read('study_case_state').select('investigation_id', 'version').alias('s')
    pending = read('evaluations').where("state='pending'").groupBy('investigation_id').agg(F.count('*').alias('pending_count')).alias('e')
    frame = (dirty.join(states, 'investigation_id', 'left').join(pending, 'investigation_id', 'left')
        .select('investigation_id', 'input_revision', F.coalesce('version', F.lit(0)).alias('version'),
                F.coalesce('pending_count', F.lit(0)).alias('pending_count'), F.col('w.updated_at'))
        .orderBy('updated_at', 'investigation_id').limit(limit))
    return json_mode({'publication_id': publication['publication_id'], 'cases': [
        {**row.asDict(), 'phase': 'evaluate' if row['pending_count'] else 'report'} for row in frame.collect()]})


def _observation_dto(row):
    fields = ('document_key', 'observation_key', 'content_sha256', 'document_type', 'platform', 'provider_id',
              'parent_document_key', 'url', 'title', 'text', 'author', 'published_at', 'observed_at', 'capture_scope')
    return json_mode({**{key: row[key] for key in fields}, 'identity_version': 1,
        'metrics': {key: row[key] for key in ('likes', 'comments', 'shares', 'views')},
        'indicators': json.loads(row['indicators_json'])})


def _report_selection(evaluations):
    """Bound both rows and bytes; full-population statistics remain in Spark."""
    selected, bytes_used, references = [], 0, {}
    for evaluation in evaluations[:100]:
        size = len(canonical(evaluation).encode('utf-8'))
        if bytes_used + size <= 32768:
            selected.append(evaluation)
            bytes_used += size
        for item in evaluation['evidence']:
            references.setdefault(item['evidence_id'], item)
    evidence, bytes_used = [], 0
    for key in sorted(references):
        item = references[key]
        size = len(canonical(item).encode('utf-8'))
        if len(evidence) < 100 and bytes_used + size <= 24576:
            evidence.append(item)
            bytes_used += size
    return selected, evidence


def _load_case_requests(read, investigation_id):
    from pyspark.sql import functions as F
    requests = []
    request_rows = read('requests').where(F.col('investigation_id') == investigation_id).orderBy('request_id').limit(21).collect()
    if len(request_rows) > 20:
        raise RuntimeError('Case request history exceeds its complete bounded contract')
    request_ids = [r['request_id'] for r in request_rows]
    execution_rows = read('executions').where(F.get_json_object('payload_json', '$.request_id').isin(request_ids)).limit(21).collect() if request_ids else []
    if len(execution_rows) > 20:
        raise RuntimeError('Case execution history exceeds its bounded contract')
    execution_by_request = {json.loads(r['payload_json'])['request_id']: r.asDict() for r in execution_rows}
    for row in request_rows:
        request = json.loads(row['payload_json'])
        execution = execution_by_request.get(row['request_id'])
        payload = json.loads(execution['payload_json']) if execution else None
        ids = payload['observation_message_ids'] if payload else []
        # The execution's references were checked before its commit. A completed
        # empty requested scope is still committed evidence of zero results.
        requests.append({'request': request, 'status': row['state'], 'observation_message_ids': ids,
                                 'observations_committed': bool(payload and payload['status'] == 'completed')})
    return requests, execution_rows


def _approved_case_queries(current, criterion):
    approved = []
    sources = {r['source_id']: r for r in current['sources']}
    current_criterion = next((r for r in current['criteria'] if r['criterion_id'] == criterion['criterion_id'] and r['revision'] == criterion['revision']), None)
    if current_criterion and current_criterion['status'] == 'active' and current_criterion['approval']['status'] == 'approved' and current_criterion['allow_research']:
        approved = [r['query_id'] for r in current['queries'] if r['status'] == 'active' and r['approval']['status'] == 'approved'
            and r['research_allowed'] and r['source_id'] in criterion['source_ids']
            and (not criterion['query_ids'] or r['query_id'] in criterion['query_ids'])
            and sources[r['source_id']]['status'] == 'active' and sources[r['source_id']]['approval']['status'] == 'approved'][:20]
    return approved


def _load_case_evaluations(read, work, pending, case_observations, case_discoveries, jobs, model_identity):
    from pyspark.sql import functions as F, Window
    keys = [r['observation_key'] for r in pending]
    observations = {r['observation_key']: r.asDict() for r in case_observations.where(F.col('observation_key').isin(keys)).collect()}
    provenance = (case_discoveries.where(F.col('observation_key').isin(keys)).alias('d')
        .join(jobs.select('observation_key', 'config_revision').alias('j'),
              (F.col('d.observation_key') == F.col('j.observation_key')) & (F.col('d.config_revision') == F.col('j.config_revision')), 'inner')
        .select('d.*').withColumn('_order', F.row_number().over(Window.partitionBy('observation_key').orderBy('query_id', 'execution_id')))
        .where('_order=1').collect())
    contexts = {r['observation_key']: r.asDict() for r in provenance}
    for job in pending:
        context = contexts.get(job['observation_key'])
        if context is None:
            raise RuntimeError('Pending work has no captured source/query provenance')
        work['pending_inputs'].append({'observation': _observation_dto(observations[job['observation_key']]),
            'config_revision': job['config_revision'], 'source_id': context['source_id'], 'query_id': context['query_id']})
    cached = read('evaluation_results').where(F.col('investigation_id') == work['investigation_id']).where(F.col('observation_key').isin(keys))
    if model_identity is not None:
        if set(model_identity) != {'model_id', 'model_revision', 'prompt_version'} or any(not isinstance(v, str) or not v for v in model_identity.values()):
            raise ValueError('Invalid model identity for completed evaluation lookup')
        for key, value in model_identity.items():
            cached = cached.where(F.col(key) == value)
    completed = cached.select('evaluation_key').limit(101).collect()
    if len(completed) > 100:
        raise RuntimeError('Completed key lookup requires the exact model identity')
    work['completed_evaluation_keys'] = [r['evaluation_key'] for r in completed[:100]]


def _load_case_report(read, work, case_observations):
    from pyspark.sql import functions as F, Window
    latest_documents = (case_observations.withColumn('_document_order', F.row_number().over(Window.partitionBy('document_key')
        .orderBy(F.desc('observed_at'), F.desc('observation_key')))).where('_document_order=1').drop('_document_order'))
    results = read('evaluation_results').where(F.col('investigation_id') == work['investigation_id'])
    latest_results = (results.withColumn('_result_order', F.row_number().over(Window.partitionBy('observation_key')
        .orderBy(F.desc('evaluated_at'), F.desc('evaluation_key')))).where('_result_order=1').drop('_result_order'))
    decisions = latest_documents.alias('o').join(latest_results.select('observation_key', 'decision').alias('e'), 'observation_key', 'left')
    metrics = ('likes', 'comments', 'shares', 'views')
    facts = decisions.agg(F.count('*').alias('documents'), F.sum(F.when(F.col('decision') == 'relevant', 1).otherwise(0)).alias('relevant'),
        F.sum(F.when(F.col('decision').isNull() | (F.col('decision') == 'error'), 1).otherwise(0)).alias('missing'),
        *[F.sum(key).alias(key) for key in metrics],
        *[F.sum(F.when(F.col(key).isNull(), 1).otherwise(0)).alias('missing_' + key) for key in metrics]).first().asDict()
    documents, missing = int(facts['documents']), int(facts['missing'] or 0)
    work['facts'] = {'distinct_documents': documents, 'distinct_observations': case_observations.count(),
        'relevant_documents': int(facts['relevant'] or 0) if missing < documents else None, 'missing_evaluations': missing}
    evaluated = latest_results.orderBy(F.desc('evaluated_at'), 'evaluation_key').limit(100).collect()
    work['evaluations'], work['evidence'] = _report_selection([json.loads(r['payload_json']) for r in evaluated])
    ids = [r['evidence_id'] for r in work['evidence']]
    if ids:
        base = {'evidence_ids': ids, 'population_count': documents}
        work['indicators'] = [{'key': 'documents', 'label': 'Documentos distintos', 'value': str(documents), 'unit': 'count',
            'period_start': None, 'period_end': None, 'method': 'count_distinct_documents', 'missing_count': 0, **base}]
        for key in metrics:
            work['indicators'].append({'key': key, 'label': key, 'value': str(facts[key]) if facts[key] is not None else None,
                'unit': 'count', 'period_start': None, 'period_end': None, 'method': 'reported_metric_sum',
                'missing_count': int(facts['missing_' + key] or 0), **base})
    if work['facts']['distinct_observations'] > len(work['evaluations']):
        work['limitations'].append('Las cifras cubren el caso completo; las citas y evaluaciones son una selección de hasta 100.')


def _load_case_source_limits(read, current_revision):
    """Research must use policies from the same published configuration."""
    from pyspark.sql import functions as F
    rows = read('source_limits').orderBy(F.desc('catalog_revision')).limit(1).collect()
    if not rows:
        return None
    if rows[0]['config_revision'] != current_revision:
        raise ValueError('Published source limits are not current for case research')
    return json.loads(rows[0]['payload_json'])


def load_case_work(spark, binding, investigation_id, *, batch_size=10, model_identity=None):
    """Hydrate one bounded case with full-population SQL facts and fixed queries.

    Returned evaluate inputs/citations are capped. Counts and reported metric sums
    are computed over the complete case in Spark, never over that driver sample.
    """
    if not isinstance(investigation_id, str) or not re.fullmatch(r'[0-9a-f]{64}', investigation_id):
        raise ValueError('Invalid case identity')
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError('Case evaluation batch_size must be between one and 100')
    from pyspark.sql import functions as F
    publication, read = _publication_reader(spark, binding)
    case_rows = read('investigations').where(F.col('investigation_id') == investigation_id).limit(2).collect()
    signals = read('case_work').where(F.col('investigation_id') == investigation_id).limit(2).collect()
    if len(case_rows) != 1 or len(signals) != 1:
        raise ValueError('Case work is absent from the committed publication')
    case, signal = case_rows[0].asDict(), signals[0].asDict()
    snapshots = read('registry')
    original = snapshots.where(F.col('config_revision') == case['config_revision']).first()
    current_row = snapshots.orderBy(F.desc('config_revision')).first()
    if original is None or current_row is None:
        raise ValueError('Case configuration is unavailable')
    config, current = json.loads(original['payload_json']), json.loads(current_row['payload_json'])
    criterion = next((r for r in config['criteria'] if r['criterion_id'] == case['criterion_id'] and r['revision'] == case['criterion_revision']), None)
    if criterion is None:
        raise ValueError('Original case criterion is unavailable')
    stored_state = read('study_case_state').where(F.col('investigation_id') == investigation_id).first()
    prior = json.loads(stored_state['state_json']) if stored_state else None
    jobs = read('evaluations').where(F.col('investigation_id') == investigation_id)
    pending = jobs.where("state='pending'").orderBy('observation_month', 'observation_key').limit(batch_size).collect()
    work = {'scope': {**_scope(binding), 'contract_version': '1.0.2', 'contract_sha256': CONTRACT_SHA256},
        'investigation_id': investigation_id, 'criterion': criterion, 'config_revision': case['config_revision'],
        'window_start': json_mode(case['window_start']), 'phase': 'evaluate' if pending else 'report',
        'input_revision': _case_input_revision(signal['input_revision'], 'evaluate' if pending else 'report', [r['observation_key'] for r in pending]),
        'pending_inputs': [], 'completed_evaluation_keys': [],
        'evaluations': [], 'evidence': [], 'facts': None, 'indicators': [], 'query_ids': [], 'requests': [],
        'requests_complete': True, 'budget_round': None, 'report_revision': 0,
        'approved_query_ids': [], 'limitations': []}
    work['source_limits'] = _load_case_source_limits(read, current['revision'])
    case_observations = (jobs.select('observation_key', 'observation_month').distinct()
        .join(read('observations'), ['observation_key', 'observation_month'], 'inner'))
    discovery = read('discoveries')
    case_discoveries = discovery.join(case_observations.select('observation_key'), 'observation_key', 'left_semi')
    query_rows = case_discoveries.select('query_id').distinct().orderBy('query_id').limit(101).collect()
    work['query_ids'] = [r['query_id'] for r in query_rows[:100]]
    if len(query_rows) > 100:
        work['limitations'].append('La lista de consultas mostrada está limitada; las cifras cubren todo el caso.')
    work['requests'], execution_rows = _load_case_requests(read, investigation_id)
    budgets = read('budget_rounds').where(F.col('config_revision') == current['revision']).orderBy(F.desc('opened_at'), F.desc('revision')).limit(1).collect()
    if budgets:
        work['budget_round'] = json.loads(budgets[0]['message_json'])
    work['approved_query_ids'] = _approved_case_queries(current, criterion)
    reports = read('reports').where(F.col('investigation_id') == investigation_id).agg(F.max('revision')).first()[0]
    work['report_revision'] = int(reports or 0)
    if pending:
        _load_case_evaluations(read, work, pending, case_observations, case_discoveries, jobs, model_identity)
    else:
        _load_case_report(read, work, case_observations)
    return json_mode({'work': work, 'state': prior, 'execution_results': [json.loads(r['message_json']) for r in execution_rows],
        'configuration': json.loads(current_row['message_json']), 'publication_id': publication['publication_id'],
        'publication_sha256': fingerprint(json_mode(publication))})
