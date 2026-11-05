import ast
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.modules.ai_data_governance import central_sql
from app.modules.ai_data_governance.activity_commit import ActivityCommitter, ActivityPending, activity_receipt
from app.modules.ai_data_governance.activity_transport import run_native_activity
from tests.support.ai_dg_fixtures import MemoryJournal, activity_fixture


@pytest.mark.parametrize('cleanup_fails', [False, True])
@pytest.mark.parametrize('command_fails', [False, True])
def test_completed_activity_and_cleanup_are_distinct_boundaries(monkeypatch, cleanup_fails, command_fails):
    binding, _, batch = activity_fixture()
    binding['query_compute_key'] = 'compute'
    signer, check = object(), Mock()
    sdk = Mock()
    sdk.create_execution_context.return_value = SimpleNamespace(data=SimpleNamespace(id='owned'))
    if cleanup_fails:
        sdk.delete_context.side_effect = RuntimeError('recorded cleanup failure')
    monkeypatch.setattr(central_sql, 'runtime_sdk_config', Mock(return_value={'region': binding['region']}))
    monkeypatch.setenv('AIDP_GATEWAY_ENDPOINT', 'https://gateway.aidp.us-chicago-1.oci.oraclecloud.com')
    monkeypatch.setitem(sys.modules, 'aidputils', SimpleNamespace(secrets=SimpleNamespace(get=Mock())))
    monkeypatch.setitem(sys.modules, 'oci.dataflowdp', SimpleNamespace(DataflowdpClient=Mock(return_value=sdk)))
    monkeypatch.setitem(sys.modules, 'oci.retry', SimpleNamespace(NoneRetryStrategy=lambda: 'no-retry'))
    observed = {'commands': [], 'persisted': None, 'command_fails':command_fails}
    diagnostics = []
    def diagnose(stage, transition, error=None):
        diagnostics.append((stage,transition,type(error).__name__ if error else None))

    class RecordedNativeClient:
        def __init__(self, **kwargs):
            assert kwargs['signer'] is signer
            self.client = kwargs['oci_sdk_client']

        def run_command(self, **kwargs):
            assert kwargs['cleanup_context'] is True
            assert kwargs['cluster_id'] == 'compute' and kwargs['language'] == 'PYTHON'
            self.client.create_execution_context('details', 'compute')
            # Decode only the literal data emitted by the real fixed-program adapter.
            payload = kwargs['command'].split('activity_input = json.loads(', 1)[1].split(')\n', 1)[0]
            command = json.loads(ast.literal_eval(payload))
            mode = command['mode']
            observed['commands'].append(mode)
            if mode == 'APPEND_ACTIVITY':
                observed['persisted'] = command['payload']
            else:
                assert mode == 'VERIFY_ACTIVITY'
                assert observed['persisted'] == command['payload']
            if observed['command_fails']:
                raise RuntimeError('recorded command failure')
            result = activity_receipt(command['payload'])
            return {'status': 'SUCCEEDED', 'outputs': [{'type': 'TEXT_PLAIN', 'value': json.dumps(result)}]}

    monkeypatch.setitem(sys.modules, 'aidputils.agents.tools.dataflow.client',
        SimpleNamespace(DataflowCommandClient=RecordedNativeClient))
    journal = MemoryJournal()
    committer = ActivityCommitter(journal,
        lambda mode, value: run_native_activity(binding, signer, 'fixed-program', mode, value, check,diagnose=diagnose),
        check,diagnose=diagnose)
    if cleanup_fails or command_fails:
        with pytest.raises(ActivityPending):
            committer.commit(batch)
        assert observed['persisted'] == batch
        assert not any(kind in {'materialized', 'responses'} for kind, _ in journal.values)
        assert observed['commands'] == ['APPEND_ACTIVITY']
        sdk.base_client.session.close.assert_called_once()
        failures=[stage for stage,transition,_ in diagnostics if transition=='failed']
        assert failures[0] == ('command' if command_fails else 'cleanup')
        assert ('cleanup' in failures) is cleanup_fails
        assert (('command','completed',None) in diagnostics) is (not command_fails)
        assert (('cleanup','completed',None) in diagnostics) is (not cleanup_fails)
        # Explicit recovery reuses the frozen intent; it never retries the append.
        sdk.delete_context.side_effect = None
        observed['command_fails'] = False
        assert committer.commit(batch) == activity_receipt(batch)
        assert observed['commands'] == ['APPEND_ACTIVITY', 'VERIFY_ACTIVITY']
    else:
        assert committer.commit(batch) == activity_receipt(batch)
        assert observed['commands'] == ['APPEND_ACTIVITY']
    assert observed['commands'].count('APPEND_ACTIVITY') == 1
    assert sum(kind == 'materialized' for kind, _ in journal.values) == 1
