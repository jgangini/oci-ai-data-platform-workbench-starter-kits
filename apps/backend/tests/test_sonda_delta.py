"""Synthetic offline Sonda persistence checks; real Delta checks live in the script."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest

from app import sonda_contract as contract
from app.labs.ai_social_network.notebooks import sonda_delta as delta

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('update',[False,True])
def test_merge_stays_on_bound_location_without_catalog_path_lookup(monkeypatch,update):
    events=[]
    operation=SimpleNamespace()
    def step(name):
        def call(*args,**kwargs):
            events.append((name,args,kwargs))
            return operation
        return call
    for name in ('alias','merge','whenMatchedUpdateAll','whenNotMatchedInsertAll','execute'):
        setattr(operation,name,step(name))
    def for_path(spark,path):
        events.append(('path',path))
        return operation
    monkeypatch.setitem(sys.modules,'delta.tables',SimpleNamespace(DeltaTable=SimpleNamespace(forPath=for_path)))
    frame=SimpleNamespace(alias=lambda _:frame)
    written=[]
    def create(rows,schema):
        written.extend(rows)
        assert schema=='validated-schema'
        return frame
    def forbidden_sql(_):
        pytest.fail('A write must not follow a different catalog location')
    spark=SimpleNamespace(sql=forbidden_sql,table=lambda _:SimpleNamespace(schema='validated-schema'),createDataFrame=create)
    bound=binding()
    condition='s.version > t.version'
    delta._rows(spark,bound,'receipts',[{'message_id':'m'}],('message_id',),condition,update)
    assert events[0]==('path',delta.definitions(bound)['ai_social_receipts']['location'])
    assert written==[{**delta._scope(bound),'message_id':'m'}]
    predicate=next(args[1] for name,args,*_ in events if name=='merge')
    assert all('t.'+key+' <=> s.'+key in predicate for key in ('registry_id','generation_id','message_id'))
    matched=[e for e in events if e[0]=='whenMatchedUpdateAll']
    expected=[('whenMatchedUpdateAll',(),{'condition':condition})] if update else []
    assert matched==expected
    assert events[-1][0]=='execute'


def fixture(name):
    raw = (ROOT / 'contracts/sonda/v1/fixtures' / name).read_bytes()
    return contract.parse(raw, registry_id=UUID(int=1), generation_id=UUID(int=2),
                          contract_sha256=delta.CONTRACT_SHA256).model_dump(mode='json')


def binding():
    return {'catalog': 'spark_catalog', 'gold_schema': 'sonda_gold', 'control_schema': 'sonda_control',
            'namespace': 'synthetic', 'source_bucket': 'fixture_source', 'gold_bucket': 'fixture_gold',
            'artifacts_bucket': 'fixture_artifacts', 'registry_id': str(UUID(int=1)),
            'generation_id': str(UUID(int=2)), 'contract_sha256': delta.CONTRACT_SHA256}


def validate(value):
    return contract.parse(contract.encoded(value), registry_id=UUID(int=1), generation_id=UUID(int=2),
                          contract_sha256=delta.CONTRACT_SHA256).model_dump(mode='json')


def test_dedicated_locations_and_low_cardinality_partitions():
    definitions = delta.definitions(binding())
    assert len(definitions) == 28
    for name, definition in definitions.items():
        assert '/registries/' + str(UUID(int=1)) + '/generations/' + str(UUID(int=2)) + '/' in definition['location']
        assert definition['location'].endswith('/' + definition['layer'] + '/' + name)
        assert not set(definition['partitions']) & {'registry_id', 'generation_id', 'query_id', 'document_key'}
        assert definition['columns'].startswith(delta.SCOPE)
    assert definitions['ai_social_registry']['partitions'] == ()
    assert definitions['ai_social_document_current']['partitions'] == ('document_type', 'platform')


def test_tool_catalog_backfill_replays_only_missing_current_rows(monkeypatch):
    config = fixture('01-configuration.json')['payload']
    expected = delta.tool_catalog_rows(config, validated=True)
    stored = [{**delta._scope(binding()), **expected[0]}]
    monkeypatch.setattr(delta, '_latest_revision', lambda *args: config['revision'])
    monkeypatch.setattr(delta, '_configuration', lambda *args: config)
    monkeypatch.setattr(delta, '_find', lambda *args, **kwargs: copy.deepcopy(stored))
    writes = []
    def write(spark, bound, suffix, rows, keys):
        assert suffix == 'tool_catalog' and keys == ('snapshot_key',)
        writes.extend(copy.deepcopy(rows))
        stored.extend({**delta._scope(bound), **row} for row in rows)
    monkeypatch.setattr(delta, '_rows', write)
    assert delta.sync_tool_catalog(object(), binding()) is True
    assert writes == expected[1:]
    assert delta.sync_tool_catalog(object(), binding()) is False
    assert writes == expected[1:]
    stored[0]['max_items'] += 1
    with pytest.raises(ValueError, match='immutable tool catalog'):
        delta.sync_tool_catalog(object(), binding())


def test_tool_catalog_empty_install_does_not_invent_configuration(monkeypatch):
    monkeypatch.setattr(delta, '_latest_revision', lambda *args: 0)
    monkeypatch.setattr(delta, '_rows', lambda *args: pytest.fail('No configuration means no tools'))
    assert delta.sync_tool_catalog(object(), binding()) is False


def x_catalog_configuration(revision=20):
    config = fixture('01-configuration.json')['payload']
    config.update(revision=revision, previous_revision=revision-1)
    config['queries'] = config['queries'][:2]
    config['connectors'][0].update(connector_id='x', operation='search_posts', modes=['posts'], research_fields=['target'])
    config['sources'][0].update(connector_id='x', category='social', platform='x', allowed_domains=[])
    for query, target in zip(config['queries'], ['@oracle', '#OracleCloud']):
        query.update(mode='posts', target=target)
    config['catalog_sha256'] = contract.catalog_fingerprint(config['connectors'])
    return contract.Configuration.model_validate(config).model_dump(mode='json')


def catalog_store(monkeypatch, config, initial):
    stored = [{**delta._scope(binding()), **copy.deepcopy(row)} for row in initial]
    writes = []
    monkeypatch.setattr(delta, '_latest_revision', lambda *args: config['revision'])
    monkeypatch.setattr(delta, '_configuration', lambda *args: copy.deepcopy(config))
    def find(spark, bound, suffix, **where):
        if suffix == 'tool_catalog':
            return copy.deepcopy([row for row in stored if row['config_revision'] == where['config_revision']])
        if suffix == 'registry':
            return [{'payload_sha256': delta.fingerprint(config)}] if where['config_revision'] == config['revision'] else []
        return []
    def write(spark, bound, suffix, rows, keys, **kwargs):
        if suffix != 'tool_catalog':
            return
        assert keys == ('snapshot_key',)
        present = {row['snapshot_key'] for row in stored}
        for row in rows:
            if row['snapshot_key'] not in present:
                writes.append(copy.deepcopy(row))
                stored.append({**delta._scope(bound), **copy.deepcopy(row)})
                present.add(row['snapshot_key'])
    monkeypatch.setattr(delta, '_find', find)
    monkeypatch.setattr(delta, '_rows', write)
    return stored, writes


def test_pre_x_projection_reproduces_original_prepared_runner_bytes():
    config = x_catalog_configuration()
    legacy = delta.tool_catalog_rows(config, validated=True, include_x_targets=False)
    # Golden bytes cross-checked against the reviewed tool-projection functions
    # from prepared runner SHA 6d3ca3d429538836e6024a51cf990ba19590006b3e5b75cec8539d4393400286.
    assert contract.sha256(contract.encoded(legacy)) == '3cadd6e012d90bfab9f9dd7a90b481156a99c384774949156859d31ab779886a'
    assert all('target_override' not in json.loads(row['input_schema_json'])['properties'] for row in legacy)
    current = delta.tool_catalog_rows(config, validated=True)
    assert {row['snapshot_key'] for row in current} == {row['snapshot_key'] for row in legacy}
    assert all('target_override' in json.loads(row['input_schema_json'])['properties'] for row in current)
    assert all(before['metadata_sha256'] != after['metadata_sha256'] for before, after in zip(legacy, current))


def test_legacy_catalog_recovery_completes_only_its_own_projection(monkeypatch):
    config = x_catalog_configuration()
    legacy = delta.tool_catalog_rows(config, validated=True, include_x_targets=False)
    stored, writes = catalog_store(monkeypatch, config, legacy[:1])
    before = copy.deepcopy(stored[0])
    assert delta.sync_tool_catalog(object(), binding()) is True
    assert writes == legacy[1:] and stored[0] == before
    assert delta.sync_tool_catalog(object(), binding()) is False
    assert writes == legacy[1:]


def test_interrupted_configuration_replay_uses_legacy_plan_before_effects(monkeypatch):
    config = x_catalog_configuration()
    legacy = delta.tool_catalog_rows(config, validated=True, include_x_targets=False)
    stored, writes = catalog_store(monkeypatch, config, legacy[:1])
    monkeypatch.setattr(delta, '_check_managed_history', lambda *args: None)
    monkeypatch.setattr(delta, '_check_record_successors', lambda *args: None)
    message = {'kind': 'configuration', 'payload': config}
    plan = delta._prepare_configuration(object(), binding(), message)
    assert plan['tool_catalog'] == legacy
    delta._effect(object(), binding(), message, plan)
    assert stored == [{**delta._scope(binding()), **row} for row in legacy]
    assert writes == legacy[1:]
    assert delta.sync_tool_catalog(object(), binding()) is False


@pytest.mark.parametrize('failure', ['mixed', 'payload', 'digest', 'limits', 'unknown_field', 'duplicate_key'])
def test_unknown_or_mixed_catalog_projection_is_never_adopted(monkeypatch, failure):
    config = x_catalog_configuration()
    legacy = delta.tool_catalog_rows(config, validated=True, include_x_targets=False)
    current = delta.tool_catalog_rows(config, validated=True)
    initial = copy.deepcopy(legacy)
    if failure == 'mixed':
        initial[1] = current[1]
    elif failure == 'payload':
        initial[0]['payload_json'] += ' '
    elif failure == 'digest':
        initial[0]['metadata_sha256'] = '0'*64
    elif failure == 'limits':
        initial[0]['max_items'] += 1
    elif failure == 'unknown_field':
        initial[0]['unreviewed_projection'] = True
    else:
        initial.append(copy.deepcopy(initial[0]))
    stored, writes = catalog_store(monkeypatch, config, initial)
    before = copy.deepcopy(stored)
    with pytest.raises(ValueError, match='Conflicting immutable tool catalog'):
        delta.sync_tool_catalog(object(), binding())
    with pytest.raises(ValueError, match='Conflicting immutable tool catalog'):
        delta._prepare_configuration(object(), binding(), {'kind': 'configuration', 'payload': config})
    assert not writes and stored == before


def test_next_configuration_uses_new_x_projection_without_mutating_legacy(monkeypatch):
    config = x_catalog_configuration()
    legacy = delta.tool_catalog_rows(config, validated=True, include_x_targets=False)
    stored, writes = catalog_store(monkeypatch, config, legacy)
    old = copy.deepcopy(stored)
    assert delta.sync_tool_catalog(object(), binding()) is False
    config.update(revision=21, previous_revision=20)
    current = delta.tool_catalog_rows(config, validated=True)
    assert not {row['snapshot_key'] for row in current} & {row['snapshot_key'] for row in legacy}
    plan = delta._prepare_configuration(object(), binding(), {'kind': 'configuration', 'payload': config})
    assert plan['tool_catalog'] == current
    assert delta.sync_tool_catalog(object(), binding()) is True
    assert writes == current and stored[:len(old)] == old
    assert all('target_override' in json.loads(row['input_schema_json'])['properties'] for row in writes)


def test_fresh_catalog_uses_current_projection(monkeypatch):
    config = x_catalog_configuration()
    _, writes = catalog_store(monkeypatch, config, [])
    assert delta.sync_tool_catalog(object(), binding()) is True
    assert writes == delta.tool_catalog_rows(config, validated=True)


def test_empty_tool_catalog_upgrade_publishes_once_without_new_messages(monkeypatch):
    config = fixture('01-configuration.json')['payload']
    config.update(queries=[], criteria=[])
    versions = {name: 0 for name in delta.definitions(binding()) if name != 'ai_social_tool_catalog'}
    publication = {'publication_id': 'legacy', 'versions_json': delta.canonical(versions)}
    monkeypatch.setitem(sys.modules, 'pyspark.sql', SimpleNamespace(functions=SimpleNamespace()))
    monkeypatch.setattr(delta, '_pending', lambda *args: [])
    monkeypatch.setattr(delta, '_study_pending', lambda *args: [])
    monkeypatch.setattr(delta, '_case_study_pending', lambda *args: [])
    monkeypatch.setattr(delta, '_latest_revision', lambda *args: config['revision'])
    monkeypatch.setattr(delta, '_configuration', lambda *args: config)
    monkeypatch.setattr(delta, '_find', lambda s, b, suffix, **kwargs: [publication] if suffix == 'publication' else [])
    monkeypatch.setattr(delta, '_refresh_daily', lambda *args: None)
    writes = []
    def write(spark, bound, suffix, rows, keys, **kwargs):
        if rows:
            assert suffix == 'publication'
            publication.update(rows[0])
            writes.append(suffix)
    monkeypatch.setattr(delta, '_rows', write)
    spark = SimpleNamespace(sql=lambda query: SimpleNamespace(first=lambda: {'version': 0}))
    first = delta.publish(spark, binding())
    assert first['publication_id'] != 'legacy'
    assert json.loads(first['versions_json'])['ai_social_tool_catalog'] == 0
    second = delta.publish(spark, binding())
    assert second['publication_id'] == first['publication_id']
    assert writes == ['publication']


def test_source_limits_need_applied_core_transport_before_delta_write(monkeypatch):
    snapshot = json.loads((ROOT/'contracts/sonda-source-limits/v1/fixtures/01-initial.json').read_bytes())
    message = fixture('01-configuration.json')
    monkeypatch.setattr(delta, '_configuration', lambda *args: message['payload'])
    def find(spark, bound, suffix, **where):
        if suffix == 'registry':
            return [{'message_json': delta.canonical(message)}]
        return [{'state': 'APPLYING', 'code': 'applied'}]
    monkeypatch.setattr(delta, '_find', find)
    monkeypatch.setattr(delta, '_rows', lambda *args: pytest.fail('Uncommitted core cannot apply limits'))
    with pytest.raises(ValueError, match='APPLIED core'):
        delta.apply_source_limits(object(), binding(), snapshot,
            snapshot_sha256=delta.fingerprint(snapshot), core_message_id=message['message_id'])


def test_source_limits_scope_and_hash_fail_before_spark_access():
    snapshot = json.loads((ROOT/'contracts/sonda-source-limits/v1/fixtures/01-initial.json').read_bytes())
    with pytest.raises(ValueError, match='fingerprint'):
        delta.apply_source_limits(object(), binding(), snapshot, snapshot_sha256='a'*64, core_message_id=str(UUID(int=99)))
    snapshot['generation_id'] = str(UUID(int=999))
    with pytest.raises(ValueError, match='scope'):
        delta.apply_source_limits(object(), binding(), snapshot,
            snapshot_sha256=delta.fingerprint(snapshot), core_message_id=str(UUID(int=99)))


def test_document_statistics_cover_query_dates_and_metrics_without_indexing_long_text():
    for name in ('ai_social_document_current','ai_social_observations'):
        definition=delta.definitions(binding())[name]
        columns=[field.strip().split()[0] for field in definition['columns'].split(',')]
        indexed=set(columns[:definition['indexed_columns']])
        assert {'published_date','published_at','observed_at','likes','comments','shares','views'}<=indexed
        assert not {'text','url','title','indicators_json'} & indexed


@pytest.mark.parametrize('key,value', [('registry_id', 'not-a-uuid'), ('generation_id', str(UUID(int=3)).upper()),
    ('gold_schema', 'unsafe.name'), ('gold_schema','invalid-hyphen'), ('catalog', "x'; DROP TABLE x"), ('local_root', 'file:///tmp'),
    ('location_root', 'https://example.com'), ('contract_sha256', 'a' * 64)])
def test_binding_refuses_override_and_invalid_identifiers(key, value):
    value = '00000000-0000-0000-0000-00000000000A' if key == 'generation_id' else value
    with pytest.raises(ValueError):
        delta.definitions({**binding(), key: value})


def test_bucket_and_schema_separation():
    for change in ({'gold_bucket': 'fixture_source'}, {'control_schema': 'sonda_gold'}):
        with pytest.raises(ValueError):
            delta.definitions({**binding(), **change})


def test_native_delta_catalog_names_are_plain_validated_identifiers():
    for name,definition in delta.definitions(binding()).items():
        assert '`' not in definition['table']
        assert definition['table'].endswith('.'+name)
        assert definition['description']
    assert delta.checked_binding({**binding(),'source_bucket':'valid-hyphen'})['source_bucket']=='valid-hyphen'


def test_projection_preserves_unknown_metrics_and_awaits_real_model_result():
    message = fixture('02-observations.json')
    config = fixture('01-configuration.json')['payload']
    rows = delta.observation_rows(message, config)
    assert len(rows['document_current']) == len(rows['observations']) == len(rows['evaluations']) == 2
    assert len(rows['investigations']) == 1
    for row in rows['document_current']:
        assert row['comments'] is None and row['likes'] is None and row['platform'] is None
    for row in rows['evaluations']:
        assert row['state'] == 'pending'
        assert row['score'] is None and row['model_id'] is None and row['decision'] is None
    assert rows['investigations'][0]['state'] == 'awaiting_evaluation'


def test_multi_query_discovery_does_not_change_document_or_work_identity():
    first = fixture('02-observations.json')
    second = copy.deepcopy(first)
    config = fixture('01-configuration.json')['payload']
    second['message_id'] = str(UUID(int=600))
    second['payload'].update(query_id=config['queries'][1]['query_id'], execution_id=str(UUID(int=601)))
    a, b = [delta.observation_rows(validate(m), config) for m in (first, second)]
    for table, key in (('document_current', 'document_key'), ('observations', 'observation_key'), ('evaluations', 'work_key')):
        assert {r[key] for r in a[table]} == {r[key] for r in b[table]}
    assert {r['discovery_key'] for r in a['discoveries']}.isdisjoint({r['discovery_key'] for r in b['discoveries']})


def test_changed_metrics_create_business_version_not_an_extra_document():
    value = fixture('02-observations.json')
    config = fixture('01-configuration.json')['payload']
    old = value['payload']['observations'][0]
    new = copy.deepcopy(old)
    new['metrics']['comments'] = 0
    new['observed_at'] = '2026-02-01T00:00:00Z'
    new.update(contract.observation_fingerprints(new))
    value['payload']['observations'] = [new, old]
    rows = delta.observation_rows(validate(value), config)
    assert len(rows['document_current']) == 1 and len(rows['observations']) == 2
    assert rows['document_current'][0]['comments'] == 0
    assert rows['document_current'][0]['observation_key'] == new['observation_key']


@pytest.mark.parametrize('mutate', [
    lambda p: p.update(source_revision=2), lambda p: p.update(query_revision=2),
    lambda p: p.update(source_id=str(UUID(int=999))),
    lambda p: p['observations'][0].update(url='https://unapproved.example/evidence')])
def test_capture_requires_exact_historical_revision_and_reviewed_host(mutate):
    message = fixture('02-observations.json')
    mutate(message['payload'])
    with pytest.raises(ValueError):
        delta.observation_rows(message, fixture('01-configuration.json')['payload'])


def test_old_capture_is_not_reinterpreted_as_latest_paused_config():
    message = fixture('02-observations.json')
    historical = fixture('01-configuration.json')['payload']
    latest = copy.deepcopy(historical)
    latest['revision'] = 2
    latest['queries'][0]['status'] = 'paused'
    assert len(delta.observation_rows(message, historical)['observations']) == 2
    with pytest.raises(ValueError, match='revision'):
        delta.observation_rows(message, latest)


def test_criterion_query_filter_and_pause_do_not_create_pending_work():
    config = fixture('01-configuration.json')['payload']
    config['criteria'][0]['query_ids'] = [config['queries'][1]['query_id']]
    assert delta.observation_rows(fixture('02-observations.json'), config)['evaluations'] == []
    config['criteria'][0]['query_ids'] = []
    config['criteria'][0]['status'] = 'paused'
    assert delta.observation_rows(fixture('02-observations.json'), config)['investigations'] == []


def test_scope_rejection_happens_before_any_spark_operation():
    message = fixture('02-observations.json')
    message['generation_id'] = str(UUID(int=3))
    with pytest.raises(ValueError, match='scope'):
        delta.apply_messages(object(), binding(), [message])


def test_inbound_api_cannot_accept_outbound_results():
    with pytest.raises(NotImplementedError):
        delta.apply_messages(object(), binding(), [fixture('05-report.json')])


def test_failure_keeps_replayable_journal_and_never_publishes(monkeypatch):
    message = fixture('01-configuration.json')
    journals = {}
    monkeypatch.setattr(delta, '_recover_studies', lambda *args: None)
    monkeypatch.setattr(delta, '_recover_case_studies', lambda *args: None)
    monkeypatch.setattr(delta, '_pending', lambda spark, b, states: [r for r in journals.values() if r['state'] in states])
    monkeypatch.setattr(delta, '_find', lambda spark, b, suffix, **where: [journals[where['message_id']]] if where.get('message_id') in journals else [])
    monkeypatch.setattr(delta, '_prepare', lambda *args: {})
    monkeypatch.setattr(delta, '_dirty_dates', lambda *args: [])
    def write(spark, b, suffix, rows, *args, **kwargs):
        assert suffix == 'receipts'
        for row in rows:
            journals[row['message_id']] = copy.deepcopy(row)
    monkeypatch.setattr(delta, '_rows', write)
    def fail(*args):
        raise RuntimeError('Synthetic interruption after durable journal')
    monkeypatch.setattr(delta, '_effect', fail)
    monkeypatch.setattr(delta, 'publish', lambda *args: pytest.fail('Interrupted write must not publish'))
    with pytest.raises(RuntimeError, match='Synthetic interruption'):
        delta.apply_messages(object(), binding(), [message])
    assert journals[message['message_id']]['state'] == 'APPLYING'
    assert json.loads(journals[message['message_id']]['message_json']) == message
    monkeypatch.setattr(delta, '_effect', lambda *args: None)
    monkeypatch.setattr(delta, 'publish', lambda *args: {'synthetic': True})
    delta.apply_messages(object(), binding(), [])
    assert journals[message['message_id']]['state'] == 'READY'


def test_message_identity_conflict_is_not_a_business_rejection(monkeypatch):
    message = fixture('01-configuration.json')
    monkeypatch.setattr(delta, '_recover_studies', lambda *args: None)
    monkeypatch.setattr(delta, '_recover_case_studies', lambda *args: None)
    monkeypatch.setattr(delta, '_pending', lambda *args: [])
    monkeypatch.setattr(delta, '_find', lambda *args, **kwargs: [{'message_sha256': 'a' * 64, 'state': 'APPLIED'}])
    with pytest.raises(ValueError, match='Conflicting message identity'):
        delta.apply_messages(object(), binding(), [message])


@pytest.mark.parametrize('change,expected', [
    ({'revision': 1}, 'stale'), ({'revision': 3, 'status': 'open'}, 'reopen'),
    ({'revision': 3, 'config_revision': 2}, 'immutable')])
def test_budget_revision_and_terminal_status_are_enforced(monkeypatch, change, expected):
    current = fixture('10-budget-round-closed.json')['payload']
    value = fixture('10-budget-round-closed.json')
    value['payload'].update(change)
    monkeypatch.setattr(delta, '_configuration', lambda *args: {})
    monkeypatch.setattr(delta, '_find', lambda spark, b, suffix, **where:
        [{'payload_json': delta.canonical(current)}] if suffix == 'budget_rounds' else [])
    with pytest.raises(ValueError, match=expected):
        delta._prepare(object(), binding(), value)


def test_exact_budget_revision_is_idempotent_and_conflict_is_rejected(monkeypatch):
    message = fixture('09-budget-round-open.json')
    digest = delta.fingerprint(message['payload'])
    monkeypatch.setattr(delta, '_configuration', lambda *args: {})
    monkeypatch.setattr(delta, '_find', lambda *args, **kwargs: [{'payload_sha256': digest}])
    assert delta._prepare(object(), binding(), message) == {}
    message['payload']['remaining_items'] = None
    with pytest.raises(ValueError, match='immutable budget'):
        delta._prepare(object(), binding(), message)


def test_pin_is_mandatory_and_legacy_sonda_cannot_activate():
    value = binding()
    value.pop('contract_sha256')
    with pytest.raises(ValueError, match='pinned contract'):
        delta.checked_binding(value)


def test_dirty_replay_cannot_reject_and_publish_partial_effects(monkeypatch):
    message = fixture('01-configuration.json')
    monkeypatch.setattr(delta, '_recover_studies', lambda *args: None)
    monkeypatch.setattr(delta, '_recover_case_studies', lambda *args: None)
    dirty = {'message_id': message['message_id'], 'message_sha256': delta.fingerprint(message),
             'message_json': delta.canonical(message), 'state': 'APPLYING'}
    monkeypatch.setattr(delta, '_pending', lambda *args: [dirty])
    monkeypatch.setattr(delta, '_find', lambda *args, **kwargs: [dirty])
    def broken_reference(*args):
        raise ValueError('Synthetic external state mismatch')
    monkeypatch.setattr(delta, '_prepare', broken_reference)
    monkeypatch.setattr(delta, '_rows', lambda *args, **kwargs: pytest.fail('Dirty journal must remain APPLYING'))
    monkeypatch.setattr(delta, 'publish', lambda *args: pytest.fail('Partial business effects must not publish'))
    with pytest.raises(RuntimeError, match='remains APPLYING'):
        delta.apply_messages(object(), binding(), [])


def test_hadoop_local_uri_rendering_preserves_strict_production_location():
    assert delta.same_location('file:/tmp/synthetic/table', 'file:///tmp/synthetic/table')
    assert not delta.same_location('file:/tmp/other/table', 'file:///tmp/synthetic/table')
    assert not delta.same_location('file://remote/tmp/synthetic/table', 'file:///tmp/synthetic/table')
    assert not delta.same_location('oci://other@namespace/path', 'oci://gold@namespace/path')
    assert not delta.same_location('oci://gold@namespace/other', 'oci://gold@namespace/path')


def study_fixture():
    config = fixture('01-configuration.json')['payload']
    rows = delta.observation_rows(fixture('02-observations.json'), config)
    case_id = rows['investigations'][0]['investigation_id']
    report = fixture('05-report.json')['payload']
    report['investigation_id'] = case_id
    report['report_id'] = delta.fingerprint([case_id, 'report'])
    result = {'evaluations': report['evaluations'], 'reports': [report], 'investigation_requests': [], 'proposals': [],
              'state': {'scope': {**delta._scope(binding()), 'contract_version': '1.0.2', 'contract_sha256': delta.CONTRACT_SHA256},
                        'evaluation_cases': {e['evaluation_key']: case_id for e in report['evaluations']}}}
    return config, rows, result


def fake_study_store(monkeypatch, config, rows):
    store = {key: copy.deepcopy(values) for key, values in rows.items()}
    monkeypatch.setattr(delta, '_configuration', lambda *args: config)
    def find(spark, b, suffix, **where):
        return [r for r in store.get(suffix, []) if all(r.get(k) == v for k, v in where.items())]
    monkeypatch.setattr(delta, '_find', find)
    return store


def test_study_results_bind_actual_quotes_rule_model_and_case(monkeypatch):
    config, rows, result = study_fixture()
    fake_study_store(monkeypatch, config, rows)
    plan = delta._prepare_study(object(), binding(), result)
    assert len(plan['evaluations']) == 2 and len(plan['evidence']) == 2
    assert result['evaluations'][0]['decision'] == 'relevant'
    assert result['evaluations'][1]['decision'] == 'not_relevant'


@pytest.mark.parametrize('field,value', [('quote', 'Fabricated quotation'), ('url', 'https://example.com/fabricated'), ('document_key', 'a' * 64)])
def test_study_cannot_invent_evidence(monkeypatch, field, value):
    config, rows, result = study_fixture()
    fake_study_store(monkeypatch, config, rows)
    result['evaluations'][0]['evidence'][0][field] = value
    with pytest.raises(ValueError, match='evidence'):
        delta._prepare_study(object(), binding(), result)


def test_result_scope_and_model_revision_are_not_interchangeable(monkeypatch):
    config, rows, result = study_fixture()
    fake_study_store(monkeypatch, config, rows)
    result['evaluations'][0]['model_revision'] = 'different-version'
    with pytest.raises(ValueError, match='model and prompt'):
        delta._prepare_study(object(), binding(), result)
    result['state']['scope']['generation_id'] = str(UUID(int=999))
    with pytest.raises(ValueError, match='scope'):
        delta._prepare_study(object(), binding(), result)


def test_study_outbox_contains_parseable_typed_wire_messages():
    _, _, result = study_fixture()
    journal = {'created_at': delta.utc('2026-01-01T00:30:00Z')}
    first = list(delta._study_outbox(binding(), result, journal))
    second = list(delta._study_outbox(binding(), result, journal))
    assert first == second and len(first) == 1
    value = validate(json.loads(first[0]['payload_json']))
    assert value['kind'] == 'report' and value['direction'] == 'aidp_to_huginn'


def test_evaluation_result_identity_conflict_is_not_silently_overwritten(monkeypatch):
    config, rows, result = study_fixture()
    store = fake_study_store(monkeypatch, config, rows)
    evaluation = result['evaluations'][0]
    store['evaluation_results'] = [{'evaluation_key': evaluation['evaluation_key'], 'payload_sha256': 'a' * 64}]
    with pytest.raises(ValueError, match='Immutable evaluation'):
        delta._prepare_study(object(), binding(), result)


def test_cross_generation_study_is_rejected_before_recovery_or_spark():
    _, _, result = study_fixture()
    result['state']['scope']['generation_id'] = str(UUID(int=999))
    with pytest.raises(ValueError, match='scope'):
        delta.apply_study_results(object(), binding(), result, result_id='synthetic')


def case_result_fixture():
    _, rows, result = study_fixture()
    key = rows['investigations'][0]['investigation_id']
    result.update(reports=[], proposals=[], investigation_requests=[])
    result['state'] = {'scope': result['state']['scope'], 'investigation_id': key,
        'version': 1, 'predecessor_revision': 0, 'phase': 'evaluate',
        'input_revision': delta.fingerprint(['synthetic', key]), 'last_input_fingerprint': delta.fingerprint(['input', key]),
        'report_revision': 0, 'request_ids': [], 'proposal_ids': []}
    return result


def test_case_state_is_compact_and_evaluation_adapter_does_not_copy_history():
    result = case_result_fixture()
    state = delta._checked_case_result(binding(), result, 0)
    assert state['version'] == 1
    assert set(delta._case_result(result)['state']) == {'scope', 'evaluation_cases'}
    assert 'observations' not in delta.canonical(state)


@pytest.mark.parametrize('change', [
    {'observations': []}, {'snapshots': {}}, {'version': True}, {'predecessor_revision': 2},
    {'input_revision': 'not-a-hash'}, {'request_ids': [str(UUID(int=10))] * 21}])
def test_case_state_rejects_history_rollback_and_oversized_references(change):
    result = case_result_fixture()
    result['state'].update(change)
    with pytest.raises(ValueError):
        delta._checked_case_result(binding(), result, 0)


def test_case_result_cannot_mix_evaluate_and_report_phases():
    result = case_result_fixture()
    result['reports'] = [fixture('05-report.json')['payload']]
    with pytest.raises(ValueError, match='commit before reporting'):
        delta._checked_case_result(binding(), result, 0)
    result['reports'] = []
    result['evaluations'] *= 51
    with pytest.raises(ValueError, match='bounded batch'):
        delta._checked_case_result(binding(), result, 0)


def test_case_predecessor_cas_rejects_stale_state_before_business_writes(monkeypatch):
    result = case_result_fixture()
    monkeypatch.setattr(delta, '_find', lambda *args, **kwargs: [{'version': 5, 'result_id': 'other'}])
    monkeypatch.setattr(delta, '_prepare_study', lambda *args: pytest.fail('Stale case cannot write business outputs'))
    with pytest.raises(ValueError, match='Stale case predecessor'):
        delta._prepare_case_result(object(), binding(), result, 0)


def test_case_input_revision_prevents_clearing_new_work(monkeypatch):
    result = case_result_fixture()
    monkeypatch.setattr(delta, '_find', lambda spark, b, suffix, **kwargs:
        [] if suffix == 'study_case_state' else [{'input_revision': 'a' * 64}])
    monkeypatch.setattr(delta, '_case_pending_keys', lambda *args: ['b' * 64])
    with pytest.raises(ValueError, match='input changed'):
        delta._prepare_case_result(object(), binding(), result, 0)


def test_case_scope_rejects_before_any_spark_or_recovery():
    result = case_result_fixture()
    result['state']['scope']['generation_id'] = str(UUID(int=999))
    with pytest.raises(ValueError, match='scope'):
        delta.apply_case_study_results(object(), binding(), result, result_id='synthetic', predecessor_revision=0)


@pytest.mark.parametrize('sequence', [True, 0, -1, '1', 9007199254740992])
def test_transport_sequence_requires_real_positive_integer_before_spark(sequence):
    message = fixture('01-configuration.json')
    with pytest.raises(ValueError, match='sequence'):
        delta.apply_messages(object(), binding(), [message], source_sequences={message['message_id']: sequence})


def test_invalid_message_rejection_never_stores_the_invalid_body(monkeypatch):
    rows = {}
    monkeypatch.setattr(delta, '_recover_studies', lambda *args: None)
    monkeypatch.setattr(delta, '_recover_case_studies', lambda *args: None)
    monkeypatch.setattr(delta, '_pending', lambda *args: [])
    monkeypatch.setattr(delta, '_find', lambda *args, **kwargs: list(rows.values()))
    def save(spark, b, suffix, records, *args, **kwargs):
        assert suffix == 'receipts'
        rows.update({r['message_id']: copy.deepcopy(r) for r in records})
    monkeypatch.setattr(delta, '_rows', save)
    monkeypatch.setattr(delta, 'publish', lambda *args: {'synthetic': True})
    delta.reject_transport_message(object(), binding(), message_id=str(UUID(int=800)), raw_sha256='f' * 64,
        kind='observations', code='invalid_business', source_sequence=17)
    receipt = rows[str(UUID(int=800))]
    assert receipt['message_json'] == 'null' and receipt['source_sequence'] == 17
    assert receipt['raw_sha256'] == 'f' * 64
    with pytest.raises(ValueError, match='Conflicting'):
        delta.reject_transport_message(object(), binding(), message_id=str(UUID(int=800)), raw_sha256='e' * 64,
            kind='observations', code='invalid_business', source_sequence=17)


def test_pending_case_limit_is_checked_without_scanning():
    for value in (0, 101, True):
        with pytest.raises(ValueError, match='limit'):
            delta.list_pending_cases(object(), binding(), limit=value)


def test_public_api_timestamp_encoding_is_utc_json():
    value = delta.json_mode({'when': delta.utc('2026-01-01T05:00:00+05:00'), 'day': delta.utc('2026-01-01T00:00:00Z').date()})
    assert value == {'when': '2026-01-01T00:00:00.000000Z', 'day': '2026-01-01'}


def test_case_batch_identity_binds_selected_keys_and_not_other_cases():
    signal = delta.fingerprint(['case', 1])
    a = delta._case_input_revision(signal, 'evaluate', ['a', 'b'])
    assert a == delta._case_input_revision(signal, 'evaluate', ['a', 'b'])
    assert a != delta._case_input_revision(signal, 'evaluate', ['a'])
    assert a != delta._case_input_revision(signal, 'evaluate', ['b', 'a'])
    assert a != delta._case_input_revision(signal, 'report', [])


@pytest.mark.parametrize('batch_size', [0, 101, True])
def test_production_batch_limit_is_checked_before_spark(batch_size):
    result = case_result_fixture()
    with pytest.raises(ValueError, match='batch_size'):
        delta.load_case_work(object(), binding(), result['state']['investigation_id'], batch_size=batch_size)
    with pytest.raises(ValueError, match='batch_size'):
        delta.apply_case_study_results(object(), binding(), result, result_id='synthetic', predecessor_revision=0, batch_size=batch_size)


def test_report_hydration_bounds_bytes_without_fabricating_counts():
    _, _, result = study_fixture()
    large = copy.deepcopy(result['evaluations'][0])
    large['reason'] = 'é' * 20000
    values, evidence = delta._report_selection([large] + result['evaluations'])
    assert large not in values
    assert sum(len(delta.canonical(r).encode('utf-8')) for r in values) <= 32768
    assert sum(len(delta.canonical(r).encode('utf-8')) for r in evidence) <= 24576
    assert {r['evidence_id'] for r in evidence} <= {e['evidence_id'] for row in [large] + result['evaluations'] for e in row['evidence']}


def test_research_observation_projection_preserves_original_author_and_fingerprints():
    message = fixture('02-observations.json')
    original = message['payload']['observations'][0]
    original['author'] = 'northstar_official'
    original.update(contract.observation_fingerprints(original))
    original = contract.Observation.model_validate(original).model_dump(mode='json')
    message['payload']['observations'] = [original]
    persisted = delta.observation_rows(message, fixture('01-configuration.json')['payload'])['observations'][0]
    persisted.update(source_id=message['payload']['source_id'], query_id=message['payload']['query_id'])
    before = copy.deepcopy(persisted)
    work = {'limitations': []}
    delta._case_research_observation_rows(work, [persisted])
    result = work['research_observations'][0]
    assert result['source_id'] == message['payload']['source_id']
    assert result['query_id'] == message['payload']['query_id']
    assert contract.Observation.model_validate(result['observation']).model_dump(mode='json') == original
    assert result['observation']['author'] == 'northstar_official'
    assert result['observation']['text'] == original['text'] and result['observation']['title'] == original['title']
    assert persisted == before and not work['limitations']


def test_research_observation_driver_count_and_bytes_are_bounded_without_truncation():
    config = fixture('01-configuration.json')['payload']
    message = fixture('02-observations.json')
    rows = delta.observation_rows(message, config)['observations']
    provenance = {'source_id': message['payload']['source_id'], 'query_id': message['payload']['query_id']}
    work = {'limitations': []}
    delta._case_research_observation_rows(work, [{**rows[0], **provenance}] * 101)
    assert len(work['research_observations']) == 100 and len(work['limitations']) == 1
    original = message['payload']['observations'][0]
    original['text'] = '文' * 20000
    original.update(contract.observation_fingerprints(original))
    message['payload']['observations'] = [original]
    large = delta.observation_rows(message, config)['observations'][0]
    work = {'limitations': []}
    delta._case_research_observation_rows(work, [{**large, **provenance}] * 10)
    assert 0 < len(work['research_observations']) < 10
    assert len(work['limitations']) == 1
    assert sum(len(delta.canonical(row).encode('utf-8')) for row in work['research_observations']) <= 245760
    assert all(contract.Observation.model_validate(row['observation']).text == original['text']
        for row in work['research_observations'])


def research_origin_fixture():
    configuration, captured, study = study_fixture()
    discovery = copy.deepcopy(captured['discoveries'][0])
    request = fixture('03-investigation-request.json')['payload']
    request['investigation_id'] = captured['investigations'][0]['investigation_id']
    result = fixture('04-execution-result.json')
    result['payload'].update(status='completed', execution_id=discovery['execution_id'],
        request_id=request['request_id'], observation_message_ids=[discovery['message_id']],
        record_count=1, spent_microusd=1000, coverage='requested_scope')
    result = contract.ExecutionMessage.model_validate(result).model_dump(mode='json')
    execution = {key: result['payload'][key] for key in (
        'execution_id', 'status', 'config_revision', 'query_id', 'query_revision', 'record_count', 'spent_microusd')}
    execution.update(payload_json=delta.canonical(result['payload']), message_json=delta.canonical(result))
    work = {'scope': study['state']['scope'], 'investigation_id': request['investigation_id'],
        'criterion': configuration['criteria'][0], 'requests': [{'request': request, 'status': 'completed',
            'observations_committed': True, 'observation_message_ids': result['payload']['observation_message_ids']}]}
    return work, execution, discovery


def test_research_origin_requires_completed_exact_case_request_and_discovery():
    work, execution, discovery = research_origin_fixture()
    index = delta._case_authorized_research_executions(work, [execution])
    assert delta._case_research_origin(work, discovery, index) == work['requests'][0]['request']['request_id']
    primary = copy.deepcopy(discovery)
    primary['execution_id'] = str(UUID(int=999))
    assert delta._case_research_origin(work, primary, index) is None
    assert delta._case_research_origin(work, discovery, {}) is None


def test_research_origin_accepts_spark_row_index_access_without_dict_get():
    work, execution, discovery = research_origin_fixture()
    class IndexedRow:
        def __getitem__(self, key):
            return execution[key]
    index = delta._case_authorized_research_executions(work, [IndexedRow()])
    assert delta._case_research_origin(work, discovery, index) == work['requests'][0]['request']['request_id']


@pytest.mark.parametrize('field,value', [
    ('investigation_id', 'a'*64), ('criterion_id', str(UUID(int=999))), ('criterion_revision', 2),
    ('config_revision', 2), ('query_id', str(UUID(int=999))), ('query_revision', 2),
    ('budget_round_id', str(UUID(int=999))),
])
def test_foreign_or_mismatched_request_never_grants_research_origin(field, value):
    work, execution, discovery = research_origin_fixture()
    work['requests'][0]['request'][field] = value
    index = delta._case_authorized_research_executions(work, [execution])
    assert not index and delta._case_research_origin(work, discovery, index) is None


@pytest.mark.parametrize('failure', ['pending', 'uncertain', 'not_committed', 'wrong_observations',
    'foreign_scope', 'unknown_request', 'primary_execution', 'failed_execution', 'wrong_message', 'wrong_gold_row'])
def test_unconfirmed_or_foreign_execution_never_grants_research_origin(failure):
    work, execution, discovery = research_origin_fixture()
    message = json.loads(execution['message_json'])
    if failure in ('pending', 'uncertain'):
        work['requests'][0]['status'] = failure
    elif failure == 'not_committed':
        work['requests'][0]['observations_committed'] = False
    elif failure == 'wrong_observations':
        work['requests'][0]['observation_message_ids'] = []
    elif failure == 'foreign_scope':
        message['generation_id'] = str(UUID(int=999))
    elif failure == 'unknown_request':
        message['payload']['request_id'] = str(UUID(int=999))
        execution['payload_json'] = delta.canonical(message['payload'])
    elif failure == 'primary_execution':
        message['payload']['request_id'] = None
        execution['payload_json'] = delta.canonical(message['payload'])
    elif failure == 'failed_execution':
        message['payload']['status'] = 'failed'
        execution.update(status='failed', payload_json=delta.canonical(message['payload']))
    elif failure == 'wrong_gold_row':
        execution['query_revision'] = 2
    else:
        message['payload']['record_count'] = 0
    execution['message_json'] = delta.canonical(message)
    index = delta._case_authorized_research_executions(work, [execution])
    assert not index and delta._case_research_origin(work, discovery, index) is None


@pytest.mark.parametrize('field,value', [
    ('source_id', str(UUID(int=999))), ('message_id', str(UUID(int=999))),
    ('config_revision', 2), ('query_id', str(UUID(int=999))), ('query_revision', 2),
    ('budget_round_id', str(UUID(int=999))),
])
def test_discovery_requires_exact_completed_execution_link(field, value):
    work, execution, discovery = research_origin_fixture()
    index = delta._case_authorized_research_executions(work, [execution])
    discovery[field] = value
    assert delta._case_research_origin(work, discovery, index) is None


def test_criterion_query_scope_and_ambiguous_executions_fail_closed():
    work, execution, discovery = research_origin_fixture()
    work['criterion']['query_ids'] = [str(UUID(int=999))]
    assert not delta._case_authorized_research_executions(work, [execution])
    work['criterion']['query_ids'] = []
    duplicate = copy.deepcopy(execution)
    message = json.loads(duplicate['message_json'])
    message['payload']['execution_id'] = str(UUID(int=999))
    duplicate.update(execution_id=message['payload']['execution_id'],
        payload_json=delta.canonical(message['payload']), message_json=delta.canonical(message))
    with pytest.raises(ValueError, match='ambiguous completed'):
        delta._case_authorized_research_executions(work, [execution, duplicate])
