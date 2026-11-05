import ast
from copy import deepcopy

import pytest

from app.modules.ai_data_governance.workflow import governance_workflow
from app.modules.ai_data_governance.pipeline_source import pipeline_task_source
from app.modules.ai_data_governance.workflow_receipts import execution_receipt_key, load_execution_receipt
from app.modules.ai_data_governance.native_pipeline import DescriptionTransport, admit_pipeline_task
from app.modules.ai_data_governance.native_documentary import documentary_invocation_batches
from app.modules.ai_data_governance.task_arguments import TaskArguments
from tests.modules.ai_data_governance.test_ai_dg_documentary_hooks import Store
from tests.modules.ai_data_governance.test_ai_dg_runtime_source import config
from tests.modules.ai_data_governance.test_ai_dg_native_documentary import JOB, RUN, TASK, BINDING, responses, batch


def binding():
    return {**config(), "model_id": "model", "compartment_id": "compartment", "documentation_agent_key": JOB,
            "documentary_revision": "a" * 64}


def manifest():
    return {"schema_version": 1, "job_key": JOB, "job_run_id": RUN, "documentation_agent_key": JOB,
        "documentary_revision": "a" * 64,
        "scopes": [{"scope_id": "b" * 64, "inventory_run_id": "c" * 64, "batch_ref": "d" * 64}]}


def test_workflow_has_four_actual_tasks_with_separate_retries_and_replica_all_done():
    value = governance_workflow(job_name="wf_ai_dg_sync_master_catalog", cluster_key=JOB, documentation_agent_key=TASK)
    tasks = value["tasks"]
    assert [t["type"] for t in tasks] == ["PYTHON_TASK", "AGENT_TASK", "PYTHON_TASK", "PYTHON_TASK"]
    assert [t["taskKey"] for t in tasks] == ["sync_inventory", "document_metadata", "publish_metadata", "replicate_model"]
    assert tasks[1]["agentKey"] == TASK and tasks[1]["prompt"] == "{{job.run_id}}:{{job.repair_count}}"
    assert tasks[3]["runIf"] == "ALL_DONE" and tasks[3]["dependsOn"] == [{"taskKey": "publish_metadata"}]
    assert all(t["maxRetries"] == 1 and not t["isRetryOnTimeout"] for t in tasks)
    assert value["maxConcurrentRuns"] == 1 and not value["queue"]["isEnabled"]
    assert "schedule" not in value
    assert len({t["filePath"] for t in tasks if t["type"] == "PYTHON_TASK"}) == 3


@pytest.mark.parametrize("phase", ["sync_inventory", "publish_metadata", "replicate_model"])
def test_generated_native_entries_load_real_modules_and_preserve_dataclass_decorators(phase, monkeypatch):
    import sys
    from types import ModuleType
    source = pipeline_task_source(binding(), phase)
    module = ModuleType("candidate_runtime")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    namespace = module.__dict__
    exec(compile(source, phase + ".py", "exec"), namespace)
    args = namespace["TaskArguments"](RUN, TASK, phase, 1, 0)
    assert args.task_key == phase
    assert namespace["InventoryRecords"]().assets == []
    assert namespace["DocumentationAdmission"].model_fields["work_id"]
    assert not any(isinstance(n, ast.ImportFrom) and n.level for n in ast.walk(ast.parse(source)))
    assert "base64" not in source.decode() and "run_native_pipeline" in namespace


