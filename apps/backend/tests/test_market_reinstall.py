"""Destructive reinstall admission and reconciliation without native side effects."""
import copy
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.aidp import AidpProvisionConflict, AidpProvisionPending
from app.market_installation import reinstall
from app import market_intelligence as installer
from test_market_installer import client, state, OP
from test_api import make_client, login

NEW_OP = '00000000-0000-4000-8000-000000000002'
SCOPE = '/workspaces/workspace'


class Native:
    def __init__(self):
        self.settings = SimpleNamespace(artifacts_bucket_name='artifacts', objectstorage_namespace='ns',
                                        bucket_name='landing', gods_eye_control_bucket='gold')
        self.objects = {}
        self.calls, self.saved = [], []
        self.runs = []
        self.object_storage = SimpleNamespace(get_bucket=lambda *a: SimpleNamespace(data=SimpleNamespace(versioning='Disabled')))
        self._resource_name = lambda row: row.get('displayName', '')
        self._governance_run_state = lambda row: row['state']
        self._catalog = lambda name: {'key': 'oci_medallion'}

    def _save(self, value):
        self.saved.append(copy.deepcopy(value))

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        row = self.objects.get(path)
        if method == 'GET':
            assert row is not None or kwargs.get('allow_not_found')
            return (copy.deepcopy(row), {'etag': 'revision'}) if kwargs.get('include_headers') else copy.deepcopy(row)
        if method == 'DELETE':
            assert kwargs['headers'] == {'if-match': 'revision'}
            self.objects.pop(path, None)
        return {}

    def _list(self, path, **kwargs):
        if path == '/schemas':
            return [{'key': 'oci_medallion.oci_artifacts', 'displayName': 'oci_artifacts'}]
        if path.endswith('/jobRuns'):
            return copy.deepcopy(self.runs)
        return [copy.deepcopy(row) for key, row in self.objects.items() if key.rsplit('/', 1)[0] == path]

    def _agents(self, workspace, module):
        return [row for row in self._list(SCOPE + '/agents') if row['displayName'] == module]

    def _schema_tables(self, *args):
        return self._list('/tables')


def populated():
    api = Native()
    api.objects[SCOPE + '/agents/market'] = {'key': 'market', 'type': 'CODE', 'displayName': reinstall.MODULE,
        'entryFilePath': reinstall.ROOT + '/agent/market_intelligence.py'}
    api.objects[SCOPE + '/agents/governance'] = {'key': 'governance', 'type': 'CODE', 'displayName': 'ai_data_governance', 'computeKey': 'gov-compute'}
    api.objects[SCOPE + '/jobs/job'] = {'key': 'job', 'path': reinstall.ROOT + '/.setup_old',
        'tasks': [{'source': 'WORKSPACE', 'filePath': reinstall.ROOT + '/.setup_old/model.py'}]}
    api.objects['/tables/market-table'] = {'key': 'market-table', 'displayName': 'ai_mi_sources', 'tableType': 'MANAGED',
        'catalogKey': 'oci_medallion', 'schemaKey': 'oci_medallion.oci_artifacts',
        'tableProperties': [{'propertyName': 'ai_mi.module_id', 'propertyValue': reinstall.MODULE},
                            {'propertyName': 'ai_mi.schema_sha256', 'propertyValue': installer.schema_sha256()}]}
    api.objects['/tables/gov-table'] = {'key': 'gov-table', 'displayName': 'ai_dg_config'}
    api.objects[SCOPE + '/clusters/compute'] = {'key': 'compute', 'displayName': reinstall.COMPUTE_NAME,
        'type': 'AI_COMPUTE', 'description': 'AI Market Intelligence development compute; operation=' + OP}
    api.objects['/credentials/apify'] = {'key': 'apify', 'displayName': 'AiMarketIntelligenceApifyBearer',
        'type': 'SECRET_TOKEN', 'lifecycleState': 'ACTIVE', 'credentialDescription': 'AI Market Intelligence Apify connection; operation=' + OP}
    api.objects['/credentials/shared'] = {'key': 'shared', 'displayName': 'AidpRuntime'}
    saved = state('reinstall_inventory')
    saved.update(mode='reinstall', previous_operation_id=OP, retired_operations=[OP], previous_resources={})
    return api, saved


