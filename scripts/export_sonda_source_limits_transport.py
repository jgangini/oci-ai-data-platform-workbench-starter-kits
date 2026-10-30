"""Export the pure Source Limits transport proposal; no storage or activation."""
import argparse
import copy
import json
from pathlib import Path
from uuid import UUID

from app.sonda_contract import ConfigMessage, ReceiptMessage, StreamEntry, encoded, sha256
from app.sonda_source_limits import SourceLimitsSnapshot, source_limits_digest
from app.sonda_source_limits_transport import (
    CORE_CONTRACT_SHA256, LIMITS_CAPABILITY_SHA256, LIMITS_TRANSPORT_MAX_BYTES,
    LIMITS_TRANSPORT_VERSION, SourceLimitsAck, SourceLimitsHead,
    check_applied_limits_ack, check_limits_head_successor, parse_limits_transport,
    source_limits_transport_schema,
)

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT/'contracts/sonda-source-limits-transport/v1'


def transport_evidence():
    """Core is already at 8; historical exact configs still prove sidecars 6→7→8."""
    uid = lambda number: str(UUID(int=number))
    core = json.loads((ROOT/'contracts/sonda/v1/fixtures/01-configuration.json').read_text(encoding='utf-8'))
    scope = {'registry_id': uid(1), 'generation_id': uid(2)}
    core_scope = {**scope, 'contract_version': '1.0.2', 'contract_sha256': CORE_CONTRACT_SHA256}
    source = core['payload']['sources'][0]
    policy = {'source_id': source['source_id'], 'source_revision': source['revision'],
        'policy_revision': 5, 'max_items_per_execution': 20,
        'max_cost_per_round_microusd': 100, 'max_starts_per_round': 2}
    core5 = copy.deepcopy(core)
    core5['payload'].update(revision=5, previous_revision=4)
    core5 = ConfigMessage.model_validate(core5).model_dump(mode='json')
    previous = SourceLimitsSnapshot.model_validate({**scope, 'catalog_revision': 5,
        'previous_catalog_sha256': 'a'*64, 'config_revision': 5,
        'configuration_payload_sha256': sha256(encoded(core5['payload'])), 'policies': [policy]})
    # The synthetic predecessor5 is an already-trusted anchor, not a fabricated
    # claim that this fixture proves historical snapshots1–4 or live resources.
    evidence = {'synthetic': True, 'current_core_revision': 8,
        'trusted_predecessor': previous.model_dump(mode='json'), 'steps': []}
    versions = {'ai_social_'+name: 8 for name in ('registry', 'sources', 'queries', 'criteria',
        'budget_rounds', 'document_current', 'observations', 'discoveries', 'evaluations',
        'evaluation_results', 'investigations', 'evidence', 'reports', 'proposals', 'daily',
        'executions', 'requests', 'study_state', 'study_case_state', 'case_work', 'budget_round_revisions',
        'tool_catalog', 'source_limits')}
    publication = {**scope, 'publication_id': sha256(encoded([scope, 8, versions])),
        'config_revision': 8, 'versions_json': encoded(versions).decode('utf-8'),
        'message_ids_json': encoded([uid(108)]).decode('utf-8'), 'published_at': '2026-01-01T00:10:00Z'}
    previous_head = None
    for revision in (6, 7, 8):
        config = copy.deepcopy(core5)
        config.update(message_id=uid(100 + revision), **core_scope)
        config['payload'].update(revision=revision, previous_revision=revision-1)
        config = ConfigMessage.model_validate(config).model_dump(mode='json')
        policy = {**policy, 'policy_revision': revision, 'max_cost_per_round_microusd': 100-revision}
        snapshot = SourceLimitsSnapshot.model_validate({**scope, 'catalog_revision': revision,
            'previous_catalog_sha256': source_limits_digest(previous), 'config_revision': revision,
            'configuration_payload_sha256': sha256(encoded(config['payload'])), 'policies': [policy]})
        snapshot_raw = encoded(snapshot.model_dump(mode='json'))
        ref = {**scope, 'source_limits_capability_version': '1.0.0',
            'source_limits_capability_sha256': LIMITS_CAPABILITY_SHA256,
            'core_contract_version': '1.0.2', 'core_contract_sha256': CORE_CONTRACT_SHA256,
            'catalog_revision': revision, 'snapshot_sha256': sha256(snapshot_raw),
            'previous_catalog_sha256': snapshot.previous_catalog_sha256,
            'config_revision': revision, 'configuration_payload_sha256': sha256(encoded(config['payload']))}
        head = SourceLimitsHead.model_validate({'kind': 'source_limits_head', 'transport_version': '1.0.0', 'ref': ref})
        if previous_head is not None:
            assert check_limits_head_successor(previous_head, head) == 'replace'
        config_raw = encoded(config)
        entry = StreamEntry.model_validate({**core_scope, 'kind': 'stream_entry', 'direction': 'huginn_to_aidp',
            'sequence': revision, 'message_id': config['message_id'], 'message_kind': 'configuration',
            'message_sha256': sha256(config_raw), 'message_bytes': len(config_raw), 'previous_entry_sha256': 'b'*64})
        receipt = ReceiptMessage.model_validate({**core_scope, 'kind': 'processing_receipt',
            'direction': 'aidp_to_huginn', 'message_id': uid(200+revision), 'created_at': '2026-01-01T00:10:00Z',
            'correlation_id': None, 'payload': {'source_sequence': revision, 'source_message_id': config['message_id'],
                'status': 'applied', 'code': 'applied', 'explanation': 'Synthetic historical core application.'}})
        receipt_raw = encoded(receipt.model_dump(mode='json'))
        ack = SourceLimitsAck.model_validate({'kind': 'source_limits_ack', 'transport_version': '1.0.0',
            'state': 'applied', 'ref': ref, 'core_configuration_message_id': config['message_id'],
            'core_configuration_source_sequence': revision, 'core_configuration_raw_sha256': sha256(config_raw),
            'core_configuration_payload_sha256': ref['configuration_payload_sha256'],
            'processing_receipt_message_id': str(receipt.message_id), 'processing_receipt_raw_sha256': sha256(receipt_raw),
            'processing_receipt_payload': receipt.payload.model_dump(mode='json'),
            'publication_id': publication['publication_id'], 'publication_config_revision': 8, 'serving_versions': versions})
        row = {**scope, 'catalog_revision': revision, 'config_revision': revision,
            'snapshot_sha256': ref['snapshot_sha256'], 'configuration_payload_sha256': ref['configuration_payload_sha256'],
            'serving_version': versions['ai_social_source_limits']}
        checked = check_applied_limits_ack(ack, snapshot_raw=snapshot_raw, configuration_raw=config_raw,
            entry_raw=encoded(entry.model_dump(mode='json')), receipt_raw=receipt_raw,
            publication=publication, applied_row=row, previous_snapshot=previous)
        for document in (head, checked):
            raw = encoded(document.model_dump(mode='json'))
            parse_limits_transport(raw, expected_sha256=sha256(raw), registry_id=UUID(int=1), generation_id=UUID(int=2))
        evidence['steps'].append({'snapshot': snapshot.model_dump(mode='json'), 'head': head.model_dump(mode='json'),
            'ack': ack.model_dump(mode='json'), 'core_configuration': config,
            'core_entry': entry.model_dump(mode='json'), 'core_receipt': receipt.model_dump(mode='json'),
            'publication': publication, 'applied_row': row})
        previous, previous_head = snapshot, head
    return evidence


