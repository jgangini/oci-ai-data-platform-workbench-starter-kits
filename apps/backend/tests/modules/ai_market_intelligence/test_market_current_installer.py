"""Current installation cannot enter the legacy SQL or production lifecycle."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.services.oci.aidp import AidpProvisionConflict, AidpProvisionError, AidpProvisionPending
from app.modules.ai_market_intelligence import client as installer
from app.modules.ai_market_intelligence import client as current
from app.modules.ai_market_intelligence.installation.agent_package import agent_package
from tests.modules.ai_market_intelligence.test_market_installer import OP, ENDPOINT, state
from tests.api.test_api import make_client, login


def client():
    api = current.MarketAidpClient.__new__(current.MarketAidpClient)
    api.settings = SimpleNamespace(aidp_region='us-chicago-1', agent_model_id='model',
        objectstorage_namespace='namespace', compartment_id='compartment')
    api._save = lambda item: None
    api._workspace = lambda: {'key': 'workspace'}
    api._ensure_folder = lambda workspace, path: None
    return api


def saved(phase='verify', **resources):
    return state(phase, **resources)


def test_market_phases_never_create_sql_tables_or_publish_production():
    api = client()
    record = saved()
    events = []
    api._read_installation = lambda workspace: record
    api._ensure_governance_bucket = lambda: events.append('storage')
    api._shared_compute = api._catalog = api._model_step = api._ensure_agent_deployment = lambda *a, **k: pytest.fail('Legacy effect')
    api._files = api._agent = api._test_step = lambda *a: events.append('current')
    api._compute_step = lambda *a: events.append('compute')
    api._shared_oci_credential = lambda: {'key': 'oci'}
    api._ensure_role_permission = lambda *a: events.append('permission')
    record['resources'].update(agent_key='agent', agent_compute_key='compute')
    # Exercise every current transition, including the shared credential and permissions steps.
    for phase in installer.PHASES[:-1]:
        assert record['phase'] == phase
        if phase == 'credentials':
            record['resources']['apify_credential_key'] = 'project'
        if phase != 'credentials':
            result = api._advance('owner', OP, True, None, None)
        else:
            # Credentials are separately covered by the existing shared tests.
            record['phase'] = 'compute'
    assert result['installed'] and result['deployment_surface'] == 'TEST'
    assert 'runtime_version' not in result and record['phase'] == 'ready'
    assert result['operation_type'] == 'install'
    assert not {'mode', 'previous_operation_id'} & result.keys()
    assert 'production' not in installer.installation_phases(record)
    assert events[0] == 'storage' and 'compute' in events


def test_only_current_receipt_at_control_root_can_resume():
    api = client()
    paths = []
    api._workspace_json = lambda workspace, path, message: paths.append(path) or saved()
    assert api._read_installation('workspace') == saved()
    assert paths == [installer.CONTROL_ROOT + '/installer.json']
    assert installer.CONTROL_ROOT != installer.ROOT
    for change in ({'installer_version':1}, {'runtime_version':'v2'},
                   {'publication_root':'/Workspace/.control/modules/ai_market_intelligence_v2_installed'},
                   {'workspace':'other'}, {'mode':'install'}, {'mode':'reinstall'},
                   {'previous_operation_id':None}, {'previous_operation_id':OP}):
        rejected = {**saved(), **change}
        before = copy.deepcopy(rejected)
        api._workspace_json = lambda *args: rejected
        api._save = lambda *args: pytest.fail('Unsupported receipt must not be modified')
        with pytest.raises(AidpProvisionConflict, match='incompatible'):
            api._read_installation('workspace')
        assert rejected == before



def test_files_preserve_foreign_manifest_or_unowned_source():
    api = client()
    api._agents = lambda *a: []
    api._upload_file = lambda *a, **k: pytest.fail('Do not overwrite existing files')
    for descriptor, body in (({'owner': 'another'}, None), (None, b'foreign source')):
        api._workspace_json = lambda *a: descriptor
        api._workspace_object = lambda *a, **k: (body, {'type': 'FILE'} if body else {})
        with pytest.raises(AidpProvisionConflict, match='preserved|Unowned'):
            api._files(saved('files'))


def test_files_resume_owned_partial_upload_without_changing_dependencies():
    api = client()
    record = saved('files')
    objects = {}
    api._agents = lambda *a: []
    api._workspace_json = lambda workspace, path, message: json.loads(objects[path]) if path in objects else None
    api._workspace_object = lambda workspace, path, **k: (objects.get(path), {'type': 'FILE'} if path in objects else {})
    def upload(workspace, path, content, **kwargs):
        assert kwargs['repair_drift'] is False
        assert path.startswith(installer.AGENT_ROOT + '/')
        if path in objects:
            assert objects[path] == content
        objects[path] = content
    api._upload_file = upload
    api._files(record)
    before = copy.deepcopy(objects)
    api._files(record)
    assert before == objects
    assert objects[installer.AGENT_ROOT + '/requirements.txt'] == b'httpx==0.28.1\npypdf==6.20.1\n'
    source = objects[installer.AGENT_ROOT + '/market_intelligence.py']
    assert b'class Records' not in source and b'SCHEMA_DEFINITION' not in source
    assert len(record['resources']['source_sha256']) == 64
    from app.modules.ai_market_intelligence.installation.agent_package import authoring_files
    for name, raw in authoring_files().items():
        assert objects[installer.AGENT_ROOT + '/' + name] == raw


def test_uncertain_agent_creation_is_not_repeated():
    api = client()
    record = saved('agent', agent_compute_key='compute')
    api._workspace_json = lambda *a: api._descriptor(record)
    api._agents = lambda *a: []
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        raise TimeoutError('Submission outcome unknown')
    api._request = request
    with pytest.raises(TimeoutError):
        api._agent(record)
    with pytest.raises(AidpProvisionConflict, match='reconcile'):
        api._agent(record)
    assert len(calls) == 1


def probe_frame(result, *, role='assistant', status='completed', message_status='completed'):
    return b'data: ' + json.dumps({'response': {'status': status, 'output': [
        {'role': role, 'status': message_status, 'content': [{'type': 'output_text', 'text': json.dumps(result)}]}]}}).encode() + b'\n\n'


@pytest.mark.parametrize('fault', [
    pytest.param({}, id='valid'),
    pytest.param({'updates': {'source_sha256': 'b' * 64}}, id='source'),
    pytest.param({'updates': {'authenticated_session_bound': False}}, id='session'),
    pytest.param({'role': 'tool'}, id='role'),
    pytest.param({'status': 'in_progress'}, id='truncated'),
    pytest.param({'status': 'FAILED'}, id='error'),
    pytest.param({'updates': {'versions': {'python': '3.11.13', 'packages': {}}}}, id='package'),
    pytest.param({'updates': {'model_calls': 1}}, id='effect'),
    pytest.param({'message_status': 'FAILED'}, id='message_failed'),
    pytest.param({'message_status': 'CANCELLED'}, id='message_cancelled'),
    pytest.param({'message_status': 'INCOMPLETE'}, id='message_incomplete'),
    pytest.param({'status': 'in_progress', 'message_status': 'in_progress', 'done': True}, id='done_only'),
    pytest.param({'status': 'in_progress', 'message_status': 'COMPLETED', 'done': True, 'valid': True}, id='completed_message_with_done'),
])
def test_probe_requires_current_source_authenticated_session_and_no_effects(fault):
    api = client()
    api.signer = object()
    result = {'mode': 'market_runtime_probe', 'schema_version': 1, 'source_sha256': 'a' * 64,
        'research_enabled': False, 'business_tables_required': False, 'authenticated_session_bound': True,
        'model_calls': 0, 'apify_starts': 0,
        'versions': {'python': '3.11.13', 'packages': {name: 'observed' for name in current.RUNTIME_DISTRIBUTIONS}}}
    result.update(fault.get('updates', {}))
    raw = probe_frame(result, role=fault.get('role', 'assistant'), status=fault.get('status', 'completed'),
                      message_status=fault.get('message_status', 'completed'))
    if fault.get('done'):
        raw += b'data: [DONE]\n\n'
    closed = []
    def post(endpoint, **kwargs):
        assert kwargs['json']['input'][0]['content'][0]['text'] == 'inspect_runtime'
        assert kwargs['headers']['x-deployment-type'] == 'TEST' and kwargs['allow_redirects'] is False
        return SimpleNamespace(raise_for_status=lambda: None, headers={'Content-Type': 'text/event-stream'},
            iter_content=lambda **k: iter([raw]), close=lambda: closed.append(True))
    api.session = SimpleNamespace(post=post)
    if fault and not fault.get('valid'):
        with pytest.raises((ValueError, AidpProvisionError)):
            api._probe_market(ENDPOINT, OP, 'a' * 64)
    else:
        receipt = api._probe_market(ENDPOINT, OP, 'a' * 64)
        assert receipt['semantic_accepted'] is False and receipt['mcp_verified'] is False
    assert closed == [True]


def test_admin_api_exposes_only_current_factory_and_rejects_engine_selectors(tmp_path):
    portal = make_client(tmp_path)
    calls = []
    class Native:
        async def status(self):
            return installer.public_status(saved())
        async def operate(self, *args, **kwargs):
            calls.append(args)
            return installer.public_status(saved())
    portal.app.state.market_factory = Native
    login(portal)
    observed = portal.get('/api/admin/modules/ai_market_intelligence')
    assert observed.status_code == 200 and observed.json()['deployment_surface'] == 'TEST'
    path = '/api/admin/users/user-id/modules/ai_market_intelligence'
    for version in ('legacy', 'v2', 'other'):
        assert portal.post(path, json={'operation_id':OP, 'runtime_version':version}).status_code == 400
        assert portal.get('/api/admin/modules/ai_market_intelligence?runtime_version='+version).status_code == 400
    assert calls == []
    assert portal.post(path, json={'operation_id':OP}).status_code == 202
    assert calls == [('ocid1.user.oc1..ada', OP)]
    assert portal.post(path + '/reinstall', json={
        'operation_id':OP, 'previous_operation_id':OP,
        'confirmation':'ai_market_intelligence'}).status_code == 404
    assert calls == [('ocid1.user.oc1..ada', OP)]
    for old_field in ({'mode':'reinstall'}, {'previous_operation_id':OP}):
        assert portal.post(path, json={'operation_id':OP, **old_field}).status_code == 400
    assert calls == [('ocid1.user.oc1..ada', OP)]



def test_market_has_no_reinstall_method_or_model_phases():
    api = client()
    from app.modules.ai_market_intelligence.api import LocalMarketClient
    assert not hasattr(api, 'reinstall') and not hasattr(LocalMarketClient(), 'reinstall')
    for phase in ('model_prepare', 'model_run', 'model_verify', 'cleanup', 'production'):
        with pytest.raises(AidpProvisionConflict, match='does not belong'):
            api._step(saved(phase), None, None)


def test_market_package_keeps_its_declared_dependencies_separate_from_legacy():
    _, files = agent_package({'schema_version': 1, 'research_enabled': False})
    assert files == {'requirements.txt': b'httpx==0.28.1\npypdf==6.20.1\n'}


def test_submitted_frozen_package_can_finish_after_backend_changes_without_rebinding(monkeypatch):
    import hashlib
    api = client()
    source, dependency = b'original source', b'original requirements'
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in
              [('market_intelligence.py', source), ('requirements.txt', dependency)]}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    record = saved('test', source_sha256=hashes['market_intelligence.py'], test_intent={'package_sha256': digest})
    record['pack_sha256'] = '0' * 64
    api._workspace_json = lambda *a: api._descriptor(record)
    objects = {'market_intelligence.py': source, 'requirements.txt': dependency}
    api._workspace_object = lambda workspace, path, **k: (objects[path.rsplit('/', 1)[1]], {})
    before = copy.deepcopy(record)
    api._assert_candidate(record)
    assert record == before, 'Do not replace the saved candidate with the new backend package'
    objects['requirements.txt'] = b'changed'
    with pytest.raises(AidpProvisionConflict, match='bytes changed'):
        api._assert_candidate(record)
    record['phase'] = 'files'
    with pytest.raises(AidpProvisionConflict, match='exact saved package'):
        api._assert_candidate(record)


def test_native_test_without_deployment_version_uses_its_exact_identity(monkeypatch):
    api = client()
    record = saved('test', agent_key='agent', agent_compute_key='compute', source_sha256='a' * 64,
                   test_intent={'previous_version': None})
    row = {'key': 'deployment', 'timeCreated': '2026-11-03T05:33:47.306Z', 'deploymentVersion': None,
           'lifecycleState': 'ACTIVE', 'agentFlowComputeKey': 'compute', 'endpointUrl': ENDPOINT}
    session = {'key': OP, 'agentFlowKey': 'agent', 'agentFlowComputeKey': 'compute', 'endpointUrl': ENDPOINT}
    native = SimpleNamespace(test_deployments=lambda *a: [copy.deepcopy(row)],
        _list=lambda *a, **k: [{key:value for key,value in session.items() if key!='endpointUrl'}],
        _request=lambda *a, **k: copy.deepcopy(session), session=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(current, 'MarketPlaygroundClient', lambda settings: native)
    api._probe_market = lambda endpoint, session, sha: {'source_sha256': sha}
    api._test_step(record, OP)
    identity = record['resources']['test_verified']['deployment_identity']
    assert identity['key'] == 'deployment' and identity['deploymentVersion'] is None
    row['key'] = 'other-revision'
    assert current.deployment_identity(row) != identity
    row['timeCreated'] = None
    with pytest.raises(RuntimeError, match='creation time'):
        api._test_step(record, OP)


@pytest.mark.parametrize('change',[{'agentFlowKey':'other'},{'agentFlowComputeKey':'other'},
                                  {'endpointUrl':ENDPOINT+'/other'},{'deploymentType':'PROD'},
                                  {'lifecycleState':'FAILED'}])
def test_missing_session_type_requires_matching_agent_compute_and_test_endpoint(change):
    row={'key':OP,'agentFlowKey':'agent','agentFlowComputeKey':'compute','endpointUrl':ENDPOINT}
    assert current.bound_test_sessions([row],'agent','compute',ENDPOINT)==[row]
    assert current.bound_test_sessions([{**row,**change}],'agent','compute',ENDPOINT)==[]


def test_current_compute_preserves_foreign_owner_and_resumes_exact_identity():
    from app.modules.ai_market_intelligence.installation.compute import compute_payload
    api = client()
    foreign = {**compute_payload('00000000-0000-4000-8000-000000000002'), 'key':'foreign', 'state':'ACTIVE'}
    before = copy.deepcopy(foreign)
    reads = []
    api._list = lambda *a, **k: [foreign]
    def observe_foreign(method, path, **kwargs):
        assert method == 'GET' and path == '/workspaces/workspace/clusters/foreign'
        reads.append(path)
        return copy.deepcopy(foreign)
    api._request = observe_foreign
    with pytest.raises(RuntimeError, match='outside this project operation'):
        api._compute_step(saved('compute'))
    assert foreign == before and reads == ['/workspaces/workspace/clusters/foreign']
    rows, posts = [], []
    api._list = lambda *a, **k: rows
    def request(method, path, **kwargs):
        if method == 'POST':
            posts.append(kwargs['payload'])
            rows.append({**kwargs['payload'], 'key':'current', 'state':'ACTIVE'})
            return rows[-1]
        return next(row for row in rows if path.endswith('/'+row['key']))
    api._request = request
    record = saved('compute')
    api._compute_step(record)
    api._compute_step(record)
    assert record['resources']['agent_compute_key'] == 'current'
    assert len(posts) == 1 and posts[0]['displayName'] == api.compute_name


@pytest.mark.parametrize('fields', [{'state': 'CREATING'}, {'lifecycleState': 'UPDATING'}, {}])
def test_pending_compute_key_is_saved_and_resume_waits_for_same_resource(fields):
    api = client()
    record, rows, calls, writes = saved('compute'), [], [], []
    api._save = lambda item: writes.append(copy.deepcopy(item))
    api._list = lambda *a, **k: rows
    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == 'POST':
            rows.append({**kwargs['payload'], 'key': 'current', **fields})
        assert method == 'POST' or path == '/workspaces/workspace/clusters/current'
        return rows[0]
    api._request = request
    for _ in range(2):
        with pytest.raises(AidpProvisionPending, match='dedicated AI Compute'):
            api._compute_step(record)
        assert writes[-1]['resources']['agent_compute_key'] == 'current'
        assert record['resources']['compute_creation_operation'] == OP
        assert record['operation_id'] == OP and record['phase'] == 'compute'
    rows[0].update(lifecycleState='ACTIVE', state='ACTIVE')
    api._compute_step(record)
    assert record['resources']['agent_compute_key'] == 'current'
    posts = [call for call in calls if call[0] == 'POST']
    assert len(posts) == 1 and posts[0][2]['retry_scope'] == 'ai-market-compute:' + OP



def test_test_session_submission_is_durable_and_does_not_repeat_after_timeout():
    api = client()
    record = saved('test', agent_key='agent', agent_compute_key='compute')
    record['test_sessions'] = []
    calls = []
    def request(method, path, **kwargs):
        calls.append((method, kwargs['payload']))
        raise TimeoutError('Unknown session submission')
    native = SimpleNamespace(_request=request)
    with pytest.raises(TimeoutError):
        api._test_session(native, record, [], None)
    with pytest.raises(AidpProvisionConflict, match='Reconcile'):
        api._test_session(native, record, [], None)
    assert len(calls) == 1 and calls[0][1]['deploymentType'] == 'TEST'
    row = {'key': 'session', 'displayName': api._agent_name + ' ' + OP}
    record['test_sessions'] = [{'value': 'session'}]
    assert api._test_session(native, record, [row], None) == 'session'
    assert api._test_session(native, record, [row], None) == 'session'
    with pytest.raises(AidpProvisionConflict, match='bound to'):
        api._test_session(native, record, [row], 'foreign')
    assert len(calls) == 1


def test_market_initial_test_uses_verified_native_init_contract(monkeypatch):
    from app.modules.ai_market_intelligence.installation.playground import MarketPlaygroundClient
    root = installer.AGENT_ROOT
    ga = SimpleNamespace(_workspace=lambda: {'key': 'workspace'},
        _request=lambda *a: {'type': 'CODE', 'computeKey': 'compute', 'pathInfo': root,
            'entryFilePath': root + '/market_intelligence.py', 'dependenciesFilePath': root + '/requirements.txt'},
        _workspace_object=lambda *a, **k: (b'content', {}), _content_matches=lambda *a: True,
        session=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr('app.modules.ai_market_intelligence.installation.playground.AidpClient', lambda settings: ga)
    native = MarketPlaygroundClient.__new__(MarketPlaygroundClient)
    native.settings = object()
    native.test_deployments = lambda *a: []
    calls, intents = [], []
    def request(method, path, **kwargs):
        assert intents, 'Persist intent before submitting TEST'
        calls.append((method, path, kwargs['payload']))
        return {}, {}
    native._request = request
    receipt = native.refresh('workspace', 'agent', 'compute', b'content', assets={'requirements.txt': b'content'},
        expected_workspace='workspace', agent_root=root, initialize=True, on_submit=intents.append)
    assert calls == [('POST', '/workspaces/workspace/actions/redeployAgentFlow',
        {'agentFlowKey': 'agent', 'agentFlowComputeKey': 'compute', 'deploymentType': 'TEST'})]
    assert receipt['deployment_type'] == 'TEST' and receipt['previous_version'] is None
