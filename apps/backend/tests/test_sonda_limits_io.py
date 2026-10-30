"""Offline protected-object adapter regressions; no OCI, Spark, model or provider calls."""
import copy
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import UUID

import pytest

from app import sonda_contract as core
from app import sonda_source_limits as limits
from app import sonda_source_limits_transport as codec
from app.labs.ai_social_network.notebooks import sonda_limits_io as io

ROOT = Path(__file__).resolve().parents[3]


def binding():
    return {'registry_id': str(UUID(int=1)), 'generation_id': str(UUID(int=2)),
        'catalog': 'oci_medallion', 'gold_schema': 'oci_gold', 'control_schema': 'oci_control',
        'namespace': 'fixture_ns', 'source_bucket': 'source', 'gold_bucket': 'gold',
        'artifacts_bucket': 'artifacts', 'contract_sha256': limits.CORE_CONTRACT_SHA256}


class Objects:
    def __init__(self):
        self.data, self.puts, self.reads = {}, [], []
        self.write_mode = None
        self.read_error_key = None

    def get(self, role, key):
        assert role == 'artifacts'
        self.reads.append(key)
        if key == self.read_error_key:
            raise OSError('Synthetic protected read unavailable')
        return SimpleNamespace(data=self.data[key]) if key in self.data else None

    def put(self, role, key, raw, expected):
        assert role == 'artifacts' and expected is None
        assert key not in self.data
        self.puts.append((key, raw))
        if self.write_mode == 'fail_missing':
            raise OSError('Synthetic uncertain write')
        self.data[key] = b'conflicting bytes' if self.write_mode == 'fail_different' else raw
        if self.write_mode in ('fail_same', 'fail_different'):
            raise OSError('Synthetic uncertain write')


class Row(dict):
    def asDict(self):
        return dict(self)


class Spark:
    def __init__(self, fixture):
        self.fixture, self.queries = fixture, []
        self.publication_available = True
        self.core_revision = 8

    def sql(self, query):
        self.queries.append(query)
        rows = []
        if query.startswith('DESCRIBE HISTORY'):
            rows = [{'version': 9}, {'version': 8}]
        elif 'ai_social_publication VERSION AS OF' in query:
            if self.publication_available:
                rows = [self.fixture.publication]
        elif 'ai_social_source_limits VERSION AS OF' in query:
            revision = int(re.search(r'catalog_revision=(\d+)', query)[1])
            rows = [self.fixture.rows[revision]] if revision in self.fixture.rows else []
        elif 'ORDER BY catalog_revision DESC LIMIT 1' in query:
            rows = [self.fixture.rows[max(self.fixture.rows)]] if self.fixture.rows else []
        elif 'SELECT COALESCE(MAX(config_revision),0)' in query:
            assert 'oci_medallion.oci_gold.ai_social_registry' in query
            assert io._limits_where(binding()) in query
            rows = [{'config_revision': self.core_revision}]
        else:
            raise AssertionError('Unexpected fixture SQL: ' + query)
        return SimpleNamespace(collect=lambda: [Row(row) for row in rows])


