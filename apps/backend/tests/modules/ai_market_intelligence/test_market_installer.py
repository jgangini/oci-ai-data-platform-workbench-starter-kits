"""Current TEST-installer guards; native side effects are recorded, never executed."""

import asyncio

import copy

import json

from types import SimpleNamespace

from pydantic import SecretStr

import pytest

from app.services.oci.aidp import AidpProvisionConflict, AidpProvisionError, AidpProvisionPending

from app.modules.ai_market_intelligence import client as installer

from tests.api.test_api import make_client, login

from tests.services.oci.test_aidp import bare_client, FakeResponse

OP = '00000000-0000-4000-8000-000000000001'

ENDPOINT = 'https://gateway.aidp.us-chicago-1.oci.oraclecloud.com/agent'

def state(phase='verify', **resources):
    return {'installer_version': 2, 'publication_root': installer.ROOT, 'module_id': installer.MODULE_ID, 'workspace': 'workspace',
            'operation_id': OP, 'owner': 'owner', 'reuse_existing': True, 'phase': phase,
            'status': 'installing', 'resources': resources,
            'pack_sha256': installer.load_package(installer.MODULE_ID, require_available=False).pack_sha256}

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
        assert kwargs['headers']['path'] == installer.CONTROL_ROOT + '/installer.json'
        writes.append(kwargs['data'])
        stored['body'] = json.loads(kwargs['data'])
    api._request = request
    saved = state()
    for _ in range(2):
        installer.MarketAidpClient._save(api, saved)
        assert api._read_installation('workspace') == saved
    assert writes and all(api._content_matches(json.loads(body), body) for body in writes)

def test_resume_freezes_owner_operation_settings_and_pack_before_mutations():
    api = client()
    saved = state()
    api._workspace = lambda: {'key': 'workspace'}
    api._read_installation = lambda workspace: copy.deepcopy(saved)
    api._step = lambda *args: pytest.fail('A mismatched operation must not mutate AIDP')
    for owner, operation, reuse in [('other', OP, True), ('owner', str(OP).replace('001', '002'), True), ('owner', OP, False)]:
        with pytest.raises(AidpProvisionConflict):
            api._advance(owner, operation, reuse, None, None)
    saved['pack_sha256'] = '0' * 64
    api._save = lambda *args: pytest.fail('A changed package must be rejected before writes')
    with pytest.raises(AidpProvisionConflict, match='exact saved package'):
        api._advance('owner', OP, True, None, None)

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
    saved = state('files', existing='keep-this-resource')
    api._workspace = lambda: {'key': 'workspace'}
    api._read_installation = lambda workspace: saved
    api._step = lambda *a: transport._request(
        'POST', '/workspaces/workspace/notebook/api/contents/parent?token=synthetic-sensitive-query',
        payload={'type': 'notebook', 'token': 'synthetic-sensitive-request'})
    result = api._advance('owner', OP, True, None, None)
    assert result['status'] == 'error' and result['phase'] == 'files'
    assert 'HTTP 400' in result['message']
    assert saved['resources'] == {'existing': 'keep-this-resource'}
    assert saved['native_error'] == {
        'method': 'POST', 'phase': 'content', 'status_code': 400,
        'endpoint': '/workspaces/{workspace}/notebook/api/contents/parent',
        'request_id': 'request/ref-1'}
    assert 'synthetic-sensitive' not in json.dumps(saved)
    assert 'native_error' not in result
    api._step = lambda *a: None
    assert api._advance('owner', OP, True, None, None)['phase'] == 'agent'
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
    result = installer.public_status(state('test', apify_credential_key='project', apify_token_suffix=suffix))
    assert result['apify_token_suffix'] == ''

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
    api._read_installation = lambda workspace: state('test', apify_credential_key='project', apify_token_suffix='7Q2k')
    api._save = lambda *a: pytest.fail('Status must not write')
    def fail(api):
        raise error
    monkeypatch.setattr(installer, '_project_credential', fail)
    observed = asyncio.run(api.status())
    assert observed['apify_credential_status'] == expected and not observed['apify_credential_available']
    assert 'apify_token_suffix' not in observed
    assert observed['operation_id'] == OP and observed['phase'] == 'test'
    assert str(error) not in json.dumps(observed)

def test_registry_is_not_an_installation_phase():
    assert 'registry' not in installer.PHASES
    assert installer.PHASES == (
        'verify', 'credentials', 'compute', 'files', 'agent', 'permissions', 'test', 'ready')

@pytest.mark.parametrize('lookup', ['match', 'missing', 'foreign', 'pending', 'timeout', 'http_error'])
def test_status_resolves_saved_administrator_without_exposing_native_identity_or_mutating(tmp_path, lookup):
    from app.services.oci.identity import IdentityPending
    from oci._vendor.requests.exceptions import Timeout, HTTPError
    portal = make_client(tmp_path)
    calls = []
    class Market:
        async def status(self):
            return {**installer.public_status(state('test')), 'administrator_ocid': 'ocid1.user.oc1..ada'}
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
    assert result['phase'] == 'test'
    assert calls == [{'ocid1.user.oc1..ada'}]
