import copy
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.labs.ai_social_network.notebooks import social_ingest as ingest


def config(**changes):
    return dict(namespace='namespace', source_bucket='landing', source_prefix='aidp-social/',
                gold_bucket='gold', artifacts_bucket='artifacts', catalog='oci_medallion',
                gold_schema='oci_gold', control_schema='oci_artifacts', credential_name='AidpRuntime',
                identity_sha256='a' * 64, **changes)


def source(**changes):
    return dict(source_object='aidp-social/events/network=x/date=2026-10-28/events.json', etag='v1', **changes)


def event(**changes):
    payload = {'id': 'post-1', 'platform': 'x', 'text': 'Observed source text', 'url': 'https://example.test/post-1',
               'run_id': 'capture-one', 'published_at': '2026-10-27T10:00:00Z',
               'raw': {'likeCount': 4, 'query': 'first filter'}, 'geography': {}, 'multimedia': []}
    payload.update(changes)
    return {'event_id': 1, 'created_at': '2026-10-28T12:00:00Z', 'payload': payload}


def test_repeated_capture_does_not_create_a_business_version():
    first = event()
    second = copy.deepcopy(first)
    second.update(event_id=2, created_at='2026-10-29T12:00:00Z')
    second['payload']['run_id'] = 'another-run'
    second['payload']['raw']['query'] = 'different search filter'
    assert ingest.normalize_event(first, source())['version_hash'] == ingest.normalize_event(second, source())['version_hash']


@pytest.mark.parametrize('change', [{'text': 'Changed source text'}, {'raw': {'likeCount': 5}},
                                     {'published_at': '2026-10-27T11:00:00Z'}])
def test_new_content_or_metrics_are_new_versions_of_the_same_post(change):
    before, after = (ingest.normalize_event(item, source()) for item in (event(), event(**change)))
    assert before['post_key'] == after['post_key']
    assert before['version_hash'] != after['version_hash']


def test_absent_metrics_locations_and_publication_time_remain_unknown():
    row = ingest.normalize_event(event(raw={}, published_at=None), source())
    assert row['like_count'] is None and row['comment_count'] is None
    assert row['published_at'] is None and row['published_date'] is None
    assert json.loads(row['geography_json']) == {}


def test_facebook_reported_comments_are_not_mistaken_for_missing_metric():
    raw = {'comments': 6, 'topComments': [{'commentId': 'observed', 'text': 'An observed comment'}]}
    row = ingest.normalize_event(event(platform='facebook', raw=raw),
        {**source(), 'source_object': 'aidp-social/events/network=facebook/date=2026-10-28/events.json'})
    assert row['comment_count'] == 6
    assert json.loads(row['attributes_json'])['topComments'] == raw['topComments']
    assert row['comment_count'] != len(raw['topComments']), 'Captured top comments are not the reported total'
    unknown = ingest.normalize_event(event(platform='facebook', raw={'comments':'6'}),
        {**source(), 'source_object': 'aidp-social/events/network=facebook/date=2026-10-28/events.json'})
    assert unknown['comment_count'] is None, 'Post count normalization accepts reported integers, not numeric strings'


def test_observed_comment_model_is_separate_from_reported_metrics_and_provider_credentials():
    definitions = ingest.definitions()
    assert definitions['ai_social_comment_current'][1] == 'platform'
    assert definitions['ai_social_comment_history'][1] == 'observation_month'
    assert 'capture_scope' in definitions['ai_social_comment_current'][0]
    assert 'comment_current_version BIGINT' in definitions['ai_social_publication'][0]
    assert 'comment_history_version BIGINT' in definitions['ai_social_publication'][0]
    assert 'model_version INT' in definitions['ai_social_ingest_state'][0]
    assert 'profile_picture' not in ingest.COMMENT_COLUMNS and 'gender' not in ingest.COMMENT_COLUMNS


@pytest.mark.parametrize('changes', [{'platform': 'unsupported'}, {'platform': 'youtube'}, {'id': ''}, {'id': None}])
def test_invalid_identity_or_network_folder_is_rejected(changes):
    with pytest.raises(ValueError):
        ingest.normalize_event(event(**changes), source())


