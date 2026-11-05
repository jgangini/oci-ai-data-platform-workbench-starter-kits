import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.modules.ai_data_governance.case_changes import case_change
from app.modules.ai_data_governance.case_readmission import CaseReadmission, case_catalog_evidence_query, case_specification_links, checked_case_catalog_proof, checked_case_prior_state
from app.modules.ai_data_governance.consultation import ConsultationHooks
from app.modules.ai_data_governance.inventory import canonical, identity, table_records
from tests.modules.ai_data_governance.test_ai_dg_consultation import harness


AT = datetime(2026, 11, 1, tzinfo=timezone.utc)


def readmission_fixture():
    hooks, projections, _, caller, _ = harness()
    config = hooks.binding
    facts = dict(table_key='catalog.schema.orders', name='orders', native_version=None,
        description='Issued orders', columns=[dict(name='ABC13', native_key='native-column', ordinal=0,
            data_type='STRING', description=None)])
    records = table_records('platform', projections[0]['scope_id'], 'inventory', facts, AT)
    rows = []
    for asset, version, projection, proof in zip(records.assets, records.versions, records.projections, records.evidence, strict=True):
        proof = {**proof, 'observed_at': proof['observed_at'].isoformat()}
        rows.append(dict(scope_id=projection['scope_id'], asset_id=asset['asset_id'], asset_kind=asset['asset_kind'],
            version_id=version['version_id'], metadata_hash=version['metadata_hash'], metadata_json=version['metadata_json'],
            qualified_name=version['qualified_name'], current_version_id=asset['current_version_id'],
            verified_version_id=projection['verified_version_id'], identity_status=asset['identity_status'],
            lifecycle_state=asset['lifecycle_state'], consistency_state=projection['consistency_state'], evidence=proof))
    spec = dict(action='OPEN', scope_id=projections[0]['scope_id'], assets=[dict(asset_id=row['asset_id'],
        assessed_version_id=row['version_id'], impact_role='SUBJECT', evidence_id=row['evidence']['evidence_id'])
        for row in rows], summary='Investigar calidad y uso', previous=None, draft=None, previous_decision=None)
    query = AsyncMock(side_effect=lambda sql, **kwargs: deepcopy(rows if 'AI_DG_EVIDENCE' in sql else projections))
    readmission = CaseReadmission(config, caller, query, lambda: ConsultationHooks(config, caller, query), now=lambda: AT)
    return readmission, spec, rows, projections, caller, query


def test_exact_persisted_table_and_column_proofs_require_a_fresh_native_catalog_on_each_confirmation():
    reader, spec, _, _, caller, query = readmission_fixture()
    for _ in range(2):
        assert asyncio.run(reader.revalidate(spec)) is True
    assert caller.call_count == 2
    assert query.await_count == 4
    assert [call.kwargs['limit'] for call in query.call_args_list] == [32, 16, 32, 16]
    assert all(call.args == ('GET', '/tables/catalog.schema.orders') for call in caller.call_args_list)


def test_persisted_proof_query_is_fixed_scoped_bounded_and_preserves_spark_microseconds():
    reader, spec, _, _, _, _ = readmission_fixture()
    sql = case_catalog_evidence_query(reader.binding, spec)
    assert sql.endswith('LIMIT 17') and "timestampFormat','yyyy-MM-dd HH:mm:ss.SSSSSSXXX" in sql
    assert all(name in sql for name in ('AI_DG_SCOPE_ASSET', 'AI_DG_ASSET_VERSION', 'AI_DG_EVIDENCE', 's.enabled=TRUE'))
    assert spec['scope_id'].encode().hex() in sql
    assert all(row['assessed_version_id'].encode().hex() in sql for row in spec['assets'])
    assert all(word not in sql for word in ('UPDATE ', 'DELETE ', 'INSERT ', ';', 'expires_at>'))


@pytest.mark.parametrize('field,value', [('asset_id',''), ('assessed_version_id','not-a-version'),
    ('evidence_id',None), ('impact_role','UPSTREAM'), ('case_id','b'*64)])
