from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from app.modules.ai_data_governance.deploy_studio_install import GovernanceInstaller, JOB_NAME, job_matches
from app.services.oci.aidp import AidpClient

WORKSPACE = '11111111-1111-4111-8111-111111111111'
SPARK = '22222222-2222-4222-8222-222222222222'
COMPUTE = '33333333-3333-4333-8333-333333333333'
DOC = '44444444-4444-4444-8444-444444444444'
AGENT = '55555555-5555-4555-8555-555555555555'
JOB = '66666666-6666-4666-8666-666666666666'


class Pending(Exception):
    def __init__(self, message, phase):
        super().__init__(message)
        self.phase = phase


class Owner:
    def __init__(self):
        self.settings = SimpleNamespace(aidp_region='us-chicago-1',
            aidp_platform_id='ocid1.aidataplatform.oc1.us-chicago-1.example',
            agent_model_id='ocid1.generativeaimodel.oc1.us-chicago-1.example',
            compartment_id='ocid1.compartment.oc1..example', artifacts_bucket_name='artifacts',
            objectstorage_namespace='namespace')
        self.files, self.agents, self.job, self.runs, self.tasks = {}, {}, {}, {}, {}
        self.test, self.doc_deployments, self.calls, self.saved = [], [], [], []
        self.schema_state = 'ACTIVE'

    def _write_module_manifest(self, workspace, manifest): self.saved.append(deepcopy(manifest))
    def _ensure_governance_bucket(self): return False
    def _ensure_governance_credential(self): return 'credential', False
    def _ensure_catalog(self, name): return {'key': 'oci_medallion'}, False
    def _shared_compute(self, workspace): return {'key': SPARK}
    def _ensure_agent_compute(self, workspace): return {'key': COMPUTE}, False
    def _ensure_workspace_layout(self, workspace, folders): return False
    def _governance_oci_config(self): return {'credential_name': 'AidpRuntime', 'identity_sha256': 'a'*64}
    def _agents(self, workspace, name): return [a for a in self.agents.values() if a['displayName']==name]
    def _resource_name(self, item): return item.get('displayName') or item.get('name')
    def _job_key(self, workspace, name): return JOB if self.job else ''
    def _create_job(self, workspace, payload): self.job=deepcopy(payload); return JOB
    def _agent_deployments(self, path): return self.doc_deployments
    def _workspace_object_key(self, workspace, path): return path
    def _ensure_role_permission(self, *args, **kwargs): return False
    def _page_items(self, body): return body['items']
    _timestamp = staticmethod(AidpClient._timestamp)

    def _list(self, path, **kwargs):
        if path=='/schemas': return [{'key':'oci_medallion.oci_artifacts','displayName':'oci_artifacts',
                                      'lifecycleState':self.schema_state}]
        if path.endswith('/jobRuns'): return list(self.runs.values())
        raise AssertionError(path)

    def _upload_file(self, workspace, path, body, **kwargs):
        changed=self.files.get(path)!=body
        self.files[path]=body
        return changed

    def _ensure_agent(self, workspace, compute, name, folder, source, descriptor, **kwargs):
        if kwargs.get('before_create'):
            kwargs['before_create']()
        key=DOC if name.endswith('sync_master_catalog') else AGENT
        self.agents[key]={'key':key,'displayName':name,'pathInfo':folder}
        return key, True

    def _ensure_agent_deployment(self, workspace, key, compute, name):
        self.calls.append(('DOCUMENTARY_DEPLOY',key))
        self.doc_deployments=[{'key':'documentary-deployment','lifecycleState':'ACTIVE'}]
        return self.doc_deployments[0], True

    def _request(self, method, path, **kwargs):
        self.calls.append((method,path,deepcopy(kwargs)))
        if path == '/catalogs':
            return {'items':[{'key':'oci_medallion'}]}, {}
        if path == '/schemas':
            return {'items':[{'key':'oci_medallion.oci_artifacts'}, {'key':'oci_medallion.oci_gold'}]}, {}
        if path.endswith('/agentFlowDeployments'):
            assert kwargs['legacy'] is True
            return {'items':deepcopy(self.test)}, {}
        if path.endswith('/actions/redeployAgentFlow'):
            assert kwargs['payload']['deploymentType']=='TEST' and kwargs['legacy'] is True
            self.test=[{'key':'test-deployment','agentFlowKey':AGENT,'lifecycleState':'ACTIVE'}]
            return {}
        if '/taskRuns/' in path: return self.tasks[path.rsplit('/',1)[1]]
        if '/jobRuns/' in path: return self.runs[path.rsplit('/',1)[1]]
        if path.endswith('/jobRuns') and method=='POST':
            number=len(self.runs)+1
            key='run-'+str(number)
            mapping={t['taskKey']:str(number)+'-'+t['taskKey'] for t in self.job['tasks']}
            self.runs[key]={'key':key,'jobKey':JOB,'timeCreated':datetime.now(timezone.utc).isoformat(),
                'state':{'status':'RUNNING'},'taskToTaskRunMap':mapping}
            for task, task_run in mapping.items():
                self.tasks[task_run]={'jobRunKey':key,'jobKey':JOB,'taskKey':task,'state':{'status':'RUNNING'}}
            return {'key':key}
        if '/jobs/' in path:
            if method=='PUT': self.job=deepcopy(kwargs['payload'])
            return deepcopy(self.job)
        raise AssertionError((method,path))

    def complete(self, key, status='SUCCEEDED'):
        self.runs[key]['state']['status']=status
        for task in self.runs[key]['taskToTaskRunMap'].values(): self.tasks[task]['state']['status']=status


