"""Empty-installer boundaries; native side effects are recorded, never executed."""
import asyncio
import copy
import json
from types import SimpleNamespace
from pydantic import SecretStr

import pytest

from app.aidp import AidpProvisionConflict, AidpProvisionError, AidpProvisionPending
from app import market_intelligence as installer
from test_api import make_client, login
from test_aidp import bare_client, FakeResponse


OP = '00000000-0000-4000-8000-000000000001'
ENDPOINT = 'https://gateway.aidp.us-chicago-1.oci.oraclecloud.com/agent'


def state(phase='verify', **resources):
    return {'installer_version': 1, 'module_id': installer.MODULE_ID, 'workspace': 'workspace',
            'operation_id': OP, 'owner': 'owner', 'reuse_existing': True, 'phase': phase,
            'status': 'installing', 'resources': resources,
            'pack_sha256': installer.load_lab_pack(installer.MODULE_ID, require_available=False).pack_sha256}


def client():
    value = installer.MarketAidpClient.__new__(installer.MarketAidpClient)
    value.settings = SimpleNamespace(aidp_region='us-chicago-1', artifacts_bucket_name='artifacts',
                                     objectstorage_namespace='namespace')
    value._save = lambda item: None
    return value


def test_receipt_survives_native_json_readback_and_resume_without_format_drift():
    api = client()
    stored, writes = {}, []
    api._workspace_object = lambda *a, **k: (stored.get('body'), {'type': 'FILE'} if stored else {})
    def request(method, path, **kwargs):
        assert method == 'POST' and path == '/workspaces/workspace/objects'
        assert kwargs['headers']['path'] == installer.ROOT + '/installer.json'
        writes.append(kwargs['data'])
        stored['body'] = json.loads(kwargs['data'])
    api._request = request
    saved = state()
    for _ in range(2):
        installer.MarketAidpClient._save(api, saved)
        assert api._read_installation('workspace') == saved
    assert writes and all(api._content_matches(json.loads(body), body) for body in writes)


def test_agent_descriptor_uses_the_same_native_json_byte_contract(monkeypatch):
    api = client()
    api._runtime_config = lambda saved: {}
    monkeypatch.setattr(installer, 'agent_package', lambda config: (b'source', {}))
    def ensure(workspace, compute, module, root, source, descriptor, **kwargs):
        assert api._content_matches(json.loads(descriptor), descriptor)
        return 'agent', False
    api._ensure_agent = ensure
    api._verify_agent_bindings = lambda *args: None
    saved = state('agent', agent_compute_key='compute')
    api._step(saved, None, None)
    assert saved['resources']['agent_key'] == 'agent'


def output_receipt():
    receipt = {'schema_sha256': installer.schema_sha256(), 'documented_columns': 145,
               'tables': [{'table': table['name'], 'rows': 0, 'provider': 'delta', 'columns': len(table['columns'])}
                          for table in installer.definition()['tables']]}
    notebook = {'cells': [{'outputs': [{'text': ['AI_MARKET_MODEL_READY:' + json.dumps(receipt)]}]}]}
    return {'data': [{'value': json.dumps(notebook)}]}


@pytest.mark.parametrize('drift', ['entryFilePath', 'dependenciesFilePath', 'computeKey', None])
def test_native_agent_bindings_are_reconciled_without_replacing_identity(drift):
    api = client()
    api._agent_deployments = lambda *a: []
    expected = {'key': 'agent', 'displayName': installer.MODULE_ID, 'type': 'CODE', 'pathInfo': installer.ROOT + '/agent',
                'entryFilePath': installer.ROOT + '/agent/market_intelligence.py',
                'dependenciesFilePath': installer.ROOT + '/agent/requirements.txt', 'computeKey': 'compute'}
    current = {**expected, **({drift: 'old-binding'} if drift else {})}
    calls = []
    def request(method, path, **kwargs):
        assert path == '/workspaces/workspace/agents/agent'
        calls.append(method)
        if method == 'PUT':
            assert kwargs['headers'] == {'if-match': 'revision-1'}
            assert set(kwargs['payload']) == {'entryFilePath', 'dependenciesFilePath', 'computeKey'}
            current.update(kwargs['payload'])
        else:
            return (dict(current), {'ETag': 'revision-1'}) if kwargs.get('include_headers') else dict(current)
    api._request = request
    api._verify_agent_bindings('workspace', 'agent', 'compute')
    assert current == expected
    assert calls == (['GET', 'PUT', 'GET'] if drift else ['GET'])