def test_inventory_is_complete_read_only_and_excludes_shared_resources():
    api, saved = populated()
    reinstall.inventory(api, saved)
    assert {item['kind'] for item in saved['retirement']['items']} == {'workflow', 'agent', 'table', 'compute', 'credential'}
    assert {item['key'] for item in saved['retirement']['items']} == {'job', 'market', 'market-table', 'compute', 'apify'}
    assert all(method == 'GET' for method, _, _ in api.calls)


@pytest.mark.parametrize('fault', ['foreign_table', 'foreign_task', 'shared_compute', 'foreign_credential', 'versioned_bucket'])
def test_inventory_fails_before_any_deletion_for_unverified_scope(fault):
    api, saved = populated()
    if fault == 'foreign_table':
        api.objects['/tables/market-table']['tableProperties'] = []
    elif fault == 'foreign_task':
        api.objects[SCOPE + '/jobs/job']['tasks'][0]['filePath'] = '/Workspace/governance.py'
    elif fault == 'shared_compute':
        api.objects[SCOPE + '/agents/governance']['computeKey'] = 'compute'
    elif fault == 'foreign_credential':
        api.objects['/credentials/apify']['credentialDescription'] = 'Another project'
    else:
        api.object_storage.get_bucket = lambda *a: SimpleNamespace(data=SimpleNamespace(versioning='Enabled'))
    with pytest.raises((AidpProvisionConflict, RuntimeError)):
        reinstall.inventory(api, saved)
    assert all(method == 'GET' for method, _, _ in api.calls)
    assert 'retirement' not in saved


@pytest.mark.parametrize('kind,field,value', [
    ('workflow', 'tasks', [{'source': 'WORKSPACE', 'filePath': '/Workspace/governance.py'}]),
    ('credential', 'credentialDescription', 'Another project'), ('table', 'timeCreated', 'replacement')])
def test_resource_drift_is_rejected_after_inventory(kind, field, value):
    api, saved = populated()
    reinstall.inventory(api, saved)
    item = next(item for item in saved['retirement']['items'] if item['kind'] == kind)
    api.objects[item['path']][field] = value
    with pytest.raises(AidpProvisionConflict, match='changed'):
        reinstall._remove(api, saved, item)
    assert not any(method == 'DELETE' for method, _, _ in api.calls)


def test_compute_is_rechecked_for_new_consumers_before_deletion():
    api, saved = populated()
    reinstall.inventory(api, saved)
    api.objects.pop(SCOPE + '/agents/market')
    api.objects[SCOPE + '/agents/governance']['computeKey'] = 'compute'
    item = next(item for item in saved['retirement']['items'] if item['kind'] == 'compute')
    with pytest.raises(AidpProvisionConflict, match='still uses'):
        reinstall._remove(api, saved, item)
    assert not any(method == 'DELETE' for method, _, _ in api.calls)


def test_uncertain_delete_reconciles_absence_without_deleting_a_second_time():
    api, saved = populated()
    reinstall.inventory(api, saved)
    item = next(item for item in saved['retirement']['items'] if item['kind'] == 'table')
    with pytest.raises(AidpProvisionPending):
        reinstall._remove(api, saved, item)
    assert api.saved[-1]['retirement']['items'][2]['delete_requested'] is True
    reinstall._remove(api, saved, item)
    assert item['done'] is True
    assert sum(method == 'DELETE' for method, _, _ in api.calls) == 1
    assert '/tables/gov-table' in api.objects