def negative_vectors(evidence):
    original = evidence['steps'][0]['ack']
    cases = {'missing-ack': None}
    for name, change in (
        ('foreign-scope', lambda value: value['ref'].update(registry_id=str(UUID(int=99)))),
        ('wrong-pin', lambda value: value['ref'].update(source_limits_capability_sha256='0'*64)),
        ('wrong-receipt', lambda value: value['processing_receipt_payload'].update(source_sequence=99)),
        ('unknown-pending-state', lambda value: value.update(state='pending')),
    ):
        value = copy.deepcopy(original)
        change(value)
        cases[name] = value
    return {'synthetic': True, 'expected': 'reject_without_activation', 'cases': cases,
        'idempotent_ack': original, 'expected_identical_replay': 'same',
        'expected_changed_bytes_at_same_key': 'conflict'}


def artifacts():
    core_manifest = json.loads((ROOT/'contracts/sonda/v1/manifest.json').read_text(encoding='utf-8'))
    limits_manifest = json.loads((ROOT/'contracts/sonda-source-limits/v1/manifest.json').read_text(encoding='utf-8'))
    if core_manifest['contract_sha256'] != CORE_CONTRACT_SHA256 or limits_manifest['capability_sha256'] != LIMITS_CAPABILITY_SHA256:
        raise ValueError('Transport export requires exact frozen core and Source Limits pins')
    for directory, manifest in (('sonda', core_manifest), ('sonda-source-limits', limits_manifest)):
        package = ROOT/'contracts'/directory/'v1'
        for field in ('reference', 'schema'):
            if sha256((package/manifest[field+'_file']).read_bytes()) != manifest[field+'_sha256']:
                raise ValueError('Frozen transport dependency bytes drifted: '+directory+'/'+field)
    limits_pin = sha256(encoded({'schema_sha256': limits_manifest['schema_sha256'],
        'reference_sha256': limits_manifest['reference_sha256'], 'core_contract_sha256': CORE_CONTRACT_SHA256}))
    if limits_pin != LIMITS_CAPABILITY_SHA256:
        raise ValueError('Source Limits dependency no longer resolves to its frozen pin')
    reference = (ROOT/'apps/backend/app/sonda_source_limits_transport.py').read_bytes().replace(b'\r\n', b'\n')
    for source, target in ((b'app.sonda_contract', b'sonda_core_1_0_2'), (b'app.sonda_source_limits', b'sonda_source_limits_1_0_0')):
        if reference.count(b'from '+source+b' import ') != 1:
            raise ValueError('Portable transport import boundary changed')
        reference = reference.replace(b'from '+source+b' import ', b'from '+target+b' import ')
    schema = encoded(source_limits_transport_schema())
    evidence = transport_evidence()
    files = {'schema.json': schema, 'reference.py': reference,
        'evidence.json': encoded(evidence), 'negative-vectors.json': encoded(negative_vectors(evidence))}
    for step in evidence['steps']:
        revision = step['head']['ref']['catalog_revision']
        for kind in ('head', 'ack'):
            files[f'fixtures/{kind}-{revision}.json'] = encoded(step[kind])
    pin = sha256(encoded({'schema_sha256': sha256(schema), 'reference_sha256': sha256(reference),
        'core_contract_sha256': CORE_CONTRACT_SHA256, 'source_limits_capability_sha256': LIMITS_CAPABILITY_SHA256}))
    files['manifest.json'] = encoded({'capability_version': LIMITS_TRANSPORT_VERSION, 'capability_sha256': pin,
        'status': 'draft_not_accepted_or_activated', 'schema_file': 'schema.json', 'schema_sha256': sha256(schema),
        'reference_file': 'reference.py', 'reference_sha256': sha256(reference),
        'core_contract_version': '1.0.2', 'core_contract_sha256': CORE_CONTRACT_SHA256,
        'core_reference_file': '../../sonda/v1/reference.py', 'core_reference_module': 'sonda_core_1_0_2',
        'core_reference_sha256': core_manifest['reference_sha256'],
        'source_limits_capability_version': '1.0.0', 'source_limits_capability_sha256': LIMITS_CAPABILITY_SHA256,
        'source_limits_reference_file': '../../sonda-source-limits/v1/reference.py',
        'source_limits_reference_module': 'sonda_source_limits_1_0_0',
        'source_limits_reference_sha256': limits_manifest['reference_sha256'],
        'canonical_raw_bytes_required': True, 'max_document_bytes': LIMITS_TRANSPORT_MAX_BYTES,
        'fixtures_are_synthetic': True, 'fixture_hashes': {name: sha256(raw) for name, raw in files.items()
            if name.startswith('fixtures/') or name in ('evidence.json', 'negative-vectors.json')}})
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    for name, raw in artifacts().items():
        path = TARGET/name
        if args.check:
            if not path.is_file() or path.read_bytes() != raw:
                raise SystemExit('Source Limits transport drift: '+name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
    print('PASS draft Source Limits transport '+LIMITS_TRANSPORT_VERSION+'; no activation')


if __name__ == '__main__':
    main()
