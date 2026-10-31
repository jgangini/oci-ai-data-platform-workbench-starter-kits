"""Resumable empty installation, separate from legacy scheduled ingestion."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import UUID

from .aidp import AidpClient, AidpProvisionConflict, AidpProvisionError, AidpProvisionPending, CATALOG_NAME
from .lab_packs import load_lab_pack
from .market_installation.agent_package import agent_package
from .market_installation.compute import prepare_compute
from .market_installation.credentials import ensure_apify_credential, _project_credential, _credential_reference
from .market_installation.model_install import model_notebook, model_job
from .market_installation.playground import MarketPlaygroundClient
from .labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.schema import definition, schema_sha256

MODULE_ID = 'ai_market_intelligence'
ROOT = '/Workspace/.control/modules/' + MODULE_ID
PHASES = ('verify', 'credentials', 'model_prepare', 'model_run', 'model_verify', 'cleanup',
          'compute', 'files', 'agent', 'permissions', 'test', 'production', 'ready')


def public_status(manifest=None):
    state = manifest or {}
    return {'module_id': MODULE_ID, 'display_name': 'AI Market Intelligence',
            'status': state.get('status', 'not_installed'), 'installed': state.get('phase') == 'ready',
            'phase': state.get('phase', 'not_installed'), 'enabled': False,
            'operation_id': state.get('operation_id'), 'operation_type': 'install',
            'message': state.get('message', ''), 'bundled_version': load_lab_pack(MODULE_ID, require_available=False).pack_version,
            'reuse_existing': state.get('reuse_existing', True),
            'completed_phases': list(PHASES[:PHASES.index(state['phase'])]) if state.get('phase') in PHASES else [],
            'test_sessions': state.get('test_sessions', [])}


def model_receipt(output):
    """Only a complete, exact native stdout receipt can release bootstrap cleanup."""
    if not isinstance(output, dict):
        raise AidpProvisionPending('Waiting for the executed model output.', 'model_verify')
    if output.get('errorTrace') or output.get('isTruncated') or any(
            item.get('errorTrace') or item.get('isTruncated') for item in output.get('data', []) if isinstance(item, dict)):
        raise AidpProvisionError('The native model output is incomplete or contains an execution error.')
    values = [item.get('value') for item in output.get('data', []) if isinstance(item, dict)]
    receipts = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            notebook = json.loads(value)
        except ValueError:
            continue
        for cell in notebook.get('cells', []):
            for entry in cell.get('outputs', []):
                if entry.get('output_type') == 'error':
                    raise AidpProvisionError('The model notebook contains an execution error.')
                text = entry.get('text', '')
                text = ''.join(text) if isinstance(text, list) else text
                for line in str(text).splitlines():
                    if line.startswith('AI_MARKET_MODEL_READY:'):
                        receipts.append(json.loads(line.split(':', 1)[1]))
    if len(receipts) != 1:
        raise AidpProvisionPending('The model run has no unambiguous verification receipt yet.', 'model_verify')
    receipt = receipts[0]
    tables = receipt.get('tables', [])
    expected = {table['name']: len(table['columns']) for table in definition()['tables']}
    if (receipt.get('schema_sha256') != schema_sha256() or receipt.get('documented_columns') != 145
            or len(tables) != 14 or {row.get('table') for row in tables} != set(expected)
            or any(row.get('rows') != 0 or row.get('provider') != 'delta' or row.get('columns') != expected[row['table']] for row in tables)):
        raise AidpProvisionError('The native empty-model verification does not match the approved model.')
    return receipt


class MarketAidpClient(AidpClient):
    _agent_name = MODULE_ID
    _agent_entry_filename = 'market_intelligence.py'
    _agent_description = 'On-demand market research; installed paused without business data'

    def _read_installation(self, workspace):
        state = self._workspace_json(workspace, ROOT + '/installer.json', 'Invalid Market Intelligence installation receipt')
        if state is not None and (state.get('module_id') != MODULE_ID or state.get('installer_version') != 1
                or state.get('workspace') != workspace or state.get('phase') not in PHASES):
            raise AidpProvisionConflict('An incompatible installation receipt exists; preserve it for reconciliation.')
        return state

    def _save(self, state):
        state['updated_at'] = datetime.now(timezone.utc).isoformat()
        self._upload_file(state['workspace'], ROOT + '/installer.json', json.dumps(state, sort_keys=True, separators=(',', ':')).encode())

    async def status(self):
        def observe():
            state = self._read_installation(str(self._workspace()['key']))
            result = public_status(state)
            if state and state['phase'] == 'test' and state['resources'].get('agent_key'):
                test = MarketPlaygroundClient(self.settings)
                try:
                    sessions = test._list(f"/workspaces/{state['workspace']}/agentFlowSessions", params={'agentFlowKey': [state['resources']['agent_key']], 'deploymentType': 'TEST'})
                    result['test_sessions'] = [{'value': row['key'], 'label': row.get('displayName') or row.get('name') or row['key']} for row in sessions if row.get('key')]
                finally:
                    test.session.close()
            existing = _project_credential(self)
            result['apify_credential_available'] = bool(existing)
            return result
        return await asyncio.to_thread(observe)

    async def operate(self, user_ocid, operation_id, *, reuse_existing, token=None, test_session_id=None):
        if str(UUID(operation_id)) != operation_id:
            raise AidpProvisionConflict('A stable installation operation is required.')
        async with self._locks.setdefault(MODULE_ID, asyncio.Lock()):
            return await asyncio.to_thread(self._advance, user_ocid, operation_id, reuse_existing, token, test_session_id)

    async def agent_endpoint(self):
        def observe():
            workspace = str(self._workspace()['key'])
            state = self._read_installation(workspace)
            if not state or state['phase'] != 'ready':
                raise AidpProvisionConflict('Complete the installation before copying its agent endpoint.')
            return self._active_agent_endpoint(workspace, state['resources'])
        return await asyncio.to_thread(observe)

    def _advance(self, user, operation, reuse, token, session):
        workspace = str(self._workspace()['key'])
        state = self._read_installation(workspace)
        if state is None:
            self._ensure_workspace_layout(workspace, ('/Workspace/.control', '/Workspace/.control/modules', ROOT, ROOT + '/agent'))
            state = {'installer_version': 1, 'module_id': MODULE_ID, 'workspace': workspace,
                     'operation_id': operation, 'owner': user, 'reuse_existing': reuse,
                     'phase': 'verify', 'status': 'installing', 'resources': {},
                     'pack_sha256': load_lab_pack(MODULE_ID, require_available=False).pack_sha256}
            self._save(state)
        if (state['operation_id'] != operation or state['owner'] != user or state['reuse_existing'] is not reuse
                or state['pack_sha256'] != load_lab_pack(MODULE_ID, require_available=False).pack_sha256):
            raise AidpProvisionConflict('Resume the saved installation with the same administrator and settings.')
        if state['phase'] == 'ready':
            return public_status(state)
        state['status'] = 'installing'
        state['message'] = ''
        self._save(state)
        try:
            next_phase = self._step(state, token, session)
            if next_phase:
                state['phase'] = next_phase
            else:
                state['phase'] = PHASES[PHASES.index(state['phase']) + 1]
            state['status'] = 'active' if state['phase'] == 'ready' else 'installing'
            self._save(state)
        except AidpProvisionPending as exc:
            state['message'] = str(exc)
            self._save(state)
        except AidpProvisionConflict as exc:
            state.update(status='awaiting_action', message=str(exc))
            self._save(state)
        except Exception:
            state.update(status='error', message='This step failed. Its resources were preserved; resume the same operation after reviewing the native service.')
            self._save(state)
        return public_status(state)

    def _step(self, state, token, session):
        workspace, resources, phase = state['workspace'], state['resources'], state['phase']
        if phase == 'verify':
            self._ensure_governance_bucket()
            if not all((self.settings.agent_model_id, self.settings.objectstorage_namespace, self.settings.compartment_id)):
                raise AidpProvisionConflict('Configure the OCI model, compartment and artifacts storage before installation.')
            catalog = self._catalog(CATALOG_NAME)
            schemas = [row for row in self._list('/schemas', params={'catalogKey': catalog['key']}) if self._resource_name(row) == 'oci_artifacts']
            if len(schemas) != 1:
                raise AidpProvisionConflict('The existing oci_artifacts schema must be available; no other schema will be used.')
            resources.update(catalog_key=catalog['key'], schema_key=schemas[0]['key'], spark_key=self._shared_compute(workspace)['key'])
        elif phase == 'credentials':
            if self._shared_oci_credential() is None:
                raise AidpProvisionConflict('The shared AidpRuntime credential must be configured first.')
            existing = _project_credential(self)
            if existing and state['reuse_existing']:
                description = existing.get('credentialDescription') or existing.get('description') or ''
                if not description.startswith('AI Market Intelligence'):
                    raise AidpProvisionConflict('The existing Apify credential has unrelated ownership; it will not be changed.')
                reference = _credential_reference(existing, resources.get('apify_credential_key') or existing.get('key') or existing.get('id'), description)
            elif token:
                if not existing:
                    if resources.get('apify_create_requested'):
                        raise AidpProvisionConflict('The credential creation was submitted but is not visible. Reconcile it before resuming; another secret will not be created automatically.')
                    resources['apify_create_requested'] = True
                    self._save(state)
                reference = ensure_apify_credential(self, token, state['operation_id'], expected_key=resources.get('apify_credential_key', ''))
            else:
                raise AidpProvisionConflict('Enter an Apify token or explicitly reuse the existing project credential.')
            resources['apify_credential_key'] = reference['credential_key']
        elif phase.startswith('model_') or phase == 'cleanup':
            self._model_step(state)
        elif phase == 'compute':
            self._compute_step(state)
        elif phase == 'files':
            source, assets = agent_package(self._runtime_config(state))
            agents = self._agents(workspace, MODULE_ID)
            if len(agents) > 1 or agents and not state['reuse_existing']:
                raise AidpProvisionConflict('Existing project agent requires explicit reuse; no agent was replaced.')
            if agents:
                agent = self._request('GET', f"/workspaces/{workspace}/agents/{agents[0]['key']}")
                if agent.get('type') != 'CODE' or agent.get('entryFilePath') != ROOT + '/agent/market_intelligence.py':
                    raise AidpProvisionConflict('Existing agent has a different source binding; no files were changed.')
                if self._agent_deployments(f"/workspaces/{workspace}/agents/{agent['key']}/deployments"):
                    raise AidpProvisionConflict('An existing production deployment requires a separate reviewed update.')
            for name, content in assets.items():
                parent = (ROOT + '/agent/' + name).rsplit('/', 1)[0]
                self._ensure_folder(workspace, parent)
                self._upload_file(workspace, ROOT + '/agent/' + name, content)
            self._upload_file(workspace, ROOT + '/agent/market_intelligence.py', source)
        elif phase == 'agent':
            source, assets = agent_package(self._runtime_config(state))
            key, _ = self._ensure_agent(workspace, resources['agent_compute_key'], MODULE_ID, ROOT + '/agent', source,
                json.dumps({'module_id': MODULE_ID, 'pack_sha256': state['pack_sha256']}, sort_keys=True, separators=(',', ':')).encode(),
                repair_drift=True, expected_key=resources.get('agent_key', ''))
            resources['agent_key'] = key
        elif phase == 'permissions':
            for path, assignment in ((f"/workspaces/{workspace}/agents/{resources['agent_key']}", 'assignAgentPermissionDetails'),
                                     (f"/workspaces/{workspace}/clusters/{resources['agent_compute_key']}", 'assignClusterPermissionDetails')):
                self._ensure_role_permission(path, assignment, 'AI_DATA_PLATFORM_ADMIN', 'ADMIN')
        elif phase == 'test':
            self._test_step(state, session)
        elif phase == 'production':
            deployment, changed = self._ensure_agent_deployment(workspace, resources['agent_key'], resources['agent_compute_key'], MODULE_ID)
            if changed or (deployment.get('lifecycleState') or deployment.get('state')) != 'ACTIVE':
                raise AidpProvisionPending('Publishing the TEST-verified paused agent.', 'production')
            resources['deployment_key'] = deployment['key']
        return None

    def _runtime_config(self, state):
        return {**self._governance_oci_config(), 'region': self.settings.aidp_region,
                'model_endpoint': 'https://inference.generativeai.' + self.settings.aidp_region + '.oci.oraclecloud.com',
                'model_id': self.settings.agent_model_id, 'compartment_id': self.settings.compartment_id,
                'guardrails_config': {'policies': []}, 'tenant_id': self._oci_config['tenancy'],
                'generation_id': state['operation_id'], 'namespace': self.settings.objectstorage_namespace,
                'bucket': self.settings.artifacts_bucket_name, 'catalog': CATALOG_NAME, 'framework_schema': 'oci_artifacts',
                'gold_query_compute_id': state['resources']['spark_key'], 'budget_microusd': 0,
                'run_cap_microusd': 0, 'step_cap_microusd': 0, 'timezone': 'America/Bogota',
                'usage_cost_verified': False, 'research_enabled': False}

    def _compute_step(self, state):
        resources, operation = state['resources'], state['operation_id']
        if not resources.get('compute_creation_operation') and state['reuse_existing']:
            rows = [row for row in self._list(f"/workspaces/{state['workspace']}/clusters") if row.get('displayName') == 'aidp_ai_market_intelligence_compute']
            if len(rows) > 1:
                raise AidpProvisionConflict('Duplicate project compute names require reconciliation.')
            if rows:
                description = rows[0].get('description', '')
                prefix = 'AI Market Intelligence development compute; operation='
                if not description.startswith(prefix):
                    raise AidpProvisionConflict('The existing compute has unrelated ownership.')
                operation = str(UUID(description.removeprefix(prefix)))
                resources['agent_compute_key'] = rows[0]['key']
        resources.setdefault('compute_creation_operation', operation)
        self._save(state)
        result = prepare_compute(self, state['workspace'], resources['compute_creation_operation'], expected_key=resources.get('agent_compute_key', ''))
        resources['agent_compute_key'] = result['compute_key']
        self._save(state)
        if result['state'] == 'STOPPED':
            self._request('POST', f"/workspaces/{state['workspace']}/clusters/{result['compute_key']}/actions/start", payload={}, retry_scope='market-start:' + state['operation_id'])
        if result['state'] != 'ACTIVE':
            raise AidpProvisionPending('Waiting for the dedicated AI Compute.', 'compute')

    def _model_step(self, state):
        workspace, resources, phase = state['workspace'], state['resources'], state['phase']
        temp = ROOT + '/.setup_' + UUID(state['operation_id']).hex
        name = 'wf_ai_mi_setup_' + UUID(state['operation_id']).hex
        notebook_path = temp + '/model.ipynb'
        jobs_path = f'/workspaces/{workspace}/jobs'
        if phase == 'model_prepare':
            self._ensure_folder(workspace, temp)
            location = f'oci://{self.settings.artifacts_bucket_name}@{self.settings.objectstorage_namespace}/ai_market_intelligence/delta'
            self._upload_notebook(workspace, notebook_path, model_notebook(CATALOG_NAME, 'oci_artifacts', storage_root=location))
            payload = model_job(resources['spark_key'])
            payload.update(name=name, path=temp)
            payload['tasks'][0]['notebookPath'] = notebook_path
            key = resources.get('model_job_key') or self._job_key(workspace, name)
            if not key:
                key = self._create_job(workspace, payload)
            else:
                previous = self._request('GET', jobs_path + '/' + key, allow_not_found=True)
                if (not previous or previous.get('name') != name or previous.get('path') != temp
                        or any(task.get('notebookPath') != notebook_path for task in previous.get('tasks', []))
                        or any(cluster.get('clusterKey') != resources['spark_key'] for cluster in previous.get('jobClusters', []))):
                    raise AidpProvisionConflict('The existing bootstrap job changed ownership or binding; no definition was overwritten.')
            resources['model_job_key'] = key
            self._save(state)
            self._publish_job(workspace, key, payload, resources['spark_key'])
            return
        key = resources['model_job_key']
        job = self._request('GET', jobs_path + '/' + key, allow_not_found=True)
        if job and (job.get('name') != name or job.get('path') != temp):
            raise AidpProvisionConflict('Bootstrap job identity changed; no cleanup was performed.')
        if phase == 'model_run':
            runs = self._list(f'/workspaces/{workspace}/jobRuns', params={'jobKey': key})
            if len(runs) > 1:
                raise AidpProvisionConflict('Multiple bootstrap runs require reconciliation; no additional run was started.')
            if not runs and not resources.get('model_start_requested'):
                resources['model_start_requested'] = True
                self._save(state)
                run = self._request('POST', f'/workspaces/{workspace}/jobRuns', payload={'jobKey': key, 'parameters': [], 'queue': {'isEnabled': False}}, retry_scope='market-model:' + state['operation_id'])
                if run and run.get('key'):
                    resources['model_run_key'] = run['key']
                    self._save(state)
            elif runs:
                if runs[0].get('jobKey') != key:
                    raise AidpProvisionError('Unrelated bootstrap run returned.')
                resources['model_run_key'] = runs[0]['key']
            if not resources.get('model_run_key'):
                raise AidpProvisionPending('Reconciling the initial bootstrap start; another run will not be submitted.', 'model_run')
            return
        if phase == 'cleanup' and not job:
            if not resources.get('model_verified'):
                raise AidpProvisionError('Cleanup requires the verified empty-model receipt.')
            self._delete_workspace_path(workspace, temp, 'Removing the temporary bootstrap folder.')
            return
        if not job:
            raise AidpProvisionConflict('The recorded bootstrap job is missing before verification.')
        run = self._request('GET', f"/workspaces/{workspace}/jobRuns/{resources['model_run_key']}")
        if run.get('jobKey') != key:
            raise AidpProvisionError('Bootstrap run ownership changed.')
        status = self._governance_run_state(run)
        if status not in ('SUCCESS', 'SUCCEEDED'):
            if status in ('FAILED', 'ERROR', 'CANCELED', 'TIMED_OUT'):
                raise AidpProvisionConflict('The bootstrap failed; inspect its native task before resuming. No tables were dropped.')
            raise AidpProvisionPending('Waiting for the empty-model bootstrap.', phase)
        if phase == 'model_verify':
            task = (run.get('taskToTaskRunMap') or {}).get('create_market_model')
            if not isinstance(task, str):
                raise AidpProvisionPending('Waiting for the bootstrap task identifier.', phase)
            output = self._request('POST', f'/workspaces/{workspace}/taskRuns/{task}/actions/fetchOutput', payload={'outputKey': ''}, retry_scope='market-output:' + task)
            resources['model_verified'] = model_receipt(output)
        elif phase == 'cleanup':
            if not resources.get('model_verified'):
                raise AidpProvisionError('Cleanup requires the verified empty-model receipt.')
            if job:
                self._request('DELETE', jobs_path + '/' + key, allow_not_found=True)
                raise AidpProvisionPending('Removing the temporary bootstrap job.', phase)
            self._delete_workspace_path(workspace, temp, 'Removing the temporary bootstrap folder.')

    def _test_step(self, state, session_key):
        resources, workspace = state['resources'], state['workspace']
        client = MarketPlaygroundClient(self.settings)
        try:
            if not resources.get('test_intent'):
                source, assets = agent_package(self._runtime_config(state))
                def before_submit(intent):
                    resources['test_intent'] = intent
                    self._save(state)
                resources['test_receipt'] = client.refresh(workspace, resources['agent_key'], resources['agent_compute_key'], source, assets=assets, expected_workspace=workspace, on_submit=before_submit)
                self._save(state)
                raise AidpProvisionPending('Preparing the paused candidate in TEST.', 'test')
            rows = client.test_deployments(workspace, resources['agent_key'])
            if not rows:
                raise AidpProvisionConflict('The TEST submission is recorded but no deployment is visible. Review the native operation; another deployment will not be submitted automatically.')
            if rows[0].get('lifecycleState') != 'ACTIVE':
                raise AidpProvisionPending('Waiting for the TEST deployment. No production deployment has started.', 'test')
            sessions = client._list(f'/workspaces/{workspace}/agentFlowSessions', params={'agentFlowKey': [resources['agent_key']], 'deploymentType': 'TEST'})
            state['test_sessions'] = [{'value': row['key'], 'label': row.get('displayName') or row.get('name') or row['key']} for row in sessions if row.get('key')]
            if not session_key or session_key not in {row['value'] for row in state['test_sessions']}:
                raise AidpProvisionConflict('Open this agent in Workbench Playground, create a TEST session, then select it here and resume the connection check.')
            resources['test_verified'] = self._probe_test(rows[0]['endpointUrl'], session_key, state['operation_id'])
        finally:
            client.session.close()

    def _probe_test(self, endpoint, session, generation):
        url = urlsplit(endpoint)
        if url.scheme != 'https' or url.hostname != 'gateway.aidp.' + self.settings.aidp_region + '.oci.oraclecloud.com' or url.port or url.username or url.password or url.query or url.fragment:
            raise AidpProvisionError('Unexpected TEST endpoint returned by AIDP.')
        response = self.session.post(endpoint, auth=self.signer, stream=True, allow_redirects=False, timeout=(10, 60),
            json={'isStreamEnabled': True, 'input': [{'role': 'user', 'content': [{'type': 'INPUT_TEXT', 'text': 'Verify paused installation; do not research.'}]}],
                  'sessionId': session, 'session_id': session, 'metadata': {}},
            headers={'Accept': 'text/event-stream', 'x-session-id': session, 'x-deployment-type': 'TEST'})
        progress, result, size, completed = False, None, 0, False
        deadline = time.monotonic() + 120
        try:
            response.raise_for_status()
            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                raise AidpProvisionError('TEST did not return native SSE.')
            for raw in response.iter_lines():
                if time.monotonic() > deadline:
                    raise AidpProvisionError('TEST exceeded the connection verification window.')
                size += len(raw)
                if size > 2_000_000:
                    raise AidpProvisionError('TEST output exceeded the verification limit.')
                if not raw.startswith(b'data: {'):
                    continue
                event = json.loads(raw[6:])
                if event.get('error') or 'error' in str(event.get('type', '')).lower() or event.get('response', {}).get('error'):
                    raise AidpProvisionError('TEST returned a stream error.')
                if str(event.get('response', {}).get('status', '')).upper() in {'FAILED', 'ERROR', 'CANCELLED', 'CANCELED', 'INCOMPLETE'}:
                    raise AidpProvisionError('TEST ended without a successful completion.')
                for message in event.get('response', {}).get('output', []):
                    if str(message.get('status', '')).upper() in {'FAILED', 'ERROR', 'CANCELLED', 'CANCELED', 'INCOMPLETE'}:
                        raise AidpProvisionError('TEST ended without a successful completion.')
                    for part in message.get('content', []):
                        text = part.get('text', '') if part.get('type') == 'output_text' else ''
                        if text == 'Revisando la solicitud.':
                            progress = True
                        try:
                            value = json.loads(text)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(value, dict) and value.get('mode') == 'configured_paused' and progress:
                            result = value
                            completed = str(message.get('status', '')).upper() == 'COMPLETED'
            if (not completed or not result or result.get('generation_id') != generation or result.get('mcp_verified') is not True or result.get('research_enabled') is not False
                    or result.get('apify_starts') != 0 or result.get('model_calls') != 0 or result.get('streaming') != 'graph_milestones'):
                raise AidpProvisionError('The TEST response did not verify this paused installation revision.')
            return {'generation_id': generation, 'streaming_verified': True, 'research_enabled': False}
        finally:
            response.close()
