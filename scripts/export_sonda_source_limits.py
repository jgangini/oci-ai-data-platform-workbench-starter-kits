"""Export Source Limits 1.0.0 draft and synthetic vectors; no runtime activation."""
import argparse
import copy
import json
from pathlib import Path
from uuid import UUID

from app.sonda_contract import Configuration, encoded, sha256
from app.sonda_source_limits import (
    CORE_CONTRACT_SHA256, SOURCE_LIMITS_MAX_BYTES, SOURCE_LIMITS_VERSION,
    SourceLimitsSnapshot, SourceRoundLedger, check_source_limits,
    parse_source_limits, reserve_source_start, source_limits_digest,
)

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT/'contracts/sonda-source-limits/v1'


def synthetic_vectors():
    """Full-state successive snapshots, exact core payloads and uncertain ledger."""
    raw = json.loads((ROOT/'contracts/sonda/v1/fixtures/01-configuration.json').read_text(encoding='utf-8'))
    config = Configuration.model_validate(raw['payload']).model_dump(mode='json')
    uid = lambda number: str(UUID(int=number))
    source = config['sources'][0]
    policy = {'source_id': source['source_id'], 'source_revision': source['revision'], 'policy_revision': 1,
        'max_items_per_execution': 20, 'max_cost_per_round_microusd': 100, 'max_starts_per_round': 2}
    initial = {'kind': 'source_limits', 'capability_version': SOURCE_LIMITS_VERSION,
        'core_contract_version': '1.0.2', 'core_contract_sha256': CORE_CONTRACT_SHA256,
        'registry_id': uid(1), 'generation_id': uid(2), 'catalog_revision': 1,
        'previous_catalog_sha256': None, 'config_revision': config['revision'],
        'configuration_payload_sha256': sha256(encoded(config)), 'policies': [policy]}
    cases = [('01-initial', config, initial)]
    changes = (
        ('02-lowered-caps', {'policy_revision': 2, 'max_items_per_execution': 2,
            'max_cost_per_round_microusd': 20, 'max_starts_per_round': 1}),
        ('03-null-cap-removal', {'policy_revision': 3, 'max_items_per_execution': None,
            'max_cost_per_round_microusd': None, 'max_starts_per_round': None}),
        ('04-source-removal', None),
    )
    previous = None
    for name, changeset in changes:
        _, prior_config, prior_snapshot = cases[-1]
        previous = SourceLimitsSnapshot.model_validate(prior_snapshot)
        config = copy.deepcopy(prior_config)
        config.update(revision=config['revision'] + 1, previous_revision=config['revision'])
        current = copy.deepcopy(prior_snapshot)
        current.update(catalog_revision=current['catalog_revision'] + 1,
            previous_catalog_sha256=source_limits_digest(previous), config_revision=config['revision'])
        if changeset is None:
            config.update(sources=[], queries=[], criteria=[])
            current['policies'] = []
        else:
            current['policies'][0].update(changeset)
        current['configuration_payload_sha256'] = sha256(encoded(config))
        cases.append((name, config, current))
    files = {}
    previous = None
    for name, config, value in cases:
        checked = check_source_limits(value, config, registry_id=UUID(int=1),
            generation_id=UUID(int=2), previous=previous)
        raw = encoded(checked.model_dump(mode='json'))
        parse_source_limits(raw, expected_sha256=sha256(raw), configuration=config,
            registry_id=UUID(int=1), generation_id=UUID(int=2), previous=previous)
        files['fixtures/'+name+'.json'] = raw
        files['fixtures/'+name+'-core-configuration.json'] = encoded(config)
        previous = checked
    uncertain = SourceRoundLedger.model_validate({'registry_id': uid(1), 'generation_id': uid(2),
        'round_id': uid(3), 'source_id': source['source_id'], 'spent_microusd': 10,
        'pending_reserved_microusd': 70, 'starts': 1})
    files['fixtures/ledger-uncertain.json'] = encoded(uncertain.model_dump(mode='json'))
    start = {'items': 2, 'reserve_microusd': 10, 'verified_free': False}
    reserved = reserve_source_start(uncertain, start, policy=initial['policies'][0])
    files['fixtures/reservation-sequence.json'] = encoded({'synthetic': True,
        'commit_requires_existing_broker_lock_or_cas': True,
        'before': uncertain.model_dump(mode='json'), 'start': start,
        'policy': initial['policies'][0], 'after_reservation': reserved.model_dump(mode='json'),
        'after_provider_response_lost': reserved.model_dump(mode='json'),
        'lowered_policy': cases[1][2]['policies'][0],
        'expected_start_after_lowering': 'denied', 'expected_reconciliation': 'same_identity'})
    return files


def artifacts():
    core_manifest = json.loads((ROOT/'contracts/sonda/v1/manifest.json').read_text(encoding='utf-8'))
    core_raw = (ROOT/'apps/backend/app/sonda_contract.py').read_bytes().replace(b'\r\n', b'\n')
    if (core_manifest['contract_sha256'] != CORE_CONTRACT_SHA256 or
        sha256(core_raw) != core_manifest['reference_sha256']):
        raise ValueError('The published core pin and reference must remain byte-exact')
    reference = (ROOT/'apps/backend/app/sonda_source_limits.py').read_bytes().replace(b'\r\n', b'\n')
    if reference.count(b'from app.sonda_contract import ') != 1:
        raise ValueError('The portable reference import boundary changed')
    reference = reference.replace(b'from app.sonda_contract import ', b'from sonda_core_1_0_2 import ')
    schema = encoded(SourceLimitsSnapshot.model_json_schema())
    capability_hash = sha256(encoded({'schema_sha256': sha256(schema),
        'reference_sha256': sha256(reference), 'core_contract_sha256': CORE_CONTRACT_SHA256}))
    files = {'schema.json': schema, 'reference.py': reference, **synthetic_vectors()}
    manifest = {'capability_version': SOURCE_LIMITS_VERSION,
        'status': 'draft_not_accepted_or_activated', 'capability_sha256': capability_hash,
        'schema_file': 'schema.json', 'schema_sha256': sha256(schema),
        'reference_file': 'reference.py', 'reference_sha256': sha256(reference),
        'reference_import_rewrite': 'app.sonda_contract -> sonda_core_1_0_2',
        'core_contract_version': '1.0.2', 'core_contract_sha256': CORE_CONTRACT_SHA256,
        'core_reference_file': '../../sonda/v1/reference.py', 'core_reference_module': 'sonda_core_1_0_2',
        'core_reference_sha256': sha256(core_raw), 'fixtures_are_synthetic': True,
        'max_document_bytes': SOURCE_LIMITS_MAX_BYTES, 'canonical_raw_bytes_required': True,
        'fixtures': {name: sha256(raw) for name, raw in files.items() if name.startswith('fixtures/')}}
    files['manifest.json'] = encoded(manifest)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    files = artifacts()
    for name, raw in files.items():
        path = TARGET/name
        if args.check:
            if not path.is_file() or path.read_bytes() != raw:
                raise SystemExit('Draft Source Limits drift: '+name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
    print('PASS draft Source Limits '+SOURCE_LIMITS_VERSION+'; '+
        str(sum(name.startswith('fixtures/') for name in files))+' synthetic fixtures; not activated')


if __name__ == '__main__':
    main()