def fixture():
    owner=Owner()
    manifest={'runtime_family':'AI_DATA_GOVERNANCE_TEST','status':'installing','phase':'verify','enabled':False,
        'operation':{'operation_id':'77777777-7777-4777-8777-777777777777','type':'install'},'resources':{}}
    return owner,manifest,GovernanceInstaller(owner,WORKSPACE,manifest,pending=Pending,error=ValueError)


@pytest.mark.parametrize('status', ['SUCCESS', 'SUCCEEDED'])
def test_fresh_install_bootstraps_then_runs_four_tasks_and_only_consultative_test(status):
    owner,manifest,installer=fixture()
    with pytest.raises(Pending): installer.reconcile()
    assert manifest['phase']=='sync' and len(owner.job['tasks'])==1
    assert json.loads(owner.job['tasks'][0]['commandLineArguments']) == [owner.job['tasks'][0]['filePath']]
    assert manifest['resources']['bundle_manifest']['bindings']['source_scopes'] == [
        {'catalog_key':'oci_medallion','schema_key':'oci_medallion.oci_artifacts'},
        {'catalog_key':'oci_medallion','schema_key':'oci_medallion.oci_gold'}]
    owner.complete('run-1', status)
    with pytest.raises(Pending): installer.reconcile()
    assert owner.calls[-1]==('DOCUMENTARY_DEPLOY',DOC)
    with pytest.raises(Pending): installer.reconcile()
    assert owner.test[0]['agentFlowKey']==AGENT
    with pytest.raises(Pending): installer.reconcile()
    assert manifest['phase']=='activation' and len(owner.job['tasks'])==4
    assert owner.job['schedule']['pauseStatus']=='PAUSED'
    owner.complete('run-2', status)
    assert installer.reconcile()['status']=='active'
    assert manifest['validation_stage']=='TEST_PENDING'
    assert owner.job['schedule']=={'quartzCronExpression':'0 0/30 * * * ?', 'timezoneId':'UTC','pauseStatus':'UNPAUSED'}
    assert [c for c in owner.calls if c[0]=='DOCUMENTARY_DEPLOY']==[('DOCUMENTARY_DEPLOY',DOC)]
    assert not any(c[0]=='POST' and '/agents/'+AGENT+'/deployments' in c[1] for c in owner.calls)
    before=len(owner.calls)
    assert installer.reconcile()['status']=='active' and len(owner.calls)==before