def test_invalid_assessed_links_fail_before_any_query(field, value):
    reader, spec, _, _, caller, query = readmission_fixture()
    spec['assets'][0][field] = value
    with pytest.raises(ValueError): asyncio.run(reader.revalidate(spec))
    query.assert_not_awaited(); caller.assert_not_called()


def test_unknown_scope_is_rejected_even_when_the_historical_proof_has_self_consistent_ids():
    reader, spec, rows, _, _, _ = readmission_fixture()
    spec['scope_id'] = rows[0]['scope_id'] = rows[0]['evidence']['scope_id'] = 'unconfigured'
    proof = rows[0]['evidence']
    proof['evidence_id'] = spec['assets'][0]['evidence_id'] = identity('AI_DG_EVIDENCE', 'unconfigured',
        rows[0]['version_id'], rows[0]['metadata_hash'])
    with pytest.raises(ValueError, match='outside the case scope'):
        checked_case_catalog_proof(reader.binding, spec, spec['assets'][0], rows[0], now=AT)


def test_native_catalog_change_cannot_be_hidden_by_a_verified_delta_projection():
    reader, spec, _, _, caller, query = readmission_fixture()
    native = deepcopy(caller.return_value[0]); native['description'] = 'Changed after preview'
    caller.return_value = (native, {})
    with pytest.raises(RuntimeError, match='needs synchronization'):
        asyncio.run(reader.revalidate(spec))
    assert query.await_count == 1


@pytest.mark.parametrize('field,value', [('scope_id', 'foreign'), ('version_id', 'f'*64),
    ('current_version_id', 'f'*64), ('verified_version_id', 'f'*64), ('metadata_hash', 'f'*64),
    ('metadata_json', '{}'), ('identity_status', 'UNKNOWN'), ('lifecycle_state', 'RETIRED'),
    ('consistency_state', 'PENDING'), ('asset_kind', 'WORKFLOW'), ('qualified_name', 'foreign.schema.table')])
def test_changed_or_unadmitted_persisted_metadata_is_rejected(field, value):
    reader, spec, rows, _, _, _ = readmission_fixture()
    rows[0][field] = value
    with pytest.raises((ValueError, PermissionError)):
        asyncio.run(reader.revalidate(spec))


@pytest.mark.parametrize('field,value', [('evidence_id', 'f'*64), ('asset_version_id', 'f'*64),
    ('scope_id', 'foreign'), ('evidence_kind', 'STATIC_CODE'), ('evidence_hash', 'f'*64),
    ('facts_json', '{}'), ('source_ref', 'foreign.schema.table'), ('producer_run_id', ''),
    ('coverage_state', 'UNAVAILABLE'), ('observed_at', '2026-11-01T00:00:01+00:00'),
    ('expires_at', '2026-11-01T00:00:00+00:00'), ('observed_at', '2026-11-01')])
def test_wrong_ephemeral_expired_or_unavailable_proofs_cannot_admit_a_case(field, value):
    reader, spec, rows, _, _, _ = readmission_fixture()
    rows[0]['evidence'][field] = value
    with pytest.raises((ValueError, PermissionError)):
        asyncio.run(reader.revalidate(spec))


@pytest.mark.parametrize('coverage', ['missing', 'duplicate', 'unrelated'])
def test_evidence_coverage_must_equal_the_whole_assessed_set(coverage):
    reader, spec, rows, _, _, _ = readmission_fixture()
    if coverage == 'missing': rows.pop()
    if coverage == 'duplicate': rows[1] = deepcopy(rows[0])
    if coverage == 'unrelated': rows[1]['asset_id'] = 'another'
    with pytest.raises(PermissionError): asyncio.run(reader.revalidate(spec))


def test_no_preview_hook_cache_can_replace_fresh_admission():
    reader, spec, _, projections, _, query = readmission_fixture()
    cached = reader.hooks_factory(); cached.rows = cached._checked_rows(projections)
    reader.hooks_factory = lambda: cached
    with pytest.raises(ValueError, match='fresh consultation'):
        asyncio.run(reader.revalidate(spec))
    query.assert_not_awaited()


