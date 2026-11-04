"""Pilot admission is immutable and native freezing neither scans nor mutates history."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from test_market_intelligence_v2_runtime import V2StorageFixture, v2_active_config


def script():
    path = Path(__file__).resolve().parents[3] / 'scripts/market_v2_pilot.py'
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('market_v2_pilot_checks', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def definition(root):
    from app.lab_packs import load_lab_pack
    config = v2_active_config()
    config['services']['candidate_revision'] = load_lab_pack('ai_market_intelligence', require_available=False).pack_sha256
    (root / 'runtime.json').write_text(json.dumps(config), encoding='utf-8')
    (root / 'corpus.json').write_text('[]', encoding='utf-8')
    cases = [{'id': f'FIXTURE-{index}', 'question': f'Fixture question {index}',
              'question_sha256': hashlib.sha256(f'Fixture question {index}'.encode()).hexdigest(),
              'reuse_session_from': None} for index in range(6)]
    oracles = {'cases': [{'case_id': case['id'], 'question_sha256': case['question_sha256'],
                          'acceptance_ready': True} for case in cases]}
    (root / 'oracles.json').write_text(json.dumps(oracles), encoding='utf-8')
    value = {'version': 1, 'requirements_ref': 'fixture requirements', 'corpus_file': 'corpus.json',
        'corpus_sha256': hashlib.sha256((root / 'corpus.json').read_bytes()).hexdigest(),
        'oracle_file': 'oracles.json', 'oracle_sha256': hashlib.sha256((root / 'oracles.json').read_bytes()).hexdigest(),
        'candidate_revision': config['services']['candidate_revision'], 'runtime_config': 'runtime.json',
        'max_comparative_cycles': 2, 'max_reference_passes': 1, 'cases': cases}
    path = root / 'definition.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    return path, value


def test_prepare_requires_reviewed_questions_and_preserves_the_admitted_definition(tmp_path):
    module = script()
    path, value = definition(tmp_path)
    assert module.prepare_pilot(tmp_path, path)['pilot_cases'] == 6
    assert module.prepare_pilot(tmp_path, path)['max_comparative_cycles'] == 2
    admitted = tmp_path / '.aidp-local/v2/pilot/definition.json'
    before = admitted.read_bytes()
    value['requirements_ref'] = 'a changed authority'
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='immutable'):
        module.prepare_pilot(tmp_path, path)
    assert admitted.read_bytes() == before


@pytest.mark.parametrize('fault', ['question', 'oracle', 'corpus', 'cycle', 'dependency', 'escape'])
def test_pilot_rejects_changed_inputs_or_unbounded_cycles_before_native_access(tmp_path, fault):
    module = script()
    path, value = definition(tmp_path)
    if fault == 'question':
        value['cases'][0]['question'] = 'changed'
    elif fault == 'oracle':
        (tmp_path / 'oracles.json').write_text('{}', encoding='utf-8')
    elif fault == 'corpus':
        (tmp_path / 'corpus.json').write_text('changed', encoding='utf-8')
    elif fault == 'cycle':
        value['max_comparative_cycles'] = 3
    elif fault == 'dependency':
        value['cases'][0]['reuse_session_from'] = value['cases'][-1]['id']
    else:
        value['runtime_config'] = '../outside.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError):
        module.prepare_pilot(tmp_path, path)
    assert not (tmp_path / '.aidp-local/v2/pilot/definition.json').exists()


def test_two_pilot_drivers_cannot_mutate_the_same_native_target(tmp_path):
    module = script()
    with module.pilot_lock(tmp_path):
        with pytest.raises(OSError):
            with module.pilot_lock(tmp_path):
                pytest.fail('A second driver acquired the shared pilot lock')
    with module.pilot_lock(tmp_path):
        pass


@pytest.mark.parametrize('name', ['corpus', 'oracles'])
def test_run_rechecks_admitted_document_hashes_before_native_access(tmp_path, name):
    from types import SimpleNamespace
    module = script()
    path, _ = definition(tmp_path)
    module.prepare_pilot(tmp_path, path)
    (tmp_path / (name + '.json')).write_text('changed after admission', encoding='utf-8')
    with pytest.raises(ValueError, match='before execution'):
        module.run_pilot(SimpleNamespace(), tmp_path, apply=True)


def test_pending_native_request_is_observed_then_replayed_without_repeating_inference(tmp_path):
    module = script()
    case = {'id': 'FIXTURE-0', 'question': 'Fixture question'}
    delivery = tmp_path / 'delivery-01'
    delivery.mkdir()
    pending = {'complete': True, 'response_status': 'pending'}
    (delivery / 'result.json').write_text(json.dumps(pending), encoding='utf-8')
    original_receipt = (delivery / 'result.json').read_bytes()
    snapshots = {'request': {'status': 'running', 'response': None}, 'effects': {'operations': {'one': 'pending'}}}
    def no_inference(*args, **kwargs):
        pytest.fail('A pending research request must not be submitted again')
    assert module.pilot_delivery(None, case, tmp_path, snapshots, invoke=no_inference) == pending
    assert module.record_pilot_observation(tmp_path, snapshots, pending) is False
    assert not (tmp_path / 'request.json').exists()
    assert len(list((tmp_path / 'observations').glob('*.json'))) == 1
    final = {'request': {'status': 'completed', 'response': {'status': 'partial'}},
             'effects': {'operations': {'one': 'completed'}}}
    calls = []
    def replay(lifecycle, command, output, **kwargs):
        assert command == {'action': 'replay', 'request_id': 'p5-ref-01-FIXTURE-0'}
        calls.append(command)
        output.mkdir()
        result = {'complete': True, 'response_status': 'partial'}
        (output / 'result.json').write_text(json.dumps(result), encoding='utf-8')
        return result
    result = module.pilot_delivery(None, case, tmp_path, final, invoke=replay)
    assert module.record_pilot_observation(tmp_path, final, result) is True
    assert json.loads((tmp_path / 'request.json').read_text()) == final['request']
    assert len(list((tmp_path / 'observations').glob('*.json'))) == 2
    assert module.pilot_delivery(None, case, tmp_path, final, invoke=no_inference) == result
    assert len(calls) == 1 and (delivery / 'result.json').read_bytes() == original_receipt


def test_freeze_reads_the_exact_owned_request_without_listing_or_writing(tmp_path):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.runtime_core import Scope
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_effects import V2ObjectSnapshot, V2Effects
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_session import V2Session
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_state import V2RunStore
    module = script()
    binding = v2_active_config()['services']
    scope = Scope(binding['tenant_id'], 'fixture-owner', binding['generation_id'])
    storage = V2StorageFixture()
    session = V2Session(storage, binding['namespace'], binding['bucket'], scope, 'fixture-session')
    pointer = session.reserve('fixture-request', 'Fixture question', binding['candidate_revision'])
    snapshot = V2ObjectSnapshot(storage, binding['namespace'], binding['bucket'], scope,
        pointer['revision'], 'fixture-request', 'request', session_binding=session.binding)
    request = V2RunStore(snapshot, pointer['revision']).begin('fixture-request', 'Fixture question')
    effects = V2ObjectSnapshot(storage, binding['namespace'], binding['bucket'], scope,
        pointer['revision'], 'fixture-request', 'effects', session_binding=session.binding)
    effects.write({'binding': V2Effects(effects, pointer['revision'], 'fixture-request').binding, 'operations': {}}, None)
    before = storage.revision
    result = module.freeze_pilot_request(storage, binding, 'fixture-owner', 'fixture-session', 'fixture-request', pointer['revision'])
    assert result['request'] == request and storage.revision == before
    with pytest.raises(ValueError, match='authenticated operator'):
        module.freeze_pilot_request(storage, binding, 'foreign-owner', 'fixture-session', 'fixture-request', pointer['revision'])
    with pytest.raises(ValueError, match='case, session and candidate'):
        module.freeze_pilot_request(storage, binding, 'fixture-owner', 'fixture-session', 'fixture-request', 'a' * 64)
    assert storage.revision == before


def test_recorded_failed_attempt_closes_only_the_case_and_blocks_dependent_followup(tmp_path):
    from types import SimpleNamespace
    module = script()
    pilot = tmp_path / 'pilot'
    case_dir = pilot / 'FIXTURE-0'
    snapshots = {'request': {'status': 'failed', 'response': None}, 'effects': {'operations': {'one': 'running'}}}
    result = {'complete': True, 'response_status': 'failed'}
    assert module.record_pilot_observation(case_dir, snapshots, result) is True
    case = {'id': 'FIXTURE-FOLLOWUP', 'question': 'Summarize', 'question_sha256': 'fixture-hash',
            'reuse_session_from': 'FIXTURE-0'}
    blocked = module.run_pilot_case(SimpleNamespace(root=tmp_path), pilot, case, {}, None, None, None)
    assert blocked['response_status'] == 'blocked' and blocked['semantic_accepted'] is False
    assert blocked['transport_complete'] is False
    assert json.loads((case_dir / 'effects.json').read_text())['operations']['one'] == 'running'
    assert not (pilot / case['id'] / 'session-intent.json').exists()


def test_a_delivered_clarification_closes_the_case_without_becoming_a_research_pass(tmp_path):
    module=script()
    snapshots={'request':{'status':'needs_clarification','response':{'status':'needs_clarification','references':[]}},'effects':{}}
    assert module.record_pilot_observation(tmp_path,snapshots,{'complete':True,'response_status':'needs_clarification'}) is True
    assert json.loads((tmp_path/'request.json').read_text())['response']['references']==[]


def test_calibration_cannot_reset_inputs_repeat_a_candidate_or_open_a_third_cycle(tmp_path):
    module=script()
    path,value=definition(tmp_path)
    module.prepare_pilot(tmp_path,path)
    with pytest.raises(ValueError,match='previous admitted pass'):
        module.prepare_pilot(tmp_path,path,cycle=1)
    base=tmp_path/'.aidp-local/v2/pilot'
    (base/'reference-progress.json').write_text(json.dumps({'cases':{case['id']:{'terminal':True} for case in value['cases']}}))
    with pytest.raises(ValueError,match='revised candidate'):
        module.prepare_pilot(tmp_path,path,cycle=1)
    prior=json.loads((base/'definition.json').read_text())
    prior['candidate_revision']='a'*64
    (base/'definition.json').write_text(json.dumps(prior))
    assert module.prepare_pilot(tmp_path,path,cycle=1)['calibration_cycle']==1
    assert module.prepare_pilot(tmp_path,path,cycle=1)['calibration_cycle']==1
    saved=json.loads((base/'cycle-01/definition.json').read_text())
    assert saved['previous_definition_sha256']==hashlib.sha256((base/'definition.json').read_bytes()).hexdigest()
    value['requirements_ref']='another authority'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='preserve reviewed'):
        module.prepare_pilot(tmp_path,path,cycle=1)
    with pytest.raises(ValueError,match='only reference'):
        module.pilot_directory(tmp_path,3)


def test_explicit_resume_reuses_a_known_capture_once_and_rejects_unresolved_model(tmp_path):
    module=script()
    case={'id':'FIXTURE-0','question':'Research'}
    directory=tmp_path/'delivery-01'
    directory.mkdir()
    (directory/'result.json').write_text(json.dumps({'complete':True,'response_status':'pending'}))
    snapshots={'request':{'status':'running','response':None},'effects':{'operations':{
        'capture':{'kind':'apify','state':'running','receipt':{'runId':'existing-run'}}}}}
    calls=[]
    def invoke(lifecycle,command,output,**kwargs):
        calls.append(command)
        assert output.name=='delivery-02' and command=={'request_id':'p5-ref-01-FIXTURE-0','question':'Research'}
        return {'complete':True,'response_status':'partial'}
    snapshots['effects']['operations']['model']={'kind':'model','state':'uncertain'}
    with pytest.raises(ValueError,match='unresolved model'):
        module.pilot_delivery(None,case,tmp_path,snapshots,invoke=invoke,resume=True)
    assert calls==[]
    del snapshots['effects']['operations']['model']
    result=module.pilot_delivery(None,case,tmp_path,snapshots,invoke=invoke,resume=True)
    assert result['response_status']=='partial' and len(calls)==1
    with pytest.raises(ValueError,match='already resumed'):
        module.pilot_delivery(None,case,tmp_path,snapshots,invoke=invoke,resume=True)
    assert len(calls)==1
