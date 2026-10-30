"""Offline interoperability and trust-boundary regressions; no cloud/Actor calls."""
import copy
import json
from pathlib import Path
from uuid import UUID

import pytest

from app.sonda_contract import (ChatInput, catalog_fingerprint, encoded,
                                evaluation_fingerprint, message_target,
                                observation_fingerprints, parse, sha256)

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / 'contracts/sonda/v1'


def fixture(name):
    return json.loads((CONTRACT / 'fixtures' / name).read_text(encoding='utf-8'))


def validate(value, **scope):
    manifest = json.loads((CONTRACT / 'manifest.json').read_text())
    return parse(encoded(value), registry_id=scope.get('registry_id', UUID(int=1)),
        generation_id=scope.get('generation_id', UUID(int=2)), contract_sha256=manifest['contract_sha256'])


@pytest.mark.parametrize('path', sorted((CONTRACT/'fixtures').glob('*.json')))
def test_portable_fixture(path):
    raw = path.read_bytes()
    value = json.loads(raw)
    if path.name == 'chat-v2.json':
        assert ChatInput.model_validate_json(raw).version == 2
    else:
        assert validate(value).kind == value['kind']


def test_manifest_pins_exact_bytes_and_bundle():
    manifest = json.loads((CONTRACT/'manifest.json').read_text())
    assert manifest['schema_sha256'] == sha256((CONTRACT/'schema.json').read_bytes())
    assert manifest['chat_schema_sha256'] == sha256((CONTRACT/'chat.schema.json').read_bytes())
    assert manifest['reference_sha256'] == sha256((CONTRACT/'reference.py').read_bytes())
    assert manifest['conformance_vectors_sha256'] == sha256((CONTRACT/'conformance-vectors.json').read_bytes())
    assert manifest['contract_sha256'] == sha256(encoded({key:manifest[key] for key in ('schema_sha256','chat_schema_sha256','reference_sha256')}))
    for name, expected in manifest['fixtures'].items():
        assert sha256((CONTRACT/name).read_bytes()) == expected


@pytest.mark.parametrize('change', [
    lambda c: c['queries'].append(copy.deepcopy(c['queries'][0])),
    lambda c: c['queries'][0].update(source_id=str(UUID(int=999))),
    lambda c: c['queries'][0].update(max_items=True),
    lambda c: c['queries'][0].update(approval={'status':'pending','decided_by':None,'decided_at':None}),
    lambda c: c['criteria'][0].update(source_ids=[str(UUID(int=999))]),
    lambda c: c['sources'][0].update(allowed_domains=[]),
    lambda c: c['sources'][0].update(allowed_domains=['*.example.com']),
    lambda c: c['sources'][0].update(connector_revision=2),
    lambda c: c['policy'].update(research_reserve_microusd=999999999),
    lambda c: c['policy'].update(round_budget_microusd=0.2),
    lambda c: c['policy'].update(max_concurrency=6),
    lambda c: c['policy'].update(max_items_per_round=9007199254740992),
    lambda c: c['queries'][0]['parameters'].update(api_key='not-a-real-secret'),
    lambda c: c['queries'][0].update(arbitrary_endpoint='https://example.com'),
    lambda c: c.update(revision=3),
    lambda c: c['connectors'][0].update(actor_build='unreviewed'),
])
def test_invalid_configuration_is_not_approved(change):
    value = fixture('01-configuration.json')
    change(value['payload'])
    with pytest.raises(ValueError):
        validate(value)


def test_more_than_five_queries_remain_independent():
    config = validate(fixture('01-configuration.json')).payload
    assert len(config.queries) == 6
    assert len({row.query_id for row in config.queries}) == 6


def test_agent_proposal_cannot_approve_or_activate_itself():
    value = fixture('06-proposal.json')
    record = value['payload']['change']['record']
    record.update(status='active', approval=fixture('01-configuration.json')['payload']['criteria'][0]['approval'])
    with pytest.raises(ValueError):
        validate(value)


@pytest.mark.parametrize('scope', [{'registry_id':UUID(int=99)}, {'generation_id':UUID(int=99)}])
def test_cross_scope_rejected(scope):
    with pytest.raises(ValueError,match='scope'):
        validate(fixture('02-observations.json'), **scope)


