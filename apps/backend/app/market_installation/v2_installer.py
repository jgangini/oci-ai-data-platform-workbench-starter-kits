"""Independent, paused TEST installation; legacy research and tables are untouched."""
import asyncio
import hashlib
import json
import time
from urllib.parse import urlsplit

from app.aidp import AidpProvisionConflict, AidpProvisionError, AidpProvisionPending
from app.market_intelligence import MarketAidpClient
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.runtime_versions import RUNTIME_DISTRIBUTIONS
from .agent_package import agent_package
from .compute import V2_COMPUTE_NAME
from .playground import MarketPlaygroundClient, deployment_identity
from .v2_stream import V2GatewayStream


class V2ProbeStream(V2GatewayStream):
    def __init__(self):
        super().__init__(require_response=False)
        self.result = None
        self.completed_probe = False

    def event(self):
        packets = super().event()
        if self.lines and b'\n'.join(self.lines).strip() != b'[DONE]':
            event = json.loads(b'\n'.join(self.lines))
            response = event.get('response') or {}
            completed = event.get('type') == 'response.completed' or str(response.get('status', '')).lower() == 'completed'
            self.completed_probe = self.completed_probe or bool(self.result and completed)
        return packets

    def finish(self):
        super().finish()
        if not self.completed_probe:
            raise ValueError('Native probe has no completed response')

    def public_output(self, output):
        if not isinstance(output, list) or len(output) > 256:
            return super().public_output(output)
        for message in output:
            results = super().public_output([message])
            if results and str(message.get('status', '')).lower() == 'completed':
                self.completed_probe = True
        return []

    def public_text(self, text):
        if not isinstance(text, str):
            raise ValueError('Invalid native probe text')
        try:
            result = json.loads(text)
        except ValueError:
            return []
        if isinstance(result, dict) and result.get('mode') == 'v2_runtime_probe':
            if self.result is not None and result != self.result:
                raise ValueError('Native probe changed within one response')
            self.result = result
            return [result]
        return []


