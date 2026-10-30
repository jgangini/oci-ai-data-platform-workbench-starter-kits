"""Pure protected-byte transport checks; no cloud calls or fake ACK issuance."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
from uuid import UUID

import pytest

from app import sonda_contract as core
from app import sonda_source_limits_transport as transport

ROOT = Path(__file__).resolve().parents[3]
TARGET = ROOT/'contracts/sonda-source-limits-transport/v1'


def load_module(path, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def evidence(monkeypatch):
    exporter = load_module(ROOT/'scripts/export_sonda_source_limits_transport.py', 'limits_transport_export_tests', monkeypatch)
    return exporter.transport_evidence()


def proof_args(step, previous):
    return {'snapshot_raw': core.encoded(step['snapshot']), 'configuration_raw': core.encoded(step['core_configuration']),
        'entry_raw': core.encoded(step['core_entry']), 'receipt_raw': core.encoded(step['core_receipt']),
        'publication': step['publication'], 'applied_row': step['applied_row'], 'previous_snapshot': previous}


def test_historical_limits_6_7_8_match_own_configs_under_current_core_8(evidence):
    assert evidence['current_core_revision'] == 8
    previous = evidence['trusted_predecessor']
    for step in evidence['steps']:
        ack = transport.check_applied_limits_ack(step['ack'], **proof_args(step, previous))
        assert ack.ref.config_revision in (6, 7, 8)
        assert ack.publication_config_revision == 8
        assert len(ack.serving_versions) == 23
        assert set(ack.serving_versions) >= {'ai_social_source_limits', 'ai_social_registry'}
        previous = step['snapshot']


def test_historical_sidecar_cannot_use_only_latest_core_config(evidence):
    step = evidence['steps'][0]
    args = proof_args(step, evidence['trusted_predecessor'])
    args['configuration_raw'] = core.encoded(evidence['steps'][-1]['core_configuration'])
    with pytest.raises(ValueError, match='historical core configuration'):
        transport.check_applied_limits_ack(step['ack'], **args)


def test_ack_publication_hash_matches_existing_native_reader(evidence):
    from app.labs.ai_social_network.notebooks.agent.sonda_chat import publication_metadata
    step = evidence['steps'][0]
    versions = json.loads(step['publication']['versions_json'])
    binding = {key: step['ack']['ref'][key] for key in ('registry_id', 'generation_id')}
    metadata = publication_metadata(binding, step['publication'], 8, versions)
    assert metadata['publication_id'] == step['ack']['publication_id']
    assert metadata['config_revision'] == step['ack']['publication_config_revision']


@pytest.mark.parametrize('index', [0, 1, 2])
@pytest.mark.parametrize('kind', ['head', 'ack'])
def test_head_and_ack_exact_canonical_raw_round_trip(evidence, index, kind):
    raw = core.encoded(evidence['steps'][index][kind])
    parsed = transport.parse_limits_transport(raw, expected_sha256=core.sha256(raw), registry_id=UUID(int=1), generation_id=UUID(int=2))
    assert parsed.model_dump(mode='json') == evidence['steps'][index][kind]
    altered = b' '+raw
    with pytest.raises(ValueError, match='digest'):
        transport.parse_limits_transport(altered, expected_sha256=core.sha256(raw), registry_id=UUID(int=1), generation_id=UUID(int=2))
    with pytest.raises(ValueError, match='canonical'):
        transport.parse_limits_transport(altered, expected_sha256=core.sha256(altered), registry_id=UUID(int=1), generation_id=UUID(int=2))


@pytest.mark.parametrize('mutate', [
    lambda value: value.update(kind='unknown'),
    lambda value: value.update(state='pending'),
    lambda value: value.update(state='rejected'),
    lambda value: value.update(extra='unreviewed'),
    lambda value: value['ref'].update(extra='unreviewed'),
    lambda value: value['ref'].update(source_limits_capability_version='2.0.0'),
    lambda value: value['ref'].update(source_limits_capability_sha256='0'*64),
    lambda value: value['ref'].update(core_contract_sha256='0'*64),
    lambda value: value['ref'].update(catalog_revision=True),
    lambda value: value['ref'].update(config_revision='6'),
    lambda value: value['processing_receipt_payload'].update(status='rejected'),
    lambda value: value['processing_receipt_payload'].update(source_sequence=99),
    lambda value: value['processing_receipt_payload'].update(source_message_id=str(UUID(int=99))),
    lambda value: value.update(publication_config_revision=5),
    lambda value: value.update(publication_id='0'*64),
    lambda value: value['serving_versions'].pop('ai_social_source_limits'),
    lambda value: value['serving_versions'].update(ai_social_registry=True),
])
def test_unknown_unapplied_and_mismatched_ack_denied(evidence, mutate):
    value = copy.deepcopy(evidence['steps'][0]['ack'])
    mutate(value)
    raw = core.encoded(value)
    with pytest.raises(ValueError):
        transport.parse_limits_transport(raw, expected_sha256=core.sha256(raw), registry_id=UUID(int=1), generation_id=UUID(int=2))


def test_foreign_scope_denied_even_with_self_consistent_publication_hash(evidence):
    value = copy.deepcopy(evidence['steps'][0]['ack'])
    value['ref']['registry_id'] = str(UUID(int=99))
    scope = {key: value['ref'][key] for key in ('registry_id', 'generation_id')}
    value['publication_id'] = core.sha256(core.encoded([scope, value['publication_config_revision'], value['serving_versions']]))
    raw = core.encoded(value)
    with pytest.raises(ValueError, match='scope'):
        transport.parse_limits_transport(raw, expected_sha256=core.sha256(raw), registry_id=UUID(int=1), generation_id=UUID(int=2))


@pytest.mark.parametrize('field', ['snapshot_raw', 'configuration_raw', 'entry_raw', 'receipt_raw'])
def test_altered_protected_proof_bytes_are_not_an_ack(evidence, field):
    step = evidence['steps'][0]
    args = proof_args(step, evidence['trusted_predecessor'])
    if field == 'entry_raw':
        entry = copy.deepcopy(step['core_entry'])
        entry['message_bytes'] += 1
        args[field] = core.encoded(entry)
    elif field == 'receipt_raw':
        receipt = copy.deepcopy(step['core_receipt'])
        receipt['message_id'] = str(UUID(int=99))
        args[field] = core.encoded(receipt)
    else:
        args[field] = b' '+args[field]
    with pytest.raises(ValueError):
        transport.check_applied_limits_ack(step['ack'], **args)


@pytest.mark.parametrize('field,change', [
    ('core_configuration', {'generation_id': str(UUID(int=99))}),
    ('core_entry', {'generation_id': str(UUID(int=99))}),
    ('core_entry', {'sequence': 7}),
    ('core_entry', {'message_sha256': '0'*64}),
    ('core_receipt', {'generation_id': str(UUID(int=99))}),
])
def test_core_scope_and_stream_binding_are_verified(evidence, field, change):
    step = copy.deepcopy(evidence['steps'][0])
    step[field].update(change)
    with pytest.raises(ValueError):
        transport.check_applied_limits_ack(step['ack'], **proof_args(step, evidence['trusted_predecessor']))


@pytest.mark.parametrize('change', [
    {'serving_version': 9}, {'catalog_revision': 7}, {'config_revision': 7},
    {'registry_id': str(UUID(int=99))}, {'snapshot_sha256': '0'*64},
    {'configuration_payload_sha256': '0'*64}, {'serving_version': True},
    {'unknown': 'unreviewed'},
])
def test_applied_row_must_be_in_exact_published_table_version(evidence, change):
    step = copy.deepcopy(evidence['steps'][0])
    step['applied_row'].update(change)
    with pytest.raises(ValueError):
        transport.check_applied_limits_ack(step['ack'], **proof_args(step, evidence['trusted_predecessor']))


@pytest.mark.parametrize('change', [
    {'registry_id': str(UUID(int=99))}, {'generation_id': str(UUID(int=99))},
    {'config_revision': 7}, {'config_revision': True}, {'publication_id': '0'*64},
    {'versions_json': '{"ai_social_source_limits":8,"ai_social_source_limits":9}'},
])
def test_actual_writer_publication_must_match_ack(evidence, change):
    step = copy.deepcopy(evidence['steps'][0])
    step['publication'].update(change)
    with pytest.raises(ValueError):
        transport.check_applied_limits_ack(step['ack'], **proof_args(step, evidence['trusted_predecessor']))


def test_serving_map_allows_22_to_23_but_never_exceeds_26(evidence):
    value = copy.deepcopy(evidence['steps'][0]['ack'])
    assert len(value['serving_versions']) == 23
    scope = {key: value['ref'][key] for key in ('registry_id', 'generation_id')}
    for count in (22, 23, 26, 27):
        changed = copy.deepcopy(value)
        versions = {key: index for index, key in enumerate(list(value['serving_versions'])[:count])}
        while len(versions) < count:
            versions['ai_social_extra_'+str(len(versions))] = 0
        changed['serving_versions'] = versions
        # The 22-map fixture retains required source_limits and registry explicitly.
        if 'ai_social_source_limits' not in versions:
            versions.pop(next(key for key in reversed(versions) if key != 'ai_social_registry'))
            versions['ai_social_source_limits'] = 8
        changed['publication_id'] = core.sha256(core.encoded([scope, 8, versions]))
        if count <= 26:
            assert len(transport.SourceLimitsAck.model_validate(changed).serving_versions) == count
        else:
            with pytest.raises(ValueError):
                transport.SourceLimitsAck.model_validate(changed)


def test_paths_are_bound_and_accept_no_user_path_or_noncanonical_uuid(evidence):
    ref = evidence['steps'][0]['head']['ref']
    prefix = 'ai_social_network/registries/'+str(UUID(int=1))+'/generations/'+str(UUID(int=2))+'/control/source_limits/v1/'
    assert transport.source_limits_head_key(UUID(int=1), UUID(int=2)) == prefix+'head.json'
    assert transport.source_limits_snapshot_key(ref) == prefix+'snapshots/6-'+ref['snapshot_sha256']+'.json'
    assert transport.source_limits_ack_key(ref) == prefix+'acks/6-'+ref['snapshot_sha256']+'.json'
    for invalid in ('../other', '00000000-0000-0000-0000-00000000000A'):
        with pytest.raises(ValueError):
            transport.source_limits_head_key(invalid, UUID(int=2))
    with pytest.raises(ValueError):
        transport.source_limits_snapshot_key({**ref, 'snapshot_sha256': '../key'})


def test_missing_ack_and_noncanonical_duplicate_oversized_documents_deny(evidence):
    step = evidence['steps'][0]
    with pytest.raises(ValueError):
        transport.check_applied_limits_ack(None, **proof_args(step, evidence['trusted_predecessor']))
    for raw in (b'', b' '* (transport.LIMITS_TRANSPORT_MAX_BYTES + 1),
        b'{"kind":"source_limits_head","kind":"source_limits_ack"}'):
        with pytest.raises(ValueError):
            transport.parse_limits_transport(raw, expected_sha256=core.sha256(raw), registry_id=UUID(int=1), generation_id=UUID(int=2))


def test_immutable_ack_replay_keeps_same_bytes_and_rejects_conflict(evidence):
    raw = core.encoded(evidence['steps'][0]['ack'])
    assert transport.immutable_limits_write(None, raw) == 'create'
    assert transport.immutable_limits_write(raw, raw) == 'same'
    with pytest.raises(ValueError, match='Conflicting'):
        transport.immutable_limits_write(raw, raw+b' ')
    with pytest.raises(ValueError):
        transport.immutable_limits_write(bytearray(raw), raw)


def test_head_successor_requires_same_scope_exact_predecessor_and_cas(evidence):
    before, after = [step['head'] for step in evidence['steps'][:2]]
    assert transport.check_limits_head_successor(before, before) == 'same'
    assert transport.check_limits_head_successor(before, after) == 'replace'
    with pytest.raises(ValueError):
        transport.check_limits_head_successor(None, before)
    for changes in ({'previous_catalog_sha256': '0'*64}, {'catalog_revision': 8},
        {'config_revision': 6}, {'generation_id': str(UUID(int=99))}):
        changed = copy.deepcopy(after)
        changed['ref'].update(changes)
        with pytest.raises(ValueError):
            transport.check_limits_head_successor(before, changed)
    initial = copy.deepcopy(before)
    initial['ref'].update(catalog_revision=1, config_revision=1, previous_catalog_sha256=None)
    assert transport.check_limits_head_successor(None, initial) == 'create'


def test_export_matches_source_and_portable_existing_frozen_modules(monkeypatch):
    exporter = load_module(ROOT/'scripts/export_sonda_source_limits_transport.py', 'limits_transport_drift_tests', monkeypatch)
    files = exporter.artifacts()
    assert all((TARGET/name).read_bytes() == raw for name, raw in files.items())
    manifest = json.loads(files['manifest.json'])
    assert manifest['status'] == 'draft_not_accepted_or_activated'
    for prefix in ('core', 'source_limits'):
        path = (TARGET/manifest[prefix+'_reference_file']).resolve()
        assert core.sha256(path.read_bytes()) == manifest[prefix+'_reference_sha256']
        load_module(path, manifest[prefix+'_reference_module'], monkeypatch)
    portable = load_module(TARGET/'reference.py', 'limits_transport_portable_tests', monkeypatch)
    assert all(core.sha256(files[name]) == digest for name, digest in manifest['fixture_hashes'].items())
    evidence = json.loads(files['evidence.json'])
    previous = evidence['trusted_predecessor']
    for step in evidence['steps']:
        assert portable.check_applied_limits_ack(step['ack'], **proof_args(step, previous)).ref.catalog_revision == step['snapshot']['catalog_revision']
        previous = step['snapshot']
