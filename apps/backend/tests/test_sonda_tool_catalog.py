"""Synthetic catalog projection checks; no broker, Spark, provider or model calls."""
import copy
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from app import sonda_contract as core
from app.labs.ai_social_network.notebooks.sonda_tools import (
    TOOL_CATALOG_COLUMNS, tool_catalog_definition, tool_catalog_rows)

ROOT = Path(__file__).resolve().parents[3]


def configuration():
    return json.loads((ROOT/'contracts/sonda/v1/fixtures/01-configuration.json').read_text(encoding='utf-8'))


def one_query():
    value = configuration()['payload']
    value['queries'] = value['queries'][:1]
    return value


def reviewed_catalog(value):
    value['catalog_sha256'] = core.catalog_fingerprint(value['connectors'])
    return value


def test_core_fixture_projection_is_deterministic_and_preserves_input():
    message = configuration()
    original = copy.deepcopy(message)
    rows = tool_catalog_rows(message)
    assert message == original
    assert rows == tool_catalog_rows(core.ConfigMessage.model_validate(message))
    assert rows == tool_catalog_rows(message['payload'])
    assert rows == tool_catalog_rows(core.Configuration.model_validate(message['payload']))
    assert len(rows) == len(message['payload']['queries']) == 6
    reordered = copy.deepcopy(message['payload'])
    reordered['queries'].reverse()
    assert tool_catalog_rows(reordered) == rows
    assert all(row['configured_eligible'] and row['provider_kind'] == 'apify' for row in rows)
    assert len({row['tool_id'] for row in rows}) == len(rows)
    assert all(re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,63}', row['tool_name']) for row in rows)
    assert not any('registry_id' in row or 'generation_id' in row for row in rows)


def test_ids_survive_configuration_revision_but_snapshot_and_metadata_change():
    value = one_query()
    before = tool_catalog_rows(value)[0]
    value['revision'], value['previous_revision'] = 2, 1
    after = tool_catalog_rows(value)[0]
    assert after['tool_id'] == before['tool_id'] and after['tool_name'] == before['tool_name']
    assert after['snapshot_key'] != before['snapshot_key']
    assert after['metadata_sha256'] != before['metadata_sha256']
    query = value['queries'][0]
    query['revision'], query['label'] = 2, 'Updated reviewed query'
    changed = tool_catalog_rows(value)[0]
    assert changed['tool_id'] == after['tool_id'] and changed['snapshot_key'] == after['snapshot_key']
    assert changed['metadata_sha256'] != after['metadata_sha256']
    assert changed['query_revision'] == 2


def test_catalog_schema_matches_rows_and_metadata_hash_matches_exact_payload():
    definition = tool_catalog_definition()
    assert definition['columns'] == TOOL_CATALOG_COLUMNS
    assert definition['partitions'] == () and definition['layer'] == 'gold'
    columns = [item.strip().split()[0] for item in TOOL_CATALOG_COLUMNS.split(',')]
    for row in tool_catalog_rows(one_query()):
        assert set(row) == set(columns)
        assert core.sha256(row['payload_json'].encode('utf-8')) == row['metadata_sha256']
        payload = json.loads(row['payload_json'])
        assert payload['reviewed_connector']['actor_id'] == 'fixture/public-pages'
        assert payload['reviewed_connector']['actor_build'] == 'fixture-only'
        assert payload['reviewed_connector']['reviewed_by'] == 'fixture-operator'
        assert payload['reviewed_connector']['input_schema_sha256']
        assert {key: payload[key] for key in row if key not in ('metadata_sha256', 'payload_json')} == {
            key: value for key, value in row.items() if key not in ('metadata_sha256', 'payload_json')}


@pytest.mark.parametrize('status', ['paused', 'draft', 'archived'])
def test_ineligible_queries_remain_visible_without_becoming_invocable(status):
    value = one_query()
    value['queries'][0]['status'] = status
    row = tool_catalog_rows(value)[0]
    assert row['query_status'] == status and not row['configured_eligible']
    assert row['query_approval_status'] == 'approved'


def test_source_and_query_both_require_current_approved_active_state():
    value = one_query()
    value['sources'][0]['status'] = value['queries'][0]['status'] = 'paused'
    value['sources'][0]['approval'] = {'status': 'pending', 'decided_by': None, 'decided_at': None}
    row = tool_catalog_rows(value)[0]
    assert not row['configured_eligible'] and row['source_approval_status'] == 'pending'
    value['sources'][0]['status'] = 'active'
    with pytest.raises(ValueError):
        tool_catalog_rows(value)  # An active, unapproved source is invalid core configuration.


@pytest.mark.parametrize('failure', ['connector_revision', 'connector_catalog', 'query_source', 'mode'])
def test_reviewed_reference_mismatches_are_rejected_before_rows_exist(failure):
    value = one_query()
    if failure == 'connector_revision':
        value['sources'][0]['connector_revision'] = 2
    elif failure == 'connector_catalog':
        value['connectors'][0]['actor_build'] = 'unreviewed-build'
    elif failure == 'query_source':
        value['queries'][0]['source_id'] = '00000000-0000-0000-0000-000000000099'
    else:
        value['queries'][0]['mode'] = 'comments'
    with pytest.raises(ValueError):
        tool_catalog_rows(value)


