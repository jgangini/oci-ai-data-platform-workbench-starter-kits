from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from app.aidp import API_VERSION, AidpClient, AidpProvisionPending, participant_owner_key
from app.autonomous import ParticipantDatabase
from app.governance import (
    DAMA_SYSTEM_PROMPT,
    agent_source,
    database_names,
    external_catalog_name,
    governed_lineage_contracts,
    governed_metric_queries,
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
    queries = governed_metric_queries(
        "u101",
        "u101_aidp_lab",
        {"telco_lineage": {"gold": ("customer_360",)}},
    )
    lineage = governed_lineage_contracts(
        "u101",
        "u101_aidp_lab",
        {
            "telco_lineage": {
                "ENTITY": ("landing.crm_customers->bronze.crm_customers",),
                "COLUMN": (
                    "bronze.crm_customers.document_number->"
                    "silver.customer_master.document_number",
                ),
            }
        },
    )
    return agent_source(
        model_id="ocid1.generativeaimodel.oc1.us-chicago-1.chat",
        region="us-chicago-1",
        compartment_id="ocid1.compartment.oc1..participant",
        participant_key="u101",
        catalog_key="u101-catalog-key",
        catalog_name="u101_aidp_lab",
        schema_keys={
            "landing": "landing-key",
            "bronze": "bronze-key",
            "silver": "silver-key",
            "gold": "gold-key",
        },
        spark_compute_key="shared-spark-key",
        metric_queries=queries,
        lineage_contracts=lineage,
    ).decode("utf-8")


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


def test_agent_source_reads_live_master_catalog_and_predefined_spark_queries() -> None:
    source = rendered_agent_source()

    assert source.count("SELECT ") == 1
    assert all(keyword not in source.upper() for keyword in (" INSERT ", " UPDATE ", " DELETE ", " MERGE ", " DROP ", " ALTER "))
    assert "u102" not in source.casefold()
    assert "LAB_METRICS" not in source and "LINEAGE_RELATIONS" not in source
    assert '"queryType": "SPARK"' in source
    assert 'CONFIG["spark_compute_key"]' in source
    assert "SHOW TABLES LIKE 'u101_*'" in source
    assert "catalog_inventory_{layer}" in source
    assert "canonical_package_contract" in source
    assert "fetchLineage" not in source
    assert "get_resource_principals_signer" not in source
    assert "Credential Store" not in source
    assert "LAB_ID.fullmatch" in source
    assert "Checkpointer initialization failed; using a stateless graph" in source
    assert "return [_inventory_tool(layer) for layer in LAYERS] + [catalog_lineage]" in source
    assert "Metric tools are unavailable; catalog tools remain active" in source
    assert '"stage": stage' in source
    assert 'getattr(response, "status_code", None)' in source
    assert 'return _error_response(self.setup_error)' in source
    assert "input=message" in source
    encoded_config = source.split("CONFIG = ", 1)[1].splitlines()[0]
    config = json.loads(encoded_config)
    assert config["catalog_name"] == "u101_aidp_lab"
    assert config["participant_key"] == "u101"
    assert config["table_prefix"] == "u101_"
    assert config["lineage_contracts"]["telco_lineage"]["ENTITY"] == [
        "u101_aidp_lab.oci_landing.u101_telco_lineage_crm_customers->"
        "u101_aidp_lab.oci_bronze.u101_telco_lineage_crm_customers"
    ]
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
    assert repr(DAMA_SYSTEM_PROMPT) in source
    assert "live Master Catalog scope" in source
    assert "observed table row counts" in source


def test_agent_pack_has_diverse_dama_acceptance_cases() -> None:
    cases = load_lab_pack("agent").agent["evaluation_cases"]
    assert len(cases) >= 10
    assert {tool for case in cases for tool in case["expected_tools"]} == {
        "catalog_inventory_landing",
        "catalog_inventory_bronze",
        "catalog_inventory_silver",
        "catalog_inventory_gold",
        "catalog_metrics_telco_lineage",
        "catalog_lineage",
    }
    assert {case["category"] for case in cases} >= {
        "catalog",
        "quality",
        "entity_lineage",
        "column_lineage",
        "stewardship",
        "security",
    }
    assert all(case["required_concepts"] for case in cases)


def test_governance_contract_contains_only_active_data_labs() -> None:
    client = bare_client()
    manifest = {
        "layout_version": 4,
        "owner_key": "owner",
        "participant_key": "u101",
        "participant_code": 101,
        "participant_email": EMAIL,
        "labs": {
            "banking": {
                "phase": "active",
                "workspace_path": f"/Workspace/medallon/u101_{EMAIL}/banking",
            },
            "telecommunications": {
                "phase": "content",
                "workspace_path": f"/Workspace/medallon/u101_{EMAIL}/telecommunications",
            },
            "agent": {
                "phase": "active",
                "workspace_path": f"/Workspace/medallon/u101_{EMAIL}/agent",
            },
        },
    }

    metrics, lineage = client._governance_contract(manifest, "owner")

    assert {lab_id for lab_id, _name, _value in metrics} == {"banking"}
    assert ("banking", "contract.assignment_status", "active") in metrics
    assert ("banking", "contract.source_row_count.customers", "200") in metrics
    assert {level for _lab_id, level, _path in lineage} == {"ENTITY", "COLUMN"}
    assert all(path.startswith("CONTRACT:") for _lab_id, _level, path in lineage)


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
    client._shared_compute = lambda workspace: (
        {"key": "shared-spark-key"}
        if workspace == "workspace"
        else (_ for _ in ()).throw(AssertionError("unexpected workspace"))
    )
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
    client._ensure_agent_deployment = lambda *_args: (
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
    assert {"/workspaces/workspace/clusters/shared-spark-key", "/catalogs/u101-catalog-key"} <= set(
        permission_paths
    )
    source = str(captured["source"])
    assert "u101_aidp_lab" in source and "shared-spark-key" in source
    assert "LAB_METRICS" not in source and "LINEAGE_RELATIONS" not in source
    assert captured["descriptor"]["participant_key"] == "u101"
    assert captured["descriptor"]["entry_file"] == "governance_agent.py"
    assert writes[-1]["agent"]["catalog_name"] == "u101_aidp_lab"


def test_external_catalog_uses_the_live_adw_connection_contract() -> None:
    client = bare_client()
    database = ParticipantDatabase(
        owner="U101_AGENT",
        reader="U101_AGENT_RO",
        reader_password="ReaderPassword1234567890",
        dsn="aidp_low",
        wallet_password="WalletPassword123",
        wallet_zip=b"wallet",
    )
    catalogs = iter((None, {"key": "u101-external-catalog"}))
    requests: list[tuple[str, str, dict]] = []
    client._catalog = lambda *_args, **_kwargs: next(catalogs)
    client._request = lambda method, path, **kwargs: requests.append(
        (method, path, kwargs["payload"])
    )

    catalog, created = client._ensure_external_catalog("u101", database)

    properties = requests[0][2]["connectionDetails"]["connectionProperties"]
    assert created and catalog["key"] == "u101-external-catalog"
    assert set(properties) == {
        "ADW_WALLET_CONTENT_BASE64",
        "ADW_WALLET_PASSWORD",
        "ADW_USERNAME",
        "ADW_PASSWORD",
        "ADW_TNS_ALIAS",
    }
    assert requests[1] == (
        "PUT",
        "/catalogs/u101-external-catalog",
        {
            "connectionDetails": {
                "connectionProperties": {"ALH_PASSWORD": database.reader_password}
            }
        },
    )


def test_existing_external_catalog_refreshes_the_aidp_password_contract() -> None:
    client = bare_client()
    database = ParticipantDatabase(
        owner="U101_AGENT",
        reader="U101_AGENT_RO",
        reader_password="ReaderPassword1234567890",
        dsn="aidp_low",
        wallet_password="WalletPassword123",
        wallet_zip=b"wallet",
    )
    client._catalog = lambda *_args, **_kwargs: {"key": "catalog-key"}
    requests: list[tuple[str, str, dict]] = []
    client._request = lambda method, path, **kwargs: requests.append(
        (method, path, kwargs["payload"])
    )

    catalog, created = client._ensure_external_catalog("u101", database)

    assert catalog["key"] == "catalog-key" and created is False
    assert requests == [
        (
            "PUT",
            "/catalogs/catalog-key",
            {
                "connectionDetails": {
                    "connectionProperties": {"ALH_PASSWORD": database.reader_password}
                }
            },
        )
    ]


def test_removed_lab_is_hidden_from_the_agent_without_deleting_evidence() -> None:
    client = bare_client()
    merged: list[tuple[str, list[tuple[str, str, str]], list]] = []
    client.governance_database = SimpleNamespace(
        ready=lambda: True,
        merge_governance=lambda participant, metrics, lineage: merged.append(
            (participant, metrics, lineage)
        ),
    )
    manifest = {
        "layout_version": 4,
        "owner_key": "owner",
        "participant_key": "u101",
        "participant_code": 101,
        "participant_email": EMAIL,
        "labs": {
            "agent": {
                "phase": "active",
                "workspace_path": f"/Workspace/medallon/u101_{EMAIL}/agent",
            }
        },
    }

    client._mark_governance_lab_removed("u101", manifest, "owner", "banking")

    assert merged == [
        (
            "u101",
            [("banking", "contract.assignment_status", "removed")],
            [],
        )
    ]


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


def test_external_schema_uses_the_published_schema_key() -> None:
    client = bare_client()
    client._list = lambda *_args, **_kwargs: [
        {
            "key": "u101-agent-schema-key",
            "displayName": "u101_agent",
            "lifecycleState": "ACTIVE",
        }
    ]

    schema = client._external_schema("catalog-key", "U101_AGENT")

    assert schema["key"] == "u101-agent-schema-key"


def test_external_schema_waits_for_catalog_import() -> None:
    client = bare_client()
    client._list = lambda *_args, **_kwargs: []

    with pytest.raises(AidpProvisionPending) as raised:
        client._external_schema("catalog-key", "U101_AGENT")

    assert raised.value.phase == "database"


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
