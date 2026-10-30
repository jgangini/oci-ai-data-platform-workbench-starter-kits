import ast
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.aidp import AidpClient, AidpProvisionConflict, AidpProvisionError, AidpProvisionPending
from app.config import Settings
from app.social_network import SocialAidpClient
from app import sonda_install as install


def test_backend_import_does_not_require_native_agent_libraries():
    backend=Path(__file__).resolve().parents[1]
    code="import sys; sys.modules['langgraph']=None; import app.main; print('backend import ready')"
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,
        env={**os.environ,'PYTHONPATH':str(backend)},timeout=30)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='backend import ready'


REGISTRY = '00000000-0000-4000-8000-000000000001'
GENERATION = '00000000-0000-4000-8000-000000000002'
OPERATION = '00000000-0000-4000-8000-000000000003'


@pytest.fixture
def binding():
    return {'namespace': 'namespace', 'source_bucket': 'landing', 'source_prefix': 'aidp-social/',
        'gold_bucket': 'gold', 'artifacts_bucket': 'artifacts', 'catalog': 'oci_medallion',
        'gold_schema': 'oci_gold', 'control_schema': 'oci_artifacts', 'credential_name': 'AidpRuntime',
        'identity_sha256': 'a' * 64, 'registry_id': REGISTRY, 'generation_id': GENERATION,
        'contract_sha256': install.CONTRACT_SHA256, 'bootstrap_config_revision': 2,
        'model_id': 'selected', 'model_revision': '1.0.0', 'prompt_version': install.STUDY_PROMPT_VERSION,
        'region': 'us-chicago-1', 'compartment_id': 'compartment'}


@pytest.fixture
def client(binding):
    value = object.__new__(install.SondaAidpClient)
    value.settings = Settings(objectstorage_namespace='namespace', artifacts_bucket_name='artifacts',
        agent_model_id='selected', aidp_region='us-chicago-1', compartment_id='compartment')
    value._social_config = dict(binding)
    value._configured_sonda_binding = dict(binding)
    value._write_module_manifest = Mock()
    value._oci_config = {}
    value.signer = object()
    value._agents=Mock(return_value=[])
    value._ensure_activity_session=Mock(return_value=False)
    return value


def fence(binding, **changes):
    return {**binding, 'enabled': False, 'bootstrap': True, 'integrated_acceptance': False,
        'reset_verified': True, **changes}


def manifest(binding, **changes):
    return {'schema_version': 1, 'module_id': 'ai_social_network', 'status': 'installing',
        'phase': 'sync', 'config': copy.deepcopy(binding), 'enabled': False,
        'operation': {'operation_id': OPERATION, 'type': 'install'},
        'resources': {'gold_query_compute_id': 'spark', 'workflow_key': 'job',
            'sync_requested_at': '2026-10-29T18:00:00Z'}, **changes}


def storage(objects):
    def read(_namespace, _bucket, key):
        body = SimpleNamespace(raw=io.BytesIO(objects[key]), close=Mock())
        return SimpleNamespace(data=body)
    return SimpleNamespace(get_object=Mock(side_effect=read), list_objects=Mock(return_value=SimpleNamespace(
        data=SimpleNamespace(objects=[SimpleNamespace(name=key) for key in objects], next_start_with=None))))


def encoded(value):
    return json.dumps(value).encode()


def test_binding_discovery_is_exact_and_never_writes(client, binding):
    key = install.operational_prefix(binding) + 'binding.json'
    client.object_storage = storage({key: encoded(fence(binding)), 'ai_social_network/registries/readme.json': b'not read'})
    assert client._discover_binding(binding)['generation_id'] == GENERATION
    assert client.object_storage.get_object.call_args.args == ('namespace', 'artifacts', key)
    assert client.object_storage.get_object.call_count == 1