@pytest.mark.parametrize('fault', ['foreign_name', 'foreign_key', 'wrong_type', 'missing_etag', 'stale_readback'])
def test_native_agent_binding_repair_fails_closed(fault):
    api = client()
    api._agent_deployments = lambda *a: []
    current = {'key': 'agent', 'displayName': installer.MODULE_ID, 'type': 'CODE', 'pathInfo': installer.ROOT + '/agent',
               'entryFilePath': installer.ROOT + '/agent/market_intelligence.py',
               'dependenciesFilePath': '/old/requirements.txt', 'computeKey': 'compute'}
    if fault == 'foreign_name':
        current['displayName'] = 'ai_data_governance'
    if fault == 'foreign_key':
        current['key'] = 'other-agent'
    if fault == 'wrong_type':
        current['type'] = 'LOW_CODE'
    writes = []
    def request(method, path, **kwargs):
        if method == 'PUT':
            writes.append(kwargs['payload'])
            return {}
        return (current, {} if fault == 'missing_etag' else {'etag': 'revision-1'}) if kwargs.get('include_headers') else current
    api._request = request
    with pytest.raises(AidpProvisionConflict):
        api._verify_agent_bindings('workspace', 'agent', 'compute')
    assert len(writes) == (1 if fault == 'stale_readback' else 0)


@pytest.mark.parametrize('etag_available', [True, False])
def test_existing_agent_phase_uses_only_the_conditional_binding_writer(monkeypatch, etag_available):
    api = client()
    api._runtime_config = lambda saved: {}
    monkeypatch.setattr(installer, 'agent_package', lambda config: (b'changed-source', {}))
    api._ensure_agent = lambda *a, **k: pytest.fail('Existing identity must not enter the unconditional base updater')
    api._agent_deployments = lambda *a: []
    current = {'key': 'agent', 'displayName': installer.MODULE_ID, 'type': 'CODE', 'pathInfo': installer.ROOT + '/agent',
               'entryFilePath': '/Workspace/ai_market_intelligence/agent/market_intelligence.py',
               'dependenciesFilePath': '/Workspace/ai_market_intelligence/agent/requirements.txt', 'computeKey': 'old'}
    writes, descriptors = [], []
    api._upload_file = lambda *a: descriptors.append(a)
    def request(method, path, **kwargs):
        if method == 'PUT':
            assert kwargs['headers'] == {'if-match': 'revision-1'}
            writes.append(kwargs['payload'])
            current.update(kwargs['payload'])
            return {}
        return (dict(current), {'etag': 'revision-1'} if etag_available else {}) if kwargs.get('include_headers') else dict(current)
    api._request = request
    saved = state('agent', agent_key='agent', agent_compute_key='compute')
    if etag_available:
        api._step(saved, None, None)
        assert len(writes) == len(descriptors) == 1
        assert current['entryFilePath'] == installer.ROOT + '/agent/market_intelligence.py'
    else:
        with pytest.raises(AidpProvisionConflict, match='concurrency token'):
            api._step(saved, None, None)
        assert not writes and not descriptors
    assert saved['resources']['agent_key'] == 'agent'


def test_binding_repair_preserves_an_active_production_deployment():
    api = client()
    api._agent_deployments = lambda *a: [{'key': 'production'}]
    current = {'key': 'agent', 'displayName': installer.MODULE_ID, 'type': 'CODE', 'pathInfo': installer.ROOT + '/agent'}
    def request(method, path, **kwargs):
        assert method == 'GET'
        return current, {'etag': 'revision-1'}
    api._request = request
    with pytest.raises(AidpProvisionConflict, match='production deployment'):
        api._verify_agent_bindings('workspace', 'agent', 'compute')


