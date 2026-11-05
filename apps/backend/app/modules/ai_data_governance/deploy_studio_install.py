"""Resumable fresh-tenant installation of the Governance TEST candidate."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from .deployment_bundle import (ARTIFACT_ROOT, BOOTSTRAP_PATH, DOCUMENTARY_ENTRY_PATH,
                               GOVERNANCE_ENTRY_PATH, build_deployment_bundle)
from .documentary_source import documentary_entry_source
from .governance_source import governance_entry_source
from .catalog_discovery import discover_source_scopes

from .constants import RUNTIME_FAMILY
JOB_NAME = 'wf_ai_dg_sync_master_catalog'
DOCUMENTARY_NAME = 'ai_data_governance_sync_master_catalog'
SCHEDULE = {'quartzCronExpression': '0 0/30 * * * ?', 'timezoneId': 'UTC'}


def job_matches(actual, expected):
    if not isinstance(actual, dict):
        return False
    fields = ('name', 'path', 'maxConcurrentRuns', 'queue', 'jobClusters', 'schedule')
    if any(actual.get(k) != expected.get(k) for k in fields):
        return False
    tasks = actual.get('tasks')
    return bool(isinstance(tasks, list) and len(tasks) == len(expected['tasks']) and all(
        isinstance(a, dict) and all(a.get(k) == v for k, v in e.items())
        for a, e in zip(tasks, expected['tasks'], strict=True)))


class GovernanceInstaller:
    def __init__(self, client, workspace, manifest, *, pending, error):
        self.client, self.workspace, self.manifest = client, workspace, manifest
        self.pending, self.error = pending, error
        self.resources = manifest.setdefault('resources', {})
        self.prefix = '/workspaces/' + workspace

    def save(self):
        self.client._write_module_manifest(self.workspace, self.manifest)

    def wait(self, message):
        raise self.pending(message, self.manifest['phase'])

    def request(self, method, path, **kwargs):
        return self.client._request(method, path, phase=self.manifest['phase'], **kwargs)

    def reconcile(self):
        stages = {'verify': self.prepare, 'control': self.content, 'sync': self.bootstrap,
                  'agent': self.agents, 'activation': self.activate}
        while self.manifest['phase'] in stages:
            stages[self.manifest['phase']]()
        return self.manifest

    def advance(self, phase):
        self.manifest['phase'] = phase
        self.save()

    def prepare(self):
        self.client._ensure_governance_bucket()
        credential, _ = self.client._ensure_governance_credential()
        catalog, _ = self.client._ensure_catalog('oci_medallion')
        schemas = self.client._list('/schemas', params={'catalogKey': catalog['key']}, phase='control')
        selected = [s for s in schemas if self.client._resource_name(s) == 'oci_artifacts']
        if len(selected) > 1:
            raise self.error('Governance artifacts schema is ambiguous; no installation was adopted.')
        if not selected:
            self.request('POST', '/schemas', payload={'catalogName': 'oci_medallion',
                'displayName': 'oci_artifacts', 'description': 'Shared agent-framework artifacts'})
            self.wait('The artifacts schema is being created; resume this installation.')
        if selected[0].get('key') != 'oci_medallion.oci_artifacts':
            raise self.error('Governance artifacts schema identity differs from the approved model.')
        state = str(selected[0].get('lifecycleState') or 'ACTIVE').upper()
        if state in {'FAILED', 'DELETED', 'DELETING'}:
            raise self.error('The artifacts schema is unavailable; retain the installation for inspection.')
        if state != 'ACTIVE':
            self.wait('The artifacts schema is not ACTIVE yet; resume this installation.')
        scopes = discover_source_scopes(
            lambda method, path, **kwargs: self.request(method, path, include_headers=True, **kwargs),
            check_active=lambda: None)
        if {'catalog_key': 'oci_medallion', 'schema_key': 'oci_medallion.oci_artifacts'} not in scopes:
            raise self.error('Complete source discovery did not include the artifacts schema.')
        compute = self.client._shared_compute(self.workspace)
        agent_compute, _ = self.client._ensure_agent_compute(self.workspace)
        self.resources.update(credential_key=credential, cluster_key=compute['key'],
                              agent_compute_key=agent_compute['key'], source_scopes=scopes)
        folders = (ARTIFACT_ROOT, ARTIFACT_ROOT + '/tasks', ARTIFACT_ROOT + '/agents',
                   DOCUMENTARY_ENTRY_PATH.rsplit('/', 1)[0], GOVERNANCE_ENTRY_PATH.rsplit('/', 1)[0])
        self.client._ensure_workspace_layout(self.workspace, ('/Workspace', '/Workspace/.artifact',
            '/Workspace/jobs', *folders))
        self.advance('control')

    def ensure_agent(self, resource, name, entry, source):
        expected = self.resources.get(resource)
        current = self.client._agents(self.workspace, name)
        intent = resource + '_creation_submitted'
        folder = entry.rsplit('/', 1)[0]
        if expected:
            if (len(current) != 1 or str(current[0].get('key') or current[0].get('id') or '') != expected
                    or current[0].get('pathInfo') != folder):
                raise self.error('The recorded Governance Agent identity or workspace root changed; no source was written.')
        elif current:
            if not self.resources.get(intent) or len(current) != 1 or current[0].get('pathInfo') != folder:
                raise self.error('An existing Agent conflicts with the fresh Governance TEST installation.')
            expected = str(current[0].get('key') or current[0].get('id') or '')
            if not expected:
                self.wait('The submitted Agent has no verified native identity yet; do not create another.')
        elif self.resources.get(intent):
            self.wait('Agent creation has an uncertain result; reconcile the recorded identity before another POST.')
        definition_path = Path(__file__).resolve().parent / "aidp/workspaces/default/agent_flow/agents" / (name + ".json")
        definition = json.loads(definition_path.read_text(encoding="utf-8"))
        if definition["pathInfo"] != folder or definition["entry_file"] != entry.rsplit('/', 1)[1]:
            raise self.error("Governance agent descriptor disagrees with its maintained entry.")
        if expected:
            self.resources[resource] = expected
            self.save()

        def before_create():
            self.resources[intent] = True
            self.save()

        descriptor = json.dumps({'schema_version': 1, 'module_id': 'ai_data_governance',
            'deployment_stage': 'TEST', 'entry_sha256': hashlib.sha256(source).hexdigest()}).encode()
        key, _ = self.client._ensure_agent(self.workspace, self.resources['agent_compute_key'],
            name, entry.rsplit('/', 1)[0], source, descriptor, repair_drift=True,
            expected_key=expected or '', entry_filename=entry.rsplit('/', 1)[1],
            before_create=before_create)
        self.resources[resource] = key
        self.save()

    def content(self):
        self.ensure_agent('documentation_agent_key', DOCUMENTARY_NAME, DOCUMENTARY_ENTRY_PATH, documentary_entry_source())
        self.ensure_agent('agent_key', 'ai_data_governance', GOVERNANCE_ENTRY_PATH, governance_entry_source())
        if not self.resources.get('workflow_key'):
            key = self.client._job_key(self.workspace, JOB_NAME)
            if key and not self.resources.get('job_creation_submitted'):
                raise self.error('An existing workflow conflicts with the fresh TEST installation.')
            if not key:
                if self.resources.get('job_creation_submitted'):
                    self.wait('Workflow creation has an uncertain result; read its identity before another POST.')
                self.resources['job_creation_submitted'] = True
                self.save()
                key = self.client._create_job(self.workspace, {'name': JOB_NAME, 'path': '/Workspace/jobs',
                    'description': 'Governance TEST candidate; native behavior acceptance pending',
                    'maxConcurrentRuns': 1, 'queue': {'isEnabled': False},
                    'schedule': {**SCHEDULE, 'pauseStatus': 'PAUSED'}})
            self.resources['workflow_key'] = key
            self.save()
        settings = self.client.settings
        binding = {**self.client._governance_oci_config(), 'region': settings.aidp_region,
            'platform_id': settings.aidp_platform_id, 'workspace_key': self.workspace,
            'job_key': self.resources['workflow_key'], 'cluster_key': self.resources['cluster_key'],
            'query_compute_key': self.resources['cluster_key'], 'agent_key': self.resources['agent_key'],
            'documentation_agent_key': self.resources['documentation_agent_key'],
            'model_id': settings.agent_model_id, 'compartment_id': settings.compartment_id,
            'storage_root': f'oci://{settings.artifacts_bucket_name}@{settings.objectstorage_namespace}/oci_artifacts',
            'source_scopes': self.resources['source_scopes'],
            'deployment_stage': 'TEST'}
        bundle = build_deployment_bundle(binding)
        for path, body in bundle['files'].items():
            self.client._upload_file(self.workspace, path, body, repair_drift=True)
        self.resources.update(bundle_manifest=bundle['manifest'], workflow_definition=bundle['workflow'],
                              source_revision=bundle['manifest']['consultative_revision'])
        self.publish(self.bootstrap_job())
        self.advance('sync')

    def bootstrap_job(self):
        return {'name': JOB_NAME, 'path': '/Workspace/jobs', 'maxConcurrentRuns': 1,
            'description': 'Additive18-table bootstrap for Governance TEST; no truncation or deletion',
            'queue': {'isEnabled': False}, 'jobClusters': [{'clusterKey': self.resources['cluster_key']}],
            'schedule': {**SCHEDULE, 'pauseStatus': 'PAUSED'},
            'tasks': [{'type': 'PYTHON_TASK', 'taskKey': 'bootstrap18tables', 'filePath': BOOTSTRAP_PATH,
                'commandLineArguments': json.dumps([BOOTSTRAP_PATH]), 'cluster': {'clusterKey': self.resources['cluster_key']},
                'dependsOn': [], 'runIf': 'ALL_SUCCESS', 'maxRetries': 0,
                'isRetryOnTimeout': False, 'timeoutSeconds': 1800}]}

    def publish(self, payload):
        path = self.prefix + '/jobs/' + self.resources['workflow_key']
        if not job_matches(self.request('GET', path), payload):
            self.request('PUT', path, payload=payload)
            if not job_matches(self.request('GET', path), payload):
                self.wait('The exact Governance workflow graph is not visible yet.')

    def completed_run(self, field, tasks):
        recorded = self.resources.get(field)
        if recorded is None:
            self.resources[field] = {'submitted_at': datetime.now(timezone.utc).isoformat(), 'state': 'SUBMITTING'}
            self.save()
            run = self.request('POST', self.prefix + '/jobRuns', payload={
                'jobKey': self.resources['workflow_key'], 'parameters': [], 'queue': {'isEnabled': False}},
                retry_scope='governance:' + self.manifest['operation']['operation_id'] + ':' + field)
            key = (run or {}).get('key')
            if key:
                self.resources[field]['key'] = key
                self.save()
            self.wait('The Governance installation task is running; observe this same operation.')
        key = self.reconcile_run_key(recorded)
        run = self.request('GET', self.prefix + '/jobRuns/' + key)
        if run.get('jobKey') != self.resources['workflow_key']:
            raise self.error('Governance run belongs to a different workflow.')
        state = (run.get('state') or {}).get('status')
        if state in {'FAILED', 'CANCELED', 'TIMED_OUT', 'INTERNAL_ERROR', 'TERMINATED'}:
            raise self.error('Governance native installation task failed; retain the run and inspect its logs.')
        if state not in {'SUCCESS', 'SUCCEEDED'}:
            self.wait('The submitted Governance workflow has not completed yet.')
        self.verify_task_runs(run, key, tasks)
        recorded['state'] = 'VERIFIED'
        self.save()

    def reconcile_run_key(self, recorded):
        key = recorded.get('key')
        if not key:
            rows = self.client._list(self.prefix + '/jobRuns', params={'jobKey': self.resources['workflow_key']}, phase='sync')
            at = self.client._timestamp(recorded['submitted_at'])
            rows = [r for r in rows if (self.client._timestamp(r.get('timeCreated') or r.get('createdAt')
                    or r.get('timeStarted') or r.get('startTime')) or 0) >= at]
            if len(rows) != 1:
                self.wait('The submitted workflow result is uncertain; do not resubmit it.')
            key = rows[0]['key']
            recorded['key'] = key
            self.save()
        return key

    def verify_task_runs(self, run, key, tasks):
        mapping = run.get('taskToTaskRunMap') or {}
        if set(mapping) != set(tasks):
            raise self.error('Governance run does not contain the expected task graph.')
        for task, task_run in mapping.items():
            detail = self.request('GET', self.prefix + '/taskRuns/' + task_run)
            if (detail.get('jobRunKey') != key or detail.get('jobKey') != self.resources['workflow_key']
                    or detail.get('taskKey') != task or (detail.get('state') or {}).get('status') not in {'SUCCESS', 'SUCCEEDED'}):
                raise self.error('Governance task completion or identity was not verified.')

    def bootstrap(self):
        self.completed_run('bootstrap_run', ['bootstrap18tables'])
        self.advance('agent')

    def test_deployments(self):
        rows, cursor = [], None
        for _ in range(20):
            body, headers = self.request('GET', self.prefix + '/agentFlowDeployments', legacy=True,
                params={'agentFlowKey': self.resources['agent_key'], 'deploymentType': 'TEST',
                        'limit': '100', **({'page': cursor} if cursor else {})}, include_headers=True)
            rows.extend(self.client._page_items(body))
            next_cursor = next((v for k,v in headers.items() if k.lower() == 'opc-next-page'), None)
            if not next_cursor:
                return rows
            if next_cursor == cursor:
                raise self.error('TEST deployment listing repeated its cursor.')
            cursor = next_cursor
        raise self.error('TEST deployment listing is incomplete.')

    def agents(self):
        documentary = self.resources['documentation_agent_key']
        path = self.prefix + '/agents/' + documentary + '/deployments'
        deployments = self.client._agent_deployments(path)
        self.check_deployment_states(deployments)
        if not any(d.get('lifecycleState') == 'ACTIVE' for d in deployments):
            if not self.resources.get('documentary_deploy_submitted'):
                self.resources['documentary_deploy_submitted'] = True
                self.save()
                self.client._ensure_agent_deployment(self.workspace, documentary,
                    self.resources['agent_compute_key'], DOCUMENTARY_NAME)
            self.wait('The workflow documentary worker deployment is starting; no consultative PROD was requested.')
        tests = [d for d in self.test_deployments() if d.get('lifecycleState') != 'DELETED']
        self.check_deployment_states(tests)
        if len(tests) > 1:
            raise self.error('The candidate has multiple TEST deployments; reconcile without replacement.')
        if not tests or tests[0].get('lifecycleState') != 'ACTIVE':
            if not self.resources.get('test_deploy_submitted'):
                self.resources['test_deploy_submitted'] = True
                self.save()
                self.request('POST', self.prefix + '/actions/redeployAgentFlow', legacy=True, payload={
                    'agentFlowKey': self.resources['agent_key'],
                    'agentFlowComputeKey': self.resources['agent_compute_key'], 'deploymentType': 'TEST'},
                    retry_scope='governance-test:' + self.manifest['operation']['operation_id'])
            self.wait('The consultative TEST candidate is starting; observe its existing deployment.')
        if tests[0].get('agentFlowKey') != self.resources['agent_key']:
            raise self.error('TEST deployment belongs to another agent.')
        self.resources['test_deployment_key'] = tests[0]['key']
        self.permissions()
        self.publish(self.workflow(paused=True))
        self.advance('activation')

    def check_deployment_states(self, deployments):
        for deployment in deployments:
            state = str(deployment.get('lifecycleState') or deployment.get('state') or '').upper()
            if state.endswith('FAILED') or state in {'DELETING', 'CANCELED', 'CANCELLED', 'INACTIVE'}:
                raise self.error('Governance Agent deployment failed or stopped; retain its identity and inspect it before retrying.')

    def permissions(self):
        for key in ('documentation_agent_key', 'agent_key'):
            self.client._ensure_role_permission(self.prefix + '/agents/' + self.resources[key],
                'assignAgentPermissionDetails', 'AI_DATA_PLATFORM_ADMIN', 'ADMIN', include_columns=True)
        object_key = self.client._workspace_object_key(self.workspace, ARTIFACT_ROOT)
        self.client._ensure_role_permission(self.prefix + '/objects/' + quote(object_key, safe=''),
            'assignWorkspaceObjectPermissionDetails', 'AI_DATA_PLATFORM_ADMIN', 'ADMIN', inheritable=True)

    def workflow(self, *, paused):
        return {**self.resources['workflow_definition'],
                'schedule': {**SCHEDULE, 'pauseStatus': 'PAUSED' if paused else 'UNPAUSED'}}

    def activate(self):
        self.completed_run('initial_workflow_run', ['sync_inventory', 'document_metadata', 'publish_metadata', 'replicate_model'])
        self.publish(self.workflow(paused=False))
        self.manifest.update(status='active', phase='active', enabled=True, validation_stage='TEST_PENDING')
        self.manifest['operation']['phase'] = 'complete'
        self.save()