@pytest.mark.parametrize('change', [
    {'reset_verified': False}, {'enabled': 0}, {'contract_sha256': 'b' * 64},
    {'bootstrap_config_revision': None}, {'bootstrap_config_revision': True},
    {'model_revision': 'unverified'}, {'credential_name': 'OtherCredential'},
    {'generation_id': '00000000-0000-4000-8000-000000000009'},
])
def test_binding_discovery_rejects_invalid_protected_declaration(client, binding, change):
    client.object_storage = storage({install.operational_prefix(binding) + 'binding.json': encoded(fence(binding, **change))})
    with pytest.raises(AidpProvisionConflict):
        client._discover_binding(binding)


@pytest.mark.parametrize('change', [{'source_bucket': 'other'}, {'identity_sha256': 'b' * 64}])
def test_unrelated_storage_or_identity_cannot_be_adopted(client, binding, change):
    client.object_storage = storage({install.operational_prefix(binding) + 'binding.json': encoded(fence(binding, **change))})
    with pytest.raises(AidpProvisionConflict, match='Exactly one'):
        client._discover_binding(binding)


def test_duplicate_binding_is_ambiguous_even_if_one_has_old_pin(client, binding):
    second = {**binding, 'generation_id': '00000000-0000-4000-8000-000000000009'}
    client.object_storage = storage({install.operational_prefix(binding) + 'binding.json': encoded(fence(binding)),
        install.operational_prefix(second) + 'binding.json': encoded(fence(second, contract_sha256='b' * 64))})
    with pytest.raises(AidpProvisionConflict, match='Exactly one'):
        client._discover_binding(binding)


@pytest.mark.parametrize('raw', [b'{"enabled":false,"enabled":true}', b'[]', b'\xff'])
def test_control_json_rejects_ambiguous_or_invalid_json(client, raw):
    client.object_storage = storage({'control': raw})
    with pytest.raises(AidpProvisionConflict):
        client._control_json('control')


def test_control_json_has_bounded_read_and_closes_response(client):
    stream = io.BytesIO(b'x' * 20)
    close = Mock()
    client.object_storage = SimpleNamespace(get_object=Mock(return_value=SimpleNamespace(data=SimpleNamespace(raw=stream, close=close))))
    with pytest.raises(AidpProvisionError, match='limit'):
        client._control_json('control', maximum=10)
    assert stream.tell() == 11
    close.assert_called_once()


def model_client(client, *, error=None, models=None, **changes):
    model = SimpleNamespace(id='selected', version='1.0.0', lifecycle_state='ACTIVE', capabilities=['CHAT'], **{})
    for key, value in changes.items():
        setattr(model, key, value)
    native = SimpleNamespace(get_model=Mock(return_value=SimpleNamespace(data=model), side_effect=error),
        list_models=Mock(return_value=SimpleNamespace(data=models if models is not None else [model], headers={})),
        base_client=SimpleNamespace(close=Mock()))
    factory = Mock(return_value=native)
    client._oci = SimpleNamespace(generative_ai=SimpleNamespace(GenerativeAiClient=factory))
    return native, factory


def service_error(status):
    value = RuntimeError('private service body must not escape')
    value.status = status
    return value


def test_model_revision_is_read_from_selected_server_model(client):
    native, factory = model_client(client)
    selected = client._resolve_model()
    assert selected['model_revision'] == '1.0.0' and selected['prompt_version'] == install.STUDY_PROMPT_VERSION
    native.get_model.assert_called_once_with('selected')
    native.list_models.assert_not_called()
    assert factory.call_args.args[0]['region'] == 'us-chicago-1'
    native.base_client.close.assert_called_once()


def test_base_model_falls_back_to_exact_official_catalog_id(client):
    native, _ = model_client(client, error=service_error(404))
    assert client._resolve_model()['model_revision'] == '1.0.0'
    native.list_models.assert_called_once_with(compartment_id='compartment')


