from __future__ import annotations

import ast
import json
import re
import threading
from types import SimpleNamespace

import pytest

from app.aidp import API_VERSION, AidpClient, AidpProvisionPending, participant_owner_key
from app.governance import (
    DAMA_SYSTEM_PROMPT,
    agent_source,
    database_names,
    external_catalog_name,
)
from app.lab_packs import load_lab_pack


USER_OCID = "ocid1.user.oc1..aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
EMAIL = "ada@example.com"


def bare_client() -> AidpClient:
    client = object.__new__(AidpClient)
    client.base = f"https://aidp.example.invalid/{API_VERSION}/aiDataPlatforms/platform"
    client.signer = object()
    client._session_lock = threading.Lock()
    client._locks = {}
    client.settings = SimpleNamespace(
        bucket_name="bucket",
        objectstorage_namespace="namespace",
        aidp_region="us-chicago-1",
        agent_model_id="ocid1.generativeaimodel.oc1.us-chicago-1.example",
        aidp_platform_id="ocid1.aidataplatform.oc1.us-chicago-1.example",
        compartment_id="ocid1.compartment.oc1..example",
        autonomous_database_id="ocid1.autonomousdatabase.oc1.us-chicago-1.example",
    )
    client.governance_database = SimpleNamespace(ready=lambda: False)
    return client


def rendered_agent_source() -> str:
    return agent_source(
        model_id="ocid1.generativeaimodel.oc1.us-chicago-1.chat",
        region="us-chicago-1",
        compartment_id="ocid1.compartment.oc1..participant",
        platform_id="ocid1.aidataplatform.oc1.us-chicago-1.participant",
        participant_key="u101",
        catalog_name="u101_aidp_lab",
    ).decode("utf-8")


def rendered_agent_function(name: str):
    module = ast.parse(rendered_agent_source())
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "governance_agent.py", "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize(
    ("participant_key", "expected"),
    [
        ("u101", ("U101_AGENT", "U101_AGENT_RO")),
        ("u999", ("U999_AGENT", "U999_AGENT_RO")),
        ("u1000", ("U1000_AGENT", "U1000_AGENT_RO")),
    ],
)
def test_database_names_are_participant_scoped(
    participant_key: str, expected: tuple[str, str]
) -> None:
    assert database_names(participant_key) == expected
    assert external_catalog_name(participant_key).startswith(participant_key)


@pytest.mark.parametrize("participant_key", ["u100", "u_101", "u101x", "U101", "u1;drop user admin"])
def test_database_names_reject_non_participant_identifiers(participant_key: str) -> None:
    with pytest.raises(ValueError, match="starting at u101"):
        database_names(participant_key)