@pytest.mark.parametrize('root', ['/Workspace/ai_market_intelligence/agent', None])
def test_correct_entry_does_not_hide_a_stale_or_missing_project_root(root):
    api = client()
    detail = {'key': 'agent', 'displayName': installer.MODULE_ID, 'type': 'CODE', 'pathInfo': root,
              'entryFilePath': installer.ROOT + '/agent/market_intelligence.py',
              'dependenciesFilePath': installer.ROOT + '/agent/requirements.txt', 'computeKey': 'compute'}
    def request(method, path, **kwargs):
        assert method == 'GET', 'A create-only project root cannot be repaired by PUT'
        return detail, {'etag': 'revision-1'}
    api._request = request
    with pytest.raises(AidpProvisionConflict, match='project root'):
        api._verify_agent_bindings('workspace', 'agent', 'compute')


def test_model_requires_an_exact_empty_receipt_before_cleanup():
    assert len(installer.model_receipt(output_receipt())['tables']) == 14
    for invalid in ({'data': []}, {'data': output_receipt()['data'] * 2}):
        with pytest.raises(AidpProvisionPending):
            installer.model_receipt(invalid)
    invalid = output_receipt()
    notebook = json.loads(invalid['data'][0]['value'])
    notebook['cells'][0]['outputs'][0]['text'][0] = notebook['cells'][0]['outputs'][0]['text'][0].replace('"rows": 0', '"rows": 1', 1)
    invalid['data'][0]['value'] = json.dumps(notebook)
    with pytest.raises(AidpProvisionError, match='approved model'):
        installer.model_receipt(invalid)


def test_python_stdout_requires_the_same_unique_empty_model_receipt():
    marker = json.loads(output_receipt()['data'][0]['value'])['cells'][0]['outputs'][0]['text'][0]
    output = {'data': [{'value': 'Spark task output\n' + marker + '\n'}]}
    assert len(installer.model_receipt(output)['tables']) == 14
    with pytest.raises(AidpProvisionPending, match='unambiguous'):
        installer.model_receipt({'data': [{'value': marker + '\n' + marker}]})
    with pytest.raises(AidpProvisionError, match='incomplete'):
        installer.model_receipt({**output, 'isTruncated': True})
    for flag in ({'isBase64': True}, {'compression': 'GZIP'}):
        with pytest.raises(AidpProvisionError, match='explicit decoding'):
            installer.model_receipt({'data': [{**output['data'][0], **flag}]})


@pytest.mark.parametrize('flag', ['errorTrace', 'isTruncated'])
def test_incomplete_native_output_never_releases_bootstrap(flag):
    invalid = {**output_receipt(), flag: True}
    with pytest.raises(AidpProvisionError, match='incomplete'):
        installer.model_receipt(invalid)


def test_resume_freezes_owner_operation_settings_and_pack_before_mutations():
    api = client()
    saved = state()
    api._workspace = lambda: {'key': 'workspace'}
    api._read_installation = lambda workspace: copy.deepcopy(saved)
    api._step = lambda *args: pytest.fail('A mismatched operation must not mutate AIDP')
    for owner, operation, reuse in [('other', OP, True), ('owner', str(OP).replace('001', '002'), True), ('owner', OP, False)]:
        with pytest.raises(AidpProvisionConflict):
            api._advance(owner, operation, reuse, None, None)


def test_failed_phase_preserves_resources_and_hides_provider_secret():
    api = client()
    saved = state('credentials', existing='keep-this-resource')
    api._workspace = lambda: {'key': 'workspace'}
    api._read_installation = lambda workspace: saved
    def fail(*args):
        raise RuntimeError('synthetic-sensitive-provider-message')
    api._step = fail
    result = api._advance('owner', OP, True, None, None)
    assert result['status'] == 'error' and result['phase'] == 'credentials'
    assert saved['resources'] == {'existing': 'keep-this-resource'}
    assert 'synthetic-sensitive' not in json.dumps(result)
    assert 'synthetic-sensitive' not in json.dumps(saved)
    assert saved['native_error']['exception_type'] == 'RuntimeError'
    assert saved['native_error']['frames'][-1]['function'] == 'fail'
    assert isinstance(saved['native_error']['frames'][-1]['line'], int)
    assert 'native_error' not in result