def test_active_runs_are_cancelled_once_and_rechecked_before_table_removal():
    api, saved = populated()
    reinstall.inventory(api, saved)
    api.runs = [{'key': 'run', 'jobKey': 'job', 'state': 'RUNNING'}]
    for phase in ('reinstall_stop', 'reinstall_tables'):
        saved['phase'] = phase
        with pytest.raises(AidpProvisionPending, match='stop'):
            reinstall.step(api, saved)
    assert sum(method == 'POST' for method, _, _ in api.calls) == 1
    assert not any(method == 'DELETE' for method, _, _ in api.calls)


def test_table_removal_waits_for_workflows_and_agent_compute_absence():
    api, saved = populated()
    reinstall.inventory(api, saved)
    saved['phase'] = 'reinstall_tables'
    with pytest.raises(AidpProvisionConflict, match='workflow still exists'):
        reinstall.step(api, saved)
    assert not any(method == 'DELETE' for method, _, _ in api.calls)


def test_complete_retirement_removes_only_the_project_and_can_resume_every_native_delete():
    api, saved = populated()
    removed_files = []
    api._delete_workspace_path = lambda workspace, path, message: removed_files.append(path)
    api.object_storage.list_objects = lambda ns, bucket, **kwargs: SimpleNamespace(data=SimpleNamespace(objects=[]))
    preserved = {path: copy.deepcopy(api.objects[path]) for path in ('/credentials/shared', '/tables/gov-table', SCOPE + '/agents/governance')}
    for phase in reinstall.PHASES:
        saved['phase'] = phase
        for attempt in range(10):
            try:
                reinstall.step(api, saved)
                break
            except AidpProvisionPending:
                continue
        else:
            pytest.fail('Retirement did not reconcile its saved native removal')
    assert api.objects == preserved
    deleted = [path for method, path, _ in api.calls if method == 'DELETE']
    assert len(deleted) == len(set(deleted)) == 5
    assert all(path.startswith(reinstall.ROOT + '/') and '.installation-history' not in path for path in removed_files)
    assert saved['resources'] == {}


def test_reinstall_archives_the_previous_receipt_before_starting_a_new_generation():
    api = client()
    old = state('model_verify', model_job_key='old-job', model_run_key='failed-run', apify_credential_key='old-apify')
    old['status'] = 'awaiting_action'
    frozen = copy.deepcopy(old)
    stored = {}
    api._workspace = lambda: {'key': 'workspace'}
    api._read_installation = lambda workspace: old
    api._ensure_folder = lambda *a: None
    api._workspace_json = lambda workspace, path, message: stored.get(path)
    def upload(workspace, path, content, **kwargs):
        import json
        assert kwargs['repair_drift'] is False
        stored[path] = json.loads(content)
    api._upload_file = upload
    api._workspace_object = lambda workspace, path, **kwargs: (stored[path], {})
    saved = []
    api._save = lambda value: saved.append(copy.deepcopy(value))
    api._advance = lambda *args: args
    result = api._reinstall('owner', NEW_OP, OP, SecretStr('synthetic-new-apify-secret'), None)
    assert result[:3] == ('owner', NEW_OP, False)
    assert stored[reinstall.ROOT + '/.installation-history/' + OP + '.json'] == frozen
    assert old == frozen
    assert saved[0]['operation_id'] == NEW_OP and saved[0]['previous_operation_id'] == OP
    assert saved[0]['phase'] == 'reinstall_inventory'
    assert saved[0]['resources'] == {}
    assert saved[0]['previous_resources'] == frozen['resources']
    assert 'synthetic-new-apify-secret' not in str(saved)


def test_existing_reinstall_is_resumed_without_rearchiving_or_resetting_effects():
    api = client()
    current = state('reinstall_tables', retained='keep-resource')
    current.update(operation_id=NEW_OP, mode='reinstall', previous_operation_id=OP)
    api._workspace = lambda: {'key': 'workspace'}
    api._read_installation = lambda workspace: current
    api._ensure_folder = lambda *a: pytest.fail('Must not restart retirement')
    api._save = lambda *a: pytest.fail('Must not reset the operation')
    api._advance = lambda *args: current
    assert api._reinstall('owner', NEW_OP, OP, None, None) is current
    assert current['resources'] == {'retained': 'keep-resource'}