def test_agent_source_reads_live_master_catalog_without_sql_connections() -> None:
    source = rendered_agent_source()

    assert "AIDPToolConf" not in source and "SQLTool" not in source
    assert re.search(
        r"\b(?:INSERT|UPDATE|DELETE|MERGE|DROP|ALTER)\s+(?:INTO|TABLE|FROM)\b",
        source,
        re.IGNORECASE,
    ) is None
    assert "u102" not in source.casefold()
    assert "LAB_METRICS" not in source and "LINEAGE_RELATIONS" not in source
    assert "Call at most one tool per model turn" in source
    assert "def catalog_inventory(" in source
    assert "search_term: str = \"\"" in source
    assert '"time_updated"' in source
    assert '"counts_by_layer"' in source
    assert 'column.get("fieldType")' in source
    assert '"observed_master_catalog"' in source
    assert "fetchLineage" in source
    assert "def _lineage_summary(" in source
    assert "def _column_lineage_component(" in source
    assert '"process_task"' in source
    assert '"notebook_path"' in source
    assert '/jobs/{quote(job_key, safe=\'\')}' in source
    assert 'defaults.get("processNodeId")' in source
    assert 'run.get("processRunEventTime")' in source
    assert 'anchor = f"{anchor}/' not in source
    assert '"limit": "25"' in source
    assert 'detail.get("tableFields")' in source
    assert 'managed.get("managedTableDataFormat")' in source
    assert 'f"aidp://catalogs@{CONFIG[\'platform_id\']}/o/"' in source
    assert "get_resource_principals_signer" not in source
    assert "get_resource_principal_delegation_token_signer" not in source
    assert 'aidputils.secrets.get(name=CONFIG["credential_name"], key=key)' in source
    assert "private_key_content=values[\"private_key\"]" in source
    assert "The shared OCI credential is unavailable or invalid" in source
    assert "TABLE_NAME.fullmatch" in source
    assert "_contains_foreign_participant" in source
    assert "def _catalog_contract(" in source
    assert 'if _name(item) == CONFIG["catalog_name"]' in source
    assert 'if _name(item) == f"oci_{layer}"' in source
    assert "Checkpointer initialization failed; using a stateless graph" in source
    assert '"stage": stage' in source
    assert 'getattr(response, "status_code", None)' in source
    assert 'return _error_response(self.setup_error)' in source
    assert "input=message" in source
    assert "Learning map:" in source
    assert "no table or lineage is hard-coded" in source
    encoded_config = source.split("CONFIG = ", 1)[1].splitlines()[0]
    config = json.loads(encoded_config)
    assert config["catalog_name"] == "u101_aidp_lab"
    assert config["credential_name"] == "AidpGovernanceOperator"
    assert config["participant_key"] == "u101"
    assert config["table_prefix"] == "u101_"
    assert config["platform_id"].startswith("ocid1.aidataplatform.")
    assert "spark_compute_key" not in config
    assert "catalog_key" not in config and "schema_keys" not in config
    compile(source, "governance_agent.py", "exec")


def test_agent_prompt_is_dama_grounded_and_explainable() -> None:
    source = rendered_agent_source()

    assert "DAMA-DMBOK" in DAMA_SYSTEM_PROMPT
    assert all(
        heading in DAMA_SYSTEM_PROMPT
        for heading in ("Evidence", "Explanation", "Governance implication", "Recommendation or limitation")
    )
    assert "another participant" in DAMA_SYSTEM_PROMPT
    assert "If evidence is unavailable" in DAMA_SYSTEM_PROMPT
    assert "Use match_count and counts_by_layer exactly as returned" in DAMA_SYSTEM_PROMPT
    assert "timeUpdated as the last Master Catalog metadata update" in DAMA_SYSTEM_PROMPT
    assert "job metadata provides notebook_path" in DAMA_SYSTEM_PROMPT
    assert "search_term and include_columns=true" in DAMA_SYSTEM_PROMPT
    expected_prompt = (
        DAMA_SYSTEM_PROMPT
        + "\nThe only allowed participant is u101. A different participant "
        "identifier in the request requires an immediate refusal without a tool call."
    )
    assert repr(expected_prompt) in source
    assert "complete live scope" in source
    assert "observed Master Catalog metadata and lineage" in source


def test_column_lineage_selects_only_the_connected_field_component() -> None:
    component = rendered_agent_function("_column_lineage_component")
    graph = {
        "nodes": [
            {
                "id": "gold-document",
                "displayName": "document_number",
                "properties": {"default": {"tableName": "u101_lab_customer_360"}},
            },
            {
                "id": "silver-document",
                "displayName": "document_number",
                "properties": {"default": {"tableName": "u101_lab_customer_master"}},
            },
            {
                "id": "gold-customer",
                "displayName": "customer_id",
                "properties": {"default": {"tableName": "u101_lab_customer_360"}},
            },
        ],
        "links": [
            {"fromNodeId": "silver-document", "toNodeId": "gold-document"},
            {"fromNodeId": "gold-customer", "toNodeId": "unrelated-customer"},
        ],
    }

    selected = component(graph, "u101_lab_customer_360", "document_number")

    assert {node["id"] for node in selected["nodes"]} == {
        "gold-document",
        "silver-document",
    }
    assert selected["links"] == [
        {"fromNodeId": "silver-document", "toNodeId": "gold-document"}
    ]