def test_model_catalog_fallback_supports_collection_pagination(client):
    native, _ = model_client(client, error=service_error(404))
    exact = SimpleNamespace(id='selected', version='actual.2', lifecycle_state='ACTIVE', capabilities=['CHAT'])
    native.list_models.side_effect = [SimpleNamespace(data=SimpleNamespace(items=[]), headers={'opc-next-page': 'next'}),
        SimpleNamespace(data=SimpleNamespace(items=[exact]), headers={})]
    assert client._resolve_model()['model_revision'] == 'actual.2'
    assert native.list_models.call_args.kwargs['page'] == 'next'


def test_auth_error_does_not_trigger_catalog_discovery_or_leak_body(client):
    native, _ = model_client(client, error=service_error(403))
    with pytest.raises(AidpProvisionError) as raised:
        client._resolve_model()
    assert 'private service' not in str(raised.value)
    native.list_models.assert_not_called()


@pytest.mark.parametrize('changes', [{'version': None}, {'version': ' '}, {'lifecycle_state': 'CREATING'}, {'capabilities': ['EMBED']}, {'id': 'other'}])
def test_unverified_or_nonchat_model_is_rejected(client, changes):
    model_client(client, **changes)
    with pytest.raises(AidpProvisionConflict):
        client._resolve_model()


def test_duplicate_exact_model_entries_are_not_arbitrarily_selected(client):
    entry = SimpleNamespace(id='selected', version='1.0.0', lifecycle_state='ACTIVE', capabilities=['CHAT'])
    model_client(client, error=service_error(404), models=[entry, entry])
    with pytest.raises(AidpProvisionConflict, match='unique'):
        client._resolve_model()


def test_configuration_keeps_binding_server_derived_and_requires_real_model(client, binding, monkeypatch):
    legacy = {key: value for key, value in binding.items() if key in install.FIXED_FIELDS or key == 'credential_name'}
    monkeypatch.setattr(SocialAidpClient, '_configure_social', lambda self, bucket, prefix: setattr(self, '_social_config', dict(legacy)))
    client.source_config = Mock(return_value=legacy)
    client._discover_binding = Mock(return_value=fence(binding))
    client._resolve_model = Mock(return_value={key: binding[key] for key in ('model_id', 'model_revision', 'prompt_version', 'region', 'compartment_id')})
    client._configure_social('landing', 'aidp-social/')
    assert client._social_config == binding
    assert client._configured_sonda_binding == binding
    assert 'private_key' not in client._social_config


def test_established_generation_reads_one_exact_fence_without_inventory(client, binding, monkeypatch):
    monkeypatch.setattr(SocialAidpClient, '_configure_social', lambda self, bucket, prefix: None)
    client.source_config = Mock(return_value={key: binding[key] for key in (*install.FIXED_FIELDS, 'credential_name')})
    client.object_storage = storage({install.operational_prefix(binding) + 'binding.json': encoded(fence(binding))})
    client._resolve_model = Mock(return_value={key: binding[key] for key in ('model_id', 'model_revision', 'prompt_version', 'region', 'compartment_id')})
    client._configure_social('landing', 'aidp-social/')
    client.object_storage.list_objects.assert_not_called()
    client.object_storage.get_object.assert_called_once()
    assert client._social_config == binding


def test_repair_manifest_retains_verified_config_and_existing_chat_identity(client, binding, monkeypatch):
    legacy = {key: value for key, value in binding.items() if key in install.FIXED_FIELDS or key == 'credential_name'}
    old = manifest(legacy, status='active', phase='active')
    old['resources']['agent_key'] = 'original-chat'
    client._workspace_json = Mock(return_value=old)
    client._governance_enabled = Mock(return_value=False)
    monkeypatch.setattr(install, 'load_lab_pack', lambda *args, **kwargs: SimpleNamespace(pack_version='1.2.0'))
    updated, complete = client._prepare_governance_reconciliation('workspace', '00000000-0000-4000-8000-000000000004', 'redeploy')
    assert not complete and updated['config'] == binding
    assert updated['resources']['agent_key'] == 'original-chat' and updated['phase'] == 'verify'