def test_native_request_failure_records_transport_metadata_without_provider_content():
    transport = bare_client()
    transport.session = SimpleNamespace(request=lambda *a, **k: FakeResponse(
        {'code': 'BadRequest', 'message': 'synthetic-sensitive-provider-message'},
        status_code=400, headers={'opc-request-id': 'request/ref-1'}))
    api = client()
    saved = state('model_prepare', existing='keep-this-resource')
    api._workspace = lambda: {'key': 'workspace'}
    api._read_installation = lambda workspace: saved
    api._step = lambda *a: transport._request(
        'POST', '/workspaces/workspace/notebook/api/contents/parent?token=synthetic-sensitive-query',
        payload={'type': 'notebook', 'token': 'synthetic-sensitive-request'})
    result = api._advance('owner', OP, True, None, None)
    assert result['status'] == 'error' and result['phase'] == 'model_prepare'
    assert 'HTTP 400' in result['message']
    assert saved['resources'] == {'existing': 'keep-this-resource'}
    assert saved['native_error'] == {
        'method': 'POST', 'phase': 'content', 'status_code': 400,
        'endpoint': '/workspaces/{workspace}/notebook/api/contents/parent',
        'request_id': 'request/ref-1'}
    assert 'synthetic-sensitive' not in json.dumps(saved)
    assert 'native_error' not in result
    api._step = lambda *a: None
    assert api._advance('owner', OP, True, None, None)['phase'] == 'model_run'
    assert 'native_error' not in saved


def test_uncertain_secret_creation_never_repeats_a_write(monkeypatch):
    api = client()
    api._shared_oci_credential = lambda: {'key': 'shared-oci'}
    monkeypatch.setattr(installer, '_project_credential', lambda api: None)
    writes = []
    def create(*a, **k):
        writes.append(True)
        raise TimeoutError('Unknown create outcome')
    monkeypatch.setattr(installer, 'ensure_apify_credential', create)
    saved = state('credentials')
    with pytest.raises(TimeoutError):
        api._step(saved, SecretStr('synthetic-test-secret'), None)
    with pytest.raises(AidpProvisionConflict, match='another secret will not'):
        api._step(saved, SecretStr('synthetic-test-secret'), None)
    assert writes == [True]


@pytest.mark.parametrize('same_operation,create_requested', [(True, True), (False, True), (True, False)])
def test_uncertain_created_credential_is_reconciled_only_by_its_recorded_operation(monkeypatch, same_operation, create_requested):
    api = client()
    api._shared_oci_credential = lambda: {'key': 'shared-oci'}
    saved = state('credentials', apify_create_requested=create_requested)
    saved['reuse_existing'] = False
    operation = OP if same_operation else '00000000-0000-4000-8000-000000000002'
    existing = {'key': 'new-project-key', 'type': 'SECRET_TOKEN', 'lifecycleState': 'ACTIVE',
                'description': 'AI Market Intelligence Apify connection; operation=' + operation}
    monkeypatch.setattr(installer, '_project_credential', lambda api: existing)
    monkeypatch.setattr(installer, 'ensure_apify_credential', lambda *a, **k: pytest.fail('Must not create or rotate a secret'))
    api._workspace = lambda: {'key': 'workspace'}
    api._read_installation = lambda workspace: saved
    observed = asyncio.run(api.status())
    assert observed['apify_credential_owned_by_operation'] is (same_operation and create_requested)
    if same_operation and create_requested:
        api._step(saved, None, None)
        assert saved['resources']['apify_credential_key'] == 'new-project-key'
        assert saved['reuse_existing'] is False
    else:
        with pytest.raises(AidpProvisionConflict, match='Enter an Apify token'):
            api._step(saved, None, None)
        assert 'apify_credential_key' not in saved['resources']


def test_new_bearer_preserves_only_its_four_character_hint_in_the_receipt(monkeypatch):
    api = client()
    saved = state('credentials')
    writes = []
    api._save = lambda item: writes.append(copy.deepcopy(item))
    api._shared_oci_credential = lambda: {'key': 'oci'}
    monkeypatch.setattr(installer, '_project_credential', lambda api: None)
    monkeypatch.setattr(installer, 'ensure_apify_credential', lambda *a, **k: {'credential_key': 'project'})
    token = SecretStr('apify_api_synthetic_private_value7Q2k')
    api._step(saved, token, None)
    result = installer.public_status(saved)
    assert saved['resources']['apify_token_suffix'] == result['apify_token_suffix'] == '7Q2k'
    assert token.get_secret_value() not in json.dumps([writes, saved, result])
    assert writes[0]['resources']['apify_create_requested'] is True