def test_agent_pack_has_diverse_dama_acceptance_cases() -> None:
    cases = load_lab_pack("agent").agent["evaluation_cases"]
    assert len(cases) >= 10
    assert {tool for case in cases for tool in case["expected_tools"]} == {
        "catalog_inventory",
        "catalog_lineage",
    }
    assert {case["category"] for case in cases} >= {
        "catalog",
        "metadata",
        "discovery",
        "quality",
        "entity_lineage",
        "column_lineage",
        "stewardship",
        "security",
    }
    assert all(case["required_concepts"] for case in cases)


def test_agent_provisions_from_private_master_catalog_without_autonomous_mirror() -> None:
    client = bare_client()
    pack = load_lab_pack("agent")
    manifest = {
        "layout_version": 4,
        "owner_key": participant_owner_key(USER_OCID),
        "participant_key": "u101",
        "participant_code": 101,
        "participant_email": EMAIL,
        "external_catalog": None,
        "labs": {
            "agent": {
                "pack_version": pack.pack_version,
                "pack_hash": pack.pack_sha256,
                "workspace_path": f"/Workspace/medallon/u101_{EMAIL}/agent",
                "job_name": "u101_agent_data_governance",
                "phase": "workspace",
                "operation": None,
            }
        },
    }
    captured: dict[str, object] = {}
    writes: list[dict] = []
    client._workspace = lambda: {"key": "workspace"}
    client._ensure_workspace_layout = lambda *_args: False
    client._ensure_catalog = lambda name: (
        ({"key": "u101-catalog-key"}, False)
        if name == "u101_aidp_lab"
        else (_ for _ in ()).throw(AssertionError("unexpected catalog"))
    )
    client._ensure_catalog_contract = lambda key, name: (
        (
            {
                "landing": {"key": "landing-key"},
                "bronze": {"key": "bronze-key"},
                "silver": {"key": "silver-key"},
                "gold": {"key": "gold-key"},
            },
            False,
        )
        if (key, name) == ("u101-catalog-key", "u101_aidp_lab")
        else (_ for _ in ()).throw(AssertionError("unexpected schema contract"))
    )
    client._ensure_agent_compute = lambda workspace: ({"key": "ai-compute-key"}, False)
    client._ensure_agent = lambda workspace, compute, name, root, source, descriptor, **_kwargs: (
        captured.update(
            workspace=workspace,
            compute=compute,
            name=name,
            root=root,
            source=source.decode("utf-8"),
            descriptor=json.loads(descriptor),
        )
        or ("agent-key", False)
    )
    permission_paths: list[str] = []
    client._ensure_permission = lambda path, *_args, **_kwargs: (
        permission_paths.append(path) or False
    )
    client._ensure_agent_deployment = lambda *_args, **_kwargs: (
        {"key": "deployment-key", "lifecycleState": "ACTIVE"},
        False,
    )
    client._advance_lab_manifest = lambda _workspace, value, lab_id, phase: value[
        "labs"
    ][lab_id].update(phase=phase)
    client.governance_database = SimpleNamespace(
        ready=lambda: (_ for _ in ()).throw(AssertionError("Autonomous must not be read")),
        ensure_participant=lambda *_args: (_ for _ in ()).throw(
            AssertionError("Autonomous participant users must not be created")
        ),
    )
    client._write_manifest = lambda _workspace, _owner, value: writes.append(
        json.loads(json.dumps(value))
    )

    material = client._provision_agent(
        USER_OCID, EMAIL, manifest, manifest["labs"]["agent"], pack
    )

    assert material.participant_key == "u101"
    assert manifest["labs"]["agent"]["phase"] == "active"
    assert manifest["external_catalog"] is None
    assert {"/workspaces/workspace/clusters/ai-compute-key", "/catalogs/u101-catalog-key"} <= set(
        permission_paths
    )
    source = str(captured["source"])
    assert "u101_aidp_lab" in source and "shared-spark-key" not in source
    assert "LAB_METRICS" not in source and "LINEAGE_RELATIONS" not in source
    assert captured["descriptor"]["participant_key"] == "u101"
    assert captured["descriptor"]["entry_file"] == "governance_agent.py"
    assert manifest["labs"]["agent"]["deployment_source_hash"] == captured[
        "descriptor"
    ]["entry_sha256"]
    assert writes[-1]["agent"]["catalog_name"] == "u101_aidp_lab"


