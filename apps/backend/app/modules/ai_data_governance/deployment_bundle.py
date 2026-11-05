"""Pure, target-bound packaging of the maintained Governance TEST runtime."""

from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import UUID

from .delta import bootstrap_source
from .documentary_source import documentary_agent_source, documentary_entry_source
from .governance_source import governance_agent_source, governance_entry_source
from .pipeline_source import pipeline_task_source
from .workflow import ARTIFACT_ROOT, governance_workflow


BOOTSTRAP_PATH = ARTIFACT_ROOT + "/bootstrap18tables.py"
DOCUMENTARY_ENTRY_PATH = ARTIFACT_ROOT + "/agents/documentation/documentation_agent.py"
GOVERNANCE_ENTRY_PATH = ARTIFACT_ROOT + "/agents/governance/governance_agent.py"
WORKFLOW_PATH = ARTIFACT_ROOT + "/workflow.json"
MANIFEST_PATH = ARTIFACT_ROOT + "/manifest.json"

_RUNTIME_KEYS = ("region", "platform_id", "workspace_key", "credential_name", "identity_sha256",
                 "storage_root", "job_key", "source_scopes")
_MODEL_KEYS = ("model_id", "compartment_id")
_CONSULTATION_KEYS = ("region", "platform_id", "workspace_key", "agent_key", "query_compute_key",
                      "model_id", "compartment_id", "source_scopes", "credential_name", "identity_sha256",
                      "storage_root", "deployment_stage")
_BINDING_KEYS = (*_RUNTIME_KEYS, *_MODEL_KEYS, "cluster_key", "query_compute_key",
                 "documentation_agent_key", "agent_key", "deployment_stage")
_TASK_KEYS = ("sync_inventory", "publish_metadata", "replicate_model")
_MANAGED_REQUIREMENTS = b"# AIDP provides aidputils, LangGraph and OCI runtime libraries.\n"


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _component_revision(source: bytes) -> str:
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "CONFIG" for target in node.targets):
            return ast.literal_eval(node.value)["component_revision"]
    raise ValueError("The maintained agent source must declare its component revision")


def _target_binding(binding: dict) -> dict:
    if not isinstance(binding, dict) or set(binding) != set(_BINDING_KEYS):
        raise ValueError("Only the complete explicit nonsecret deployment binding is accepted")
    if binding["deployment_stage"] != "TEST":
        raise ValueError("This bundle supports only an explicit TEST deployment")
    if binding["credential_name"] != "AidpRuntime":
        raise ValueError("The shared AidpRuntime credential reference is required")
    for name in ("workspace_key", "job_key", "cluster_key", "query_compute_key",
                 "documentation_agent_key", "agent_key"):
        value = binding[name]
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("Deployment resource keys must be canonical UUIDs")
    if binding["documentation_agent_key"] == binding["agent_key"]:
        raise ValueError("Documentary and consultative agents must have distinct native identities")
    return {name: deepcopy(binding[name]) for name in _BINDING_KEYS}


def build_deployment_bundle(binding: dict) -> dict:
    """Return workspace-path bytes, workflow and hashes without deploying or writing.

    Bindings are explicit target resource identities, source scopes, artifacts URI,
    model/compartment and the expected shared credential identity digest. They are
    configuration, never credential values. Provisioning and native acceptance
    belong to the caller. Assembly reads only maintained source/model/prompts.
    ``manifest.files`` hashes every file except the manifest
    itself; the manifest is also returned as canonical UTF-8 workspace content.
    """
    target = _target_binding(binding)
    documentary_binding = {key: target[key] for key in (*_RUNTIME_KEYS, *_MODEL_KEYS)}
    documentary_source = documentary_agent_source(documentary_binding)
    documentary_revision = _component_revision(documentary_source)
    consultation_binding = {key: target[key] for key in _CONSULTATION_KEYS}
    consultation_binding["agent_code_path"] = GOVERNANCE_ENTRY_PATH
    consultation_source = governance_agent_source(consultation_binding)
    pipeline_binding = {**documentary_binding,
        "documentation_agent_key": target["documentation_agent_key"],
        "documentary_revision": documentary_revision}
    workflow = governance_workflow(job_name="wf_ai_dg_sync_master_catalog",
        cluster_key=target["cluster_key"], documentation_agent_key=target["documentation_agent_key"])
    files = {
        BOOTSTRAP_PATH: bootstrap_source(target["storage_root"]),
        DOCUMENTARY_ENTRY_PATH: documentary_entry_source(),
        DOCUMENTARY_ENTRY_PATH.rsplit("/", 1)[0] + "/documentary_runtime.py": documentary_source,
        GOVERNANCE_ENTRY_PATH: governance_entry_source(),
        GOVERNANCE_ENTRY_PATH.rsplit("/", 1)[0] + "/governance_runtime.py": consultation_source,
    }
    for entry in (DOCUMENTARY_ENTRY_PATH, GOVERNANCE_ENTRY_PATH):
        files[entry.rsplit("/", 1)[0] + "/requirements.txt"] = _MANAGED_REQUIREMENTS
    for task_key in _TASK_KEYS:
        files[ARTIFACT_ROOT + "/tasks/" + task_key + ".py"] = pipeline_task_source(pipeline_binding, task_key)
    maintained = Path(__file__).resolve().parent
    contract = json.loads((maintained / "aidp/manifest.json").read_text(encoding="utf-8"))
    for asset in contract["assets"]:
        if not asset["generated"]:
            files[asset["workspace_path"]] = (maintained / asset["source"]).read_bytes()
    files[WORKFLOW_PATH] = _json_bytes(workflow)
    manifest = {"schema_version": 1, "module": "ai_data_governance", "runtime_version": "R34-portable.1",
        "deployment_stage": "TEST", "bindings": target,
        "documentary_revision": documentary_revision,
        "consultative_revision": _component_revision(consultation_source),
        "bootstrap_path": BOOTSTRAP_PATH, "documentary_entry_path": DOCUMENTARY_ENTRY_PATH,
        "consultative_entry_path": GOVERNANCE_ENTRY_PATH, "workflow_path": WORKFLOW_PATH,
        "managed_dependencies": ["aidputils", "langgraph", "oci"],
        "files": {path: {"sha256": hashlib.sha256(source).hexdigest(), "size_bytes": len(source)}
                  for path, source in sorted(files.items())}}
    files[MANIFEST_PATH] = _json_bytes(manifest)
    return {"files": files, "workflow": workflow, "manifest": manifest}
