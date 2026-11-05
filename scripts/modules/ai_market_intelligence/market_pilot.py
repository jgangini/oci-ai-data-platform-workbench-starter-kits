"""One bounded native reference pass for the reviewed pilot; no semantic approvals."""
from contextlib import contextmanager
import hashlib
import json
import os
import re
from market_common import market_project_path, session


def write_json(path, value, *, immutable=False):
    raw = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(raw)
    else:
        temporary = path.with_suffix('.pending')
        temporary.write_text(raw, encoding='utf-8')
        temporary.replace(path)


def pilot_directory(root, cycle):
    if type(cycle) is not int or cycle not in (0,1,2):
        raise ValueError('The admitted pilot allows only reference pass zero and calibration cycles one or two')
    base=market_project_path(root,'.aidp-local/market/pilot')
    return base if cycle==0 else base / f'cycle-{cycle:02}'


def validate_pilot_cycle(root,value,cycle):
    previous=pilot_directory(root,cycle-1)
    definition_path=previous/'definition.json'
    progress_path=previous/'reference-progress.json'
    if not definition_path.exists() or not progress_path.exists():
        raise ValueError('Close the previous admitted pass before calibration')
    prior=json.loads(definition_path.read_text(encoding='utf-8'))
    progress=json.loads(progress_path.read_text(encoding='utf-8'))
    if len(progress['cases'])!=6 or not all(case['terminal'] for case in progress['cases'].values()):
        raise ValueError('Close the previous admitted pass before calibration')
    for name in ('requirements_ref','corpus_file','corpus_sha256','oracle_file','oracle_sha256','cases'):
        if prior[name]!=value[name]:
            raise ValueError('Calibration must preserve reviewed pilot questions and oracle/corpus bytes')
    if prior['candidate_revision']==value['candidate_revision']:
        raise ValueError('Calibration requires a revised candidate, not another identical trial')
    return {'calibration_cycle':cycle,'previous_definition_sha256':hashlib.sha256(definition_path.read_bytes()).hexdigest(),
            'previous_progress_sha256':hashlib.sha256(progress_path.read_bytes()).hexdigest()}