def test_active_agent_verifies_deployment_without_rotating_database_credentials() -> None:
    client = bare_client()
    pack = load_lab_pack("agent")
    state = {
        "pack_version": pack.pack_version,
        "pack_hash": pack.pack_sha256,
        "workspace_path": f"/Workspace/medallon/u101_{EMAIL}/agent",
        "job_name": "u101_agent_data_governance",
        "phase": "active",
        "operation": None,
        "agent_key": "agent-key",
        "compute_key": "compute-key",
    }
    manifest = {
        "layout_version": 4,
        "owner_key": participant_owner_key(USER_OCID),
        "participant_key": "u101",
        "participant_code": 101,
        "participant_email": EMAIL,
        "external_catalog": {"key": "catalog-key"},
        "agent": {"key": "agent-key", "compute_key": "compute-key"},
        "labs": {"agent": state},
    }
    client._workspace = lambda: {"key": "workspace"}
    client._write_manifest = lambda *_args: None
    client.governance_database = SimpleNamespace(
        ready=lambda: (_ for _ in ()).throw(AssertionError("database must not be touched")),
        ensure_participant=lambda *_args: (_ for _ in ()).throw(
            AssertionError("credentials must not rotate")
        ),
    )
    client._ensure_agent_deployment = lambda *args: (
        {"key": "deployment-key", "lifecycleState": "ACTIVE"},
        False,
    ) if args == (
        "workspace", "agent-key", "compute-key", "u101_agent_data_governance"
    ) else (_ for _ in ()).throw(AssertionError("unexpected deployment lookup"))

    material = client._provision_agent(USER_OCID, EMAIL, manifest, state, pack)

    assert material.participant_key == "u101"
    assert state["deployment_key"] == "deployment-key"
    assert manifest["agent"]["deployment_key"] == "deployment-key"


def test_active_agent_deployment_is_reused() -> None:
    client = bare_client()
    client._list = lambda *_args, **_kwargs: [
        {"key": "deployment-key", "lifecycleState": "ACTIVE"}
    ]
    client._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("an active deployment must be reused")
    )

    deployment, created = client._ensure_agent_deployment(
        "workspace", "agent-key", "compute-key", "u101_agent_data_governance"
    )

    assert deployment["key"] == "deployment-key"
    assert created is False


def test_existing_agent_is_updated_after_workspace_source_changes() -> None:
    client = bare_client()
    uploads = iter((True, False, False))
    client._upload_file = lambda *_args, **_kwargs: next(uploads)
    client._agents = lambda *_args: [{"key": "agent-key"}]
    requests: list[tuple[str, str, dict, str]] = []
    client._request = lambda method, path, **kwargs: requests.append(
        (method, path, kwargs["payload"], kwargs["retry_scope"])
    )

    agent_key, changed = client._ensure_agent(
        "workspace",
        "compute-key",
        "u101_agent_data_governance",
        "/Workspace/medallon/u101_person@example.com/agent",
        b"print('revision')\n",
        b"{}",
        repair_drift=True,
    )

    assert changed and agent_key == "agent-key"
    assert requests[0][0:2] == (
        "PUT",
        "/workspaces/workspace/agents/agent-key",
    )
    assert requests[0][2]["entryFilePath"].endswith("/governance_agent.py")
    assert requests[0][2]["computeKey"] == "compute-key"
    assert requests[0][3].startswith("agent-update:")


