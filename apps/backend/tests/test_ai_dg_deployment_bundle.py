import ast
from copy import deepcopy
import hashlib
import json
from pathlib import PurePosixPath
from uuid import UUID

import pytest

from app.ai_data_governance.delta import bootstrap_source, create_statements
from app.ai_data_governance.deployment_bundle import (
    ARTIFACT_ROOT, BOOTSTRAP_PATH, DOCUMENTARY_ENTRY_PATH, GOVERNANCE_ENTRY_PATH,
    MANIFEST_PATH, WORKFLOW_PATH, build_deployment_bundle,
)
from app.ai_data_governance.documentary_source import documentary_agent_source, documentary_entry_source
from app.ai_data_governance.governance_source import governance_agent_source, governance_entry_source
from app.ai_data_governance.pipeline_source import pipeline_task_source


def binding(offset=0):
    return {"region": "us-chicago-1", "platform_id": "ocid1.aidataplatform.oc1.us-chicago-1.synthetic" + str(offset),
        "workspace_key": str(UUID(int=101 + offset)), "job_key": str(UUID(int=102 + offset)),
        "cluster_key": str(UUID(int=103 + offset)), "query_compute_key": str(UUID(int=104 + offset)),
        "documentation_agent_key": str(UUID(int=105 + offset)), "agent_key": str(UUID(int=106 + offset)),
        "model_id": "ocid1.generativeaimodel.oc1.us-chicago-1.synthetic" + str(offset),
        "compartment_id": "ocid1.compartment.oc1..synthetic" + str(offset),
        "storage_root": "oci://synthetic-artifacts-" + str(offset) + "@syntheticnamespace/oci_artifacts",
        "source_scopes": [{"catalog_key": "oci_medallion", "schema_key": "oci_medallion.oci_artifacts"},
                          {"catalog_key": "synthetic_catalog", "schema_key": "synthetic_catalog.source"}],
        "credential_name": "AidpRuntime", "identity_sha256": hashlib.sha256(str(offset).encode()).hexdigest(),
        "deployment_stage": "TEST"}