def test_reusing_another_credential_discards_a_failed_creation_token_hint(monkeypatch):
    api = client()
    saved = state('credentials', apify_create_requested=True, apify_token_suffix='7Q2k')
    api._shared_oci_credential = lambda: {'key': 'oci'}
    existing = {'key': 'another-project-credential', 'type': 'SECRET_TOKEN', 'lifecycleState': 'ACTIVE',
                'description': 'AI Market Intelligence Apify connection; operation=another-operation'}
    monkeypatch.setattr(installer, '_project_credential', lambda api: existing)
    monkeypatch.setattr(installer, 'ensure_apify_credential', lambda *a, **k: pytest.fail('Must not create or rotate a secret'))
    api._step(saved, None, None)
    assert saved['resources']['apify_credential_key'] == 'another-project-credential'
    assert 'apify_token_suffix' not in saved['resources']
    assert installer.public_status(saved)['apify_token_suffix'] == ''


@pytest.mark.parametrize('suffix', ['', None, 1234, 'long-secret-value', 'a b!'])
def test_unvalidated_or_missing_bearer_hints_are_not_exposed(suffix):
    result = installer.public_status(state('model_verify', apify_credential_key='project', apify_token_suffix=suffix))
    assert result['apify_token_suffix'] == ''


def test_uncertain_bootstrap_start_is_not_repeated():
    api = client()
    saved = state('model_run', model_job_key='job')
    starts = []
    api._list = lambda *a, **k: []
    def request(method, path, **kwargs):
        if method == 'POST':
            starts.append(path)
            raise TimeoutError('Unknown native outcome')
        return {'name': 'wf_ai_mi_setup_' + OP.replace('-', ''), 'path': installer.ROOT + '/.setup_' + OP.replace('-', '')}
    api._request = request
    with pytest.raises(TimeoutError):
        api._model_step(saved)
    with pytest.raises(AidpProvisionPending, match='another run will not'):
        api._model_step(saved)
    assert len(starts) == 1


def test_model_job_binding_is_verified_before_overwriting_definition():
    api = client()
    api._ensure_folder = api._upload_file = lambda *a, **k: None
    api._request = lambda *a, **k: {'name': 'foreign-job', 'path': '/Workspace/shared'}
    api._publish_job = lambda *a: pytest.fail('Foreign job must not be overwritten')
    with pytest.raises(AidpProvisionConflict, match='ownership'):
        api._model_step(state('model_prepare', model_job_key='foreign', spark_key='spark'))


def test_model_preparation_uses_workspace_python_without_notebook_api():
    api = client()
    writes, published = [], []
    api._ensure_folder = lambda *a: None
    api._upload_notebook = lambda *a, **k: pytest.fail('The rejected notebook route must not be used')
    api._upload_file = lambda workspace, path, content: writes.append((workspace, path, content))
    api._job_key = lambda *a: None
    api._create_job = lambda workspace, payload: 'model-job'
    api._publish_job = lambda workspace, key, payload, compute: published.append(payload)
    saved = state('model_prepare', spark_key='spark')
    api._model_step(saved)
    path = installer.ROOT + '/.setup_' + OP.replace('-', '') + '/model.py'
    assert writes[0][:2] == ('workspace', path)
    assert b'SparkSession.builder.getOrCreate()' in writes[0][2]
    assert b'DROP TABLE' not in writes[0][2] and b'INSERT ' not in writes[0][2]
    task = published[0]['tasks'][0]
    assert task['type'] == 'PYTHON_TASK' and task['source'] == 'WORKSPACE'
    assert task['filePath'] == path and json.loads(task['commandLineArguments']) == [path]
    assert 'notebookPath' not in task and task['maxRetries'] == 0
    assert saved['resources']['model_job_key'] == 'model-job'