def test_history_and_serving_tables_are_separate_and_partitioned_at_low_cardinality():
    definitions = ingest.definitions()
    assert definitions['ai_social_history'][1] == 'observation_month'
    assert definitions['ai_social_current'][1] == 'platform'
    assert definitions['ai_social_daily'][1] == 'published_month'
    assert definitions['ai_social_publication'][1] == ''
    statements = dict(ingest.schema_statements(config()))
    assert "oci://gold@namespace/ai_social_network/gold/" in statements['ai_social_history']
    assert "oci://artifacts@namespace/ai_social_network/control/" in statements['ai_social_files']
    assert 'USING DELTA' in statements['ai_social_history']
    assert 'PARTITIONED BY (observation_month)' in statements['ai_social_history']
    assert 'dirty_dates_json' in statements['ai_social_ingest_state']


@pytest.mark.parametrize('changes', [{'source_bucket': 'gold'}, {'catalog': 'catalog;DROP'},
                                     {'source_prefix': '../other/'}, {'source_prefix': '/absolute/'}, {'max_files': 0}])
def test_config_rejects_overlapping_storage_and_unsafe_identifiers(changes):
    values = config()
    values.update(changes)
    with pytest.raises(ValueError):
        ingest.validate_config(values)


def test_source_read_is_bound_to_the_listed_etag_and_a_bounded_size():
    body = json.dumps({'events': [event()]}).encode()
    calls = []
    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(data=SimpleNamespace(raw=io.BytesIO(body)))
    selected = source()
    selected['size_bytes'] = len(body)
    rows = ingest.read_source(SimpleNamespace(get_object=get), config(), selected)
    assert len(rows) == 1
    assert calls[0][1] == {'if_match': 'v1'}
    assert rows[0]['source_etag'] == 'v1'
    assert rows[0]['post_id'] == 'post-1'


def test_empty_json_file_is_valid_and_creates_no_synthetic_records():
    body = b'{"events":[]}'
    storage = SimpleNamespace(get_object=lambda *_args, **_kwargs: SimpleNamespace(data=SimpleNamespace(raw=io.BytesIO(body))))
    selected = {**source(), 'size_bytes': len(body)}
    assert ingest.read_source(storage, config(), selected) == []


def test_discovery_resumes_the_listing_cursor_instead_of_restarting_each_bounded_run():
    calls = []
    def listing(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=SimpleNamespace(objects=[], next_start_with='next-page'))
    values = config()
    values['max_pages'] = 2
    rows, cursor, completed = ingest.discovered_files(SimpleNamespace(list_objects=listing), values, 'saved-page')
    assert rows == [] and cursor == 'next-page' and completed is False
    assert [call['start'] for call in calls] == ['saved-page', 'next-page']
    assert all(call['prefix'] == 'aidp-social/' for call in calls)
import hashlib
import sys
from unittest.mock import Mock
from types import SimpleNamespace


def test_native_injected_secret_api_is_used_without_importing_aidputils(monkeypatch):
    values = {'tenancy':'tenant', 'user':'user', 'fingerprint':'fingerprint', 'region':'us-chicago-1', 'private_key':'in-memory-only'}
    api = SimpleNamespace(secrets=SimpleNamespace(get=lambda name,key: values[key]))
    monkeypatch.setattr(ingest,'aidputils',api,raising=False)
    sdk = Mock(return_value='storage-client')
    import oci
    monkeypatch.setattr(oci.object_storage,'ObjectStorageClient',sdk)
    monkeypatch.setattr(oci.signer,'Signer',Mock(return_value='signer'))
    configured = config()
    configured['identity_sha256'] = hashlib.sha256(json.dumps(['tenant','user','fingerprint'],separators=(',',':')).encode()).hexdigest()
    assert ingest.credential_config(configured) == 'storage-client'
    assert sdk.call_args.kwargs['signer'] == 'signer'
    configured['identity_sha256'] = '0'*64
    with pytest.raises(RuntimeError):
        ingest.credential_config(configured)


def test_every_module_table_has_a_nonempty_description_in_the_deployment_contract():
    statements = dict(ingest.schema_statements(config()))
    assert set(ingest.TABLE_DESCRIPTIONS) == set(ingest.GOLD_TABLES + ingest.CONTROL_TABLES)
    assert all(len(value) > 60 for value in ingest.TABLE_DESCRIPTIONS.values())
    for name, sql in statements.items():
        assert "COMMENT '" + ingest.TABLE_DESCRIPTIONS[name] + "'" in sql
