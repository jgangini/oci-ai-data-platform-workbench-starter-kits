from types import SimpleNamespace

import pytest

from app.market_installation.playground import MarketPlaygroundClient


def test_test_lookup_rejects_wrong_agent_or_production_deployments():
    client = MarketPlaygroundClient.__new__(MarketPlaygroundClient)
    client._list = lambda *args, **kwargs: [{'agentFlowKey': 'agent', 'deploymentType': 'PROD'}]
    with pytest.raises(RuntimeError, match='out-of-scope'):
        client.test_deployments('workspace', 'agent')


def test_refresh_uses_only_test_payload_and_verified_entry(monkeypatch):
    calls = []
    source = b'approved readable source'
    ga = SimpleNamespace(_workspace=lambda: {'key': 'workspace'}, _request=lambda *a: {'type': 'CODE', 'computeKey': 'compute',
        'entryFilePath': '/Workspace/.control/modules/ai_market_intelligence/agent/market_intelligence.py'},
        _workspace_object=lambda workspace, path, **k: (b'dependency' if path.endswith('requirements.txt') else source, {}),
        _content_matches=lambda actual, expected: actual == expected,
        session=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr('app.market_installation.playground.AidpClient', lambda settings: ga)
    client = MarketPlaygroundClient.__new__(MarketPlaygroundClient)
    client.settings = object()
    client._workspace = lambda: {'key': 'workspace'}
    client._list = lambda *a, **k: [{'agentFlowKey': 'agent', 'deploymentType': 'TEST', 'deploymentVersion': 'old'}]

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs['payload']))
        return {'lifecycleState': 'CREATING'}, {'aidp-async-operation-key': 'operation'}

    client._request = request
    result = client.refresh('workspace', 'agent', 'compute', source,
                            assets={'requirements.txt': b'dependency'}, expected_workspace='workspace')
    assert result['deployment_type'] == 'TEST' and result['previous_version'] == 'old'
    assert calls[0][0] == 'POST' and calls[0][2] == {
        'agentFlowKey': 'agent', 'agentFlowComputeKey': 'compute', 'deploymentType': 'TEST'}


def test_refresh_refuses_other_workspace_before_any_request():
    client = MarketPlaygroundClient.__new__(MarketPlaygroundClient)
    client._workspace = lambda: pytest.fail('No lookup is needed for a mismatched target')
    with pytest.raises(RuntimeError, match='workspace identity'):
        client.refresh('other', 'agent', 'compute', b'source', assets={}, expected_workspace='workspace')


def test_test_revision_history_selects_newest_only_for_same_endpoint_and_compute():
    client = MarketPlaygroundClient.__new__(MarketPlaygroundClient)
    old = {'agentFlowKey': 'agent', 'deploymentType': 'TEST', 'endpointUrl': 'https://example.test/chat',
           'agentFlowComputeKey': 'compute', 'timeCreated': '2026-10-31T05:00:00Z', 'key': 'old'}
    new = {**old, 'key': 'new', 'timeCreated': '2026-10-31T06:00:00Z'}
    client._list = lambda *a, **k: [old, new]
    assert client.test_deployments('workspace', 'agent') == [new]
    for changed in ({'endpointUrl': 'https://other.test/chat'}, {'agentFlowComputeKey': 'other'},
                    {'timeCreated': old['timeCreated']}, {'timeCreated': None}):
        client._list = lambda *a, **k: [old, {**new, **changed}]
        with pytest.raises(RuntimeError, match='reconciliation'):
            client.test_deployments('workspace', 'agent')
