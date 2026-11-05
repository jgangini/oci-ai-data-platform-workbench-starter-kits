import ast
import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.ai_data_governance.governance_source import governance_agent_source, governance_entry_source
from app.modules.ai_data_governance.native_governance import consultation_config
from tests.support.ai_dg_fixtures import binding


def test_consultative_package_is_readable_deterministic_and_loads_without_private_credentials(monkeypatch):
    source = governance_agent_source(binding())
    assert source == governance_agent_source(binding())
    for forbidden in (b"-----BEGIN", b"ObjectStorageClient", b"exec("):
        assert forbidden not in source
    assert b"native_central_transport" in source and b"native_caller_transport" not in source
    assert b"CustomRemoteSigner" in source  # Model requests retain their existing native signer.
    tree = ast.parse(source)
    assert not any(isinstance(n, ast.Import) and any(a.name == "base64" for a in n.names)
        or isinstance(n, ast.ImportFrom) and n.module == "base64" for n in ast.walk(tree))
    assert not any(isinstance(n, ast.Attribute) and n.attr in {"b64decode", "decodebytes"} for n in ast.walk(tree))
    assert not any(isinstance(n, ast.ImportFrom) and n.level for n in ast.walk(tree))
    module = ModuleType("governance_runtime")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "governance_runtime.py", "exec"), module.__dict__)
    assert module.GovIntent(intent="meaning").asset_mentions == []
    assert issubclass(module.GovConsultationResponse, module.GovResponse)
    assert set(module.GovConsultationResponse.model_json_schema()["properties"]["result_state"]["enum"]) == {
        "answered", "partial", "clarification", "denied", "not_evaluable"}
    assert module.GovConsultationResponse.model_json_schema()["properties"]["case_ref"]["type"] == "null"
    assert module.InventoryRecords().assets == []
    assert module.CallerMetadataTransport and module.ConsultationHooks
    assert module.decode_projection_rows({"rows": [], "rows_fetched": 0}, 1) == []
    for incomplete in ({"rows": [], "rows_fetched": 1},
            {"metadata": {"results_truncated": True}, "rows": []}):
        with pytest.raises(ValueError):
            module.decode_projection_rows(incomplete, 1)
    assert set(module.PROMPTS) == {"G0", "G1", "G2", "G3", "G4", "G5", "G6"}
    assert len(module.CONFIG["component_revision"]) == 64
    monkeypatch.setitem(sys.modules, "aidputils.agents.toolkit.agent_helper", SimpleNamespace(pre_invoke_setup=Mock()))
    entry = ModuleType("native_entry")
    exec(compile(governance_entry_source(), "governance_agent.py", "exec"), entry.__dict__)
    assert isinstance(entry.DataGovernanceAgent().runtime, module.DataGovernanceAgent)


