from types import SimpleNamespace

import pytest

from app.market_installation.compute import compute_payload, prepare_compute


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