def test_creating_artifacts_schema_waits_before_agents_jobs_or_files():
    owner, manifest, installer = fixture()
    owner.schema_state = 'CREATING'
    with pytest.raises(Pending, match='not ACTIVE'):
        installer.reconcile()
    assert manifest['phase'] == 'verify'
    assert not owner.agents and not owner.job and not owner.files


def test_wrong_native_task_identity_never_advances_bootstrap():
    owner, manifest, installer = fixture()
    with pytest.raises(Pending): installer.reconcile()
    owner.complete('run-1', 'SUCCESS')
    owner.tasks['1-bootstrap18tables']['jobRunKey'] = 'foreign'
    with pytest.raises(ValueError, match='identity'):
        installer.reconcile()
    assert manifest['phase'] == 'sync' and not owner.doc_deployments


@pytest.mark.parametrize('date_field', ['timeCreated', 'startTime'])
def test_lost_run_ack_is_reconciled_by_read_without_another_submission(date_field):
    owner,manifest,installer=fixture()
    original=owner._request
    def lost(method,path,**kwargs):
        result=original(method,path,**kwargs)
        if method=='POST' and path.endswith('/jobRuns'): raise Pending('ack lost','sync')
        return result
    owner._request=lost
    with pytest.raises(Pending): installer.reconcile()
    assert 'key' not in manifest['resources']['bootstrap_run']
    if date_field == 'startTime':
        owner.runs['run-1']['startTime'] = owner._timestamp(owner.runs['run-1'].pop('timeCreated')) * 1000
    owner.complete('run-1')
    with pytest.raises(Pending): installer.reconcile()
    assert manifest['resources']['bootstrap_run']['key']=='run-1'
    assert len([c for c in owner.calls if c[0]=='POST' and c[1].endswith('/jobRuns')])==1


@pytest.mark.parametrize('stage', ['documentary', 'test'])
def test_terminal_deployment_after_submission_stops_without_resubmitting(stage):
    owner, manifest, installer = fixture()
    with pytest.raises(Pending): installer.reconcile()
    owner.complete('run-1')
    with pytest.raises(Pending): installer.reconcile()
    if stage == 'documentary':
        owner.doc_deployments[0]['lifecycleState'] = 'FAILED'
    else:
        with pytest.raises(Pending): installer.reconcile()
        owner.test[0]['lifecycleState'] = 'CREATE_FAILED'
    before = len(owner.calls)
    with pytest.raises(ValueError, match='deployment failed'):
        installer.reconcile()
    assert all(c[0] == 'GET' for c in owner.calls[before:])
    assert manifest['phase'] == 'agent'


def test_failed_bootstrap_never_deploys_agents_or_enables_schedule():
    owner,manifest,installer=fixture()
    with pytest.raises(Pending): installer.reconcile()
    owner.complete('run-1','FAILED')
    with pytest.raises(ValueError,match='failed'): installer.reconcile()
    assert not owner.doc_deployments and not owner.test
    assert owner.job['schedule']['pauseStatus']=='PAUSED'


def test_existing_unowned_agent_is_not_adopted():
    owner,manifest,installer=fixture()
    owner.agents['foreign']={'key':'foreign','displayName':'ai_data_governance_sync_master_catalog','pathInfo':'/Workspace/foreign'}
    with pytest.raises(ValueError,match='existing Agent'): installer.reconcile()
    assert not owner.job and not owner.runs


@pytest.mark.parametrize('change',['direction','dependencies','agent','file','schedule','task_count'])
def test_workflow_readback_rejects_material_drift(change):
    owner,manifest,installer=fixture()
    with pytest.raises(Pending): installer.reconcile()
    expected=installer.bootstrap_job(); actual=deepcopy(expected)
    if change=='direction': actual['tasks'][0]['runIf']='ALL_DONE'
    elif change=='dependencies': actual['tasks'][0]['dependsOn']=[{'taskKey':'foreign'}]
    elif change=='agent': actual['jobClusters'][0]['clusterKey']='foreign'
    elif change=='file': actual['tasks'][0]['filePath']='/Workspace/foreign.py'
    elif change=='schedule': actual['schedule']['pauseStatus']='UNPAUSED'
    else: actual['tasks']=[]
    assert not job_matches(actual,expected)


