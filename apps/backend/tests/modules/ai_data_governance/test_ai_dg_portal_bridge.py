from unittest.mock import Mock
import asyncio

import pytest

from app.services.oci.aidp import AidpClient, AidpProvisionConflict, AidpProvisionError
from tests.modules.ai_data_governance.test_governance_lifecycle import module_client, OPERATION_ID, USER_OCID


def test_candidate_manifest_is_marked_before_its_first_persisted_write():
    client = module_client()
    client._ensure_workspace_layout = Mock()
    client._write_module_manifest = Mock()
    result = client._start_governance_install('workspace', OPERATION_ID, 'install')
    assert result['runtime_family'] == 'AI_DATA_GOVERNANCE_TEST'
    assert result['validation_stage'] == 'TEST_PENDING'
    assert client._write_module_manifest.call_count == 1
    assert client._write_module_manifest.call_args.args[1]['runtime_family'] == 'AI_DATA_GOVERNANCE_TEST'


def test_candidate_deletion_is_disabled_without_native_requests():
    client = module_client()
    client._module_manifest = Mock(return_value={'runtime_family':'AI_DATA_GOVERNANCE_TEST'})
    client._request = Mock()
    with pytest.raises(AidpProvisionConflict, match='deletion is disabled'):
        client._delete_governance_module(USER_OCID, OPERATION_ID)
    client._request.assert_not_called()


def test_candidate_production_endpoint_is_blocked_before_a_deployment_read():
    client = module_client()
    client._module_manifest = Mock(return_value={'runtime_family':'AI_DATA_GOVERNANCE_TEST', 'status':'active'})
    client._request = Mock()
    with pytest.raises(AidpProvisionConflict, match='TEST candidate'):
        asyncio.run(client.governance_agent_endpoint())
    client._request.assert_not_called()




@pytest.mark.parametrize('filename', ['../escape.py', '/absolute.py', 'market_intelligence.py'])
def test_explicit_governance_filename_rejects_unapproved_entries_before_upload(filename):
    client = object.__new__(AidpClient)
    client._upload_file = Mock()
    with pytest.raises(AidpProvisionError, match='Unsupported Governance entry'):
        client._ensure_agent('workspace', 'compute', 'agent', '/Workspace/agent', b'code', b'{}',
                             repair_drift=False, entry_filename=filename)
    client._upload_file.assert_not_called()


@pytest.mark.parametrize('filename', ['governance_agent.py', 'documentation_agent.py'])
def test_explicit_governance_filename_preserves_owned_agent_without_a_native_update(filename):
    client = object.__new__(AidpClient)
    client._upload_file = Mock(return_value=False)
    client._agents = Mock(return_value=[{'key': 'existing', 'displayName': 'agent', 'pathInfo': '/Workspace/governance'}])
    client._request = Mock()
    assert client._ensure_agent('workspace', 'compute', 'agent', '/Workspace/governance', b'code', b'{}',
        repair_drift=False, expected_key='existing', entry_filename=filename) == ('existing', False)
    assert [call.args[1] for call in client._upload_file.call_args_list] == [
        '/Workspace/governance/' + filename, '/Workspace/governance/requirements.txt',
        '/Workspace/governance/agent-manifest.json']
    client._request.assert_not_called()






@pytest.mark.parametrize(('agents', 'expected_key'), [
    ([], 'owned'),
    ([{'key': 'other', 'pathInfo': '/Workspace/governance'}], 'owned'),
    ([{'key': 'owned', 'pathInfo': '/Workspace/foreign'}], 'owned'),
    ([{'key': 'owned'}], 'owned'),
    ([{'key': 'owned', 'pathInfo': '/Workspace/governance'}], ''),
    ([{'key': 'owned', 'pathInfo': '/Workspace/governance'}, {'key': 'other', 'pathInfo': '/Workspace/governance'}], 'owned'),
])
def test_agent_identity_and_root_mismatches_stop_before_all_source_writes(agents, expected_key):
    client = object.__new__(AidpClient)
    client._agents = Mock(return_value=agents)
    client._upload_file = Mock()
    client._request = Mock()
    with pytest.raises(AidpProvisionError):
        client._ensure_agent('workspace', 'compute', 'agent', '/Workspace/governance', b'code', b'{}',
            repair_drift=True, expected_key=expected_key)
    client._upload_file.assert_not_called()
    client._request.assert_not_called()


def test_new_agent_creation_keeps_its_source_and_native_identity_binding():
    client = object.__new__(AidpClient)
    client._agents = Mock(side_effect=[[], [{'key': 'created', 'displayName': 'agent', 'pathInfo': '/Workspace/governance'}]])
    client._upload_file = Mock(return_value=False)
    client._request = Mock()
    assert client._ensure_agent('workspace', 'compute', 'agent', '/Workspace/governance', b'code', b'{}',
        repair_drift=False) == ('created', True)
    assert client._upload_file.call_count == 3
    assert client._agents.call_count == 2
    client._request.assert_called_once()
    assert client._request.call_args.args[0] == 'POST'
    assert client._request.call_args.kwargs['payload']['pathInfo'] == '/Workspace/governance'


def test_owned_agent_source_update_reuses_the_verified_native_key():
    client = object.__new__(AidpClient)
    client._agents = Mock(return_value=[{'key': 'owned', 'displayName': 'agent', 'pathInfo': '/Workspace/governance'}])
    client._upload_file = Mock(return_value=True)
    client._request = Mock()
    assert client._ensure_agent('workspace', 'compute', 'agent', '/Workspace/governance', b'code', b'{}',
        repair_drift=True, expected_key='owned') == ('owned', True)
    client._agents.assert_called_once()
    client._request.assert_called_once()
    assert client._request.call_args.args[:2] == ('PUT', '/workspaces/workspace/agents/owned')