def test_duplicate_json_keys_rejected_before_model_parsing():
    value = fixture('01-configuration.json')
    raw = encoded(value).replace(b'"kind":"configuration"', b'"kind":"configuration","kind":"configuration"', 1)
    with pytest.raises(ValueError,match='Duplicate'):
        parse(raw,registry_id=UUID(int=1),generation_id=UUID(int=2),contract_sha256=value['contract_sha256'])


def test_capture_time_and_queries_do_not_change_business_identity():
    value = fixture('02-observations.json')
    original = value['payload']['observations'][0]
    old = observation_fingerprints(original)
    value['payload'].update(query_id=str(UUID(int=101)),execution_id=str(UUID(int=999)))
    original['observed_at'] = '2026-01-01T02:00:00Z'
    assert observation_fingerprints(original) == old
    assert validate(value).payload.observations[0].document_key == old['document_key']


def test_metric_change_adds_version_not_document_and_unknown_is_not_zero():
    row = fixture('02-observations.json')['payload']['observations'][0]
    before = observation_fingerprints(row)
    assert row['metrics']['likes'] is None
    row['metrics']['likes'] = 0
    after = observation_fingerprints(row)
    assert before['document_key'] == after['document_key']
    assert before['observation_key'] != after['observation_key']


def test_tampered_observation_and_evidence_are_rejected():
    value = fixture('02-observations.json')
    value['payload']['observations'][0]['text'] = 'Changed content without changing its fingerprint'
    with pytest.raises(ValueError,match='fingerprint'):
        validate(value)
    report = fixture('05-report.json')
    report['payload']['evidence'][0]['quote'] = 'Invented quotation'
    with pytest.raises(ValueError,match='fingerprint'):
        validate(report)


def test_negative_semantic_verdict_and_budget_refusal_are_explicit():
    report = validate(fixture('05-report.json')).payload
    assert [v.decision for v in report.evaluations] == ['relevant','not_relevant']
    assert report.status == 'completed_with_limits' and report.coverage.exhausted_budget
    execution = validate(fixture('04-execution-result.json')).payload
    assert execution.status == 'budget_exhausted' and execution.record_count == 0
    report = fixture('05-report.json')
    report['payload']['evaluations'][0]['score'] = 0.1
    with pytest.raises(ValueError,match='threshold'):
        validate(report)


def test_stream_chain_is_complete_and_points_to_exact_message_bytes():
    messages = {value['message_id']:encoded(value) for path in (CONTRACT/'fixtures').glob('*.json')
                if (value:=json.loads(path.read_text())).get('payload')}
    for direction in ('huginn_to_aidp','aidp_to_huginn'):
        head = fixture('head-'+direction+'.json')
        previous = None
        for sequence in range(1,head['last_sequence']+1):
            value = fixture('entry-'+direction+'-'+str(sequence)+'.json')
            assert value['previous_entry_sha256'] == previous
            raw = messages[value['message_id']]
            assert value['message_bytes'] == len(raw) and value['message_sha256'] == sha256(raw)
            assert value['message_kind'] == json.loads(raw)['kind']
            previous = sha256(encoded(value))
        assert previous == head['last_entry_sha256']
        ack = fixture('ack-'+direction+'.json')
        assert ack['last_processed_sequence'] == head['last_sequence']


def test_v2_chat_is_reference_only_and_bounded():
    value = fixture('chat-v2.json')
    assert ChatInput.model_validate_json(encoded(value)).search_context.registry_id == UUID(int=1)
    value['search_context']['actor_id'] = 'unreviewed'
    with pytest.raises(ValueError):
        ChatInput.model_validate_json(encoded(value))


def test_connector_operation_cannot_claim_another_mode():
    value = fixture('01-configuration.json')
    value['payload']['connectors'][0]['operation'] = 'comments'
    value['payload']['catalog_sha256'] = catalog_fingerprint(value['payload']['connectors'])
    with pytest.raises(ValueError,match='operation/mode'):
        validate(value)


@pytest.mark.parametrize('at', ['2026-01-01T00:00:00.000000Z','2025-12-31T19:00:00-05:00'])
def test_catalog_hash_is_independent_of_equivalent_timestamp_spelling(at):
    value = fixture('01-configuration.json')
    original = value['payload']['catalog_sha256']
    value['payload']['connectors'][0]['reviewed_at'] = at
    assert catalog_fingerprint(value['payload']['connectors']) == original
    assert validate(value).payload.catalog_sha256 == original