@pytest.mark.parametrize('when', ['projection', 'evidence'])
def test_query_cancellation_propagates_without_a_positive_confirmation(when):
    reader, spec, _, projections, _, query = readmission_fixture()
    query.side_effect = [asyncio.CancelledError()] if when == 'projection' else [projections, asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError): asyncio.run(reader.revalidate(spec))


def test_native_caller_is_rechecked_after_evidence_read():
    reader, spec, _, _, caller, query = readmission_fixture()
    original = query.side_effect
    async def change_identity(sql, **kwargs):
        value = original(sql, **kwargs)
        if 'AI_DG_EVIDENCE' in sql: caller.check_identity.side_effect = PermissionError('Caller changed')
        return value
    query.side_effect = change_identity
    with pytest.raises(PermissionError, match='Caller changed'):
        asyncio.run(reader.revalidate(spec))


def history_fixture():
    reader, spec, proofs, _, _, _ = readmission_fixture()
    args = dict(subject='operator', session='session', request_id='77777777-7777-4777-8777-777777777777',
        scope_id=spec['scope_id'], run_id='opening-run', policy_version='p1', at=AT, assets=spec['assets'])
    opened = case_change(**args, action='OPEN', summary=spec['summary'])
    draft = dict(problem='Falta medición', asset_ids=[r['asset_id'] for r in opened['assets']],
        expected_versions=[r['assessed_version_id'] for r in opened['assets']], suggested_action='Medir nulidad',
        responsible_ref='data-steward', verification_plan='Leer medición vigente', remaining_gaps=['Sin medición'])
    proposed = case_change(**{**args, 'at': AT+timedelta(seconds=1), 'request_id':'88888888-8888-4888-8888-888888888888'},
        action='PROPOSE', previous=opened['case'], draft=draft)
    approved = case_change(**{**args, 'at': AT+timedelta(seconds=2), 'request_id':'99999999-9999-4999-8999-999999999999'},
        action='APPROVE', previous=proposed['case'], valid_until=AT+timedelta(hours=1))
    spec.update(action='REVOKE', assets=approved['assets'], previous=approved['case'], summary=None,
        previous_decision=approved['decisions'][0])
    case_id = approved['case']['case_id']
    rows = [dict(record_kind='CASE', case_id=case_id, data=canonical(approved['case'])),
        dict(record_kind='DECISION', case_id=case_id, data=canonical(approved['decisions'][0]))]
    names = {p['asset_id']:p['qualified_name'] for p in proofs}
    rows.extend(dict(record_kind='ASSET', case_id=case_id,
        data=canonical({k:v for k,v in link.items() if k!='case_id'} | dict(qualified_name=names[link['asset_id']], admitted=True)))
        for link in approved['assets'])
    return reader, spec, proofs, rows


def test_revocation_reads_the_exact_historical_case_and_proofs_without_requiring_current_business_assets():
    reader, spec, proofs, history = history_fixture()
    for proof in proofs:
        proof.update(current_version_id='new-version', verified_version_id='new-version', lifecycle_state='RETIRED')
        proof['evidence']['expires_at'] = (AT-timedelta(days=1)).isoformat()
    reader.query = AsyncMock(side_effect=[history, proofs])
    assert asyncio.run(reader.revalidate(spec)) is True
    reader.caller.assert_not_called()
    assert [call.kwargs['limit'] for call in reader.query.call_args_list] == [48, 16]


@pytest.mark.parametrize('failure', ['case', 'asset', 'decision', 'future_decision'])
def test_changed_or_partially_materialized_prior_state_stops_readmission(failure):
    reader, spec, _, history = history_fixture()
    kind = {'case':'CASE', 'asset':'ASSET', 'decision':'DECISION', 'future_decision':'DECISION'}[failure]
    target = next(row for row in history if row['record_kind']==kind)
    value = json.loads(target['data'])
    if failure == 'case': value['summary'] = 'Another case revision'
    if failure == 'asset': value['evidence_id'] = 'f'*64
    if failure == 'decision': value['actor_ref'] = 'another-actor'
    if failure == 'future_decision': value['recorded_at'] = (AT+timedelta(seconds=3)).isoformat()
    target['data'] = canonical(value)
    with pytest.raises(PermissionError):
        checked_case_prior_state(reader.binding, spec, case_specification_links(reader.binding, spec), history)


