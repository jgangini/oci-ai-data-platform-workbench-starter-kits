"""TEST-only lifecycle verified against the native Workbench client.

The GA deployment helper publishes PROD. This adapter uses the separate TEST
surface; it never supplies or accepts a production deployment type.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import PurePosixPath

from app.aidp import AidpClient


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

    def refresh(self, workspace, agent_key, compute_key, source, *, assets, expected_workspace, on_submit=None):
        if workspace != expected_workspace:
            raise RuntimeError('Playground workspace identity changed')
        if not isinstance(source, bytes) or not source:
            raise ValueError('The exact saved candidate is required')
        candidate = _candidate(source, assets)
        # Verify through the GA surface rather than translating resource fields.
        ga = AidpClient(self.settings)
        try:
            if ga._workspace()['key'] != workspace:
                raise RuntimeError('Playground workspace identity changed')
            detail = ga._request('GET', f'/workspaces/{workspace}/agents/{agent_key}')
            entry = '/Workspace/.control/modules/ai_market_intelligence/agent/market_intelligence.py'
            if detail.get('type') != 'CODE' or detail.get('entryFilePath') != entry or detail.get('computeKey') != compute_key:
                raise RuntimeError('Playground candidate binding differs from the project agent')
            root = '/Workspace/.control/modules/ai_market_intelligence/agent/'
            for name, content in candidate.items():
                body, _ = ga._workspace_object(workspace, root + name, phase='content')
                if not ga._content_matches(body, content):
                    raise RuntimeError('Saved native package differs from the candidate: ' + name)
        finally:
            ga.session.close()
        rows = self.test_deployments(workspace, agent_key)
        previous = rows[0].get('deploymentVersion') if rows else None
        payload = {'agentFlowKey': agent_key, 'agentFlowComputeKey': compute_key, 'deploymentType': 'TEST'}
        hashes = {name: hashlib.sha256(content).hexdigest() for name, content in candidate.items()}
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if on_submit:
            on_submit({'package_sha256': digest, 'previous_version': previous})
        if rows:
            path = f'/workspaces/{workspace}/agentFlows/{agent_key}/deployments/actions/redeploy'
            result, headers = self._request('POST', path, payload=payload, include_headers=True,
                phase='deployment', retry_scope='market-test:' + agent_key + ':' + compute_key + ':' + digest)
        else:
            result, headers = self._request('PUT', f'/workspaces/{workspace}/agentFlowDeployments',
                                            payload=payload, include_headers=True, phase='deployment')
        return {'package_sha256': digest, 'previous_version': previous, 'compute_key': compute_key,
                'deployment_type': 'TEST', 'operation_key': next((value for key, value in headers.items()
                    if key.lower() == 'aidp-async-operation-key'), None),
                'accepted_state': result.get('lifecycleState') if isinstance(result, dict) else None}


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