def test_install_cannot_silently_claim_active_legacy_is_sonda(client, binding):
    legacy = {key: value for key, value in binding.items() if key in install.FIXED_FIELDS or key == 'credential_name'}
    client._workspace_json = Mock(return_value=manifest(legacy, status='active'))
    with pytest.raises(AidpProvisionConflict, match='explicit Sonda redeploy'):
        client._prepare_governance_reconciliation('workspace', OPERATION, 'install')


def test_native_render_replaces_only_one_reviewed_slot(binding):
    assert b"'registry_id'" in install._render(b'WORKER_BINDING = {}\n', 'WORKER_BINDING', binding)
    for raw in (b'WORKER_BINDING={}', b'WORKER_BINDING = {}\nWORKER_BINDING = {}'):
        with pytest.raises(AidpProvisionError):
            install._render(raw, 'WORKER_BINDING', binding)


def test_bootstrap_job_is_serial_paused_and_invokes_runner(client, binding, monkeypatch):
    client._runtime_binding = Mock(return_value=binding)
    client._upload_notebook = Mock(return_value=False)
    monkeypatch.setattr(install, 'ingestion_source', lambda: b'import json\nRUNNER_BINDING = {}\n')
    job, changed = client._governance_job_payload('workspace', 'spark', paused=True)
    assert not changed and job['maxConcurrentRuns'] == 1 and job['queue'] == {'isEnabled': False}
    assert job['schedule'] == {'quartzCronExpression': '0 0/5 * * * ?', 'timezoneId': 'UTC', 'pauseStatus': 'PAUSED'}
    code = ''.join(client._upload_notebook.call_args.args[2]['cells'][0]['source'])
    assert 'run_sonda(RUNNER_BINDING)' in code and 'RUNNER_BINDING = {}' not in code
    assert job['tasks'][0]['notebookPath'].endswith('/sonda_ingest.ipynb')
    assert job['tasks'][0]['maxRetries'] == 0 and not job['tasks'][0]['isRetryOnTimeout']


def test_bootstrap_delegates_only_under_explicit_paused_fence(client, binding, monkeypatch):
    inherited = Mock()
    monkeypatch.setattr(SocialAidpClient, '_control_social', inherited)
    client._fence = Mock(return_value=fence(binding))
    state = manifest(binding)
    client._control_social('workspace', state, 'install')
    inherited.assert_called_once_with('workspace', state, 'install')
    for changes in ({'enabled': True}, {'bootstrap': False}):
        client._fence.return_value = fence(binding, **changes)
        with pytest.raises(AidpProvisionConflict):
            client._control_social('workspace', state, 'install')
    assert inherited.call_count == 1


def prepared_receipt(binding, revision=2):
    versions = {name: 0 for name in install.definitions(binding) if name not in install.EXCLUDED_VERSIONS}
    scope = {key: binding[key] for key in ('registry_id', 'generation_id')}
    publication = {**scope, 'config_revision': revision, 'versions_json': json.dumps(versions),
        'publication_id': install.fingerprint([scope, revision, versions])}
    return {**scope, 'contract_sha256': install.CONTRACT_SHA256, 'table_count': 28, 'status': 'prepared',
        'completed_at': '2026-10-29T18:01:00Z', 'publication': publication}


@pytest.mark.parametrize('revision', [2, 3])
def test_sync_needs_current_success_and_coherent_expected_configuration(client, binding, revision):
    state = manifest(binding)
    client._successful_governance_sync = Mock(return_value=True)
    receipt = prepared_receipt(binding, revision)
    client._control_json = Mock(return_value=receipt)
    client._sync_social('workspace', state, 'install')
    assert state['phase'] == 'metadata' and state['resources']['bootstrap_publication_id'] == receipt['publication']['publication_id']
    client._successful_governance_sync.assert_called_once_with('workspace', 'job', requested_at='2026-10-29T18:00:00Z')


