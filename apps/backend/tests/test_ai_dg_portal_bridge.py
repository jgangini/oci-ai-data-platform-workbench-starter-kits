from unittest.mock import Mock
import asyncio

import pytest

from app.aidp import AidpClient, AidpProvisionConflict, AidpProvisionError
from app.config import _governance_runtime_mode
from app.market_intelligence import MarketAidpClient
from test_governance_lifecycle import module_client, OPERATION_ID, USER_OCID


def test_candidate_manifest_is_marked_before_its_first_persisted_write():
    client = module_client()
    client._ensure_workspace_layout = Mock()
    client._write_module_manifest = Mock()
    result = client._start_governance_install('workspace', OPERATION_ID, 'install', test_candidate=True)
    assert result['runtime_family'] == 'AI_DG_V2_TEST'
    assert result['validation_stage'] == 'TEST_PENDING'
    assert client._write_module_manifest.call_count == 1
    assert client._write_module_manifest.call_args.args[1]['runtime_family'] == 'AI_DG_V2_TEST'


def test_candidate_deletion_cannot_enter_the_legacy_cleanup_path():
    client = module_client()
    client._module_manifest = Mock(return_value={'runtime_family':'AI_DG_V2_TEST'})
    client._prepare_governance_deletion = Mock()
    client._request = Mock()
    with pytest.raises(AidpProvisionConflict, match='legacy deletion is disabled'):
        client._delete_governance_module(USER_OCID, OPERATION_ID)
    client._prepare_governance_deletion.assert_not_called()
    client._request.assert_not_called()


def test_candidate_production_endpoint_is_blocked_before_a_deployment_read():
    client = module_client()
    client._module_manifest = Mock(return_value={'runtime_family':'AI_DG_V2_TEST', 'status':'active'})
    client._request = Mock()
    with pytest.raises(AidpProvisionConflict, match='TEST candidate'):
        asyncio.run(client.governance_agent_endpoint())
    client._request.assert_not_called()


def test_market_agent_default_filename_is_preserved_without_a_governance_override():
    client = object.__new__(MarketAidpClient)
    client._upload_file = Mock(return_value=False)
    client._agents = Mock(return_value=[])
    with pytest.raises(AidpProvisionError, match='recorded governance Agent identity'):
        client._ensure_agent('workspace', 'compute', 'market', '/Workspace/market', b'code', b'{}',
                             repair_drift=False, expected_key='existing')
    assert client._upload_file.call_args_list[0].args[1] == '/Workspace/market/market_intelligence.py'


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
    client._agents = Mock(return_value=[{'key': 'existing', 'displayName': 'agent'}])
    client._request = Mock()
    assert client._ensure_agent('workspace', 'compute', 'agent', '/Workspace/governance', b'code', b'{}',
        repair_drift=False, expected_key='existing', entry_filename=filename) == ('existing', False)
    assert [call.args[1] for call in client._upload_file.call_args_list] == [
        '/Workspace/governance/' + filename, '/Workspace/governance/requirements.txt',
        '/Workspace/governance/agent-manifest.json']
    client._request.assert_not_called()


@pytest.mark.parametrize('mode', ['legacy', 'v2_test'])
def test_known_runtime_mode(mode):
    assert _governance_runtime_mode(mode) == mode


def test_invalid_runtime_mode_is_not_silently_promoted_to_production():
    with pytest.raises(ValueError, match='legacy or v2_test'):
        _governance_runtime_mode('v2_production')