def test_cleanup_after_job_deletion_is_resumable_and_exactly_scoped():
    api = client()
    api._request = lambda *a, **k: None
    removed = []
    api._delete_workspace_path = lambda workspace, path, message: removed.append((workspace, path))
    api._model_step(state('cleanup', model_job_key='job', model_verified={'verified': True}))
    assert removed == [('workspace', installer.ROOT + '/.setup_' + OP.replace('-', ''))]
    with pytest.raises(AidpProvisionError, match='verified'):
        api._model_step(state('cleanup', model_job_key='job'))


@pytest.mark.parametrize('output_key,task_owner', [('native-output', 'run'), ('', 'run'), ('native-output', 'foreign')])
def test_model_verification_fetches_only_the_owned_task_output(output_key, task_owner):
    api = client()
    posted = []
    def request(method, path, **kwargs):
        if method == 'POST':
            posted.append(kwargs['payload'])
            return output_receipt()
        if path.endswith('/jobs/job'):
            return {'name': 'wf_ai_mi_setup_' + OP.replace('-', ''),
                    'path': installer.ROOT + '/.setup_' + OP.replace('-', '')}
        if path.endswith('/jobRuns/run'):
            return {'jobKey': 'job', 'state': 'SUCCESS', 'taskToTaskRunMap': {'create_market_model': 'task'}}
        assert path.endswith('/taskRuns/task')
        return {'outputKey': output_key, 'jobRunKey': task_owner}
    api._request = request
    saved = state('model_verify', model_job_key='job', model_run_key='run')
    if task_owner != 'run':
        with pytest.raises(AidpProvisionError, match='ownership'):
            api._model_step(saved)
    elif not output_key:
        with pytest.raises(AidpProvisionPending, match='output key'):
            api._model_step(saved)
    else:
        api._model_step(saved)
        assert posted == [{'outputKey': 'native-output'}]
        assert saved['resources']['model_verified']['documented_columns'] == 145
    if not output_key or task_owner != 'run':
        assert not posted and 'model_verified' not in saved['resources']


def test_status_refresh_reads_current_test_sessions_without_saving(monkeypatch):
    api = client()
    api._workspace = lambda: {'key': 'workspace'}
    api._read_installation = lambda workspace: state('test', agent_key='agent')
    api._save = lambda *a: pytest.fail('GET status must not write')
    monkeypatch.setattr(installer, '_project_credential', lambda api: None)
    monkeypatch.setattr(installer, 'MarketPlaygroundClient', lambda settings: SimpleNamespace(
        _list=lambda *a, **k: [{'key': OP, 'displayName': 'New TEST session'}], session=SimpleNamespace(close=lambda: None)))
    observed = asyncio.run(api.status())
    assert observed['test_sessions'] == [{'value': OP, 'label': 'New TEST session'}]
    assert not observed['installed']


@pytest.mark.parametrize('change,expected_key,expected_status', [
    ({}, '', 'configured'), ({}, 'project', 'configured'), ({}, 'other-key', 'conflict'),
    ({'type': 'OAUTH'}, '', 'conflict'), ({'lifecycleState': 'INACTIVE'}, '', 'conflict'),
    ({'description': 'Another module'}, '', 'conflict'), ({'key': ''}, '', 'conflict'),
    (None, '', 'missing'), (None, 'project', 'conflict'),
])
def test_credential_status_matches_installation_metadata_guards_without_exposing_secrets(monkeypatch, change, expected_key, expected_status):
    api = client()
    api._workspace = lambda: {'key': 'workspace'}
    api._read_installation = lambda workspace: state('credentials', **({'apify_credential_key': expected_key} if expected_key else {}))
    api._save = lambda *a: pytest.fail('Status must not write')
    existing = None if change is None else {'key': 'project', 'type': 'SECRET_TOKEN', 'lifecycleState': 'ACTIVE',
        'description': 'AI Market Intelligence Apify connection', 'secretValue': 'synthetic-private-value', **change}
    monkeypatch.setattr(installer, '_project_credential', lambda api: existing)
    observed = asyncio.run(api.status())
    assert observed['apify_credential_status'] == expected_status
    assert observed['apify_credential_available'] is (expected_status == 'configured')
    assert 'synthetic-private-value' not in json.dumps(observed)
    assert 'apify_credential_key' not in observed


@pytest.mark.parametrize('error,expected', [(RuntimeError('Duplicate credentials'), 'conflict'),
    (AidpProvisionError('Synthetic provider failure'), 'unknown'), (AidpProvisionPending('Pending'), 'unknown')])