def test_sql_timezone_and_fractional_precision_do_not_change_the_assessed_head():
    reader, spec, _, history = history_fixture()
    for row in history:
        data = json.loads(row['data'])
        for key,value in data.items():
            if key.endswith('_at') or key == 'valid_until':
                data[key] = datetime.fromisoformat(value).isoformat(timespec='microseconds').replace('+00:00','Z')
        row['data'] = canonical(data)
    checked_case_prior_state(reader.binding, spec, case_specification_links(reader.binding, spec), history)


def test_an_old_decision_is_not_reinterpreted_as_the_head_of_a_new_treatment():
    reader, spec, _, history = history_fixture()
    spec['previous'] = {**spec['previous'], 'updated_at':(AT+timedelta(seconds=4)).isoformat()}
    spec.update(action='APPROVE', previous_decision=None)
    history[0]['data'] = canonical(spec['previous'])
    checked_case_prior_state(reader.binding, spec, case_specification_links(reader.binding, spec), history)


def confirmation_fixture():
    from app.modules.ai_data_governance.case_confirmation import CaseConfirmations
    from tests.support.ai_dg_fixtures import CaseJournal
    from tests.modules.ai_data_governance.test_ai_dg_case_confirmation import journal_call

    reader, spec, proofs, _, caller, query = readmission_fixture()
    reader.binding.update(agent_key='11111111-1111-4111-8111-111111111111', component_revision='a'*64, deployment_stage='TEST')
    context = SimpleNamespace(dh_user_principal='opaque', subject_id='operator', session_id='session')
    authority = AsyncMock(return_value=dict(policy_version='p1', expires_at=(AT+timedelta(hours=1)).isoformat()))
    clock = [AT]
    adapter = CaseConfirmations(reader.binding, CaseJournal(), get_context=lambda: context, journal_call=journal_call,
        authorize_action=authority, revalidate=reader.revalidate, now=lambda: clock[0])
    return adapter, spec, proofs, caller, query, authority, clock


def test_real_confirmation_readmits_catalog_and_persisted_proof_but_replay_only_recovers_the_frozen_change():
    adapter, spec, _, caller, query, authority, clock = confirmation_fixture()
    preview = asyncio.run(adapter.offer(spec))
    clock[0] += timedelta(seconds=20)
    command = asyncio.run(adapter.confirm(preview['command'], request_id='11111111-1111-4111-8111-111111111111'))
    assert command['mode'] == 'COMMIT_CASE' and command['change']['assets'][0]['evidence_id'] in {r['evidence_id'] for r in spec['assets']}
    assert caller.call_count == 2 and query.await_count == 4 and authority.await_count == 2
    caller.side_effect = AssertionError('A replay must not read current business assets')
    query.side_effect = AssertionError('A replay must not seek a new assessment')
    authority.side_effect = AssertionError('A replay must not renew authority')
    clock[0] += timedelta(hours=2)
    replay = asyncio.run(adapter.confirm(preview['command'], request_id='22222222-2222-4222-8222-222222222222'))
    assert replay == dict(mode='RECONCILE_CASE', change=command['change'], grant=None)


def test_changed_persisted_evidence_between_preview_and_confirmation_never_freezes_a_grant():
    adapter, spec, proofs, _, _, _, _ = confirmation_fixture()
    preview = asyncio.run(adapter.offer(spec))
    proofs[0]['evidence']['facts_json'] = '{}'
    with pytest.raises(PermissionError):
        asyncio.run(adapter.confirm(preview['command'], request_id='11111111-1111-4111-8111-111111111111'))
    assert not any(kind=='responses' for kind, _ in adapter.journal.rows)