def test_active_agent_deployment_is_redeployed_for_new_source_revision() -> None:
    client = bare_client()
    client._list = lambda *_args, **_kwargs: [
        {
            "key": "deployment-key",
            "displayName": "u101_agent_data_governance_deployment",
            "lifecycleState": "ACTIVE",
        }
    ]
    requests: list[tuple[str, str, dict, str]] = []
    client._request = lambda method, path, **kwargs: (
        requests.append(
            (method, path, kwargs["payload"], kwargs["retry_scope"])
        )
        or {"key": "deployment-key", "lifecycleState": "CREATING"}
    )

    deployment, changed = client._ensure_agent_deployment(
        "workspace",
        "agent-key",
        "compute-key",
        "u101_agent_data_governance",
        redeploy_revision="abc123",
    )

    assert changed and deployment["key"] == "deployment-key"
    assert requests == [
        (
            "POST",
            "/workspaces/workspace/agents/agent-key/deployments/actions/redeploy",
            {
                "displayName": "u101_agent_data_governance_deployment",
                "description": "Production deployment for the participant governance Agent",
                "agentComputeKey": "compute-key",
                "agentKey": "agent-key",
            },
            "agent-redeploy:agent-key:abc123",
        )
    ]


def test_missing_agent_deployment_is_created_on_shared_ai_compute(monkeypatch) -> None:
    client = bare_client()
    monkeypatch.setattr(
        "app.aidp.uuid.uuid4", lambda: SimpleNamespace(hex="revision12345678")
    )
    client._list = lambda *_args, **_kwargs: []
    requests: list[tuple[str, str, dict]] = []
    client._request = lambda method, path, **kwargs: (
        requests.append((method, path, kwargs["payload"]))
        or {"key": "deployment-key", "lifecycleState": "CREATING"}
    )

    deployment, created = client._ensure_agent_deployment(
        "workspace", "agent-key", "compute-key", "u101_agent_data_governance"
    )

    assert created and deployment["key"] == "deployment-key"
    assert requests == [
        (
            "POST",
            "/workspaces/workspace/agents/agent-key/deployments/actions/deploy",
            {
                "displayName": "u101_agent_data_governance_agent-ke_revision_deployment",
                "description": "Production deployment for the participant governance Agent",
                "agentComputeKey": "compute-key",
                "agentKey": "agent-key",
            },
        )
    ]


def test_external_catalog_cleanup_waits_while_aidp_is_deleting() -> None:
    client = bare_client()
    client._catalog = lambda *_args, **_kwargs: {
        "key": "u101-external-catalog",
        "lifecycleState": "DELETING",
    }
    client._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("DELETE must not be repeated while AIDP is already deleting")
    )

    with pytest.raises(AidpProvisionPending) as raised:
        client._cleanup_external_catalog("u101", {})

    assert raised.value.phase == "cleanup"


def test_agent_cleanup_forgets_deleted_manifest_resources() -> None:
    manifest = {
        "agent": {"key": "stale-agent"},
        "external_catalog": {"key": "stale-catalog"},
        "labs": {"agent": {"phase": "workspace"}},
    }

    AidpClient._forget_agent_resources(manifest)

    assert manifest == {
        "agent": None,
        "external_catalog": None,
        "labs": {"agent": {"phase": "workspace"}},
    }


def test_agent_cleanup_removes_aidp_then_autonomous_then_workspace() -> None:
    client = bare_client()
    pack = load_lab_pack("agent")
    state = {
        "pack_version": pack.pack_version,
        "pack_hash": pack.pack_sha256,
        "workspace_path": f"/Workspace/medallon/u101_{EMAIL}/agent",
        "job_name": "u101_agent_data_governance",
        "phase": "active",
        "operation": None,
        "external_catalog_name": external_catalog_name("u101"),
    }
    calls: list[tuple[str, ...]] = []
    client._cleanup_agent = lambda workspace, name: calls.append(("agent", workspace, name))
    client._cleanup_external_catalog = lambda key, value: calls.append(
        ("catalog", key, str(value["external_catalog_name"]))
    )
    client.governance_database = SimpleNamespace(
        drop_participant=lambda key: calls.append(("autonomous", key))
    )
    client._delete_workspace_path = lambda workspace, path, _message: calls.append(
        ("workspace", workspace, path)
    )

    client._cleanup_lab("workspace", "u101", "agent", state)

    assert calls == [
        ("agent", "workspace", "u101_agent_data_governance"),
        ("catalog", "u101", external_catalog_name("u101")),
        ("autonomous", "u101"),
        ("workspace", "workspace", f"/Workspace/medallon/u101_{EMAIL}/agent"),
    ]
