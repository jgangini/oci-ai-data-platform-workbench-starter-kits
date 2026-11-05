import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.ai_data_governance.code_context import source_references
from app.modules.ai_data_governance.code_usage import CallerCodeVerifier
from app.modules.ai_data_governance.consultation import ConsultationHooks
from app.modules.ai_data_governance.inventory import identity
from tests.modules.ai_data_governance.test_ai_dg_consultation import harness, resolved


JOB = '12345678-1234-4234-8234-123456789abc'
PATH = '/Workspace/jobs/orders.py'
SOURCE = 'spark.table("catalog.schema.orders").select("ABC13")\nprivate_token="never-return-this"'


def usage_harness():
    base, rows, native, caller, query = harness()
    caller.code_reads_enabled = True
    hooks = ConsultationHooks(base.binding, caller, query, code_usage_enabled=True)
    assets = resolved(hooks)[1:2]
    parsed = source_references(PATH, SOURCE)
    core = next(ref for ref in parsed['references'] if ref['column_name'] == 'ABC13')
    ref = {**core, 'source_path': PATH, 'source_hash': parsed['source_hash'], 'job_key': JOB,
           'task_key': 'read_orders', 'execution_evidence': False}
    job = {'key': JOB, 'tasks': [{'taskKey': 'read_orders', 'type': 'PYTHON_TASK', 'source': 'WORKSPACE', 'filePath': PATH}]}
    now = datetime.now(timezone.utc)
    facts = {'usage_context': {'target': rows[1]['qualified_name'], 'references': [ref],
        'execution_evidence': False, 'analysis_complete': False}, 'observation_run_id': 'a' * 64}
    candidate = {'evidence_id': identity('fixture-evidence'), 'scope_id': rows[1]['scope_id'],
        'version_id': rows[1]['version_id'], 'kind': 'STATIC_CODE', 'facts_json': json.dumps(facts),
        'coverage': 'PARTIAL', 'observed_at': (now - timedelta(minutes=1)).isoformat(),
        'expires_at': (now + timedelta(minutes=30)).isoformat()}
    def request(method, path, **kwargs):
        if path.startswith('/tables/'):
            return native, {}
        if path.endswith('/jobs'):
            return {'items': []}, {}
        if '/jobs/' in path:
            return job, {}
        if '/objects/' in path:
            return SOURCE, {}
        raise AssertionError('Unexpected source request')
    caller.side_effect = request
    query.return_value = [candidate]
    return hooks, assets, rows[1], ref, job, caller, query, candidate, request


def read_usage(hooks, assets):
    return asyncio.run(hooks.read('find_code_usage', assets, {}))


def test_authorized_static_usage_is_partial_and_excludes_source_text():
    hooks, assets, _, _, _, _, _, _, _ = usage_harness()
    proofs = read_usage(hooks, assets)
    assert len(proofs) == 1 and proofs[0]['kind'] == 'STATIC_CODE' and proofs[0]['coverage'] == 'partial'
    assert proofs[0]['facts']['execution_evidence'] is False
    assert 'never-return-this' not in json.dumps(proofs)
    asyncio.run(hooks.reauthorize_evidence({}, assets=assets, evidence=proofs))


@pytest.mark.parametrize('denied', ['/jobs/', '/objects/'])
def test_table_allowed_but_source_denied_is_not_returned(denied):
    hooks, assets, _, _, _, caller, _, _, request = usage_harness()
    def read(method, path, **kwargs):
        if denied in path:
            raise PermissionError('private-message-not-for-model')
        return request(method, path, **kwargs)
    caller.side_effect = read
    assert read_usage(hooks, assets) == []


@pytest.mark.parametrize('change', [{'filePath': '/Workspace/other.py'}, {'source': 'GIT_PROVIDER'}, {'source': None}, {'type': 'AGENT_TASK'}])
def test_task_retargeting_never_reuses_identical_source_bytes(change):
    hooks, assets, _, _, job, _, _, _, _ = usage_harness()
    job['tasks'][0].update(change)
    assert read_usage(hooks, assets) == []