@pytest.fixture
def installation(monkeypatch):
    spec = importlib.util.spec_from_file_location('limits_io_fixture_export', ROOT/'scripts/export_sonda_source_limits_transport.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    template = module.transport_evidence()['steps'][0]
    state = SimpleNamespace(objects=Objects(), rows={}, steps={}, applications=[], crash_after_apply=False,
        publication=copy.deepcopy(template['publication']))
    bound = binding()
    state.inbound = SimpleNamespace(scope={**io._limits_scope(bound), 'contract_sha256': limits.CORE_CONTRACT_SHA256},
        root=core.stream_prefix(bound['registry_id'], bound['generation_id'], 'huginn_to_aidp'),
        direction='huginn_to_aidp', source_prefix='aidp-social')
    state.inbound._entry = lambda sequence: state.inbound.root + 'entries/' + f'{sequence:020d}.json'
    previous = None
    for revision in range(1, 9):
        configuration = copy.deepcopy(template['core_configuration'])
        configuration['message_id'] = str(UUID(int=100 + revision))
        configuration['payload'].update(revision=revision, previous_revision=revision-1)
        configuration = core.ConfigMessage.model_validate(configuration)
        snapshot = limits.SourceLimitsSnapshot.model_validate({**io._scope(bound), 'catalog_revision': revision,
            'previous_catalog_sha256': limits.source_limits_digest(previous) if previous else None,
            'config_revision': revision,
            'configuration_payload_sha256': core.sha256(core.encoded(configuration.payload.model_dump(mode='json'))),
            'policies': [{'source_id': str(source.source_id), 'source_revision': source.revision,
                'policy_revision': revision, 'max_items_per_execution': 10,
                'max_cost_per_round_microusd': 100, 'max_starts_per_round': 2}
                for source in sorted(configuration.payload.sources, key=lambda source: str(source.source_id))]})
        raw = core.encoded(snapshot.model_dump(mode='json'))
        ref = io._limits_ref(snapshot, core.sha256(raw))
        entry = copy.deepcopy(template['core_entry'])
        entry.update(sequence=revision, message_id=str(configuration.message_id),
            message_sha256=core.sha256(core.encoded(configuration.model_dump(mode='json'))),
            message_bytes=len(core.encoded(configuration.model_dump(mode='json'))),
            previous_entry_sha256='b'*64 if revision > 1 else None)
        receipt = copy.deepcopy(template['core_receipt'])
        receipt.update(message_id=str(UUID(int=200+revision)))
        receipt['payload'].update(source_message_id=str(configuration.message_id), source_sequence=revision)
        proof = {'configuration': configuration, 'source_sequence': revision,
            'configuration_raw': core.encoded(configuration.model_dump(mode='json')),
            'entry_raw': core.encoded(entry), 'receipt_raw': core.encoded(receipt)}
        row = {**io._scope(bound), 'catalog_revision': revision, 'config_revision': revision,
            'configuration_payload_sha256': snapshot.configuration_payload_sha256,
            'snapshot_sha256': ref.snapshot_sha256, 'payload_json': raw.decode(),
            'core_configuration_message_id': str(configuration.message_id),
            'core_configuration_source_sequence': revision, 'core_configuration_raw_sha256': core.sha256(proof['configuration_raw'])}
        ack = io._limits_ack_value(ref, proof, state.publication)
        step = {'snapshot': snapshot, 'raw': raw, 'ref': ref, 'row': row, 'proof': proof, 'ack': ack}
        state.steps[revision] = step
        config_path = core.message_target('configuration', configuration.message_id, **io._scope(bound),
            direction='huginn_to_aidp', source_prefix='aidp-social')['key']
        receipt_path = core.message_target('processing_receipt', receipt['message_id'], **io._scope(bound),
            direction='aidp_to_huginn', source_prefix='aidp-social')['key']
        state.objects.data[config_path] = proof['configuration_raw']
        state.objects.data[receipt_path] = proof['receipt_raw']
        state.objects.data[state.inbound._entry(revision)] = proof['entry_raw']
        state.objects.data[codec.source_limits_snapshot_key(ref)] = raw
        if revision <= 5:
            state.rows[revision] = row
            state.objects.data[codec.source_limits_ack_key(ref)] = core.encoded(ack.model_dump(mode='json'))
            witness = {key: row[key] for key in ('registry_id', 'generation_id', 'catalog_revision',
                'config_revision', 'snapshot_sha256', 'configuration_payload_sha256')}
            witness['serving_version'] = ack.serving_versions['ai_social_source_limits']
            state.objects.data[io._limits_proof_key(ref)] = core.encoded(io._limits_journal(ack, state.publication, witness))
        previous = snapshot
    state.head_key = codec.source_limits_head_key(**io._scope(bound))
    state.objects.data[state.head_key] = core.encoded({'kind': 'source_limits_head',
        'transport_version': '1.0.0', 'ref': state.steps[8]['ref'].model_dump(mode='json')})

    def find(spark, bound, suffix, **where):
        if suffix == 'source_limits':
            return [copy.deepcopy(state.rows[where['catalog_revision']])] if where['catalog_revision'] in state.rows else []
        revision = where.get('config_revision') or UUID(where.get('message_id') or where.get('source_message_id')).int - 100
        step = state.steps[revision]
        if suffix == 'registry':
            return [{'message_json': step['proof']['configuration_raw'].decode()}]
        if suffix == 'receipts':
            return [{'state': 'APPLIED', 'code': 'applied', 'source_sequence': revision,
                'raw_sha256': core.sha256(step['proof']['configuration_raw'])}]
        if suffix == 'outbox':
            assert where['kind'] == 'processing_receipt'
            return [{'payload_json': step['proof']['receipt_raw'].decode(), 'status': 'ready'}]
        raise AssertionError(suffix)

    def apply(spark, bound, snapshot, *, snapshot_sha256, core_message_id):
        revision = snapshot['catalog_revision']
        assert snapshot == state.steps[revision]['snapshot'].model_dump(mode='json')
        assert snapshot_sha256 == state.steps[revision]['ref'].snapshot_sha256
        assert core_message_id == str(state.steps[revision]['proof']['configuration'].message_id)
        state.applications.append(revision)
        state.rows[revision] = copy.deepcopy(state.steps[revision]['row'])
        if state.crash_after_apply:
            state.crash_after_apply = False
            raise RuntimeError('Synthetic crash after committed Delta before ACK')
        return {'publication': copy.deepcopy(state.publication), 'snapshot_row': copy.deepcopy(state.rows[revision])}

    state.find = find
    state.spark = Spark(state)
    monkeypatch.setattr(io, '_find', find)
    monkeypatch.setattr(io, 'apply_source_limits', apply)
    return state


def consume(state, **kwargs):
    return io.consume_source_limits(state.spark, binding(), state.objects, state.inbound, **kwargs)


def test_applied_core8_historical_limits6_7_8_apply_in_order_and_replay(installation):
    state = installation
    result = consume(state)
    assert result == {'state': 'applied', 'applied_revisions': 3, 'catalog_revision': 8, 'expected_catalog_revision': 8}
    assert state.applications == [6, 7, 8]
    assert len(state.objects.puts) == 6
    assert len([key for key, _ in state.objects.puts if '/acks/' in key]) == 3
    before = list(state.objects.puts)
    assert consume(state)['applied_revisions'] == 0
    assert state.objects.puts == before and state.applications == [6, 7, 8]
    for revision in (6, 7, 8):
        ref = state.steps[revision]['ref']
        ack = codec.parse_limits_transport(state.objects.data[codec.source_limits_ack_key(ref)],
            expected_sha256=core.sha256(state.objects.data[codec.source_limits_ack_key(ref)]), **io._limits_scope(binding()))
        assert ack.ref.config_revision == revision and ack.publication_config_revision == 8


def test_crash_after_delta_recovers_same_revision_ack_before_advancing(installation):
    state = installation
    state.crash_after_apply = True
    with pytest.raises(RuntimeError, match='committed Delta'):
        consume(state)
    assert max(state.rows) == 6 and not state.objects.puts
    assert consume(state)['state'] == 'applied'
    assert state.applications == [6, 6, 7, 8]
    assert len(state.objects.puts) == 6


def test_missing_processing_receipt_defers_without_ack_or_advancing(installation):
    state = installation
    receipt_id = json.loads(state.steps[6]['proof']['receipt_raw'])['message_id']
    key = core.message_target('processing_receipt', receipt_id, **io._scope(binding()),
        direction='aidp_to_huginn', source_prefix='aidp-social')['key']
    saved = state.objects.data.pop(key)
    assert consume(state) == {'state': 'pending', 'applied_revisions': 0, 'catalog_revision': 5, 'expected_catalog_revision': 8}
    assert not state.objects.puts and not state.applications
    state.objects.data[key] = saved
    assert consume(state)['state'] == 'applied'


def test_applied_unacked_snapshot_blocks_later_work_until_receipt_published(installation):
    state = installation
    state.rows[6] = state.steps[6]['row']
    receipt_id = json.loads(state.steps[6]['proof']['receipt_raw'])['message_id']
    path = core.message_target('processing_receipt', receipt_id, **io._scope(binding()),
        direction='aidp_to_huginn', source_prefix='aidp-social')['key']
    state.objects.data.pop(path)
    assert consume(state)['state'] == 'pending'
    assert not state.applications and not state.objects.puts


def test_missing_head_after_policy_is_error_not_legacy(installation):
    state = installation
    state.objects.data.pop(state.head_key)
    with pytest.raises(ValueError, match='head disappeared'):
        consume(state)
    assert not state.objects.puts
    state.rows.clear()
    assert consume(state)['state'] == 'legacy'


@pytest.mark.parametrize('mode,success', [('fail_same', True), ('fail_different', False), ('fail_missing', False)])
def test_uncertain_create_resolves_only_exact_same_key_bytes(installation, mode, success):
    state = installation
    state.objects.write_mode = mode
    if success:
        assert consume(state, max_revisions=1)['catalog_revision'] == 6
    else:
        with pytest.raises((ValueError, OSError)):
            consume(state, max_revisions=1)
    assert len(state.objects.puts) == (2 if success else 1) and state.applications == [6]


def test_batch_bound_and_followup_begin_from_last_applied_snapshot(installation):
    state = installation
    assert consume(state, max_revisions=1)['state'] == 'pending'
    assert state.applications == [6]
    assert consume(state, max_revisions=1)['catalog_revision'] == 7
    assert consume(state, max_revisions=1)['state'] == 'applied'
    assert state.applications == [6, 7, 8]


@pytest.mark.parametrize('failure', ['entry_hash', 'receipt_payload', 'snapshot_scope', 'head_reference'])
def test_wrong_protected_proof_cannot_issue_ack(installation, failure):
    state = installation
    step = state.steps[6]
    if failure == 'entry_hash':
        raw = json.loads(step['proof']['entry_raw'])
        raw['message_sha256'] = '0'*64
        state.objects.data[state.inbound._entry(6)] = core.encoded(raw)
    elif failure == 'receipt_payload':
        key = core.message_target('processing_receipt', str(UUID(int=206)), **io._scope(binding()),
            direction='aidp_to_huginn', source_prefix='aidp-social')['key']
        raw = json.loads(state.objects.data[key])
        raw['payload']['source_sequence'] = 8
        state.objects.data[key] = core.encoded(raw)
    elif failure == 'snapshot_scope':
        raw = step['snapshot'].model_dump(mode='json')
        raw['registry_id'] = str(UUID(int=99))
        state.objects.data[codec.source_limits_snapshot_key(step['ref'])] = core.encoded(raw)
    else:
        head = json.loads(state.objects.data[state.head_key])
        head['ref']['config_revision'] = 7
        state.objects.data[state.head_key] = core.encoded(head)
    with pytest.raises(ValueError):
        consume(state)
    assert not state.objects.puts and not state.applications


def test_current_existing_ack_requires_actual_historical_publication(installation):
    state = installation
    state.objects.data.pop(io._limits_proof_key(state.steps[5]['ref']))
    state.spark.publication_available = False
    with pytest.raises(ValueError, match='bounded history window'):
        consume(state)
    assert not state.applications and not state.objects.puts


def test_private_prepared_proof_survives_more_than64_new_publications(installation):
    state = installation
    assert consume(state, max_revisions=1)['catalog_revision'] == 6
    state.spark.queries.clear()
    state.spark.publication_available = False
    state.objects.data[state.head_key] = core.encoded({'kind': 'source_limits_head',
        'transport_version': '1.0.0', 'ref': state.steps[6]['ref'].model_dump(mode='json')})
    assert consume(state)['state'] == 'pending'
    assert state.applications == [6]
    assert not any('DESCRIBE HISTORY' in query for query in state.spark.queries)


def test_crash_after_prepared_journal_recovers_exact_ack_without_new_publication(installation):
    state = installation
    original_put = state.objects.put
    def fail_ack(role, key, raw, expected):
        if '/acks/' in key:
            raise OSError('Synthetic crash between journal and ACK')
        return original_put(role, key, raw, expected)
    state.objects.put = fail_ack
    with pytest.raises(OSError, match='between journal'):
        consume(state, max_revisions=1)
    assert state.applications == [6]
    assert io._limits_proof_key(state.steps[6]['ref']) in state.objects.data
    state.objects.put = original_put
    state.spark.queries.clear()
    state.spark.publication_available = False
    state.objects.data[state.head_key] = core.encoded({'kind': 'source_limits_head',
        'transport_version': '1.0.0', 'ref': state.steps[6]['ref'].model_dump(mode='json')})
    assert consume(state)['state'] == 'pending'
    assert state.applications == [6]
    assert state.objects.data[codec.source_limits_ack_key(state.steps[6]['ref'])] == core.encoded(
        state.steps[6]['ack'].model_dump(mode='json'))
    assert not any('DESCRIBE HISTORY' in query for query in state.spark.queries)


@pytest.mark.parametrize('failure', ['unknown_field', 'wrong_snapshot', 'wrong_witness', 'noncanonical'])
def test_private_proof_journal_rejects_invalid_or_unbound_facts(installation, failure):
    state = installation
    key = io._limits_proof_key(state.steps[5]['ref'])
    value = json.loads(state.objects.data[key])
    if failure == 'unknown_field':
        value['issuer_claim'] = 'not-authentication'
    elif failure == 'wrong_snapshot':
        value['ref']['snapshot_sha256'] = '0'*64
    elif failure == 'wrong_witness':
        value['applied_row']['config_revision'] = 4
    raw = core.encoded(value)
    state.objects.data[key] = b' ' + raw if failure == 'noncanonical' else raw
    with pytest.raises(ValueError):
        consume(state)
    assert not state.applications and not state.objects.puts


def test_current_head_same_digest_different_core_reference_is_rejected(installation):
    state = installation
    head = {'kind': 'source_limits_head', 'transport_version': '1.0.0',
        'ref': state.steps[5]['ref'].model_dump(mode='json')}
    head['ref']['config_revision'] = 6
    state.objects.data[state.head_key] = core.encoded(head)
    with pytest.raises(ValueError, match='head differs'):
        consume(state)


def test_read_error_propagates_and_does_not_become_legacy(installation):
    state = installation
    state.objects.read_error_key = state.head_key
    with pytest.raises(OSError, match='read unavailable'):
        consume(state)
    assert not state.objects.puts


def test_foreign_inbound_stream_is_rejected_before_storage(installation):
    state = installation
    state.inbound.scope['registry_id'] = UUID(int=99)
    with pytest.raises(ValueError, match='bound inbound'):
        consume(state)
    assert not state.objects.reads


@pytest.mark.parametrize('bound', [0, 26, True])
def test_invalid_batch_size_rejected(installation, bound):
    with pytest.raises(ValueError, match='between one and 25'):
        consume(installation, max_revisions=bound)


def test_bounded_chain_gap_fails_before_snapshot_reads(installation):
    state = installation
    value = json.loads(state.objects.data[state.head_key])
    value['ref']['catalog_revision'] = 70
    state.objects.data[state.head_key] = core.encoded(value)
    with pytest.raises(ValueError, match='bounded history window'):
        consume(state)
    assert not any('/snapshots/' in key for key in state.objects.reads)
    assert not state.applications and not state.objects.puts


def test_empty_install_can_apply_canonical_first_policy(installation):
    state = installation
    state.rows.clear()
    state.objects.data[state.head_key] = core.encoded({'kind': 'source_limits_head',
        'transport_version': '1.0.0', 'ref': state.steps[1]['ref'].model_dump(mode='json')})
    state.objects.data.pop(codec.source_limits_ack_key(state.steps[1]['ref']))
    state.objects.data.pop(io._limits_proof_key(state.steps[1]['ref']))
    state.spark.core_revision = 1
    assert consume(state)['state'] == 'applied'
    assert state.applications == [1]


def test_saved_row_conflicting_configuration_metadata_blocks_recovery(installation):
    state = installation
    state.rows[5]['config_revision'] = 4
    with pytest.raises(ValueError, match='row metadata differs'):
        consume(state)
    assert not state.applications and not state.objects.puts


def test_historical_head7_core8_acks_but_waits_for_current_head8(installation):
    state = installation
    head = {'kind': 'source_limits_head', 'transport_version': '1.0.0',
        'ref': state.steps[7]['ref'].model_dump(mode='json')}
    state.objects.data[state.head_key] = core.encoded(head)
    assert consume(state) == {'state': 'pending', 'applied_revisions': 2,
        'catalog_revision': 7, 'expected_catalog_revision': 7}
    assert state.applications == [6, 7]
    ack_keys = [key for key, _ in state.objects.puts if '/acks/' in key]
    assert ack_keys == [codec.source_limits_ack_key(state.steps[revision]['ref']) for revision in (6, 7)]
    before_puts = list(state.objects.puts)
    state.spark.queries.clear()
    assert consume(state)['state'] == 'pending'
    assert state.objects.puts == before_puts and state.applications == [6, 7]
    assert not any('DESCRIBE HISTORY' in query for query in state.spark.queries)
    head['ref'] = state.steps[8]['ref'].model_dump(mode='json')
    state.objects.data[state.head_key] = core.encoded(head)
    assert consume(state) == {'state': 'applied', 'applied_revisions': 1,
        'catalog_revision': 8, 'expected_catalog_revision': 8}
    assert state.applications == [6, 7, 8]


@pytest.mark.parametrize('revision', [None, '8', True, 4])
def test_invalid_core_revision_is_not_treated_as_current_policy(installation, revision):
    state = installation
    state.spark.core_revision = revision
    state.objects.data[state.head_key] = core.encoded({'kind': 'source_limits_head',
        'transport_version': '1.0.0', 'ref': state.steps[5]['ref'].model_dump(mode='json')})
    with pytest.raises(ValueError, match='historical core revision'):
        consume(state)
    assert not state.objects.puts and not state.applications