@pytest.mark.parametrize('change', [
    {'completed_at': '2026-10-29T17:59:59Z'}, {'completed_at': '2026-10-29T18:01:00'},
    {'table_count': 25}, {'table_count': True}, {'status': 'active'},
    {'generation_id': '00000000-0000-4000-8000-000000000009'}, {'contract_sha256': 'b' * 64},
])
def test_old_or_wrong_generation_receipt_cannot_advance_bootstrap(client, binding, change):
    client._successful_governance_sync = Mock(return_value=True)
    client._control_json = Mock(return_value={**prepared_receipt(binding), **change})
    with pytest.raises(AidpProvisionPending):
        client._sync_social('workspace', manifest(binding), 'install')
    client._write_module_manifest.assert_not_called()


@pytest.mark.parametrize('change', [
    {'config_revision': 1}, {'publication_id': 'b' * 64}, {'versions_json': '{}'},
    {'versions_json': '{"ai_social_registry":0,"ai_social_registry":0}'},
    {'registry_id': '00000000-0000-4000-8000-000000000009'},
])
def test_invalid_or_before_target_publication_cannot_advance_sync(client, binding, change):
    receipt = prepared_receipt(binding)
    receipt['publication'].update(change)
    client._successful_governance_sync = Mock(return_value=True)
    client._control_json = Mock(return_value=receipt)
    with pytest.raises(AidpProvisionPending):
        client._sync_social('workspace', manifest(binding), 'install')
    client._write_module_manifest.assert_not_called()


def test_missing_receipt_or_unsuccessful_job_stays_pending(client, binding):
    client._successful_governance_sync = Mock(return_value=False)
    client._control_json = Mock()
    with pytest.raises(AidpProvisionPending):
        client._sync_social('workspace', manifest(binding), 'install')
    client._control_json.assert_not_called()
    client._successful_governance_sync.return_value = True
    client._control_json.return_value = None
    with pytest.raises(AidpProvisionPending):
        client._sync_social('workspace', manifest(binding), 'install')


def test_all_catalog_descriptions_use_authoritative_definitions(client, binding):
    definitions = install.definitions(binding)
    client._catalog = Mock(return_value={'key': 'catalog'})
    client._list = Mock(return_value=[{'key': 'gold', 'displayName': 'oci_gold'}, {'key': 'control', 'displayName': 'oci_artifacts'}])
    client._schema_tables = Mock(side_effect=lambda catalog, schema: [{'key': name, 'displayName': name}
        for name, value in definitions.items() if value['layer'] == ('gold' if schema == 'gold' else 'control')])
    client._describe_catalog_table = Mock(return_value=True)
    client._catalog_descriptions()
    assert client._describe_catalog_table.call_count == 28
    for call in client._describe_catalog_table.call_args_list:
        assert call.args[-1] == definitions[call.args[2]]['description']


def test_catalog_metadata_preserves_operator_description_and_checks_generation(client, binding):
    name, definition = next(iter(install.definitions(binding).items()))
    detail = {'key': 'table', 'displayName': name, 'catalogKey': 'catalog', 'schemaKey': 'schema',
        'tableType': 'EXTERNAL', 'location': definition['location'], 'description': 'Reviewed operator purpose',
        'externalTableDefinition': {'objectStorageLocationPath': definition['location'], 'externalTableDataFormat': 'DELTA'}}
    client._request = Mock(return_value=(detail, {}))
    assert client._describe_catalog_table('catalog', 'schema', name, 'table', 'Default purpose')
    assert client._request.call_count == 1
    client._request = Mock(return_value=({**detail, 'location': definition['location'] + '/wrong-generation'}, {}))
    with pytest.raises(AidpProvisionError):
        client._describe_catalog_table('catalog', 'schema', name, 'table', 'Default purpose')


