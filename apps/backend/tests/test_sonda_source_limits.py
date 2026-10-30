"""Synthetic pure sidecar checks; no provider calls, ledger writes or activation."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
from uuid import UUID

import pytest

from app import sonda_contract as core
from app import sonda_source_limits as limits

ROOT = Path(__file__).resolve().parents[3]


def configuration():
    raw = json.loads((ROOT/'contracts/sonda/v1/fixtures/01-configuration.json').read_text(encoding='utf-8'))['payload']
    return core.Configuration.model_validate(raw).model_dump(mode='json')


def snapshot(config=None):
    config = config or configuration()
    return {'registry_id': str(UUID(int=1)), 'generation_id': str(UUID(int=2)),
        'catalog_revision': 1, 'previous_catalog_sha256': None,
        'config_revision': config['revision'], 'configuration_payload_sha256': core.sha256(core.encoded(config)),
        'policies': [{'source_id': source['source_id'], 'source_revision': source['revision'],
            'policy_revision': 1, 'max_items_per_execution': 20,
            'max_cost_per_round_microusd': 100, 'max_starts_per_round': 2}
            for source in sorted(config['sources'], key=lambda source: source['source_id'])]}


def checked(value=None, config=None, previous=None):
    return limits.check_source_limits(value or snapshot(config), config or configuration(),
        registry_id=UUID(int=1), generation_id=UUID(int=2), previous=previous)


def ledger():
    return {'registry_id': str(UUID(int=1)), 'generation_id': str(UUID(int=2)),
        'round_id': str(UUID(int=3)), 'source_id': snapshot()['policies'][0]['source_id'],
        'spent_microusd': 0, 'pending_reserved_microusd': 0, 'starts': 0}


def start(**overrides):
    return {'items': 2, 'reserve_microusd': 40, 'verified_free': False, **overrides}


def policy(**overrides):
    return {**snapshot()['policies'][0], **overrides}


def test_exact_scope_configuration_and_full_source_snapshot():
    value = checked()
    assert len(value.policies) == len(configuration()['sources'])
    raw = core.encoded(value.model_dump(mode='json'))
    assert limits.parse_source_limits(raw, expected_sha256=limits.source_limits_digest(value),
        configuration=configuration(), registry_id=UUID(int=1), generation_id=UUID(int=2)) == value
    assert limits.source_limits_digest(value) == core.sha256(raw)
    schema = limits.SourceLimitsSnapshot.model_json_schema()
    assert schema['additionalProperties'] is False
    assert schema['properties']['capability_version']['const'] == '1.0.0'


@pytest.mark.parametrize('change', [
    {'registry_id': str(UUID(int=99))}, {'generation_id': str(UUID(int=99))},
    {'configuration_payload_sha256': '0'*64}, {'config_revision': 2},
    {'previous_catalog_sha256': '0'*64}, {'catalog_revision': 2},
    {'core_contract_sha256': '0'*64}, {'credential': 'not-allowed'},
])
def test_invalid_snapshot_fails(change):
    with pytest.raises(ValueError):
        checked({**snapshot(), **change})


@pytest.mark.parametrize('field', ['source_revision', 'policy_revision', 'max_items_per_execution',
    'max_cost_per_round_microusd', 'max_starts_per_round'])
@pytest.mark.parametrize('invalid', [True, -1, '3', 9007199254740992])
def test_policy_numbers_are_bounded_and_strict(field, invalid):
    value = snapshot()
    value['policies'][0][field] = invalid
    with pytest.raises(ValueError):
        checked(value)


@pytest.mark.parametrize('mutate', [
    lambda value: value['policies'].pop(),
    lambda value: value['policies'].append(copy.deepcopy(value['policies'][0])),
    lambda value: value['policies'][0].update(source_revision=2),
    lambda value: value['policies'][0].update(source_id=str(UUID(int=99))),
])
def test_sources_are_complete_distinct_ordered_and_exact(mutate):
    value = snapshot()
    mutate(value)
    with pytest.raises(ValueError):
        checked(value)


def test_policy_order_is_canonical_for_multiple_sources():
    config = configuration()
    added = copy.deepcopy(config['sources'][0])
    added['source_id'] = str(UUID(int=999))
    config['sources'].append(added)
    value = snapshot(config)
    assert len(checked(value, config).policies) == 2
    value['policies'].reverse()
    with pytest.raises(ValueError, match='canonical source order'):
        checked(value, config)


def test_successor_nulls_explicitly_remove_caps_without_removing_source():
    before = checked()
    config = configuration()
    config.update(revision=2, previous_revision=1)
    value = snapshot(config)
    value.update(catalog_revision=2, previous_catalog_sha256=limits.source_limits_digest(before))
    value['policies'][0].update(policy_revision=2, max_items_per_execution=None,
        max_cost_per_round_microusd=None, max_starts_per_round=None)
    assert checked(value, config, before).policies[0].max_starts_per_round is None
    value['policies'][0]['policy_revision'] = 1
    with pytest.raises(ValueError, match='policy revision'):
        checked(value, config, before)


@pytest.mark.parametrize('change', [
    {'previous_catalog_sha256': '0'*64}, {'catalog_revision': 3},
    {'config_revision': 1, 'configuration_payload_sha256': core.sha256(core.encoded(configuration()))},
])
def test_successor_requires_chain_and_advanced_core_config(change):
    before = checked()
    config = configuration()
    config.update(revision=2, previous_revision=1)
    value = snapshot(config)
    value.update(catalog_revision=2, previous_catalog_sha256=limits.source_limits_digest(before))
    value.update(change)
    selected_config = configuration() if value['config_revision'] == 1 else config
    with pytest.raises(ValueError):
        checked(value, selected_config, before)


def test_removing_source_requires_matching_core_removal():
    before = checked()
    config = configuration()
    config.update(revision=2, previous_revision=1)
    removed = config['sources'].pop()['source_id']
    config['queries'] = [query for query in config['queries'] if query['source_id'] != removed]
    for criterion in config['criteria']:
        criterion['source_ids'] = [source for source in criterion['source_ids'] if source != removed]
        criterion['query_ids'] = [query['query_id'] for query in config['queries']]
    config['criteria'] = [criterion for criterion in config['criteria'] if criterion['source_ids']]
    value = snapshot(config)
    value.update(catalog_revision=2, previous_catalog_sha256=limits.source_limits_digest(before))
    assert removed not in {str(policy.source_id) for policy in checked(value, config, before).policies}


def test_only_true_legacy_absence_preserves_existing_limits():
    source_id = snapshot()['policies'][0]['source_id']
    assert limits.policy_for_source(None, source_id, expected_sha256=None) is None
    value = checked()
    digest = limits.source_limits_digest(value)
    assert limits.policy_for_source(value, source_id, expected_sha256=digest) == value.policies[0]
    for body, expected in ((None, digest), (value, None), (value, '0'*64)):
        with pytest.raises(ValueError):
            limits.policy_for_source(body, source_id, expected_sha256=expected)
    with pytest.raises(ValueError):
        limits.policy_for_source(value, UUID(int=99), expected_sha256=digest)


def test_double_start_rechecks_atomic_ledger_and_uncertain_keeps_reservation():
    current = limits.reserve_source_start(ledger(), start(reserve_microusd=70), policy=policy())
    assert current.starts == 1 and current.pending_reserved_microusd == 70
    # A lost provider response retains this committed ledger. A second request
    # must reread it after CAS, not reuse the original available allowance.
    with pytest.raises(ValueError, match='spend'):
        limits.reserve_source_start(current, start(), policy=policy())
    current = limits.reserve_source_start(current, start(reserve_microusd=30), policy=policy())
    with pytest.raises(ValueError, match='start limit'):
        limits.reserve_source_start(current, start(reserve_microusd=0), policy=policy())


def test_spend_is_shared_across_queries_and_includes_confirmed_and_reserved():
    current = {**ledger(), 'spent_microusd': 50, 'pending_reserved_microusd': 30, 'starts': 1}
    with pytest.raises(ValueError, match='spend'):
        limits.reserve_source_start(current, start(reserve_microusd=21), policy=policy())
    assert limits.reserve_source_start(current, start(reserve_microusd=20), policy=policy()).starts == 2


def test_lowered_cap_blocks_new_start_but_not_reconciliation():
    current = limits.reserve_source_start(ledger(), start(reserve_microusd=70), policy=policy())
    with pytest.raises(ValueError, match='spend'):
        limits.reserve_source_start(current, start(reserve_microusd=1),
            policy=policy(policy_revision=2, max_cost_per_round_microusd=20))
    reconciled = limits.check_source_reconciliation(current, **{
        key: value for key, value in ledger().items() if key in ('registry_id', 'generation_id', 'round_id', 'source_id')})
    assert reconciled == current


def test_policy_revision_does_not_reset_round_identity_or_starts():
    current = limits.reserve_source_start(ledger(), start(), policy=policy(max_starts_per_round=1))
    key = limits.source_ledger_key(current)
    with pytest.raises(ValueError, match='start limit'):
        limits.reserve_source_start(current, start(), policy=policy(policy_revision=2, max_starts_per_round=1))
    assert key == limits.source_ledger_key(ledger())
    with pytest.raises(ValueError):
        limits.SourceRoundLedger.model_validate({**ledger(), 'policy_revision': 2})


def test_zero_starts_blocks_all_and_zero_spend_only_verified_free():
    with pytest.raises(ValueError, match='start limit'):
        limits.reserve_source_start(ledger(), start(reserve_microusd=0, verified_free=True), policy=policy(max_starts_per_round=0))
    with pytest.raises(ValueError, match='verified-free'):
        limits.reserve_source_start(ledger(), start(reserve_microusd=0), policy=policy(max_cost_per_round_microusd=0))
    assert limits.reserve_source_start(ledger(), start(reserve_microusd=0, verified_free=True),
        policy=policy(max_cost_per_round_microusd=0)).starts == 1


@pytest.mark.parametrize('policy_change,start_change', [
    ({'max_items_per_execution': 0}, {}),
    ({'max_items_per_execution': 1}, {}),
    ({'source_id': str(UUID(int=99))}, {}),
    ({}, {'verified_free': True}),
    ({}, {'items': True}), ({}, {'reserve_microusd': True}), ({}, {'verified_free': 1}),
])
def test_admission_rejects_invalid_or_over_limit_start(policy_change, start_change):
    with pytest.raises(ValueError):
        limits.reserve_source_start(ledger(), start(**start_change), policy=policy(**policy_change))


def test_uncapped_legacy_still_validates_start_and_bounded_ledger():
    assert limits.reserve_source_start(ledger(), start(), policy=None).starts == 1
    with pytest.raises(ValueError):
        limits.reserve_source_start({**ledger(), 'starts': 9007199254740991}, start(), policy=None)


def test_raw_parser_rejects_duplicate_fields_wrong_digest_and_oversize():
    value = checked()
    raw = core.encoded(value.model_dump(mode='json'))
    kwargs = {'expected_sha256': limits.source_limits_digest(value), 'configuration': configuration(),
        'registry_id': UUID(int=1), 'generation_id': UUID(int=2)}
    for invalid in (b'{"catalog_revision":1,"catalog_revision":2}', b' '* (limits.SOURCE_LIMITS_MAX_BYTES + 1)):
        with pytest.raises(ValueError):
            limits.parse_source_limits(invalid, **{**kwargs, 'expected_sha256': core.sha256(invalid)})
    with pytest.raises(ValueError, match='digest'):
        limits.parse_source_limits(raw, **{**kwargs, 'expected_sha256': '0'*64})


@pytest.mark.parametrize('transform', [
    lambda raw: b' ' + raw,
    lambda raw: raw.replace(b'\n', b'\r\n'),
    lambda raw: json.dumps(json.loads(raw), indent=2).encode('utf-8'),
])
def test_protected_sidecar_requires_exact_canonical_bytes(transform):
    value = checked()
    raw = core.encoded(value.model_dump(mode='json'))
    altered = transform(raw)
    kwargs = {'configuration': configuration(), 'registry_id': UUID(int=1), 'generation_id': UUID(int=2)}
    with pytest.raises(ValueError, match='raw digest'):
        limits.parse_source_limits(altered, expected_sha256=core.sha256(raw), **kwargs)
    with pytest.raises(ValueError, match='canonical bytes'):
        limits.parse_source_limits(altered, expected_sha256=core.sha256(altered), **kwargs)


def _load_module(path, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_exported_artifacts_match_authoritative_source(monkeypatch):
    exporter = _load_module(ROOT/'scripts/export_sonda_source_limits.py', 'source_limits_export_check', monkeypatch)
    target = ROOT/'contracts/sonda-source-limits/v1'
    files = exporter.artifacts()
    assert len([name for name in files if name.startswith('fixtures/')]) == 10
    assert all((target/name).read_bytes() == raw for name, raw in files.items())
    manifest = json.loads(files['manifest.json'])
    assert manifest['status'] == 'draft_not_accepted_or_activated'
    assert manifest['canonical_raw_bytes_required'] is True
    assert manifest['core_contract_sha256'] == limits.CORE_CONTRACT_SHA256
    assert all(core.sha256(files[name]) == digest for name, digest in manifest['fixtures'].items())


def test_portable_reference_uses_existing_exact_core_and_checks_all_successors(monkeypatch):
    target = ROOT/'contracts/sonda-source-limits/v1'
    manifest = json.loads((target/'manifest.json').read_text(encoding='utf-8'))
    core_path = (target/manifest['core_reference_file']).resolve()
    assert core.sha256(core_path.read_bytes()) == manifest['core_reference_sha256']
    _load_module(core_path, 'sonda_core_1_0_2', monkeypatch)
    portable = _load_module(target/'reference.py', 'source_limits_portable_check', monkeypatch)
    previous = None
    for stem in ('01-initial', '02-lowered-caps', '03-null-cap-removal', '04-source-removal'):
        raw = (target/'fixtures'/f'{stem}.json').read_bytes()
        config = json.loads((target/'fixtures'/f'{stem}-core-configuration.json').read_bytes())
        previous = portable.parse_source_limits(raw, expected_sha256=core.sha256(raw), configuration=config,
            registry_id=UUID(int=1), generation_id=UUID(int=2), previous=previous)
    assert previous.policies == []
    sequence = json.loads((target/'fixtures/reservation-sequence.json').read_bytes())
    reserved = portable.reserve_source_start(sequence['before'], sequence['start'], policy=sequence['policy'])
    assert reserved.model_dump(mode='json') == sequence['after_provider_response_lost'] == sequence['after_reservation']
    with pytest.raises(ValueError):
        portable.reserve_source_start(reserved, sequence['start'], policy=sequence['lowered_policy'])