def test_duplicate_task_key_is_not_an_unambiguous_source_binding():
    hooks, assets, _, _, job, _, _, _, _ = usage_harness()
    job['tasks'].append(deepcopy(job['tasks'][0]))
    assert read_usage(hooks, assets) == []


@pytest.mark.parametrize('change', [{'source_hash': 'f' * 64}, {'line': 2}, {'cell': 1}, {'operation': 'literal_column_groupBy'}])
def test_hash_and_exact_locator_are_revalidated(change):
    hooks, assets, _, _, _, _, _, candidate, _ = usage_harness()
    facts = json.loads(candidate['facts_json'])
    facts['usage_context']['references'][0].update(change)
    candidate['facts_json'] = json.dumps(facts)
    assert read_usage(hooks, assets) == []


def test_expired_evidence_is_not_reused_and_foreign_versions_still_fail():
    hooks, assets, _, _, _, caller, _, candidate, _ = usage_harness()
    candidate['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    caller.reset_mock()
    assert read_usage(hooks, assets) == []
    assert all('/tables/' in c.args[1] or c.args[1].endswith('/jobs') for c in caller.call_args_list)
    candidate['version_id'] = 'f' * 64
    with pytest.raises(ValueError):
        read_usage(hooks, assets)


def test_capability_is_off_by_default_and_requires_admitted_caller():
    hooks, _, _, _, _, caller, query, _, _ = usage_harness()
    default = ConsultationHooks(hooks.binding, caller, query)
    assert 'find_code_usage' not in default.capabilities
    caller.code_reads_enabled = False
    with pytest.raises(PermissionError):
        ConsultationHooks(hooks.binding, caller, query, code_usage_enabled=True)


def test_fresh_verification_does_not_reuse_an_earlier_permission_cache():
    hooks, assets, _, _, _, caller, _, _, request = usage_harness()
    proofs = read_usage(hooks, assets)
    def denied(method, path, **kwargs):
        if '/objects/' in path:
            raise PermissionError('revoked')
        return request(method, path, **kwargs)
    caller.side_effect = denied
    with pytest.raises(PermissionError):
        asyncio.run(hooks.reauthorize_evidence({}, assets=assets, evidence=proofs))


def test_verifier_identity_change_is_not_converted_to_source_absence():
    hooks, _, row, ref, _, caller, _, _, _ = usage_harness()
    verifier = CallerCodeVerifier(caller, hooks.binding['workspace_key'])
    caller.check_identity.side_effect = PermissionError('session changed')
    with pytest.raises(PermissionError):
        verifier.verify(ref, row)


@pytest.mark.parametrize('interrupt,expected_calls', [('retrieve', ['G1']), ('assess', ['G1', 'G3'])])
def test_real_graph_resume_rechecks_sources_before_review_or_answer(interrupt, expected_calls):
    from langgraph.checkpoint.memory import InMemorySaver
    from app.modules.ai_data_governance.governance_graph import build_governance_graph
    hooks, assets, row, _, _, caller, query, candidate, request = usage_harness()
    query.side_effect = lambda sql, **kwargs: [candidate] if 'e.evidence_kind' in sql else [row]
    phases = []
    async def model(phase, payload, *args):
        phases.append(phase)
        if phase == 'G1':
            return json.dumps({'intent': 'usage', 'asset_mentions': ['ABC13']})
        if phase == 'G3':
            ids = [proof['evidence_id'] for proof in payload['evidence'] if proof['kind'] == 'STATIC_CODE']
            return json.dumps({'evidence_ids': ids, 'supported_claims': [{'text': 'Referencia estática en una tarea Python.',
                'evidence_ids': ids, 'kind': 'observed'}], 'conflicts': [], 'gaps': [],
                'coverage': 'sufficient', 'next_action': 'answer'})
        raise AssertionError('Revoked evidence reached the response model')
    memory = InMemorySaver()
    def build(current):
        return build_governance_graph(authorize=current.authorize, resolve_assets=AsyncMock(return_value=assets),
            read=current.read, call_model=model, capabilities=current.capabilities, checkpointer=memory,
            reauthorize_evidence=current.reauthorize_evidence)
    async def run():
        config = {'configurable': {'thread_id': 'source-revocation-' + interrupt}}
        await build(hooks).ainvoke({'question': '¿Dónde se usa ABC13?'}, config=config, interrupt_after=[interrupt])
        def denied(method, path, **kwargs):
            if '/objects/' in path:
                raise PermissionError('revoked')
            return request(method, path, **kwargs)
        caller.side_effect = denied
        restored = ConsultationHooks(hooks.binding, caller, query, code_usage_enabled=True)
        with pytest.raises(PermissionError):
            await build(restored).ainvoke(None, config=config)
    asyncio.run(run())
    assert phases == expected_calls


def test_generated_runtime_executes_source_readmission_and_bounded_current_discovery(monkeypatch):
    import sys
    from types import ModuleType
    from app.modules.ai_data_governance.governance_source import governance_agent_source
    from tests.support.ai_dg_fixtures import binding
    hooks, assets, _, _, _, caller, query, _, _ = usage_harness()
    proofs = read_usage(hooks, assets)
    module = ModuleType('governance_source_readmission_fixture')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    source = governance_agent_source(binding())
    exec(compile(source, 'governance_runtime.py', 'exec'), module.__dict__)
    restored = module.ConsultationHooks(hooks.binding, caller, query, code_usage_enabled=True)
    restored.rows = deepcopy(hooks.rows)
    asyncio.run(restored.reauthorize_evidence({}, assets=assets, evidence=proofs))
    assert hasattr(module, 'WorkspaceCodeReader') and not hasattr(module, 'CodeContextTransport')


def test_generated_runtime_executes_first_use_code_discovery(monkeypatch):
    import sys
    from types import ModuleType
    from app.modules.ai_data_governance.governance_source import governance_agent_source
    from tests.support.ai_dg_fixtures import binding
    hooks, assets, _, _, _, caller, query, _, request = usage_harness()
    query.return_value = []
    def current(method, path, **kwargs):
        if path.endswith('/jobs'):
            return {'items': [{'key':JOB}]}, {}
        return request(method, path, **kwargs)
    caller.side_effect = current
    module = ModuleType('governance_current_code_fixture')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(governance_agent_source(binding()), 'governance_runtime.py', 'exec'), module.__dict__)
    restored = module.ConsultationHooks(hooks.binding, caller, query, code_usage_enabled=True)
    restored.rows = deepcopy(hooks.rows)
    proofs = read_usage(restored, assets)
    assert len(proofs) == 1 and proofs[0]['facts']['execution_evidence'] is False
    asyncio.run(restored.reauthorize_evidence({}, assets=assets, evidence=proofs))