def test_catalog_description_write_uses_etag(client, binding):
    name, definition = next(iter(install.definitions(binding).items()))
    detail = {'key': 'table', 'displayName': name, 'catalogKey': 'catalog', 'schemaKey': 'schema',
        'tableType': 'EXTERNAL', 'location': definition['location'], 'description': '',
        'externalTableDefinition': {'objectStorageLocationPath': definition['location'], 'externalTableDataFormat': 'DELTA'}}
    client._request = Mock(side_effect=[(detail, {'etag': 'v2'}), {}, {'description': definition['description']}])
    assert client._describe_catalog_table('catalog', 'schema', name, 'table', definition['description'])
    assert client._request.call_args_list[1].kwargs['headers'] == {'If-Match': 'v2'}


def test_activity_session_reference_is_optional_unlogged_without_a_stale_default(client):
    detail={'displayName':'agent','description':'Managed agent','entryFilePath':'/entry.py',
        'dependenciesFilePath':'/requirements.txt','computeKey':'compute',
        'sessionConfig':{'variables':{'existing':{'name':'existing','shouldLog':False},
            'sondaActivityRef':{'name':'sondaActivityRef','defaultValue':'old-reference','shouldLog':True}}}}
    desired={'name':'sondaActivityRef','description':'Opaque invocation admission reference',
        'isRequired':False,'isSystem':False,'shouldLog':False}
    updated=copy.deepcopy(detail)
    updated['sessionConfig']['variables']['sondaActivityRef']=desired
    client._request=Mock(side_effect=[detail,{},updated])
    assert install.SondaAidpClient._ensure_activity_session(client,'workspace','agent')
    payload=client._request.call_args_list[1].kwargs['payload']
    assert payload['sessionConfig']['variables']['sondaActivityRef']==desired
    assert payload['sessionConfig']['variables']['existing']==detail['sessionConfig']['variables']['existing']
    client._request=Mock(return_value=updated)
    assert not install.SondaAidpClient._ensure_activity_session(client,'workspace','agent')
    assert client._request.call_count==1


def test_activity_reference_not_visible_blocks_agent_readiness(client):
    detail={'displayName':'agent','description':'Managed agent','entryFilePath':'/entry.py',
        'dependenciesFilePath':'/requirements.txt','computeKey':'compute','sessionConfig':None}
    client._request=Mock(side_effect=[detail,{},detail])
    with pytest.raises(AidpProvisionPending):
        install.SondaAidpClient._ensure_activity_session(client,'workspace','agent')


def test_two_agents_keep_original_chat_identity_and_one_shared_compute(client, binding, monkeypatch):
    state = manifest(binding)
    state['resources']['agent_key'] = 'original-chat'
    client._ensure_agent_compute = Mock(return_value=({'key': 'ai-compute'}, False))
    client._ensure_workspace_layout = Mock()
    client._ensure_agent = Mock(side_effect=[('original-chat', False), ('private-worker', False),('private-worker',False)])
    client._ensure_global_agent_deployment = Mock(side_effect=[({'key': 'chat-deployment'}, False), ({'key': 'worker-deployment'}, False)])
    monkeypatch.setattr(install, 'chat_source', lambda: b'CHAT_BINDING = {}\n')
    monkeypatch.setattr(install, 'investigator_source', lambda: b'WORKER_BINDING = {}\n')
    assert not client._ensure_global_agent('workspace', state)
    client._ensure_agent_compute.assert_called_once_with('workspace')
    assert state['resources']['agent_key'] == 'original-chat'
    assert state['resources']['investigator']['agent_key'] == 'private-worker'
    assert client._agent_name == 'ai_social_network' and client._agent_entry_filename == 'social_agent.py'
    for call, name, filename in zip((client._ensure_agent.call_args_list[0],client._ensure_agent.call_args_list[-1]),
        ('ai_social_network', 'ai_social_network_investigator'), ('social_agent.py', 'sonda_worker.py')):
        assert call.args[1:3] == ('ai-compute', name) and call.kwargs['entry_filename'] == filename
        descriptor = json.loads(call.args[5])
        assert descriptor['entry_sha256'] == hashlib.sha256(call.args[4]).hexdigest()
        assert descriptor['contract_sha256'] == install.CONTRACT_SHA256
    assert client._ensure_agent.call_args_list[0].kwargs['expected_key'] == 'original-chat'
    private_source=ast.parse(client._ensure_agent.call_args_list[-1].args[4].decode()).body[0]
    assert ast.literal_eval(private_source.value)['activity_agent_id']=='private-worker'
    client._ensure_activity_session.assert_any_call('workspace','original-chat')
    client._ensure_activity_session.assert_any_call('workspace','private-worker')
    assert client._ensure_global_agent_deployment.call_args_list[1].kwargs['agent_name'] == 'ai_social_network_investigator'
    assert client._ensure_global_agent_deployment.call_args_list[0].args[3]['resources'] is state['resources']
    assert client._ensure_global_agent_deployment.call_args_list[1].args[3]['resources'] is state['resources']['investigator']