def test_storage_versioning_changed_after_inventory_never_reports_cleanup_complete():
    api, saved = populated()
    reinstall.inventory(api, saved)
    api.objects = {}
    api.object_storage.get_bucket = lambda *a: SimpleNamespace(data=SimpleNamespace(versioning='Enabled'))
    api.object_storage.list_objects = lambda *a, **k: pytest.fail('Must not enter unversioned cleanup')
    saved['phase'] = 'reinstall_storage'
    with pytest.raises(AidpProvisionConflict, match='versioning changed'):
        reinstall.step(api, saved)


@pytest.mark.parametrize('existing', [True, False])
def test_shared_oci_credential_is_reused_or_created_without_rotation(monkeypatch, existing):
    api = client()
    saved = state('credentials')
    api._shared_oci_credential = lambda: {'key': 'shared-runtime'} if existing else None
    calls = []
    api._ensure_governance_credential = lambda: (calls.append('create') or 'shared-runtime', True)
    monkeypatch.setattr(installer, '_project_credential', lambda client: None)
    monkeypatch.setattr(installer, 'ensure_apify_credential', lambda *a, **k: {'credential_key': 'project-apify'})
    api._step(saved, SecretStr('synthetic-new-apify-secret'), None)
    assert calls == ([] if existing else ['create'])
    assert saved['resources']['shared_oci_credential_key'] == 'shared-runtime'


def test_uncertain_shared_credential_creation_does_not_create_another(monkeypatch):
    api = client()
    saved = state('credentials')
    api._shared_oci_credential = lambda: None
    def fail():
        raise TimeoutError('Unknown outcome')
    api._ensure_governance_credential = fail
    with pytest.raises(TimeoutError):
        api._step(saved, None, None)
    api._ensure_governance_credential = lambda: pytest.fail('Must reconcile, not create again')
    with pytest.raises(AidpProvisionConflict, match='another shared credential'):
        api._step(saved, None, None)


def test_reinstall_public_status_resets_the_previous_generation_credential():
    previous = installer.public_status(state('model_verify', apify_credential_key='old-apify', apify_token_suffix='AB12'))
    current = state('reinstall_stop')
    current.update(mode='reinstall', operation_id=NEW_OP, previous_operation_id=OP)
    merged = {**previous, **installer.public_status(current)}
    assert merged['apify_credential_status'] == 'missing'
    assert merged['apify_credential_owned_by_operation'] is False
    assert merged['apify_token_suffix'] == ''


def test_reinstall_api_requires_admin_confirmation_and_never_echoes_secrets(tmp_path):
    portal = make_client(tmp_path)
    calls = []
    class Market:
        async def reinstall(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {'status': 'redeploying', 'operation_id': NEW_OP, 'phase': 'reinstall_inventory'}
    portal.app.state.market_factory = Market
    path = '/api/admin/users/user-id/modules/ai_market_intelligence/reinstall'
    body = {'operation_id': NEW_OP, 'previous_operation_id': OP, 'confirmation': reinstall.MODULE, 'reuse_existing': False,
            'apify_token': 'synthetic-new-apify-secret'}
    assert portal.post(path, json=body).status_code == 401
    login(portal)
    for bad in ({'confirmation': 'ai_data_governance'}, {'reuse_existing': True}, {'operation_id': OP}, {'apify_token': {'secret': 'must-not-echo'}}):
        response = portal.post(path, json={**body, **bad})
        assert response.status_code == 400 and 'must-not-echo' not in response.text
    assert not calls
    assert portal.post(path, json=body).status_code == 202
    assert calls[0][0] == ('ocid1.user.oc1..ada', NEW_OP, OP)
    assert isinstance(calls[0][1]['token'], SecretStr)