@contextmanager
def pilot_lock(root):
    path = market_project_path(root, '.aidp-local/market/pilot/driver.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def validate_pilot_question(case):
    if (set(case) != {'id', 'question', 'question_sha256', 'reuse_session_from'}
            or not re.fullmatch(r'[A-Z][A-Z0-9_-]{1,60}', case['id'])
            or not isinstance(case['question'], str) or not 1 <= len(case['question']) <= 500
            or hashlib.sha256(case['question'].encode()).hexdigest() != case['question_sha256']):
        raise ValueError('A pilot question or its stable identity changed')


def validate_pilot_cases(value, oracles):
    reviewed = {case['case_id']: case for case in oracles['cases']}
    if len(value['cases']) != 6 or len({case['id'] for case in value['cases']}) != 6:
        raise ValueError('The pilot must retain all six reviewed cases')
    seen = set()
    for case in value['cases']:
        validate_pilot_question(case)
        if case['reuse_session_from'] is not None and case['reuse_session_from'] not in seen:
            raise ValueError('A followup must name a preceding case in this pilot')
        rubric = reviewed.get(case['id'], {})
        if rubric.get('acceptance_ready') is not True or rubric.get('question_sha256') != case['question_sha256']:
            raise ValueError('A pilot question does not match its reviewed pre-run oracle')
        seen.add(case['id'])
    if seen != set(reviewed):
        raise ValueError('The pilot must retain exactly its reviewed oracle case set')


def validate_pilot_inputs(root, value):
    from app.packages.catalog import load_package
    required = {'version', 'requirements_ref', 'corpus_file', 'corpus_sha256', 'oracle_file', 'oracle_sha256', 'candidate_revision',
                'runtime_config', 'max_comparative_cycles', 'max_reference_passes', 'cases'}
    if set(value) != required or value['version'] != 1 or value['max_comparative_cycles'] != 2 or value['max_reference_passes'] != 1:
        raise ValueError('The pilot requires its exact finite reviewed definition')
    if value['candidate_revision'] != load_package('ai_market_intelligence', require_available=False).pack_sha256:
        raise ValueError('The pilot must bind the current runtime candidate')
    for name in ('corpus', 'oracle'):
        if hashlib.sha256(market_project_path(root, value[name + '_file']).read_bytes()).hexdigest() != value[name + '_sha256']:
            raise ValueError('The admitted corpus or oracle bytes changed')
    validate_pilot_cases(value, json.loads(market_project_path(root, value['oracle_file']).read_text(encoding='utf-8')))


def prepare_pilot(root, definition_path, *, cycle=0):
    value = json.loads(market_project_path(root, definition_path).read_text(encoding='utf-8'))
    validate_pilot_inputs(root, value)
    directory=pilot_directory(root,cycle)
    if cycle:
        value.update(validate_pilot_cycle(root,value,cycle))
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.config import validate_market_config
    binding = validate_market_config(json.loads(market_project_path(root, value['runtime_config']).read_text(encoding='utf-8')))
    if not binding['research_enabled'] or binding['services']['candidate_revision'] != value['candidate_revision']:
        raise ValueError('The pilot service binding differs from its declared candidate')
    from app.modules.ai_market_intelligence.installation.agent_package import agent_package
    source, _ = agent_package(binding)
    value['source_sha256'] = hashlib.sha256(source).hexdigest()
    value['runtime_config_sha256'] = hashlib.sha256(market_project_path(root, value['runtime_config']).read_bytes()).hexdigest()
    target = directory / 'definition.json'
    if target.exists():
        if json.loads(target.read_text(encoding='utf-8')) != value:
            raise ValueError('The admitted pilot definition is immutable; do not rename failed cycles')
    else:
        write_json(target, value, immutable=True)
    return {'pilot_cases': 6, 'reference_passes': 1, 'calibration_cycle':cycle,'max_comparative_cycles': 2, 'semantic_accepted': False}


def select_pilot_session(lifecycle, case, directory, pilot_directory, *, pass_key='p5-ref-01'):
    path = directory / 'session.json'
    tests = lifecycle._deployments('TEST')
    if path.exists():
        key = json.loads(path.read_text(encoding='utf-8'))['session_key']
    elif case['reuse_session_from']:
        previous = pilot_directory / case['reuse_session_from']
        key = json.loads((previous / 'session.json').read_text(encoding='utf-8'))['session_key']
        captured = json.loads((previous / 'request.json').read_text(encoding='utf-8'))
        if (captured.get('response') or {}).get('kind') != 'research_report':
            raise ValueError('The pilot followup requires its own preceding research report')
    else:
        label = pass_key+'-' + case['id']
        intent = directory / 'session-intent.json'
        rows = lifecycle._rows(lifecycle.ws + '/agentFlowSessions', legacy=True,
            query={'agentFlowKey': lifecycle.agent_key, 'deploymentType': 'TEST'})
        matches = [row for row in rows if (row.get('displayName') or row.get('name')) == lifecycle.name + ' ' + label]
        if intent.exists():
            if len(matches) != 1:
                raise ValueError('Reconcile the recorded session start; another session was not submitted')
            key = matches[0]['key']
        else:
            if matches:
                raise ValueError('An unowned session already uses this pilot case identity')
            write_json(intent, {'label': label, 'deployment_type': 'TEST'}, immutable=True)
            session(lifecycle, label, apply=True)
            key = lifecycle.state['test_session_key']
    detail = lifecycle._session_info(key, tests)
    if detail.get('lifecycleState') not in (None, 'ACTIVE'):
        raise ValueError('Wait for the recorded TEST session; do not create another')
    lifecycle.state['test_session_key'] = key
    lifecycle._save_state()
    if not path.exists():
        write_json(path, {'session_key': key, 'binding_verified': True}, immutable=True)
    return key


def freeze_pilot_request(storage, binding, owner_id, session_key, request_id, candidate):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.runtime_core import Scope
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.effects import MarketEffects, MarketObjectSnapshot
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.session import MarketSession
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.state import MarketRunStore
    scope = Scope(binding['tenant_id'], owner_id, binding['generation_id'])
    session = MarketSession(storage, binding['namespace'], binding['bucket'], scope, session_key)
    value, _ = session.snapshot.read()
    if not value or value.get('binding') != session.binding:
        raise ValueError('The native session does not match the authenticated operator and generation')
    pointer = value.get('requests', {}).get(request_id)
    if not pointer or pointer.get('revision') != candidate or pointer.get('session_binding') != session.binding:
        raise ValueError('The native request does not match this case, session and candidate')
    snapshots = {}
    for kind in ('request', 'effects'):
        snapshot = MarketObjectSnapshot(storage, binding['namespace'], binding['bucket'], scope,
            candidate, request_id, kind, session_binding=session.binding)
        snapshots[kind], _ = MarketRunStore(snapshot, candidate).read() if kind == 'request' else snapshot.read()
    request, effects = snapshots['request'], snapshots['effects']
    if not request or request.get('request_id') != request_id:
        raise ValueError('Native request snapshot escaped its exact scope')
    expected = MarketEffects(snapshot,candidate,request_id).binding
    if effects and effects.get('binding') != expected:
        raise ValueError('Native effect receipts escaped their exact request binding')
    return snapshots


def resume_known_capture(lifecycle,case,directory,snapshots,attempts,invoke,pass_key):
    operations=list((snapshots['effects'] or {}).get('operations',{}).items())
    if any(row['state'] in {'submitted','uncertain'} for _,row in operations):
        raise ValueError('Reconcile unresolved model or capture starts before resuming this case')
    pending=[(key,row['receipt']['runId']) for key,row in operations
             if row['kind']=='apify' and row['state']=='running' and (row.get('receipt') or {}).get('runId')]
    if not pending:
        raise ValueError('This case has no known capture to resume')
    identity=hashlib.sha256(json.dumps(sorted(pending)).encode()).hexdigest()
    permit=directory/'capture-resumes'/(identity+'.json')
    if permit.exists():
        raise ValueError('This pending capture was already resumed; reconcile its last receipt without another submission')
    write_json(permit,{'request_id':pass_key+'-'+case['id'],'known_capture_ids':[run for _,run in pending],
                      'no_uncertain_starts':True},immutable=True)
    return invoke(lifecycle,{'request_id':pass_key+'-'+case['id'],'question':case['question']},
                  directory/f'delivery-{len(attempts)+1:02}',timeout=600)


def pilot_delivery(lifecycle, case, directory, snapshots, *, invoke,pass_key='p5-ref-01',resume=False):
    """Recover a persisted final answer by replay; never restart pending inference."""
    attempts = sorted(directory.glob('delivery-*'))
    if not attempts:
        return invoke(lifecycle, {'request_id': pass_key+'-' + case['id'], 'question': case['question']},
                      directory / 'delivery-01', timeout=600)
    result_path = attempts[-1] / 'result.json'
    if not result_path.exists():
        raise ValueError('Reconcile the submitted delivery without a returned receipt; no inference was repeated')
    result = json.loads(result_path.read_text(encoding='utf-8'))
    if result['complete'] and result.get('response_status') != 'pending':
        return result
    if snapshots is None or snapshots['request']['response'] is None:
        if resume and result['complete'] and result.get('response_status')=='pending' and snapshots is not None:
            return resume_known_capture(lifecycle,case,directory,snapshots,attempts,invoke,pass_key)
        return result
    replay = directory / 'replay-01'
    if replay.exists():
        path = replay / 'result.json'
        if not path.exists():
            raise ValueError('Reconcile the submitted replay before another delivery')
        return json.loads(path.read_text(encoding='utf-8'))
    return invoke(lifecycle, {'action': 'replay', 'request_id': pass_key+'-' + case['id']}, replay, timeout=600)


def pilot_case_closed(snapshots,result):
    if not result['complete']:
        return False
    response = snapshots['request'].get('response')
    if response is None:
        return snapshots['request']['status']=='failed' and result.get('response_status')=='failed'
    return (snapshots['request']['status'] in {'completed','cancelled','needs_clarification'}
            and result.get('response_status')==response['status'] and response['status']!='pending')


def record_pilot_observation(directory, snapshots, result):
    observation = hashlib.sha256(json.dumps(snapshots, sort_keys=True).encode()).hexdigest()
    path = directory / 'observations' / (observation + '.json')
    if not path.exists():
        write_json(path, snapshots, immutable=True)
    terminal=pilot_case_closed(snapshots,result)
    if terminal:
        for kind, value in snapshots.items():
            path = directory / (kind + '.json')
            if path.exists():
                if json.loads(path.read_text(encoding='utf-8')) != value:
                    raise ValueError('A terminal pilot snapshot cannot be replaced by another observation')
            else:
                write_json(path, value, immutable=True)
    return terminal


def run_pilot_case(lifecycle, directory, case, definition, storage, binding, owner_id, *, pass_key='p5-ref-01',resume=False):
    from market_invoke import invoke_market
    case_directory = market_project_path(lifecycle.root, directory / case['id'])
    case_directory.mkdir(parents=True, exist_ok=True)
    if case['reuse_session_from']:
        prior = json.loads((directory / case['reuse_session_from'] / 'request.json').read_text(encoding='utf-8'))
        if (prior.get('response') or {}).get('kind') != 'research_report':
            blocked = {'terminal': True, 'transport_complete': False, 'response_status': 'blocked',
                'semantic_accepted': False, 'question_sha256': case['question_sha256'],
                'reason': 'The preceding pilot case did not produce a report; no followup was submitted'}
            path = case_directory / 'blocked.json'
            if not path.exists():
                write_json(path, blocked, immutable=True)
            return blocked
    key = select_pilot_session(lifecycle, case, case_directory, directory,pass_key=pass_key)
    request_id = pass_key+'-' + case['id']
    snapshots = None
    if any(case_directory.glob('delivery-*')):
        snapshots = freeze_pilot_request(storage, binding, owner_id, key, request_id, definition['candidate_revision'])
    result = pilot_delivery(lifecycle, case, case_directory, snapshots, invoke=invoke_market,pass_key=pass_key,resume=resume)
    snapshots = freeze_pilot_request(storage, binding, owner_id, key, request_id, definition['candidate_revision'])
    terminal = record_pilot_observation(case_directory, snapshots, result)
    return {'terminal': terminal, 'transport_complete': result['complete'],
        'response_status': result.get('response_status'), 'semantic_accepted': False,
        'question_sha256': case['question_sha256']}


@contextmanager
def pilot_storage(lifecycle,root,config_path):
    import oci
    binding = json.loads(config_path.read_text(encoding='utf-8'))['services']
    profile_path = lifecycle.config['oci']['config_file']
    from pathlib import Path
    profile_path = Path(profile_path).expanduser()
    if not profile_path.is_absolute():
        profile_path = root / profile_path
    profile = oci.config.from_file(str(profile_path), lifecycle.config['oci'].get('profile', 'DEFAULT'))
    identity = hashlib.sha256(json.dumps([profile[key] for key in ('tenancy', 'user', 'fingerprint')], separators=(',', ':')).encode()).hexdigest()
    if identity != binding['identity_sha256']:
        raise ValueError('The pilot operator differs from the verified service identity')
    owner_id = hashlib.sha256(profile['user'].encode()).hexdigest()
    storage = oci.object_storage.ObjectStorageClient({**profile, 'region': binding['region']})
    try:
        yield storage,binding,owner_id
    finally:
        storage.base_client.session.close()


def validate_previous_pass(root,definition,cycle):
    if not cycle:
        return
    previous=pilot_directory(root,cycle-1)
    for filename,field in [('definition.json','previous_definition_sha256'),('reference-progress.json','previous_progress_sha256')]:
        if hashlib.sha256((previous/filename).read_bytes()).hexdigest()!=definition[field]:
            raise ValueError('The preceding calibration evidence changed after admission')


def run_pilot(lifecycle, root, *, apply,cycle=0,resume_case=None):
    from app.packages.catalog import load_package
    directory = pilot_directory(root,cycle)
    definition = json.loads((directory / 'definition.json').read_text(encoding='utf-8'))
    if resume_case is not None and resume_case not in {case['id'] for case in definition['cases']}:
        raise ValueError('The resume case is not part of the admitted pilot')
    if definition.get('calibration_cycle',0)!=cycle:
        raise ValueError('The selected calibration does not match its admitted definition')
    validate_previous_pass(root,definition,cycle)
    config_path = market_project_path(root, definition['runtime_config'])
    for name in ('corpus', 'oracle'):
        if hashlib.sha256(market_project_path(root, definition[name + '_file']).read_bytes()).hexdigest() != definition[name + '_sha256']:
            raise ValueError('The admitted corpus or oracle bytes changed before execution')
    if (hashlib.sha256(config_path.read_bytes()).hexdigest() != definition['runtime_config_sha256']
            or definition['candidate_revision'] != load_package('ai_market_intelligence', require_available=False).pack_sha256
            or hashlib.sha256(market_project_path(root, lifecycle.config['agent']['entry']).read_bytes()).hexdigest() != definition['source_sha256']):
        raise ValueError('The admitted pilot binding changed; preserve its previous observations')
    if not apply:
        return {'action': 'pilot-run', 'applied': False, 'cases': len(definition['cases'])}
    progress = {'reference_pass': int(cycle==0),'calibration_cycle':cycle, 'semantic_accepted': False, 'cases': {}}
    progress_path = directory / 'reference-progress.json'
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding='utf-8'))
    with pilot_storage(lifecycle,root,config_path) as (storage,binding,owner_id):
        for case in definition['cases']:
            if progress['cases'].get(case['id'], {}).get('terminal'):
                continue
            progress['cases'][case['id']] = run_pilot_case(lifecycle, directory, case, definition, storage, binding, owner_id,
                pass_key='p5-ref-01' if cycle==0 else f'p5-cal-{cycle:02}',resume=case['id']==resume_case)
            write_json(progress_path, progress)
            if not progress['cases'][case['id']]['terminal']:
                return {'action': 'pilot-run', 'applied': True, 'state': 'reconcile_recorded_case', 'case_id': case['id']}
        return {'action': 'pilot-run', 'applied': True, 'terminal_cases': len(progress['cases']), 'semantic_accepted': False}