def test_deployment_pending_persists_updated_ledger_before_propagating(client, binding, monkeypatch):
    state = manifest(binding)
    client._ensure_agent_compute = Mock(return_value=({'key': 'ai-compute'}, False))
    client._ensure_workspace_layout = Mock()
    client._ensure_agent = Mock(return_value=('original-chat', False))
    persisted = []
    client._write_module_manifest = Mock(side_effect=lambda ws, value: persisted.append(copy.deepcopy(value)))
    def pending(ws, compute, agent, component, digest, **kwargs):
        component['resources']['deployment_redeploy_requested'] = 'request-revision'
        raise AidpProvisionPending('Waiting for native revision', 'agent')
    client._ensure_global_agent_deployment = Mock(side_effect=pending)
    monkeypatch.setattr(install, 'chat_source', lambda: b'CHAT_BINDING = {}\n')
    with pytest.raises(AidpProvisionPending):
        client._ensure_global_agent('workspace', state)
    assert persisted[-1]['resources']['deployment_redeploy_requested'] == 'request-revision'
    assert client._ensure_agent.call_count == 2


def test_first_private_deployment_during_module_upgrade_has_no_redeploy_baseline_deadlock(client,binding):
    state=manifest(binding,status='redeploying')
    digest=hashlib.sha256(b'reviewed-source').hexdigest()
    private={'agent_key':'private-worker','agent_source_hash':digest,'create_operation_id':OPERATION,
        'deployment_key':'fresh-deployment','deployment_redeploy_requested':OPERATION+':'+digest,
        'deployment_redeploy_baseline':'','deployment_redeploy_baseline_key':''}
    state['resources']['investigator']=private
    client._ensure_agent_compute=Mock(return_value=({'key':'compute'},False))
    client._ensure_workspace_layout=Mock()
    client._agent_artifacts=Mock(return_value=(b'reviewed-source',b'{}',digest))
    client._ensure_agent=Mock(side_effect=[('chat',False),('chat',False),('private-worker',False)])
    active={'key':'fresh-deployment','lifecycleState':'ACTIVE'}
    client._agent_deployments=Mock(return_value=[active])
    client._ensure_agent_deployment=Mock(return_value=(active,False))
    def deployment(*args,**kwargs):
        return AidpClient._ensure_global_agent_deployment(client,*args,**kwargs) if kwargs['agent_name']==install.INVESTIGATOR else (active,False)
    client._ensure_global_agent_deployment=deployment
    assert not client._ensure_global_agent('workspace',state)
    assert client._ensure_agent_deployment.call_args.kwargs['redeploy_revision']==''
    assert private['deployed_source_hash']==digest and 'deployment_redeploy_requested' not in private


def test_unrecorded_private_agent_is_not_silently_adopted(client,binding):
    state=manifest(binding)
    client._ensure_agent_compute=Mock(return_value=({'key':'compute'},False))
    client._ensure_workspace_layout=Mock()
    client._agent_artifacts=Mock(return_value=(b'source',b'{}','a'*64))
    client._ensure_agent=Mock(return_value=('chat',False))
    client._ensure_global_agent_deployment=Mock(return_value=({'key':'chat-deployment'},False))
    client._agents=Mock(return_value=[{'key':'unowned-investigator'}])
    with pytest.raises(AidpProvisionConflict,match='unrecorded private'):
        client._ensure_global_agent('workspace',state)
    assert client._ensure_agent.call_count==2
    assert all(call.args[2]=='ai_social_network' for call in client._ensure_agent.call_args_list)