def native_bootstrap_readback():
    _, _, installer = fixture()
    installer.resources['cluster_key'] = SPARK
    expected = installer.bootstrap_job()
    actual = deepcopy(expected)
    actual['jobClusters'][0].update(clusterName=None, newCluster=None)
    actual['tasks'][0]['cluster'].update(clusterName=None, newCluster=None)
    actual['tasks'][0].update(minRetryIntervalMillis=None, source=None, gitConfig=None, isStreaming=None)
    return actual, expected


def test_workflow_readback_accepts_observed_native_empty_cluster_metadata_without_mutation():
    actual, expected = native_bootstrap_readback()
    original = deepcopy(actual)
    assert job_matches(actual, expected)
    assert actual == original


@pytest.mark.parametrize('target', ['job', 'task'])
@pytest.mark.parametrize('field,value', [('clusterName', 'foreign'), ('newCluster', {'driverShape': 'foreign'})])
def test_workflow_readback_rejects_nonempty_compute_overrides(target, field, value):
    actual, expected = native_bootstrap_readback()
    cluster = actual['jobClusters'][0] if target == 'job' else actual['tasks'][0]['cluster']
    cluster[field] = value
    assert not job_matches(actual, expected)


@pytest.mark.parametrize('drift', ['missing', 'key', 'root', 'missing_root', 'duplicate'])
def test_recorded_agent_drift_does_not_write_sources_or_change_the_installation_receipt(drift):
    from unittest.mock import Mock
    from app.modules.ai_data_governance.deployment_bundle import GOVERNANCE_ENTRY_PATH

    owner, manifest, installer = fixture()
    manifest['resources'].update(agent_compute_key=COMPUTE, agent_key=AGENT)
    agent = {'key': AGENT, 'displayName': 'ai_data_governance', 'pathInfo': GOVERNANCE_ENTRY_PATH.rsplit('/', 1)[0]}
    if drift == 'key': agent['key'] = 'foreign'
    if drift == 'root': agent['pathInfo'] = '/Workspace/foreign'
    if drift == 'missing_root': agent.pop('pathInfo')
    if drift != 'missing': owner.agents[agent['key']] = agent
    if drift == 'duplicate': owner.agents['foreign'] = {**agent, 'key': 'foreign'}
    owner._ensure_agent = Mock()
    original = deepcopy(manifest)
    with pytest.raises(ValueError, match='recorded Governance Agent identity or workspace root'):
        installer.ensure_agent('agent_key', 'ai_data_governance', GOVERNANCE_ENTRY_PATH, b'code')
    assert manifest == original
    assert not owner.saved and not owner.files and not owner.calls
    owner._ensure_agent.assert_not_called()


def test_matching_unowned_agent_without_creation_intent_is_not_adopted():
    from unittest.mock import Mock
    from app.modules.ai_data_governance.deployment_bundle import GOVERNANCE_ENTRY_PATH

    owner, manifest, installer = fixture()
    manifest['resources']['agent_compute_key'] = COMPUTE
    owner.agents[AGENT] = {'key': AGENT, 'displayName': 'ai_data_governance', 'pathInfo': GOVERNANCE_ENTRY_PATH.rsplit('/', 1)[0]}
    owner._ensure_agent = Mock()
    with pytest.raises(ValueError, match='existing Agent conflicts'):
        installer.ensure_agent('agent_key', 'ai_data_governance', GOVERNANCE_ENTRY_PATH, b'code')
    assert not owner.saved and not owner.files
    owner._ensure_agent.assert_not_called()


def test_lost_agent_creation_ack_persists_recovered_identity_before_resuming_sources():
    from unittest.mock import Mock
    from app.modules.ai_data_governance.deployment_bundle import GOVERNANCE_ENTRY_PATH

    owner, manifest, installer = fixture()
    manifest['resources'].update(agent_compute_key=COMPUTE, agent_key_creation_submitted=True)
    owner.agents[AGENT] = {'key': AGENT, 'displayName': 'ai_data_governance', 'pathInfo': GOVERNANCE_ENTRY_PATH.rsplit('/', 1)[0]}
    owner._ensure_agent = Mock(return_value=(AGENT, False))
    installer.ensure_agent('agent_key', 'ai_data_governance', GOVERNANCE_ENTRY_PATH, b'code')
    assert owner.saved[0]['resources']['agent_key'] == AGENT
    assert owner._ensure_agent.call_args.kwargs['expected_key'] == AGENT
    assert manifest['resources']['agent_key'] == AGENT


