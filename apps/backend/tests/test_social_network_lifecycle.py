import copy
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.aidp import AidpProvisionConflict, AidpProvisionError, AidpProvisionPending
from app.config import Settings
from app.social_network import SocialAidpClient


@pytest.fixture
def client():
    client = object.__new__(SocialAidpClient)
    client.settings = Settings()
    client._social_config = {'namespace':'namespace','source_bucket':'landing','source_prefix':'social/',
        'gold_bucket':'gold','artifacts_bucket':'artifacts','catalog':'oci_medallion','gold_schema':'oci_gold',
        'control_schema':'oci_artifacts','credential_name':'AidpRuntime','identity_sha256':'a'*64}
    client._upload_notebook = Mock(return_value=False)
    return client


def test_social_job_contains_five_minute_native_schedule_and_complete_serial_dag(client):
    payload,_ = client._governance_job_payload('workspace','spark',paused=False)
    assert payload['name'] == 'wf_ai_social_network_ingest'
    assert payload['schedule'] == {'quartzCronExpression':'0 0/5 * * * ?','timezoneId':'UTC','pauseStatus':'UNPAUSED'}
    assert payload['continuous'] is None and payload['queue'] == {'isEnabled':False}
    assert payload['maxConcurrentRuns'] == 1
    assert payload['tasks'][0]['cluster'] == {'clusterKey':'spark'}
    notebook = client._upload_notebook.call_args.args[2]
    code = ''.join(notebook['cells'][0]['source'])
    assert "'source_bucket': 'landing'" in code
    assert 'private_key =' not in code and 'CONFIG = {}' not in code
    assert client._agent_compute_name != 'aidp_ai_data_governance_compute'
    paused,_ = client._governance_job_payload('workspace','spark',paused=True)
    assert paused['schedule']['pauseStatus'] == 'PAUSED'


def test_existing_manifest_identity_is_not_adopted_under_social_root(client):
    client._workspace_json = Mock(return_value={'module_id':'ai_data_governance'})
    with pytest.raises(AidpProvisionError):
        client._module_manifest('workspace')


def test_failed_sync_resumes_same_operation_with_a_new_native_run_attempt(client):
    client._write_module_manifest = Mock()
    manifest = {'status':'error','phase':'sync','operation':{'operation_id':'operation','type':'install'}}
    client._resume_governance_operation('workspace',manifest,'operation','install')
    assert manifest['phase'] == 'control' and manifest['status'] == 'installing'
    assert manifest['attempt'] == 1
    wrong = {'status':'error','phase':'sync','operation':{'operation_id':'other','type':'install'}}
    with pytest.raises(AidpProvisionConflict):
        client._resume_governance_operation('workspace',wrong,'operation','install')


def test_native_agent_runtime_has_no_landing_or_control_credentials_configuration(client):
    client.settings = Settings(agent_model_id='selected',aidp_region='us-chicago-1',compartment_id='compartment')
    client._workspace = Mock(return_value={'key':'workspace'})
    client._module_manifest = Mock(return_value={'resources':{'gold_query_compute_id':'spark-query'}})
    source, descriptor, _ = client._governance_agent_artifacts()
    first_config = next(line for line in source.decode().splitlines() if line.startswith('CONFIG ='))
    assert 'landing' not in first_config and 'artifacts' not in first_config and 'source_prefix' not in first_config
    assert 'spark-query' in first_config and 'AI_LAKEHOUSE' in descriptor.decode()
    import json
    assert client._content_matches(json.loads(descriptor), descriptor), 'Native JSON readback must round-trip to the emitted descriptor'
    assert b'AsyncProxyCheckpointClient' in source and b'flow_setup' in source


def catalog_detail(client, description=""):
    return {"key":"table-key","displayName":"ai_social_current","catalogKey":"catalog-key","schemaKey":"schema-key",
        "tableType":"EXTERNAL","description":description,"location":"oci://gold@namespace/ai_social_network/gold/ai_social_current",
        "externalTableDefinition":{"objectStorageLocationPath":"oci://gold@namespace/ai_social_network/gold/ai_social_current","externalTableDataFormat":"DELTA"}}


def test_catalog_description_requires_exact_identity_location_and_preserves_user_metadata(client):
    original = catalog_detail(client)
    described = {**original, "description":"Canonical description"}
    client._request = Mock(side_effect=[(original,{"etag":"v1"}),{},described])
    client._describe_catalog_table("catalog-key","schema-key","ai_social_current","table-key","Canonical description")
    assert client._request.call_args_list[1].kwargs == {"payload":{"description":"Canonical description"},"headers":{"If-Match":"v1"},"phase":"metadata"}
    assert client._request.call_args_list[1].args == ("PUT","/tables/table-key")
    custom = catalog_detail(client,"Operator description")
    client._request = Mock(return_value=(custom,{}))
    client._describe_catalog_table("catalog-key","schema-key","ai_social_current","table-key","Canonical description")
    assert client._request.call_count == 1
    client._request = Mock(return_value=({**original,"location":"oci://other@namespace/other"},{}))
    with pytest.raises(AidpProvisionError):
        client._describe_catalog_table("catalog-key","schema-key","ai_social_current","table-key","Canonical description")
    assert client._request.call_count == 1


def test_catalog_metadata_publishes_all_scoped_descriptions_before_waiting_for_visibility(client):
    from app.legacy_labs.ai_social_network.notebooks.social_ingest import GOLD_TABLES, TABLE_DESCRIPTIONS
    client._catalog = Mock(return_value={"key":"catalog-key"})
    client._list = Mock(return_value=[{"key":"gold-key","displayName":"oci_gold"},{"key":"control-key","displayName":"oci_artifacts"}])
    client._schema_tables = Mock(side_effect=lambda catalog,schema:[{"key":name,"displayName":name} for name in TABLE_DESCRIPTIONS if (name in GOLD_TABLES)==(schema=="gold-key")])
    client._describe_catalog_table = Mock(side_effect=[False] + [True] * (len(TABLE_DESCRIPTIONS) - 1))
    with pytest.raises(AidpProvisionPending):
        client._catalog_descriptions()
    assert {call.args[2] for call in client._describe_catalog_table.call_args_list} == set(TABLE_DESCRIPTIONS)