@pytest.mark.parametrize('builder,slot', [(install.investigator_source, 'WORKER_BINDING'),
    (install.chat_source, 'CHAT_BINDING'), (install.ingestion_source, 'RUNNER_BINDING')])
def test_reviewed_native_sources_render_and_compile_without_execution(binding, builder, slot):
    rendered = install._render(builder(), slot, binding)
    compile(rendered, '<synthetic-sonda-install>', 'exec')
    assert (slot + ' = {}').encode() not in rendered


def test_private_investigator_never_receives_developer_use(client, binding, monkeypatch):
    monkeypatch.setattr(SocialAidpClient, '_ensure_global_agent_permissions', Mock(return_value=False))
    state = manifest(binding)
    state['resources']['investigator'] = {'agent_key': 'private-worker'}
    client._role_principal_ocids = Mock(return_value=(set(), set()))
    client._list = Mock(return_value=[])
    client._workspace_object_key = Mock(side_effect=lambda ws, path: path)
    client._reconcile_role_permissions_exact = Mock(return_value=False)
    assert not client._ensure_global_agent_permissions('workspace', state)
    calls = client._reconcile_role_permissions_exact.call_args_list
    assert calls[0].args[3] == (('AI_DATA_PLATFORM_ADMIN', 'ADMIN', None),)
    assert len(calls) == 5 and all('AIDP_DEVELOPER' not in str(call) for call in calls)
    client._list.return_value = [{'isInherited': True, 'roleName': 'AIDP_DEVELOPER', 'permission': 'USE'}]
    client._permission_grantee = Mock(return_value=('AIDP_DEVELOPER', 'ROLE'))
    client._permission_values = Mock(return_value={'USE'})
    with pytest.raises(AidpProvisionError, match='non-administrator'):
        client._ensure_global_agent_permissions('workspace', state)


def test_activation_stays_paused_until_external_acceptance_without_mutating_fence(client, binding):
    state = manifest(binding)
    protected = fence(binding)
    client._fence = Mock(return_value=protected)
    client._ensure_governance_job = Mock(return_value=('job', False))
    with pytest.raises(AidpProvisionPending) as raised:
        client._activate_social('workspace', state, 'install')
    assert raised.value.phase == 'acceptance' and state['enabled'] is False and state['phase'] == 'activation'
    assert client._ensure_governance_job.call_args.kwargs == {'desired_enabled': False, 'paused': True}
    assert protected == fence(binding)


def test_accepted_activation_unpauses_only_after_native_schedule_visible(client, binding, monkeypatch):
    state = manifest(binding)
    client._fence = Mock(return_value=fence(binding, enabled=True, bootstrap=False, integrated_acceptance=True))
    client._ensure_governance_job = Mock(return_value=('job', True))
    with pytest.raises(AidpProvisionPending):
        client._activate_social('workspace', state, 'install')
    assert state['status'] != 'active'
    client._ensure_governance_job.return_value = ('job', False)
    monkeypatch.setattr(install, 'load_lab_pack', lambda *args, **kwargs: SimpleNamespace(pack_version='1.2.0'))
    client._activate_social('workspace', state, 'install')
    assert state['status'] == 'active' and state['enabled'] and state['operation']['phase'] == 'complete'
    assert client._ensure_governance_job.call_args.kwargs == {'desired_enabled': True, 'paused': False}


def test_legacy_manifest_status_remains_readable_without_protected_object(client, binding):
    legacy = {key: value for key, value in binding.items() if key in install.FIXED_FIELDS or key == 'credential_name'}
    old = manifest(legacy, status='active')
    client._workspace_json = Mock(return_value=old)
    assert client._module_manifest('workspace') == old