class MarketV2AidpClient(MarketAidpClient):
    # The development CLI owns a different root and may keep an active experiment.
    installation_root = '/Workspace/.control/modules/ai_market_intelligence_v2_installed'
    runtime_version = 'v2'
    compute_name = V2_COMPUTE_NAME
    _agent_name = 'ai_market_intelligence_v2_installed'

    def _runtime_config(self, state):
        return {'runtime_version': 'v2', 'research_enabled': False}

    def _descriptor(self, state):
        return {'module_id': 'ai_market_intelligence', 'runtime_version': 'v2',
                'operation_id': state['operation_id'], 'workspace': state['workspace'],
                'owner': state['owner'], 'pack_sha256': state['pack_sha256']}

    def _assert_candidate(self, state):
        try:
            super()._assert_candidate(state)
        except AidpProvisionConflict:
            if state['phase'] not in {'test', 'ready'} or not state['resources'].get('test_intent'):
                raise
            # Finish only the already-submitted immutable package, without replacing it.
            self._saved_test_package(state)

    def _saved_test_package(self, state):
        root = self.installation_root + '/agent'
        descriptor = self._workspace_json(state['workspace'], root + '/agent-manifest.json', 'Invalid v2 package ownership')
        if descriptor != self._descriptor(state):
            raise AidpProvisionConflict('The submitted v2 package ownership changed.')
        hashes = {}
        for name in ('market_intelligence.py', 'requirements.txt'):
            body, _ = self._workspace_object(state['workspace'], root + '/' + name, phase='content')
            if isinstance(body, str):
                body = body.encode('utf-8')
            if not isinstance(body, bytes):
                raise AidpProvisionConflict('The submitted v2 package cannot be read back exactly.')
            hashes[name] = hashlib.sha256(body).hexdigest()
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        resources = state['resources']
        if (digest != resources['test_intent'].get('package_sha256')
                or hashes['market_intelligence.py'] != resources.get('source_sha256')):
            raise AidpProvisionConflict('The submitted v2 package bytes changed; its candidate was not replaced.')

    def _step(self, state, token, session):
        phase = state['phase']
        if phase == 'verify':
            if not all((self.settings.agent_model_id, self.settings.objectstorage_namespace,
                        self.settings.compartment_id)):
                raise AidpProvisionConflict('Configure the OCI model, compartment and artifacts storage before installation.')
            self._ensure_governance_bucket()
        elif phase == 'files':
            self._files(state)
        elif phase == 'agent':
            self._agent(state)
        elif phase == 'test':
            self._test_step(state, session)
        elif phase in {'credentials', 'compute', 'permissions'}:
            return super()._step(state, token, session)
        else:
            raise AidpProvisionConflict('This phase does not belong to the v2 TEST installation.')

    def _files(self, state):
        workspace, resources = state['workspace'], state['resources']
        root = self.installation_root + '/agent'
        descriptor = self._descriptor(state)
        previous = self._workspace_json(workspace, root + '/agent-manifest.json', 'Invalid v2 package ownership')
        if previous is not None and previous != descriptor:
            raise AidpProvisionConflict('Existing v2 files belong to another installation; they were preserved.')
        agents = self._agents(workspace, self._agent_name)
        if len(agents) > 1 or agents and (previous != descriptor or resources.get('agent_key') != agents[0]['key']):
            raise AidpProvisionConflict('The existing v2 agent requires its own installation receipt; it was not adopted.')
        if agents and self._agent_deployments(f"/workspaces/{workspace}/agents/{agents[0]['key']}/deployments"):
            raise AidpProvisionConflict('An existing production deployment requires a separately reviewed update.')
        source, assets = agent_package(self._runtime_config(state), version='v2')
        if previous is None:
            for name in ('market_intelligence.py', *assets):
                body, headers = self._workspace_object(workspace, root + '/' + name, phase='content')
                if self._workspace_object_exists(body, headers):
                    raise AidpProvisionConflict('Unowned files already exist in the v2 folder; no source was overwritten.')
        # Claim only an empty folder, before the first source upload, so interrupted writes resume.
        self._upload_file(workspace, root + '/agent-manifest.json',
                          json.dumps(descriptor, sort_keys=True, separators=(',', ':')).encode(), repair_drift=False)
        for name, content in {'market_intelligence.py': source, **assets}.items():
            self._upload_file(workspace, root + '/' + name, content, repair_drift=False)
        resources['source_sha256'] = hashlib.sha256(source).hexdigest()

    def _agent(self, state):
        workspace, resources = state['workspace'], state['resources']
        root = self.installation_root + '/agent'
        descriptor = self._workspace_json(workspace, root + '/agent-manifest.json', 'Invalid v2 package ownership')
        if descriptor != self._descriptor(state):
            raise AidpProvisionConflict('The v2 package ownership changed before creating its agent.')
        expected = {'displayName': self._agent_name, 'type': 'CODE', 'pathInfo': root,
                    'description': 'AI Market Intelligence v2 paused TEST; operation=' + state['operation_id'],
                    'entryFilePath': root + '/market_intelligence.py',
                    'dependenciesFilePath': root + '/requirements.txt',
                    'computeKey': resources['agent_compute_key']}
        rows = self._agents(workspace, self._agent_name)
        if not rows:
            if resources.get('agent_create_requested'):
                raise AidpProvisionConflict('Agent creation was submitted but is not visible; reconcile it before another start.')
            resources['agent_create_requested'] = True
            self._save(state)
            self._request('POST', f'/workspaces/{workspace}/agents',
                          payload={**expected, 'sessionConfig': {'variables': {}}},
                          retry_scope='market-v2-agent:' + state['operation_id'])
            rows = self._agents(workspace, self._agent_name)
        if len(rows) != 1:
            raise AidpProvisionPending('Waiting for the recorded v2 agent identity.', 'agent')
        key = rows[0]['key']
        if resources.get('agent_key', key) != key or not resources.get('agent_create_requested'):
            raise AidpProvisionConflict('The v2 agent identity differs from this installation.')
        detail = self._request('GET', f'/workspaces/{workspace}/agents/{key}')
        if detail.get('key') != key or any(detail.get(field) != value for field, value in expected.items()):
            raise AidpProvisionConflict('The v2 source, compute or identity binding changed; no agent was updated.')
        resources['agent_key'] = key

    def _test_step(self, state, session_key):
        resources, workspace = state['resources'], state['workspace']
        client = MarketPlaygroundClient(self.settings)
        try:
            if not resources.get('test_intent'):
                source, assets = agent_package(self._runtime_config(state), version='v2')
                def submitted(intent):
                    resources['test_intent'] = intent
                    self._save(state)
                resources['test_receipt'] = client.refresh(workspace, resources['agent_key'],
                    resources['agent_compute_key'], source, assets=assets, expected_workspace=workspace,
                    agent_root=self.installation_root + '/agent', initialize=True, on_submit=submitted)
                self._save(state)
                raise AidpProvisionPending('Preparing the paused v2 candidate in TEST.', 'test')
            rows = client.test_deployments(workspace, resources['agent_key'])
            if not rows:
                raise AidpProvisionConflict('TEST was submitted but is not visible. Reconcile the recorded operation; no duplicate was submitted.')
            row = rows[0]
            if row.get('agentFlowComputeKey') != resources['agent_compute_key']:
                raise AidpProvisionConflict('The TEST compute differs from this installation.')
            previous = resources['test_intent'].get('previous_version')
            if row.get('lifecycleState') != 'ACTIVE' or previous is not None and row.get('deploymentVersion') == previous:
                raise AidpProvisionPending('Waiting for the exact v2 TEST revision.', 'test')
            identity = deployment_identity(row)
            sessions = client._list(f'/workspaces/{workspace}/agentFlowSessions',
                params={'agentFlowKey': [resources['agent_key']], 'deploymentType': 'TEST'})
            candidates = scoped_test_sessions(sessions, resources['agent_key'], resources['agent_compute_key'])
            state['test_sessions'] = [{'value': item['key'], 'label': item.get('displayName') or item['key']} for item in candidates]
            session_key = self._test_session(client, state, sessions, session_key)
            detail = client._request('GET', f'/workspaces/{workspace}/agentFlowSessions/{session_key}')
            if not bound_test_sessions([detail], resources['agent_key'], resources['agent_compute_key'], row['endpointUrl']):
                raise AidpProvisionConflict('The selected session does not match the exact TEST endpoint, agent and compute.')
            resources['test_verified'] = self._probe_v2(row['endpointUrl'], session_key, resources['source_sha256'])
            resources['test_verified']['deployment_identity'] = identity
            if client.test_deployments(workspace, resources['agent_key']) != rows:
                raise AidpProvisionConflict('TEST changed during the probe; this revision was not accepted.')
        finally:
            client.session.close()

    def _test_session(self, client, state, sessions, selected):
        resources, workspace = state['resources'], state['workspace']
        allowed = {item['value'] for item in state['test_sessions']}
        if selected:
            if selected not in allowed:
                raise AidpProvisionConflict('Select a TEST session bound to this v2 agent and compute.')
            return selected
        recorded = resources.get('test_session_key')
        if recorded:
            if recorded not in allowed:
                raise AidpProvisionConflict('The recorded TEST session is not visible with its exact binding; no new session was created.')
            return recorded
        label = self._agent_name + ' ' + state['operation_id']
        matches = [item for item in sessions if item.get('key') in allowed
                   and (item.get('displayName') or item.get('name')) == label]
        if resources.get('test_session_requested'):
            if len(matches) != 1:
                raise AidpProvisionConflict('Reconcile the submitted TEST session; no duplicate session was created.')
            resources['test_session_key'] = matches[0]['key']
            self._save(state)
            return matches[0]['key']
        if matches:
            raise AidpProvisionConflict('An unowned TEST session already uses this operation identity.')
        self._create_test_session(client, state, label)

    def _create_test_session(self, client, state, label):
        resources, workspace = state['resources'], state['workspace']
        resources['test_session_requested'] = True
        self._save(state)
        result = client._request('POST', f'/workspaces/{workspace}/agentFlowSessions',
            payload={'agentFlowKey': resources['agent_key'], 'agentFlowComputeKey': resources['agent_compute_key'],
                     'displayName': label, 'deploymentType': 'TEST'},
            retry_scope='market-v2-session:' + state['operation_id'])
        if isinstance(result, dict) and result.get('key'):
            resources['test_session_key'] = result['key']
            self._save(state)
        raise AidpProvisionPending('Verifying the recorded TEST session binding.', 'test')

    def _probe_v2(self, endpoint, session, source_sha256):
        validate_test_endpoint(endpoint, self.settings.aidp_region)
        response = self.session.post(endpoint, auth=self.signer, stream=True, allow_redirects=False, timeout=(10, 120),
            json={'isStreamEnabled': True, 'input': [{'role': 'user', 'content': [{'type': 'INPUT_TEXT', 'text': 'inspect_runtime'}]}],
                  'sessionId': session, 'session_id': session, 'metadata': {}},
            headers={'Accept': 'text/event-stream', 'x-session-id': session, 'x-deployment-type': 'TEST'})
        decoder = V2ProbeStream()
        deadline = time.monotonic() + 120
        try:
            response.raise_for_status()
            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                raise AidpProvisionError('TEST did not return native SSE.')
            for chunk in response.iter_content(chunk_size=8192):
                if time.monotonic() > deadline:
                    raise AidpProvisionError('TEST exceeded the verification window.')
                decoder.feed(chunk)
            decoder.finish()
            return v2_probe_receipt(decoder.result or {}, source_sha256)
        finally:
            response.close()

    async def agent_endpoint(self):
        def observe():
            workspace = str(self._workspace()['key'])
            state = self._read_installation(workspace)
            if not state or state['phase'] != 'ready':
                raise AidpProvisionConflict('Complete the v2 TEST installation before copying its endpoint.')
            client = MarketPlaygroundClient(self.settings)
            try:
                rows = client.test_deployments(workspace, state['resources']['agent_key'])
                verified = state['resources'].get('test_verified', {})
                if (len(rows) != 1 or rows[0].get('lifecycleState') != 'ACTIVE'
                        or rows[0].get('agentFlowComputeKey') != state['resources']['agent_compute_key']
                        or deployment_identity(rows[0]) != verified.get('deployment_identity')):
                    raise AidpProvisionConflict('The installed TEST revision changed; revalidate it before use.')
                validate_test_endpoint(rows[0]['endpointUrl'], self.settings.aidp_region)
                return rows[0]['endpointUrl']
            finally:
                client.session.close()
        return await asyncio.to_thread(observe)

    async def reinstall(self, *_args, **_kwargs):
        raise AidpProvisionConflict('V2 preserves its installation and evidence. Legacy table retirement cannot reinstall v2.')