def test_runtime_imports_and_contract_preamble_are_part_of_the_served_revision(monkeypatch):
    from app.modules.ai_data_governance import governance_source
    def revision(source):
        return next(ast.literal_eval(node.value)['component_revision'] for node in ast.parse(source).body
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CONFIG' for t in node.targets))
    before = governance_agent_source(binding())
    monkeypatch.setattr(governance_source, '_RUNTIME_PREAMBLE', governance_source._RUNTIME_PREAMBLE + '\n')
    after = governance_agent_source(binding())
    assert before != after and revision(before) != revision(after)


def test_assembled_opening_projection_retains_limits_without_registering_a_treatment(monkeypatch):
    module=ModuleType('governance_opening_runtime')
    monkeypatch.setitem(sys.modules,module.__name__,module)
    exec(compile(governance_agent_source(binding()),'governance_runtime.py','exec'),module.__dict__)
    actions=module.NativeCaseActions.__new__(module.NativeCaseActions)
    actions._opening_links=AsyncMock(return_value=('scope',[]))
    limits=['Sólo se consultó una revisión; no se evaluó todo el catálogo.']
    state=dict(intent=dict(intent='treatment',case_reference=None),
        assets=[dict(asset_id='asset',version_id='c'*64,name='catalog.schema.orders')],
        treatment=dict(problem='No hay una medición disponible.',asset_ids=['asset'],expected_versions=['c'*64],
            suggested_action='Obtener una medición',responsible_ref=None,
            verification_plan='Comprobar método y revisión',remaining_gaps=limits))
    spec,names=asyncio.run(actions._preview_specification(state))
    assert spec['action']=='OPEN' and spec['draft'] is None and spec['previous'] is None
    assert names=={'asset':'catalog.schema.orders'}
    assert spec['summary']==module.case_opening_summary(state['treatment'])
    assert limits[0] in spec['summary']


@pytest.mark.parametrize("change", [{"credential_name": ""}, {"identity_sha256": "unbound"}, {"query_compute_key": "new"},
    {"region": "foreign"}, {"model_id": "moving-model-alias"},
    {"source_scopes": [{"catalog_key": "oci_medallion", "schema_key": "foreign.oci_artifacts"}]}])
def test_native_package_rejects_implicit_credentials_and_unbound_resources(change):
    with pytest.raises(ValueError):
        governance_agent_source({**binding(), **change})


def test_memory_thread_is_stable_for_one_native_subject_session_and_isolated_from_others():
    context = SimpleNamespace(dh_user_principal="opaque-native", subject_id="subject-a", session_id="session-a")
    config = {"configurable": {"thread_id": "untrusted-reuse", "checkpoint_id": "old"}, "metadata": {"safe": "value"}}
    first = consultation_config(config, binding(), lambda: context)
    assert first == consultation_config(config, binding(), lambda: context)
    assert first["configurable"] == {"thread_id": first["configurable"]["thread_id"], "checkpoint_ns": ""}
    assert len(first["configurable"]["thread_id"]) == 36
    context.subject_id = "subject-b"
    assert first["configurable"]["thread_id"] != consultation_config(config, binding(), lambda: context)["configurable"]["thread_id"]
    context.subject_id, context.session_id = "subject-a", "session-b"
    assert first["configurable"]["thread_id"] != consultation_config(config, binding(), lambda: context)["configurable"]["thread_id"]
    assert "opaque-native" not in str(first)


def test_native_trace_metadata_uses_the_trusted_runtime_revision_not_request_metadata():
    context = SimpleNamespace(dh_user_principal='opaque', subject_id='subject', session_id='session')
    native = {'metadata': {'component_revision':'untrusted-request-value', 'safe':'retained'}}
    result = consultation_config(native, {**binding(), 'component_revision':'a' * 64}, lambda:context)
    assert result['metadata'] == {'component_revision':'a' * 64, 'safe':'retained'}
    assert native['metadata']['component_revision'] == 'untrusted-request-value'


def test_assembled_runtime_keeps_central_sql_signing_and_strict_result_decoding(monkeypatch):
    module = ModuleType("governance_central_sql_runtime")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(governance_agent_source(binding()), "governance_runtime.py", "exec"), module.__dict__)
    signer = object()
    transport = SimpleNamespace(signer=signer, access_mode="CENTRAL_METADATA", check_identity=Mock())
    run = Mock(return_value={"status":"SUCCEEDED", "json_outputs":[
        {"schema":[{"field":"payload"}], "values":[['{"asset_id":"a"}']]}]})
    module.run_central_projection = run
    assert asyncio.run(module.native_projection_query(binding(), "SELECT payload FROM controls LIMIT 2", limit=1, transport=transport)) == [{"asset_id":"a"}]
    assert run.call_args.args[1] is signer
    run.return_value["metadata"] = {"rows_fetched":2}
    with pytest.raises(ValueError):
        asyncio.run(module.native_projection_query(binding(), "SELECT payload FROM controls LIMIT 2", limit=1, transport=transport))
    sdk = Mock()
    sdk.create_execution_context.return_value = SimpleNamespace(data=SimpleNamespace(id="own"))
    client = module.CentralSqlSdk(sdk, "compute", Mock())
    client.create_execution_context("details", "compute", dh_user_principal="caller")
    assert "dh_user_principal" not in sdk.create_execution_context.call_args.kwargs
    client.cleanup()
    sdk.delete_context.assert_called_once_with("compute", "own", x_aidp_source="compute")


def test_assembled_runtime_uses_one_exact_hash_confirmation_batch(monkeypatch):
    module = ModuleType('governance_batch_runtime')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(governance_agent_source(binding()), 'governance_runtime.py', 'exec'), module.__dict__)
    checks = [{'table_key':'catalog.schema.orders','expected_hash':'a'*64},
        {'table_key':'catalog.schema.shipping','expected_hash':'b'*64}]
    run = Mock(return_value={'status':'SUCCEEDED','outputs':[{'type':'TEXT_PLAIN',
        'value':json.dumps({'results':[{**row,'matches':True} for row in checks]})}]})
    module._run_central_command = run
    transport = SimpleNamespace(signer=object(),access_mode='CENTRAL_METADATA',check_identity=Mock())
    assert asyncio.run(module.native_descriptions_confirmation(binding(),checks,transport=transport)) == {
        row['table_key']:True for row in checks}
    run.assert_called_once()
    compile(run.call_args.args[2], 'batch_confirmation.py', 'exec')