def test_uncertain_agent_creation_never_submits_a_duplicate_or_writes_sources():
    from unittest.mock import Mock
    from app.modules.ai_data_governance.deployment_bundle import GOVERNANCE_ENTRY_PATH

    owner, manifest, installer = fixture()
    manifest['resources'].update(agent_compute_key=COMPUTE, agent_key_creation_submitted=True)
    owner._ensure_agent = Mock()
    original = deepcopy(manifest)
    with pytest.raises(Pending, match='uncertain result'):
        installer.ensure_agent('agent_key', 'ai_data_governance', GOVERNANCE_ENTRY_PATH, b'code')
    assert manifest == original
    assert not owner.saved and not owner.files
    owner._ensure_agent.assert_not_called()


def test_pending_agent_file_upload_does_not_record_native_creation_and_can_resume():
    from unittest.mock import Mock
    from app.modules.ai_data_governance.deployment_bundle import GOVERNANCE_ENTRY_PATH

    owner, manifest, installer = fixture()
    manifest['resources']['agent_compute_key'] = COMPUTE
    owner._agent_entry_path = lambda root, filename=None: root + '/' + (filename or 'agent.py')
    owner._agent_description = 'Governance TEST'
    owner._ensure_agent = lambda *args, **kwargs: AidpClient._ensure_agent(owner, *args, **kwargs)
    original_upload = owner._upload_file
    pending = True

    def upload(workspace, path, body, **kwargs):
        nonlocal pending
        original_upload(workspace, path, body, **kwargs)
        if pending and path.endswith('agent-manifest.json'):
            pending = False
            raise Pending('Descriptor readback is pending', 'content')
        return True

    def request(method, path, **kwargs):
        assert method == 'POST' and path == f'/workspaces/{WORKSPACE}/agents'
        assert manifest['resources']['agent_key_creation_submitted'] is True
        owner.agents[AGENT] = {'key': AGENT, **kwargs['payload']}

    owner._upload_file = upload
    owner._request = Mock(side_effect=request)
    with pytest.raises(Pending, match='Descriptor readback'):
        installer.ensure_agent('agent_key', 'ai_data_governance', GOVERNANCE_ENTRY_PATH, b'code')
    assert not manifest['resources'].get('agent_key_creation_submitted')
    owner._request.assert_not_called()
    installer.ensure_agent('agent_key', 'ai_data_governance', GOVERNANCE_ENTRY_PATH, b'code')
    assert manifest['resources']['agent_key'] == AGENT
    assert owner._request.call_count == 1


def test_actual_agent_post_uncertainty_keeps_creation_intent_and_never_reposts():
    from unittest.mock import Mock
    from app.modules.ai_data_governance.deployment_bundle import GOVERNANCE_ENTRY_PATH

    owner, manifest, installer = fixture()
    manifest['resources']['agent_compute_key'] = COMPUTE
    owner._agent_entry_path = lambda root, filename=None: root + '/' + (filename or 'agent.py')
    owner._agent_description = 'Governance TEST'
    owner._ensure_agent = lambda *args, **kwargs: AidpClient._ensure_agent(owner, *args, **kwargs)
    owner._request = Mock(side_effect=Pending('Native POST outcome is uncertain', 'content'))
    with pytest.raises(Pending, match='Native POST outcome'):
        installer.ensure_agent('agent_key', 'ai_data_governance', GOVERNANCE_ENTRY_PATH, b'code')
    assert manifest['resources']['agent_key_creation_submitted'] is True
    with pytest.raises(Pending, match='uncertain result'):
        installer.ensure_agent('agent_key', 'ai_data_governance', GOVERNANCE_ENTRY_PATH, b'code')
    assert owner._request.call_count == 1
