import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

import pytest

from app.ai_data_governance.consultation import SourceObservationExpired, _consultation_proof
from app.ai_data_governance.governance_graph import build_governance_graph, GovernanceNodes
from test_ai_dg_code_usage import SOURCE, read_usage, usage_harness


def expired_source(hooks, assets):
    proof = deepcopy(read_usage(hooks, assets)[0])
    proof['facts'].update(observed_at=(datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat(),
        expires_at=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat())
    return _consultation_proof(hooks.rows[proof['asset_id']], 'STATIC_CODE', proof['facts'],
        'partial', discriminator=proof['facts']['source_evidence_id'])


def test_reobservation_preserves_old_proof_and_reads_exact_current_locators():
    hooks, assets, _, _, _, caller, _, _, _ = usage_harness()
    old = expired_source(hooks, assets)
    before = deepcopy(old)
    caller.reset_mock()
    fresh = asyncio.run(hooks.reobserve_evidence({}, assets=assets, evidence=[old]))
    assert old == before
    assert len(fresh) == 1 and fresh[0]['evidence_id'] != old['evidence_id']
    assert fresh[0]['facts']['source_evidence_id'] != old['facts']['source_evidence_id']
    assert fresh[0]['facts']['references'] == old['facts']['references']
    assert datetime.fromisoformat(fresh[0]['facts']['expires_at'])-datetime.fromisoformat(
        fresh[0]['facts']['observed_at']) == timedelta(minutes=10)
    assert not any(call.args[1].endswith('/jobs') for call in caller.call_args_list)
    assert any('/objects/' in call.args[1] for call in caller.call_args_list)
    assert 'never-return-this' not in json.dumps(fresh)
    asyncio.run(hooks.reauthorize_evidence({}, assets=assets, evidence=fresh))


@pytest.mark.parametrize('change', ['source', 'task', 'denied', 'identity'])
def test_reobservation_never_substitutes_changed_or_denied_source(change):
    hooks, assets, _, _, job, caller, _, _, request = usage_harness()
    old = expired_source(hooks, assets)
    def current(method, path, **kwargs):
        if '/objects/' in path:
            if change == 'source':
                return SOURCE+'\n# changed\n', {}
            if change == 'denied':
                raise PermissionError('revoked')
            if change == 'identity':
                caller.check_identity.side_effect = PermissionError('session changed')
        return request(method, path, **kwargs)
    if change == 'task':
        job['tasks'][0]['filePath'] = '/Workspace/other.py'
    caller.side_effect = current
    with pytest.raises(PermissionError):
        asyncio.run(hooks.reobserve_evidence({}, assets=assets, evidence=[old]))


def test_reobservation_cannot_repair_forged_timestamps_or_expand_assets():
    hooks, assets, _, _, _, _, _, _, _ = usage_harness()
    proof = expired_source(hooks, assets)
    with pytest.raises(PermissionError):
        asyncio.run(hooks.reobserve_evidence({}, assets=[], evidence=[proof]))
    proof['facts']['observed_at'] = (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
    proof['facts']['expires_at'] = (datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
    proof = _consultation_proof(hooks.rows[proof['asset_id']], 'STATIC_CODE', proof['facts'],
        'partial', discriminator=proof['facts']['source_evidence_id'])
    with pytest.raises(ValueError, match='future'):
        asyncio.run(hooks.reobserve_evidence({}, assets=assets, evidence=[proof]))


def test_reobservation_window_begins_before_reads_not_after_slow_verification(monkeypatch):
    from app.ai_data_governance import consultation
    hooks, assets, _, _, _, caller, _, _, request = usage_harness()
    old = expired_source(hooks, assets)
    class Clock(datetime):
        value = datetime.now(timezone.utc)
        @classmethod
        def now(cls, tz=None):
            return cls.value
    def slow(method, path, **kwargs):
        result = request(method, path, **kwargs)
        if '/objects/' in path:
            Clock.value += timedelta(minutes=11)
        return result
    caller.side_effect = slow
    monkeypatch.setattr(consultation, 'datetime', Clock)
    with pytest.raises(SourceObservationExpired):
        asyncio.run(hooks.reobserve_evidence({}, assets=assets, evidence=[old]))


def test_cancellation_stops_reobservation_without_emitting_new_proofs():
    from threading import Event
    hooks, assets, _, _, _, caller, _, _, request = usage_harness()
    old = expired_source(hooks, assets)
    entered, release = Event(), Event()
    def blocked(method, path, **kwargs):
        if '/jobs/' in path:
            entered.set()
            if not release.wait(3):
                raise TimeoutError('fixture release missing')
        return request(method, path, **kwargs)
    caller.side_effect = blocked
    caller.reset_mock()
    closed = []
    async def parent():
        try:
            await hooks.reobserve_evidence({}, assets=assets, evidence=[old])
        finally:
            closed.append(True)
    async def run():
        task = asyncio.create_task(parent())
        assert await asyncio.to_thread(entered.wait,3)
        try:
            for _ in range(2):
                task.cancel()
                for _ in range(3):
                    await asyncio.sleep(0)
            assert not task.done() and not closed
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(run())
    assert closed == [True]
    assert not any('/objects/' in call.args[1] for call in caller.call_args_list)


def recorded_source_reply(phase,payload,assets,late):
    ids = payload.get('review',{}).get('evidence_ids',
        [p['evidence_id'] for p in payload.get('evidence', [])])
    selected = payload.get('assets',assets)
    replies = {
        'G1':{'intent':'treatment' if late else 'usage', 'asset_mentions':['ABC13']},
        'G3':{'evidence_ids':ids, 'supported_claims':[{'text':'Referencia estática.',
            'evidence_ids':ids, 'kind':'observed'}], 'conflicts':[], 'gaps':[],
            'coverage':'sufficient', 'next_action':'answer'},
        'G4':{'findings':[{'text':'Uso estático.', 'evidence_ids':ids, 'kind':'observed'}],
            'potential_impacts':[], 'limitations':['No acredita ejecución.'], 'treatment_needed':True},
        'G5':{'problem':'Revisar la referencia estática.',
            'asset_ids':[asset['asset_id'] for asset in selected],
            'expected_versions':[asset['version_id'] for asset in selected],
            'suggested_action':'Revisar el archivo y la tarea observados.', 'responsible_ref':None,
            'verification_plan':'Verificar la referencia con una ejecución posterior.',
            'remaining_gaps':['No hay evidencia de ejecución.']},
        'G6':{'answer_markdown':'Referencia estática; no demuestra ejecución.',
            'cited_evidence_ids':ids, 'limitations':[], 'case_ref':None, 'result_state':'answered'}}
    return json.dumps(replies[phase])


def recovery_graph(*, expire_again=False, late=False, builder=build_governance_graph,
                   expiry_error=SourceObservationExpired, prepare=None):
    from langgraph.checkpoint.memory import InMemorySaver
    hooks, assets, _, _, _, _, _, _, _ = usage_harness()
    old = expired_source(hooks, assets)
    fresh = deepcopy(read_usage(hooks, assets)[0])
    calls = []
    checks = 0
    async def verify(config, **kwargs):
        nonlocal checks
        checks += 1
        if checks == (5 if late else 2) or expire_again and checks >= 4:
            raise expiry_error('recorded expiry')
    async def model(phase, payload, *args):
        calls.append(phase)
        return recorded_source_reply(phase,payload,assets,late)
    reobserve = AsyncMock(return_value=[fresh])
    memory = InMemorySaver()
    def build():
        return builder(authorize=AsyncMock(), resolve_assets=AsyncMock(return_value=assets),
            read=AsyncMock(return_value=[old]), call_model=model, capabilities={'find_code_usage'},
            checkpointer=memory, reauthorize_evidence=verify, reobserve_evidence=reobserve,
            prepare_case=prepare)
    return build, calls, reobserve, old, fresh


def test_expired_response_reobserves_once_and_reevaluates_before_answering():
    build, calls, observe, old, fresh = recovery_graph()
    result = asyncio.run(build().ainvoke({'question':'¿Dónde se usa ABC13?'},
        {'configurable':{'thread_id':'source-recovery'}}))
    assert calls == ['G1','G3','G3','G6']
    assert observe.await_count == 1
    assert result['source_recovery_count'] == 1
    assert result['source_recovery_history'] == [old]
    assert result['evidence'] == [fresh]
    assert result['response']['cited_evidence_ids'] == [fresh['evidence_id']]


@pytest.mark.parametrize('expires_during_g6', [(), (1,), (1, 2)])
@pytest.mark.parametrize('assembled', [False, True])
def test_read_only_source_expiry_during_generation_is_checked_before_delivery(
        monkeypatch, expires_during_g6, assembled):
    from app.ai_data_governance import consultation
    from langgraph.checkpoint.memory import InMemorySaver
    hooks, assets, _, _, _, _, _, _, _ = usage_harness()
    original = deepcopy(read_usage(hooks, assets)[0])
    latest = [original]
    calls = []
    g6_count = 0
    builder, expiry_error = build_governance_graph, SourceObservationExpired
    if assembled:
        import sys
        from types import ModuleType
        from app.ai_data_governance.governance_source import governance_agent_source
        from test_ai_dg_governance_source import binding
        module = ModuleType('governance_delivery_expiry_fixture')
        monkeypatch.setitem(sys.modules, module.__name__, module)
        exec(compile(governance_agent_source(binding()), 'governance_runtime.py', 'exec'), module.__dict__)
        builder, expiry_error = module.build_governance_graph, module.SourceObservationExpired

    class Clock(datetime):
        value = datetime.now(timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.value

    monkeypatch.setattr(consultation, 'datetime', Clock)

    async def verify(config, *, assets, evidence):
        for proof in evidence:
            try:
                hooks._checked_code_proof(proof, hooks.rows[proof['asset_id']])
            except SourceObservationExpired as exc:
                raise expiry_error(str(exc)) from exc

    async def observe(config, *, assets, evidence):
        latest[:] = [hooks._reobserved_proof(proof, Clock.value) for proof in evidence]
        return latest.copy()

    async def model(phase, payload, *args):
        nonlocal g6_count
        calls.append(phase)
        answer = recorded_source_reply(phase, payload, assets, False)
        if phase == 'G6':
            g6_count += 1
            if g6_count in expires_during_g6:
                Clock.value = datetime.fromisoformat(latest[0]['facts']['expires_at']) + timedelta(seconds=1)
        return answer

    reobserve = AsyncMock(side_effect=observe)
    graph = builder(authorize=AsyncMock(), resolve_assets=AsyncMock(return_value=assets),
        read=AsyncMock(return_value=[original]), call_model=model, capabilities={'find_code_usage'},
        checkpointer=InMemorySaver(), reauthorize_evidence=verify, reobserve_evidence=reobserve)
    invocation = graph.ainvoke({'question': '¿Dónde se usa ABC13? Sin proponer cambios.'},
        {'configurable': {'thread_id': 'read-only-delivery-expiry'}})
    if len(expires_during_g6) == 2:
        with pytest.raises(expiry_error):
            asyncio.run(invocation)
    else:
        result = asyncio.run(invocation)
        assert result['response']['cited_evidence_ids'] == [latest[0]['evidence_id']]
        assert not result.get('case_preview') and not result.get('treatment')
        if expires_during_g6:
            assert result['source_recovery_history'] == [original]
            assert original['evidence_id'] not in result['response']['cited_evidence_ids']
    assert reobserve.await_count == bool(expires_during_g6)
    assert calls == (['G1', 'G3', 'G6', 'G3', 'G6'] if expires_during_g6 else ['G1', 'G3', 'G6'])


def test_second_expiry_after_checkpoint_resume_cannot_start_another_reobservation():
    build, calls, observe, _, _ = recovery_graph(expire_again=True)
    config = {'configurable':{'thread_id':'bounded-source-recovery'}}
    async def run():
        await build().ainvoke({'question':'¿Dónde se usa ABC13?'}, config,
            interrupt_after=['reobserve'])
        assert observe.await_count == 1
        with pytest.raises(SourceObservationExpired):
            await build().ainvoke(None, config)
    asyncio.run(run())
    assert observe.await_count == 1
    assert calls == ['G1','G3','G3']


def test_expiry_after_g6_discards_old_draft_and_prepares_only_the_reevaluated_preview():
    prepare = AsyncMock(return_value={'confirmation_required':True})
    build, calls, observe, old, fresh = recovery_graph(late=True,prepare=prepare)
    result = asyncio.run(build().ainvoke({'question':'Propón un tratamiento sobre ABC13.'},
        {'configurable':{'thread_id':'post-g6-expiry'}}))
    assert calls == ['G1','G3','G4','G5','G6','G3','G4','G5','G6']
    assert observe.await_count == prepare.await_count == 1
    prepared = prepare.call_args.args[0]
    assert prepared['review']['evidence_ids'] == [fresh['evidence_id']]
    assert prepared['diagnosis']['findings'][0]['evidence_ids'] == [fresh['evidence_id']]
    assert old['evidence_id'] not in prepared['response']['cited_evidence_ids']
    assert result['source_recovery_history'] == [old]


def test_generated_runtime_executes_reobservation_and_reevaluation(monkeypatch):
    import sys
    from types import ModuleType
    from app.ai_data_governance.governance_source import governance_agent_source
    from test_ai_dg_governance_source import binding
    module = ModuleType('governance_source_recovery_fixture')
    monkeypatch.setitem(sys.modules,module.__name__,module)
    exec(compile(governance_agent_source(binding()),'governance_runtime.py','exec'),module.__dict__)
    build, calls, observe, old, fresh = recovery_graph(builder=module.build_governance_graph,
        expiry_error=module.SourceObservationExpired)
    result = asyncio.run(build().ainvoke({'question':'¿Dónde se usa ABC13?'},
        {'configurable':{'thread_id':'assembled-source-recovery'}}))
    assert calls == ['G1','G3','G3','G6'] and observe.await_count == 1
    assert result['source_recovery_history'] == [old]
    assert result['response']['cited_evidence_ids'] == [fresh['evidence_id']]


def test_changed_case_blocks_reobservation_before_reading_any_source(monkeypatch):
    from test_ai_dg_case_context import mixed_case_evidence, expire_static_observation
    from app.ai_data_governance.inventory import canonical
    hooks, assets, evidence, history, _, verifier = mixed_case_evidence(monkeypatch)
    expire_static_observation(hooks,evidence)
    body = json.loads(history[0]['data'])
    body['summary'] = 'Caso cambiado antes de recuperar'
    history[0]['data'] = canonical(body)
    with pytest.raises((PermissionError,RuntimeError)):
        asyncio.run(hooks.reobserve_evidence({},assets=assets,evidence=evidence))
    verifier.assert_not_called()


@pytest.mark.parametrize('window',['expired_before','expired_during','current'])
def test_source_recovery_does_not_renew_a_quality_measurement(monkeypatch,window):
    from app.ai_data_governance import governance_graph
    hooks, assets, _, _, _, _, _, _, _ = usage_harness()
    old = expired_source(hooks,assets)
    fresh = deepcopy(read_usage(hooks,assets)[0])
    now = datetime.now(timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return cls.current
    Clock.current = now
    monkeypatch.setattr(governance_graph,'datetime',Clock)
    quality = {**old, 'kind':'QUALITY_ASSESSMENT','evidence_id':'quality-measurement',
        'facts':{'measured_at':(now-timedelta(minutes=2)).isoformat(),
            'expires_at':(now-timedelta(minutes=1) if window=='expired_before' else now+timedelta(minutes=2)).isoformat()}}
    original_quality = deepcopy(quality)
    async def refresh(config, **kwargs):
        if window=='expired_during':
            Clock.current += timedelta(minutes=3)
        return [deepcopy(quality),fresh]
    observe = AsyncMock(side_effect=refresh)
    node = GovernanceNodes(authorize=AsyncMock(),resolve_assets=AsyncMock(),read=AsyncMock(),
        call_model=AsyncMock(),capabilities=(),reobserve_evidence=observe)
    state={'assets':assets,'evidence':[quality,old],'source_recovery_count':1,
        'source_recovery_pending':True,'source_recovery_history':[old]}
    if window=='current':
        result = asyncio.run(node.reobserve(state,{}))
        assert result['evidence'] == [original_quality,fresh]
    else:
        with pytest.raises(ValueError,match='measurement window'):
            asyncio.run(node.reobserve(state,{}))
    assert quality == original_quality
    assert observe.await_count == int(window!='expired_before')
    node.call_model.assert_not_awaited()