def config(source):
    assignments = {node.targets[0].id: node.value for node in ast.parse(source).body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    return ast.literal_eval(assignments["CONFIG"])


@pytest.fixture(scope="module")
def bundle():
    return build_deployment_bundle(binding())


def test_bundle_is_deterministic_detached_and_uses_only_workspace_destinations(bundle):
    value = binding()
    before = deepcopy(value)
    assert build_deployment_bundle(dict(reversed(list(value.items())))) == bundle
    assert value == before
    for path, source in bundle["files"].items():
        assert path.startswith(ARTIFACT_ROOT + "/")
        assert str(PurePosixPath(path)) == path and ".." not in PurePosixPath(path).parts
        assert isinstance(source, bytes)
        assert b"BEGIN PRIVATE KEY" not in source and b"BEGIN RSA PRIVATE KEY" not in source
        assert b"D:\\" not in source and b".tmp/" not in source and b".tmp\\" not in source
        if path.endswith(".py"):
            compile(source, path, "exec")
            nodes = ast.parse(source).body
            imports = {alias.name for node in nodes if isinstance(node, ast.Import) for alias in node.names}
            assert not imports & {"importlib", "base64"}
            assert not any(isinstance(node, ast.ImportFrom) and node.level for node in nodes)
    value["source_scopes"][0]["schema_key"] = "changed"
    assert bundle["manifest"]["bindings"] == before


def test_bundle_keeps_maintained_sources_bootstrap_and_cross_runtime_bindings(bundle):
    files = bundle["files"]
    documentary_path = DOCUMENTARY_ENTRY_PATH.rsplit("/", 1)[0] + "/documentary_runtime.py"
    governance_path = GOVERNANCE_ENTRY_PATH.rsplit("/", 1)[0] + "/governance_runtime.py"
    documentary = config(files[documentary_path])
    governance = config(files[governance_path])
    documentary_revision = documentary.pop("component_revision")
    governance.pop("component_revision")
    assert files[documentary_path] == documentary_agent_source(documentary)
    assert files[governance_path] == governance_agent_source(governance)
    assert files[DOCUMENTARY_ENTRY_PATH] == documentary_entry_source()
    assert files[GOVERNANCE_ENTRY_PATH] == governance_entry_source()
    assert DOCUMENTARY_ENTRY_PATH == ARTIFACT_ROOT + "/agents/documentation/documentation_agent.py"
    for entry in (DOCUMENTARY_ENTRY_PATH, GOVERNANCE_ENTRY_PATH):
        assert files[entry.rsplit("/", 1)[0] + "/requirements.txt"] == (
            b"# AIDP provides aidputils, LangGraph and OCI runtime libraries.\n")
    assert governance["deployment_stage"] == "TEST"
    assert governance["agent_code_path"] == GOVERNANCE_ENTRY_PATH
    assert files[BOOTSTRAP_PATH] == bootstrap_source(binding()["storage_root"])
    statements = create_statements(binding()["storage_root"])
    assert len(statements) == 18 and all("CREATE TABLE IF NOT EXISTS" in statement for statement in statements)
    assert all(binding()["storage_root"] in statement for statement in statements)
    for task_key in ("sync_inventory", "publish_metadata", "replicate_model"):
        source = files[ARTIFACT_ROOT + "/tasks/" + task_key + ".py"]
        task_binding = config(source)
        assert task_binding == {**documentary, "documentation_agent_key": binding()["documentation_agent_key"],
                                "documentary_revision": documentary_revision}
        assert source == pipeline_task_source(task_binding, task_key)


def test_workflow_and_manifest_hash_exact_published_bytes(bundle):
    files, workflow, manifest = bundle["files"], bundle["workflow"], bundle["manifest"]
    assert json.loads(files[MANIFEST_PATH]) == manifest
    assert json.loads(files[WORKFLOW_PATH]) == workflow
    assert set(manifest["files"]) == set(files) - {MANIFEST_PATH}
    for path, expected in manifest["files"].items():
        assert expected == {"sha256": hashlib.sha256(files[path]).hexdigest(), "size_bytes": len(files[path])}
    tasks = workflow["tasks"]
    assert [task["taskKey"] for task in tasks] == ["sync_inventory", "document_metadata", "publish_metadata", "replicate_model"]
    assert [task["dependsOn"] for task in tasks] == [[], [{"taskKey": "sync_inventory"}],
        [{"taskKey": "document_metadata"}], [{"taskKey": "publish_metadata"}]]
    assert [task["runIf"] for task in tasks] == ["ALL_SUCCESS", "ALL_SUCCESS", "ALL_SUCCESS", "ALL_DONE"]
    assert tasks[1]["agentKey"] == binding()["documentation_agent_key"]
    assert all(task["filePath"] in files and task["cluster"] == {"clusterKey": binding()["cluster_key"]}
               for task in tasks if task["type"] == "PYTHON_TASK")
    assert not workflow["queue"]["isEnabled"] and workflow["maxConcurrentRuns"] == 1


def test_second_target_has_no_old_bindings_or_stale_documentary_revision(bundle):
    target = binding(1000)
    other = build_deployment_bundle(target)
    assert other["manifest"]["bindings"] == target
    assert set(other["files"]) == set(bundle["files"])
    for path, source in other["files"].items():
        for key in ("platform_id", "workspace_key", "job_key", "storage_root", "documentation_agent_key", "agent_key"):
            assert binding()[key].encode() not in source
        if path.endswith(".py") and "/tasks/" in path:
            runtime = config(source)
            assert runtime["documentary_revision"] == other["manifest"]["documentary_revision"]
    assert other["workflow"]["tasks"][1]["agentKey"] == target["documentation_agent_key"]


@pytest.mark.parametrize(("key", "value"), [
    ("deployment_stage", "PROD"), ("credential_name", "LegacyRuntime"),
    ("identity_sha256", "not-a-digest"), ("platform_id", "ocid1.aidataplatform.oc1.eu-frankfurt-1.synthetic"),
    ("workspace_key", "../other"), ("job_key", "not-a-uuid"), ("query_compute_key", None),
    ("documentation_agent_key", str(UUID(int=106))),
    ("storage_root", "oci://bucket@namespace/oci_artifacts/../other"),
    ("source_scopes", [{"catalog_key": "other", "schema_key": "different.source"}]),
    ("source_scopes", [{"catalog_key": "synthetic_catalog", "schema_key": "synthetic_catalog.source"}]),
    ("model_id", "not-a-model"), ("compartment_id", "not-a-compartment"),
])
def test_wrong_or_incomplete_bindings_fail_closed(key, value):
    target = binding()
    target[key] = value
    with pytest.raises(ValueError):
        build_deployment_bundle(target)


@pytest.mark.parametrize("change", ["secret", "output_path", "missing"])
def test_unexpected_secret_paths_and_missing_configuration_are_rejected(change):
    target = binding()
    if change == "missing":
        target.pop("identity_sha256")
    else:
        target[change] = "forbidden"
    with pytest.raises(ValueError, match="explicit nonsecret"):
        build_deployment_bundle(target)