@pytest.mark.parametrize('stored', ['missing', 'expired'])
def test_current_source_discovery_works_after_metadata_versions_change_or_evidence_expires(stored):
    hooks, assets, _, _, job, caller, query, candidate, request = usage_harness()
    if stored == 'missing':
        query.return_value = []
    else:
        candidate['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    def current(method, path, **kwargs):
        if path.endswith('/jobs'):
            assert kwargs == {'params': {'limit': '32'}}
            return {'items': [{'key': JOB}]}, {}
        return request(method, path, **kwargs)
    caller.side_effect = current
    before = datetime.now(timezone.utc)
    proofs = read_usage(hooks, assets)
    assert len(proofs) == 1 and proofs[0]['version_id'] == assets[0]['version_id']
    assert proofs[0]['coverage'] == 'partial' and proofs[0]['facts']['execution_evidence'] is False
    assert 'never-return-this' not in json.dumps(proofs)
    assert before <= datetime.fromisoformat(proofs[0]['facts']['observed_at'])
    assert datetime.fromisoformat(proofs[0]['facts']['expires_at']) - datetime.fromisoformat(proofs[0]['facts']['observed_at']) == timedelta(minutes=10)
    assert proofs[0]['facts']['source_evidence_id'] != candidate['evidence_id']
    asyncio.run(hooks.reauthorize_evidence({}, assets=assets, evidence=proofs))
    job['tasks'][0]['filePath'] = '/Workspace/other.py'
    with pytest.raises(PermissionError):
        asyncio.run(hooks.reauthorize_evidence({}, assets=assets, evidence=proofs))


def test_cancelled_current_discovery_stops_before_following_a_job():
    from threading import Event
    from app.modules.ai_data_governance.code_usage import current_code_references
    hooks, _, row, _, _, caller, _, _, request = usage_harness()
    started, release = Event(), Event()
    def blocked(method, path, **kwargs):
        if path.endswith('/jobs'):
            started.set()
            if not release.wait(3):
                raise TimeoutError('fixture release missing')
            return {'items': [{'key': JOB}]}, {}
        return request(method, path, **kwargs)
    caller.side_effect = blocked
    caller.reset_mock()
    async def run():
        task = asyncio.create_task(current_code_references(caller, hooks.binding['workspace_key'], [row]))
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
    asyncio.run(run())
    assert len(caller.call_args_list) == 1 and caller.call_args.args[1].endswith('/jobs')


def test_changed_source_with_the_same_usage_is_rediscovered_before_old_evidence_expires():
    hooks, assets, _, ref, _, caller, _, _, request = usage_harness()
    updated = SOURCE + '\n# Updated implementation\n'
    def current(method, path, **kwargs):
        if path.endswith('/jobs'):
            return {'items': [{'key': JOB}]}, {}
        if '/objects/' in path:
            return updated, {}
        return request(method, path, **kwargs)
    caller.side_effect = current
    proofs = read_usage(hooks, assets)
    assert len(proofs) == 1
    observed = proofs[0]['facts']['references'][0]
    assert observed['source_hash'] != ref['source_hash']
    assert observed['source_hash'] == source_references(PATH, updated)['source_hash']
    assert proofs[0]['facts']['execution_evidence'] is False
    asyncio.run(hooks.reauthorize_evidence({}, assets=assets, evidence=proofs))


def test_current_discovery_shares_the_stored_source_verification_budget():
    from app.modules.ai_data_governance.code_usage import current_code_references
    hooks, _, row, _, job, caller, _, _, request = usage_harness()
    verifier = CallerCodeVerifier(caller, hooks.binding['workspace_key'])
    verifier.jobs = {'existing-' + str(n): {'key': 'existing-' + str(n)} for n in range(16)}
    def current(method, path, **kwargs):
        if path.endswith('/jobs'):
            return {'items': [{'key': JOB}]}, {}
        return request(method, path, **kwargs)
    caller.side_effect = current
    assert asyncio.run(current_code_references(caller, hooks.binding['workspace_key'], [row], verifier=verifier)) == [(row, [])]
    assert len(verifier.jobs) == 16 and JOB not in verifier.jobs


def test_source_verifier_bounds_job_reads_across_all_candidates_in_one_phase():
    hooks, _, row, ref, job, caller, _, _, request = usage_harness()
    from uuid import UUID
    keys = [str(UUID(int=n + 1)) for n in range(17)]
    def bounded_read(method, path, **kwargs):
        if '/jobs/' in path:
            return {**job, 'key': path.rsplit('/', 1)[-1]}, {}
        return request(method, path, **kwargs)
    caller.side_effect = bounded_read
    caller.reset_mock()
    verifier = CallerCodeVerifier(caller, hooks.binding['workspace_key'])
    results = [verifier.verify({**ref, 'job_key': key}, row) for key in keys]
    assert results == [True] * 16 + [False]
    assert sum('/jobs/' in c.args[1] for c in caller.call_args_list) == 16
    assert sum('/objects/' in c.args[1] for c in caller.call_args_list) == 1


def test_stored_source_payload_is_closed_and_cannot_include_raw_code():
    hooks, assets, _, _, _, _, _, candidate, _ = usage_harness()
    facts = json.loads(candidate['facts_json'])
    facts['usage_context']['source_text'] = SOURCE
    candidate['facts_json'] = json.dumps(facts)
    with pytest.raises(ValueError, match='Invalid static usage context'):
        read_usage(hooks, assets)
