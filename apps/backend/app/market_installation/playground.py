"""TEST-only lifecycle verified against the native Workbench client.

The GA deployment helper publishes PROD. This adapter uses the separate TEST
surface; it never supplies or accepts a production deployment type.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import PurePosixPath

from app.aidp import AidpClient, AidpProvisionConflict


class MarketPlaygroundClient(AidpClient):
    def __init__(self, settings):
        super().__init__(settings)
        self.base = self.base.replace('/20260430/aiDataPlatforms/', '/20240831/dataLakes/')

    def test_deployments(self, workspace, agent_key):
        rows = self._list(f'/workspaces/{workspace}/agentFlowDeployments',
            params={'agentFlowKey': [agent_key], 'deploymentType': 'TEST'})
        if any(row.get('deploymentType') != 'TEST' or row.get('agentFlowKey') != agent_key for row in rows):
            raise RuntimeError('TEST endpoint returned an out-of-scope deployment')
        if len(rows) > 1:
            # Oracle retains prior TEST records behind the same stable endpoint.
            if (len({row.get('endpointUrl') for row in rows}) != 1 or not rows[0].get('endpointUrl')
                    or len({row.get('agentFlowComputeKey') for row in rows}) != 1
                    or not rows[0].get('agentFlowComputeKey')):
                raise RuntimeError('Duplicate TEST deployments require reconciliation')
            try:
                dates = [datetime.fromisoformat(row['timeCreated'].replace('Z', '+00:00')) for row in rows]
                if any(date.tzinfo is None for date in dates) or len(set(dates)) != len(dates):
                    raise ValueError('Ambiguous TEST revision order')
                rows = [rows[max(range(len(rows)), key=dates.__getitem__)]]
            except (AttributeError, KeyError, TypeError, ValueError):
                raise RuntimeError('Duplicate TEST deployments require reconciliation') from None
        return rows

    def refresh(self, workspace, agent_key, compute_key, source, *, assets, expected_workspace, on_submit=None,
                agent_root='/Workspace/.control/modules/ai_market_intelligence/agent', initialize=False):
        if workspace != expected_workspace:
            raise RuntimeError('Playground workspace identity changed')
        if agent_root not in {'/Workspace/.control/modules/ai_market_intelligence/agent',
                              '/Workspace/.control/modules/ai_market_intelligence_v2_installed/agent'}:
            raise ValueError('Unknown Market Intelligence installation root')
        if not isinstance(source, bytes) or not source:
            raise ValueError('The exact saved candidate is required')
        candidate = _candidate(source, assets)
        # Verify through the GA surface rather than translating resource fields.
        ga = AidpClient(self.settings)
        try:
            if ga._workspace()['key'] != workspace:
                raise RuntimeError('Playground workspace identity changed')
            detail = ga._request('GET', f'/workspaces/{workspace}/agents/{agent_key}')
            entry = agent_root + '/market_intelligence.py'
            expected = {'type': 'CODE', 'entryFilePath': entry, 'pathInfo': agent_root,
                        'dependenciesFilePath': agent_root + '/requirements.txt', 'computeKey': compute_key}
            if any(detail.get(key) != value for key, value in expected.items()):
                raise RuntimeError('Playground candidate binding differs from the project agent')
            root = agent_root + '/'
            for name, content in candidate.items():
                body, _ = ga._workspace_object(workspace, root + name, phase='content')
                if not ga._content_matches(body, content):
                    raise RuntimeError('Saved native package differs from the candidate: ' + name)
        finally:
            ga.session.close()
        rows = self.test_deployments(workspace, agent_key)
        if not rows and not initialize:
            raise AidpProvisionConflict('Open this agent in Workbench Playground to initialize TEST, then resume. No deployment record was created by this check.')
        previous = rows[0].get('deploymentVersion') if rows else None
        payload = {'agentFlowKey': agent_key, 'agentFlowComputeKey': compute_key, 'deploymentType': 'TEST'}
        hashes = {name: hashlib.sha256(content).hexdigest() for name, content in candidate.items()}
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if on_submit:
            on_submit({'package_sha256': digest, 'previous_version': previous})
        path = (f'/workspaces/{workspace}/agentFlows/{agent_key}/deployments/actions/redeploy' if rows
                else f'/workspaces/{workspace}/actions/redeployAgentFlow')
        result, headers = self._request('POST', path, payload=payload, include_headers=True,
            phase='deployment', retry_scope='market-test:' + agent_key + ':' + compute_key + ':' + digest)
        return {'package_sha256': digest, 'previous_version': previous, 'compute_key': compute_key,
                'deployment_type': 'TEST', 'operation_key': next((value for key, value in headers.items()
                    if key.lower() == 'aidp-async-operation-key'), None),
                'accepted_state': result.get('lifecycleState') if isinstance(result, dict) else None}


def deployment_identity(row):
    """Native TEST can omit deploymentVersion; key and creation time still bind it."""
    if not isinstance(row.get('key'), str) or not row['key']:
        raise RuntimeError('TEST has no exact deployment identity')
    try:
        created = datetime.fromisoformat(row['timeCreated'].replace('Z', '+00:00'))
        if created.tzinfo is None:
            raise ValueError('Missing timezone')
    except (KeyError, AttributeError, TypeError, ValueError):
        raise RuntimeError('TEST has no unambiguous creation time') from None
    return {key: row.get(key) for key in ('key', 'timeCreated', 'timeUpdated', 'deploymentVersion')}


def _candidate(source, assets):
    if not isinstance(assets, dict) or not assets:
        raise ValueError('Declared dependency assets are required')
    candidate = {'market_intelligence.py': source}
    for name, content in assets.items():
        path = PurePosixPath(name)
        if (path.is_absolute() or '..' in path.parts or '\\' in name or str(path) != name
                or name == 'market_intelligence.py' or not isinstance(content, bytes)):
            raise ValueError('Invalid candidate asset path or bytes')
        candidate[name] = content
    return candidate
