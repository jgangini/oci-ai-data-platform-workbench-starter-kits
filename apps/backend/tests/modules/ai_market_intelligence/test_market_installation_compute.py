from types import SimpleNamespace

import pytest

from app.modules.ai_market_intelligence.installation.compute import compute_payload, prepare_compute
from app.services.oci.contracts import AidpProvisionError, AidpProvisionPending


OPERATION = '00000000-0000-4000-8000-000000000001'


def fake(existing=None):
    rows = [existing] if existing else []
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if method == 'POST':
            rows.append({**kwargs['payload'], 'key': 'new-market-key', 'state': 'ACTIVE'})
        return rows[0] if rows else None

    return SimpleNamespace(_workspace=lambda: {'key': 'workspace'}, _list=lambda path, **kwargs: rows,
                           _request=request), calls


def test_compute_is_dedicated_bounded_and_reused_by_operation():
    api, calls = fake()
    prepared = prepare_compute(api, 'workspace', OPERATION)
    assert prepared['compute_key'] == 'new-market-key'
    assert prepare_compute(api, 'workspace', OPERATION, expected_key='new-market-key') == prepared
    assert [method for method, _ in calls].count('POST') == 1
    assert all(method != 'DELETE' for method, _ in calls)
    assert compute_payload(OPERATION)['replicaConfig'] == {'minReplica': 1, 'maxReplica': 1}


def test_unrelated_same_named_compute_is_not_adopted():
    api, calls = fake({**compute_payload(OPERATION), 'key': 'someone-elses-compute',
                       'description': 'unrelated', 'state': 'ACTIVE'})
    with pytest.raises(RuntimeError, match='outside this project'):
        prepare_compute(api, 'workspace', OPERATION)
    assert all(method != 'POST' for method, _ in calls)


def test_workspace_mismatch_prevents_creation():
    api, calls = fake()
    with pytest.raises(RuntimeError, match='workspace identity'):
        prepare_compute(api, 'different-workspace', OPERATION)
    assert calls == []


@pytest.mark.parametrize('fields, expected_state', [
    ({'state': 'CREATING'}, 'CREATING'),
    ({'state': 'UPDATING'}, 'UPDATING'),
    ({'state': 'starting'}, 'STARTING'),
    ({'lifecycleState': 'CREATING'}, 'CREATING'),
    ({'state': 'STARTING', 'lifecycleState': 'ACTIVE'}, 'ACTIVE'),
    ({}, ''),
])
def test_validated_compute_preserves_key_while_native_state_is_pending(fields, expected_state):
    row = {**compute_payload(OPERATION), 'key': 'current', **fields}
    api, calls = fake(row)
    assert prepare_compute(api, 'workspace', OPERATION, expected_key='current') == {
        'compute_key': 'current', 'state': expected_state,
        'name': compute_payload(OPERATION)['displayName'],
    }
    assert calls == [('GET', '/workspaces/workspace/clusters/current')]


@pytest.mark.parametrize('state', ['FAILED', 'DELETING', 'DELETED', 'DELETE_FAILED', 'CANCELED', 'CANCELLED', 'START_FAILED'])
def test_terminal_compute_is_rejected_without_replacement(state):
    row = {**compute_payload(OPERATION), 'key': 'current', 'lifecycleState': state, 'state': 'CREATING'}
    api, calls = fake(row)
    with pytest.raises(AidpProvisionError, match='terminal state ' + state):
        prepare_compute(api, 'workspace', OPERATION, expected_key='current')
    assert calls == [('GET', '/workspaces/workspace/clusters/current')]


def test_creation_without_returned_key_waits_and_recovers_same_compute():
    rows, calls = [], []
    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == 'POST':
            rows.append({**kwargs['payload'], 'key': 'current', 'state': 'CREATING'})
            return {}
        assert path == '/workspaces/workspace/clusters/current'
        return rows[0]
    api = SimpleNamespace(_workspace=lambda: {'key': 'workspace'}, _list=lambda *a, **k: rows,
                          _request=request)
    with pytest.raises(AidpProvisionPending, match='same operation identity'):
        prepare_compute(api, 'workspace', OPERATION)
    pending = prepare_compute(api, 'workspace', OPERATION)
    assert pending['compute_key'] == 'current' and pending['state'] == 'CREATING'
    rows[0]['state'] = 'ACTIVE'
    assert prepare_compute(api, 'workspace', OPERATION, expected_key=pending['compute_key'])['state'] == 'ACTIVE'
    posts = [call for call in calls if call[0] == 'POST']
    assert len(posts) == 1 and posts[0][2]['retry_scope'] == 'ai-market-compute:' + OPERATION
    assert all(call[0] in {'GET', 'POST'} for call in calls)


def test_compute_shape_is_still_checked_before_waiting():
    row = {**compute_payload(OPERATION), 'key': 'current', 'state': 'UPDATING',
           'driverConfig': {'driverShapeConfig': {'ocpus': 2, 'memoryInGBs': 16}}}
    api, calls = fake(row)
    with pytest.raises(RuntimeError, match='shape differs'):
        prepare_compute(api, 'workspace', OPERATION, expected_key='current')
    assert calls == [('GET', '/workspaces/workspace/clusters/current')]