def test_static_caps_are_conservative_and_unknown_price_is_not_zero_or_live_budget():
    value = one_query()
    value['queries'][0].update(max_items=100, max_charge_microusd=300000, research_allowed=False)
    value['policy'].update(max_items_per_round=7, round_budget_microusd=70000, research_reserve_microusd=0)
    value['connectors'][0]['max_cost_microusd_per_item'] = None
    row = tool_catalog_rows(reviewed_catalog(value))[0]
    assert row['max_items'] == 7 and row['max_cost_microusd'] == 70000
    assert row['max_cost_microusd_per_item'] is None
    assert row['configured_eligible'] and not row['research_allowed']
    value['policy']['round_budget_microusd'] = 0
    zero = tool_catalog_rows(value)[0]
    assert zero['max_cost_microusd'] == 0 and zero['configured_eligible']
    assert 'source_limit_policy' not in json.loads(zero['payload_json'])


def test_callable_schema_has_fixed_query_identity_bounded_caps_and_exact_public_domains():
    value = one_query()
    schema = json.loads(tool_catalog_rows(value)[0]['input_schema_json'])
    assert schema['additionalProperties'] is False and schema['required'] == ['purpose', 'max_items']
    properties = schema['properties']
    assert set(properties) == {'purpose', 'max_items', 'target_override'}
    assert properties['purpose'] == {'type': 'string', 'minLength': 1, 'maxLength': 2000}
    assert properties['max_items'] == {'type': 'integer', 'minimum': 1, 'maximum': 10}
    target = properties['target_override']['anyOf'][0]
    for allowed in ('https://example.com/new-page', 'http://example.com:80/?q=news'):
        assert re.fullmatch(target['pattern'], allowed)
    for blocked in ('https://example.com.evil.test/', 'https://evil.test/', 'http://127.0.0.1/',
            'https://user:password@example.com/', 'https://example.com:8443/', 'https://example.com/#fragment'):
        assert not re.fullmatch(target['pattern'], blocked)


def test_target_override_requires_reviewed_connector_field_and_web_mode():
    value = one_query()
    value['connectors'][0]['research_fields'] = []
    schema = json.loads(tool_catalog_rows(reviewed_catalog(value))[0]['input_schema_json'])
    assert 'target_override' not in schema['properties']
    connector = value['connectors'][0]
    connector.update(operation='comments', modes=['comments'], research_fields=['target'])
    value['queries'][0]['mode'] = 'comments'
    value['sources'][0].update(category='social', platform='youtube')
    row = tool_catalog_rows(reviewed_catalog(value))[0]
    assert row['provider_kind'] == 'apify' and row['operation'] == 'comments'
    assert 'target_override' not in json.loads(row['input_schema_json'])['properties']


@pytest.mark.parametrize('field', ['api_key', 'token', 'password', 'private_key', 'authorization'])
def test_credentials_are_rejected_even_inside_provider_parameters(field):
    value = one_query()
    value['queries'][0]['parameters'][field] = 'synthetic-forbidden-value'
    with pytest.raises(ValueError, match='Credentials'):
        tool_catalog_rows(value)


def test_provider_inputs_and_secret_references_are_never_replicated_or_callable():
    value = one_query()
    value['queries'][0]['parameters'] = {'start_urls': ['https://unreviewed.example/'],
        'credential_name': 'synthetic-secret-reference', 'actor_id': 'unreviewed/actor'}
    row = tool_catalog_rows(value)[0]
    assert 'parameters' not in row['payload_json']
    assert 'synthetic-secret-reference' not in row['payload_json']
    assert 'unreviewed.example' not in row['payload_json']
    assert 'unreviewed/actor' not in row['payload_json']
    properties = json.loads(row['input_schema_json'])['properties']
    assert not {'actor_id', 'parameters', 'credential_name', 'endpoint', 'url'} & set(properties)


def test_url_credentials_and_fake_native_provider_are_rejected():
    value = one_query()
    value['queries'][0]['target'] = 'https://user:synthetic@example.com/'
    with pytest.raises(ValueError, match='URL credentials'):
        tool_catalog_rows(value)
    value = one_query()
    value['connectors'][0]['provider_kind'] = 'native_api'
    with pytest.raises(ValueError):
        tool_catalog_rows(reviewed_catalog(value))


def test_empty_query_catalog_is_a_real_empty_projection():
    value = one_query()
    value['queries'] = []
    assert tool_catalog_rows(value) == []


def test_prevalidated_json_projection_needs_only_standard_library():
    payload = core.Configuration.model_validate(configuration()['payload']).model_dump(mode='json')
    module = ROOT/'apps/backend/app/labs/ai_social_network/notebooks/sonda_tools.py'
    # -I -S excludes the app path and site-packages, as on the standalone Spark writer.
    program = """
import builtins, json, runpy, sys
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split('.')[0] in ('app', 'pydantic'):
        raise ImportError('Application dependency unavailable')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
namespace = runpy.run_path(sys.argv[1])
configuration = json.load(sys.stdin)
print(json.dumps(namespace['tool_catalog_rows'](configuration, validated=True)))
"""
    result = subprocess.run([sys.executable, '-I', '-S', '-c', program, str(module)],
        input=json.dumps(payload), capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == tool_catalog_rows(payload)


def test_prevalidated_mode_requires_json_instead_of_a_dto():
    with pytest.raises(TypeError, match='JSON data'):
        tool_catalog_rows(core.Configuration.model_validate(one_query()), validated=True)
