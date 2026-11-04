from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai_data_governance.catalog_discovery import discover_source_scopes, pipeline_source_scopes
from app.ai_data_governance.inventory import identity
from app.ai_data_governance.workflow_receipts import execution_receipt_key
from test_ai_dg_documentary_hooks import Store
from test_ai_dg_workflow import manifest
from test_ai_dg_native_documentary import RUN,JOB


def test_discovery_includes_shared_artifacts_and_follows_native_pages():
    request=Mock(side_effect=[
        ({"items":[{"key":"oci_medallion"}]}, {"opc-next-page":"next"}),
        ({"items":[{"key":"oci_medallion.oci_artifacts"},{"key":"oci_medallion.oci_gold"}]}, {}),
        ({"items":[{"key":"default"}]}, {}),
        ({"items":[{"key":"default.default"}]}, {}),
    ])
    scopes=discover_source_scopes(request,check_active=Mock())
    assert [s['schema_key'] for s in scopes]==['default.default','oci_medallion.oci_artifacts','oci_medallion.oci_gold']
    assert request.call_args_list[2].kwargs['params']['page']=='next'


@pytest.mark.parametrize("responses",[
    [({}, {})],
    [({"items":[{"key":"c"},{"key":"c"}]},{}), ({"items":[]},{})],
    [({"items":[{"key":"c"}]},{}), ({"items":[{"key":"other.s"}]},{})],
    [({"items":[{"key":"c"}]},{}), ({"items":[{"key":"c.s"}]*2}, {})],
    [({"items":[{"key":"c"}]},{}), ({"items":[{"key":"c.s"+str(i)} for i in range(33)]}, {})],
    [({"items":[]},{})],
])
def test_incomplete_ambiguous_or_unbounded_discovery_cannot_start_a_run(responses):
    with pytest.raises(ValueError):
        discover_source_scopes(Mock(side_effect=responses),check_active=Mock())


def test_discovery_repeated_cursor_is_not_silently_complete():
    def request(method,path,*,params):
        if path=='/schemas':return {'items':[]},{}
        return {'items':[]},{'opc-next-page':'same'}
    with pytest.raises(ValueError,match='cursor'):
        discover_source_scopes(request,check_active=Mock())


def test_effects_and_inventory_repair_use_frozen_scope_without_rediscovery():
    journal=Store()
    scope_id=identity('AI_DG_SCOPE','platform','workspace','cat','cat.schema')
    frozen=manifest();frozen['scopes'][0]['scope_id']=scope_id
    journal.create('executions',execution_receipt_key(RUN),frozen)
    scope={'scope_id':scope_id,'platform_key':'platform','workspace_key':'workspace','catalog_key':'cat','schema_key':'cat.schema','enabled':True}
    repository=SimpleNamespace(read_by_keys=Mock(return_value=[scope]))
    request=Mock(side_effect=AssertionError('Frozen phase must not rediscover catalogs'))
    binding={'platform_id':'platform','workspace_key':'workspace','job_key':JOB}
    for phase in ('sync_inventory','publish_metadata','replicate_model'):
        assert pipeline_source_scopes(repository,journal,request,binding,SimpleNamespace(job_run_id=RUN,task_key=phase),check_active=Mock())==[{'catalog_key':'cat','schema_key':'cat.schema'}]
    scope['enabled']=False
    with pytest.raises(ValueError):
        pipeline_source_scopes(repository,journal,request,binding,SimpleNamespace(job_run_id=RUN,task_key='replicate_model'),check_active=Mock())
    request.assert_not_called()