def validate_test_endpoint(endpoint, region):
    url = urlsplit(endpoint)
    if (url.scheme != 'https' or url.hostname != 'gateway.aidp.' + region + '.oci.oraclecloud.com'
            or url.port or url.username or url.password or url.query or url.fragment):
        raise AidpProvisionError('Unexpected TEST endpoint returned by AIDP.')


def bound_test_sessions(sessions, agent_key, compute_key, endpoint):
    return [item for item in scoped_test_sessions(sessions, agent_key, compute_key) if item.get('endpointUrl') == endpoint]


def scoped_test_sessions(sessions, agent_key, compute_key):
    expected = {'agentFlowKey': agent_key, 'agentFlowComputeKey': compute_key}
    return [item for item in sessions if item.get('key') and item.get('deploymentType') in (None, 'TEST')
            and item.get('lifecycleState') in (None, 'ACTIVE')
            and all(item.get(key) == value for key, value in expected.items())]


def v2_probe_receipt(result, source_sha256):
    expected = {'source_sha256': source_sha256, 'runtime_version': 'v2', 'research_enabled': False,
                'business_tables_required': False, 'authenticated_session_bound': True,
                'model_calls': 0, 'apify_starts': 0}
    if any(type(result.get(key)) is not type(value) or result[key] != value for key, value in expected.items()):
        raise AidpProvisionError('TEST did not verify this paused v2 source and authenticated session.')
    versions = result.get('versions')
    if not isinstance(versions, dict) or not versions.get('python') or not isinstance(versions.get('packages'), dict):
        raise AidpProvisionError('TEST returned incomplete runtime observations.')
    if any(not versions['packages'].get(name) for name in RUNTIME_DISTRIBUTIONS):
        raise AidpProvisionError('TEST is missing required runtime packages.')
    return {'surface': 'TEST', 'source_sha256': source_sha256, 'research_enabled': False,
            'versions': versions, 'model_calls': 0, 'apify_starts': 0,
            'semantic_accepted': False, 'mcp_verified': False}