def test_credential_lookup_failure_preserves_operation_without_claiming_a_configured_token(monkeypatch, error, expected):
    api = client()
    api._workspace = lambda: {'key': 'workspace'}
    api._read_installation = lambda workspace: state('model_verify', apify_credential_key='project', apify_token_suffix='7Q2k')
    api._save = lambda *a: pytest.fail('Status must not write')
    def fail(api):
        raise error
    monkeypatch.setattr(installer, '_project_credential', fail)
    observed = asyncio.run(api.status())
    assert observed['apify_credential_status'] == expected and not observed['apify_credential_available']
    assert 'apify_token_suffix' not in observed
    assert observed['operation_id'] == OP and observed['phase'] == 'model_verify'
    assert str(error) not in json.dumps(observed)


def test_uncertain_test_submission_never_redeploys_automatically(monkeypatch):
    api = client()
    api._runtime_config = lambda saved: {}
    monkeypatch.setattr(installer, 'agent_package', lambda config: (b'source', {'runtime': b'candidate'}))
    submissions = []
    def refresh(*a, on_submit, **k):
        on_submit({'package_sha256': 'candidate'})
        submissions.append('TEST')
        raise TimeoutError('No acknowledgement')
    fake = SimpleNamespace(refresh=refresh, test_deployments=lambda *a: [], session=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(installer, 'MarketPlaygroundClient', lambda settings: fake)
    saved = state('test', agent_key='agent', agent_compute_key='compute')
    with pytest.raises(TimeoutError):
        api._test_step(saved, None)
    with pytest.raises(AidpProvisionConflict, match='not be submitted automatically'):
        api._test_step(saved, None)
    assert submissions == ['TEST']


def test_failed_test_prevalidation_can_be_retried_without_persisted_submission(monkeypatch):
    api = client()
    api._runtime_config = lambda saved: {}
    monkeypatch.setattr(installer, 'agent_package', lambda config: (b'source', {'runtime': b'candidate'}))
    def refresh(*a, **k):
        raise ValueError('Saved package mismatch before on_submit')
    monkeypatch.setattr(installer, 'MarketPlaygroundClient', lambda settings: SimpleNamespace(
        refresh=refresh, session=SimpleNamespace(close=lambda: None)))
    saved = state('test', agent_key='agent', agent_compute_key='compute')
    for _ in range(2):
        with pytest.raises(ValueError):
            api._test_step(saved, None)
    assert 'test_intent' not in saved['resources']


def event(text, status='IN_PROGRESS'):
    return b'data: ' + json.dumps({'response': {'output': [{'status': status,
        'content': [{'type': 'output_text', 'text': text}]}]}}).encode()


def test_result(**updates):
    return json.dumps({'mode': 'configured_paused', 'generation_id': OP, 'mcp_verified': True,
                      'research_enabled': False, 'apify_starts': 0, 'model_calls': 0,
                      'streaming': 'graph_milestones', **updates})
test_result.__test__ = False


@pytest.mark.parametrize('fault', ['', 'error_after_result', 'wrong_revision', 'no_mcp', 'not_completed', 'progress_completed', 'failed_response'])
def test_sse_accepts_only_completed_current_revision_with_live_mcp(fault):
    api = client()
    api.signer = object()
    updates = {'generation_id': 'old'} if fault == 'wrong_revision' else {'mcp_verified': False} if fault == 'no_mcp' else {}
    lines = [event('Revisando la solicitud.', 'COMPLETED' if fault == 'progress_completed' else 'IN_PROGRESS'),
             event(test_result(**updates), 'IN_PROGRESS' if fault in {'not_completed', 'progress_completed'} else 'COMPLETED')]
    if fault == 'error_after_result':
        lines.append(b'data: {"type": "error", "error": "failed"}')
    if fault == 'failed_response':
        lines.append(b'data: {"response": {"status": "FAILED"}}')
    closed, requests = [], []
    response = SimpleNamespace(raise_for_status=lambda: None, headers={'Content-Type': 'text/event-stream'},
        iter_lines=lambda: iter(lines), close=lambda: closed.append(True))
    def post(*args, **kwargs):
        requests.append(kwargs)
        return response
    api.session = SimpleNamespace(post=post)
    if fault:
        with pytest.raises(AidpProvisionError):
            api._probe_test(ENDPOINT, OP, OP)
    else:
        assert api._probe_test(ENDPOINT, OP, OP)['streaming_verified'] is True
    assert closed == [True]
    assert requests[0]['headers']['x-deployment-type'] == 'TEST'
    assert requests[0]['allow_redirects'] is False


@pytest.mark.parametrize('endpoint', ['http://gateway.aidp.us-chicago-1.oci.oraclecloud.com/agent', ENDPOINT + '?token=x', ENDPOINT.replace('.com/', '.com:443/'), 'https://outside.test/agent'])
def test_test_endpoint_cannot_redirect_signed_requests(endpoint):
    api = client()
    api.session = SimpleNamespace(post=lambda *a, **k: pytest.fail('Invalid destination must not receive a request'))
    with pytest.raises(AidpProvisionError, match='Unexpected'):
        api._probe_test(endpoint, OP, OP)


def test_admin_routes_are_observation_only_until_explicit_install(tmp_path):
    portal = make_client(tmp_path)
    calls = []
    class Market:
        async def status(self):
            return installer.public_status()
        async def operate(self, *args, **kwargs):
            calls.append((args, kwargs))
            return installer.public_status(state())
    portal.app.state.market_factory = Market
    assert portal.get('/api/admin/modules/ai_market_intelligence').status_code == 401
    login(portal)
    assert portal.get('/api/admin/modules/ai_market_intelligence').status_code == 200
    assert calls == []
    path = '/api/admin/users/user-id/modules/ai_market_intelligence'
    invalid = portal.post(path, json={'operation_id': OP, 'apify_token': {'secret': 'must-not-echo'}})
    assert invalid.status_code == 400 and 'must-not-echo' not in invalid.text
    assert portal.post(path, json={'operation_id': OP, 'agent_hub_url': 'https://outside.test'}).status_code == 400
    assert portal.post(path, json={'operation_id': OP, 'reuse_existing': True}).status_code == 202
    assert calls[0][0] == ('ocid1.user.oc1..ada', OP)
    assert calls[0][1]['reuse_existing'] is True
    unauthorized = make_client(tmp_path / 'other', mode='not-admin')
    unauthorized.app.state.market_factory = Market
    login(unauthorized)
    assert unauthorized.post(path, json={'operation_id': OP}).status_code == 403
    assert len(calls) == 1


def test_registry_is_not_an_installation_phase():
    assert 'registry' not in installer.PHASES
    assert installer.PHASES[-2:] == ('production', 'ready')


@pytest.mark.parametrize('lookup', ['match', 'missing', 'foreign', 'pending', 'timeout', 'http_error'])
def test_status_resolves_saved_administrator_without_exposing_native_identity_or_mutating(tmp_path, lookup):
    from app.identity import IdentityPending
    from oci._vendor.requests.exceptions import Timeout, HTTPError
    portal = make_client(tmp_path)
    calls = []
    class Market:
        async def status(self):
            return {**installer.public_status(state('model_verify')), 'administrator_ocid': 'ocid1.user.oc1..ada'}
        async def operate(self, *args, **kwargs):
            pytest.fail('GET must not resume the installation')
    class Identity:
        async def list_users_by_ocids(self, owners):
            calls.append(owners)
            if lookup == 'pending':
                raise IdentityPending('unavailable')
            if lookup == 'timeout':
                raise Timeout('unavailable')
            if lookup == 'http_error':
                raise HTTPError('unavailable')
            if lookup == 'missing':
                return []
            return [{'id': 'user-id', 'ocid': 'ocid1.user.oc1..ada' if lookup == 'match' else 'foreign'}]
    portal.app.state.market_factory = Market
    portal.app.state.identity_factory = Identity
    login(portal)
    response = portal.get('/api/admin/modules/ai_market_intelligence')
    assert response.status_code == 200
    result = response.json()
    assert result.get('administrator_id') == ('user-id' if lookup == 'match' else None)
    assert 'administrator_ocid' not in result
    assert result['phase'] == 'model_verify'
    assert calls == [{'ocid1.user.oc1..ada'}]