def test_unicode_human_text_normalizes_without_collapsing_opaque_url_or_id():
    row = fixture('02-observations.json')['payload']['observations'][0]
    a = observation_fingerprints({**row,'text':'café'})
    b = observation_fingerprints({**row,'text':'cafe\u0301'})
    assert a == b
    a = observation_fingerprints({**row,'url':'https://example.com/café'})
    b = observation_fingerprints({**row,'url':'https://example.com/cafe\u0301'})
    assert a['document_key'] != b['document_key']
    social = {**row,'document_type':'post','platform':'instagram'}
    a = observation_fingerprints({**social,'provider_id':'café'})
    b = observation_fingerprints({**social,'provider_id':'cafe\u0301'})
    assert a['document_key'] != b['document_key']


@pytest.mark.parametrize('field', ['model_revision','prompt_version','rule_sha256'])
def test_evaluation_identity_binds_every_versioned_input(field):
    value = fixture('05-report.json')
    row = value['payload']['evaluations'][0]
    previous = row['evaluation_key']
    row[field] = 'f'*64 if field == 'rule_sha256' else 'fixture-next-version'
    assert evaluation_fingerprint(row) != previous
    with pytest.raises(ValueError,match='fingerprint'):
        validate(value)


def test_stream_cannot_carry_a_kind_from_the_opposite_producer():
    value = fixture('entry-huginn_to_aidp-1.json')
    value['message_kind'] = 'report'
    with pytest.raises(ValueError,match='direction'):
        validate(value)


def test_overflow_json_number_is_rejected_inside_provider_parameters():
    value = fixture('01-configuration.json')
    raw = encoded(value).replace(b'"parameters":{}',b'"parameters":{"score":1e999}',1)
    with pytest.raises(ValueError,match='Nonfinite'):
        parse(raw,registry_id=UUID(int=1),generation_id=UUID(int=2),contract_sha256=value['contract_sha256'])


def test_rejection_and_adjustment_replicate_only_their_decision_revision():
    rejected = validate(fixture('08-rejected-configuration.json')).payload
    assert rejected.proposal_decisions[0].decision == 'rejected'
    assert rejected.criteria[0].threshold == 0.8
    value = fixture('08-rejected-configuration.json')
    decision = value['payload']['proposal_decisions'][0]
    decision['decision'] = 'adjusted'
    with pytest.raises(ValueError,match='operator-approved'):
        validate(value)
    changed = copy.deepcopy(fixture('06-proposal.json')['payload']['change']['record'])
    changed.update(status='active',approval={'status':'approved','decided_by':decision['operator_id'],'decided_at':decision['decided_at']})
    value['payload']['criteria'] = [changed]
    assert validate(value).payload.criteria[0].threshold == 0.85
    decision['config_revision'] = 3
    with pytest.raises(ValueError,match='configuration revision'):
        validate(value)


def test_transport_uses_bound_landing_only_for_capture_bodies():
    args = (UUID(int=20),UUID(int=1),UUID(int=2),'huginn_to_aidp','aidp-social/')
    capture = message_target('observations',*args)
    config = message_target('configuration',*args)
    assert capture['bucket_role'] == 'source' and capture['key'].startswith('aidp-social/exchange/v1/')
    assert config['bucket_role'] == 'artifacts' and config['key'].startswith('ai_social_network/exchange/v1/')
    with pytest.raises(ValueError,match='prefix'):
        message_target('observations',*args[:-1],'../foreign/')


def test_budget_round_is_real_versioned_availability_not_a_spending_grant():
    opened = validate(fixture('09-budget-round-open.json')).payload
    closed = validate(fixture('10-budget-round-closed.json')).payload
    assert opened.budget_round_id == closed.budget_round_id
    assert opened.revision < closed.revision and opened.status == 'open' and closed.status == 'closed'
    assert opened.expires_at > opened.opened_at and closed.research_remaining_microusd == 0
    value = fixture('09-budget-round-open.json')
    value['payload'].update(remaining_microusd=None,research_remaining_microusd=None,remaining_items=None)
    assert validate(value).payload.research_remaining_microusd is None
    value['payload'].update(remaining_microusd=1,research_remaining_microusd=2)
    with pytest.raises(ValueError,match='availability'):
        validate(value)
    value['payload'].update(remaining_microusd=1,research_remaining_microusd=1,expires_at=value['payload']['opened_at'])
    with pytest.raises(ValueError,match='lifetime'):
        validate(value)