def test_execution_receipt_is_stable_across_repairs_and_rejects_foreign_or_duplicate_scopes():
    store = Store()
    store.create("executions", execution_receipt_key(RUN), manifest())
    assert load_execution_receipt(store, BINDING, RUN) == manifest()
    value = store.rows["executions", execution_receipt_key(RUN)]
    value["scopes"].append(deepcopy(value["scopes"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        load_execution_receipt(store, BINDING, RUN)


@pytest.mark.parametrize("phase", ["publish_metadata", "replicate_model"])
def test_native_phase_admission_checks_actual_task_identity_and_cancellation(phase):
    run, task = responses()
    run["taskToTaskRunMap"] = {phase: TASK}
    task["taskKey"] = phase
    from unittest.mock import Mock
    args = TaskArguments(RUN, TASK, phase, 1, 0)
    request = Mock(side_effect=[(run, {}), (task, {})])
    admit_pipeline_task(request, BINDING, args)
    task["state"]["isUserCanceledOrTimedOut"] = True
    request = Mock(side_effect=[(run, {}), (task, {})])
    with pytest.raises(ValueError, match="no longer active"):
        admit_pipeline_task(request, BINDING, args)


def test_doc_run_handoff_requires_current_source_and_active_native_task_even_for_empty_batch():
    from unittest.mock import Mock
    store = Store()
    value = manifest()
    store.create("executions", execution_receipt_key(RUN), value)
    b = batch()
    b["execution"].update(scope_id="b" * 64, inventory_run_id="c" * 64)
    b["batch"]["admissions"] = []
    store.create("batches", "d" * 64, b)
    request = Mock(side_effect=[(r, {}) for r in responses()])
    cfg = {**BINDING, "component_revision": "a" * 64}
    assert documentary_invocation_batches(store, cfg, RUN + ":1", request) == ["d" * 64]
    with pytest.raises(ValueError, match="revision"):
        documentary_invocation_batches(store, {**cfg, "component_revision": "f" * 64}, RUN, request)


@pytest.mark.parametrize("path,payload", [("/roles", {"description": "x [IA]"}),
    ("/tables/other.schema.table", {"description": "x [IA]"}),
    ("/tables/oci_medallion.oci_gold.t", {"name": "other"}),
    ("/tables/oci_medallion.oci_gold.t", {"description": "x"})])
def test_description_transport_rejects_scope_or_payload_expansion_before_http(path, payload):
    from unittest.mock import Mock
    session = Mock()
    request = DescriptionTransport(config(), None, session)
    with pytest.raises(ValueError):
        request("PUT", path, payload=payload)
    session.request.assert_not_called()


def test_current_pipeline_does_not_load_legacy_model_rows():
    source=pipeline_task_source(binding(),"sync_inventory")
    assert b"data_governance_metadata" not in source
    assert b"load_legacy_column_ids" not in source
    assert b"pipeline_source_scopes" in source


def test_assembled_inventory_reads_native_text_source_and_resolves_fixed_zip(monkeypatch):
    import json
    import sys
    from types import ModuleType
    from unittest.mock import MagicMock
    from urllib.parse import quote

    module = ModuleType("workflow_source_contract")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(pipeline_task_source(binding(), "sync_inventory"), "sync_inventory.py", "exec"), module.__dict__)
    cfg = config()
    root = f"/workspaces/{cfg['workspace_key']}"
    path = "/Workspace/setup/model.py"
    ddl = "CREATE TABLE oci_medallion.oci_gold.actual_table (id INT)"
    unused = "CREATE TABLE oci_medallion.oci_gold.unreachable_table (id INT)"
    source = f"STATEMENTS=[{ddl!r},{unused!r}]\nEXPECTED=[{{'name':'actual_table'}}]\nfor _,s in zip(EXPECTED,STATEMENTS):\n    spark.sql(s)"
    bodies = {root + "/jobs": {"items": [{"key": JOB}]}, root + "/jobs/" + JOB:
              {"key": JOB, "tasks": [{"type": "PYTHON_TASK", "source": "WORKSPACE", "taskKey": "create_model", "filePath": path}]}}
    session = MagicMock()
    def exchange(method, url, **options):
        route = url.split("/aiDataPlatforms/", 1)[1].split("/workspaces/", 1)[1]
        route = "/workspaces/" + route
        response = MagicMock()
        text = route == root + "/objects/" + quote(path, safe="")
        response.status_code = 200 if options['headers']['Accept'] == ('*/*' if text else 'application/json') else 406
        response.headers = {"content-type": "application/x-yaml" if text else "application/json"}
        response.iter_content.return_value = [(source if text else json.dumps(bodies[route])).encode()]
        response.__enter__.return_value = response
        return response
    session.request.side_effect = exchange
    reader = module.WorkspaceCodeReader(module.DescriptionTransport(cfg, None, session), cfg['workspace_key'], check_active=lambda: None)
    refs = reader.find("oci_medallion.oci_gold.actual_table")
    assert len(refs) == 1 and refs[0]['task_key'] == 'create_model'
    assert refs[0]['execution_evidence'] is False
    assert reader.find("oci_medallion.oci_gold.unreachable_table") == []
    assert not reader.truncated and session.request.call_count == 3
    assert all(call.args[0] == 'GET' for call in session.request.call_args_list)
