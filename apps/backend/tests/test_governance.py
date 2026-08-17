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
    credential_name,
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
        compartment_id="ocid1.compartment.oc1..example",
        autonomous_database_id="ocid1.autonomousdatabase.oc1.us-chicago-1.example",
    )
    client.governance_database = SimpleNamespace(ready=lambda: False)
    return client


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
    assert credential_name(participant_key).startswith(participant_key)
    assert external_catalog_name(participant_key).startswith(participant_key)


@pytest.mark.parametrize("participant_key", ["u100", "u_101", "u101x", "U101", "u1;drop user admin"])
def test_database_names_reject_non_participant_identifiers(participant_key: str) -> None:
    with pytest.raises(ValueError, match="starting at u101"):
        database_names(participant_key)


def test_agent_source_contains_only_predefined_read_queries() -> None:
    source = agent_source(
        model_id="ocid1.generativeaimodel.oc1.us-chicago-1.chat",
        region="us-chicago-1",
        compartment_id="ocid1.compartment.oc1..participant",
        external_catalog_key="u101-catalog-key",
        database_schema="U101_AGENT",
    ).decode("utf-8")

    assert source.count('"SELECT ') == 3
    assert all(keyword not in source.upper() for keyword in (" INSERT ", " UPDATE ", " DELETE ", " MERGE ", " DROP ", " ALTER "))
    assert "u102" not in source.casefold()
    assert "{{lab_id}}" in source and "{{lineage_level}}" in source
    encoded_config = source.split("CONFIG = ", 1)[1].splitlines()[0]
    assert json.loads(encoded_config)["schema"] == "U101_AGENT"


def test_agent_prompt_is_dama_grounded_and_explainable() -> None:
    source = agent_source(
        model_id="ocid1.generativeaimodel.oc1.us-chicago-1.chat",
        region="us-chicago-1",
        compartment_id="ocid1.compartment.oc1..participant",
        external_catalog_key="u101-catalog-key",
        database_schema="U101_AGENT",
    ).decode("utf-8")

    assert "DAMA-DMBOK" in DAMA_SYSTEM_PROMPT
    assert all(
        heading in DAMA_SYSTEM_PROMPT
        for heading in ("Evidence", "Explanation", "Governance implication", "Recommendation or limitation")
    )
    assert "another participant" in DAMA_SYSTEM_PROMPT
    assert "If evidence is unavailable" in DAMA_SYSTEM_PROMPT
    assert repr(DAMA_SYSTEM_PROMPT) in source


def test_agent_pack_has_diverse_dama_acceptance_cases() -> None:
    cases = load_lab_pack("agent").agent["evaluation_cases"]
    assert len(cases) >= 10
    assert {tool for case in cases for tool in case["expected_tools"]} == {
        "catalog_inventory",
        "catalog_metrics",
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


def test_agent_stays_pending_before_database_bootstrap_without_creating_compute() -> None:
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
    writes: list[dict] = []
    client._workspace = lambda: {"key": "workspace"}
    client._write_manifest = lambda _workspace, _owner, value: writes.append(
        json.loads(json.dumps(value))
    )
    client._ensure_workspace_layout = lambda *_args: (_ for _ in ()).throw(
        AssertionError("workspace must not be created before the database boundary")
    )
    client._ensure_agent_compute = lambda *_args: (_ for _ in ()).throw(
        AssertionError("compute must not be created before the database boundary")
    )

    with pytest.raises(AidpProvisionPending) as raised:
        client._provision_agent(USER_OCID, EMAIL, manifest, manifest["labs"]["agent"], pack)

    assert raised.value.phase == "database"
    assert manifest["labs"]["agent"]["phase"] == "database"
    assert writes[-1]["external_catalog"] is None


def test_agent_creates_participant_database_before_any_compute() -> None:
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
    database = ParticipantDatabase(
        owner="U101_AGENT",
        reader="U101_AGENT_RO",
        reader_password="ReaderPassword1234567890",
        dsn="aidp_low",
        wallet_password="WalletPassword123",
        wallet_zip=b"wallet",
    )
    writes: list[dict] = []
    client._workspace = lambda: {"key": "workspace"}
    client.governance_database = SimpleNamespace(
        ready=lambda: True,
        ensure_participant=lambda key: database if key == "u101" else None,
    )
    client._ensure_external_catalog = lambda key, value: (
        {"key": "u101-external-catalog"},
        {"key": "u101-database-credential"},
        True,
    ) if key == "u101" and value == database else (_ for _ in ()).throw(
        AssertionError("unexpected participant database")
    )
    client._external_schema = lambda catalog, owner: {
        "key": "u101-agent-schema-key",
        "displayName": owner,
    } if catalog == "u101-external-catalog" and owner == "U101_AGENT" else (
        _ for _ in ()
    ).throw(AssertionError("unexpected external schema lookup"))
    client._write_manifest = lambda _workspace, _owner, value: writes.append(
        json.loads(json.dumps(value))
    )
    client._ensure_agent_compute = lambda *_args: (_ for _ in ()).throw(
        AssertionError("compute must wait for the external catalog boundary")
    )

    with pytest.raises(AidpProvisionPending) as raised:
        client._provision_agent(USER_OCID, EMAIL, manifest, manifest["labs"]["agent"], pack)

    assert raised.value.phase == "database"
    assert writes[-1]["external_catalog"] == {
        "key": "u101-external-catalog",
        "name": external_catalog_name("u101"),
        "database_schema": "u101-agent-schema-key",
        "credential_key": "u101-database-credential",
    }
    assert writes[-1]["labs"]["agent"]["external_catalog_key"] == "u101-external-catalog"


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
    client._ensure_database_credential = lambda *_args: {
        "key": "u101-database-credential"
    }

    def request(method: str, path: str, **kwargs):
        if method == "GET":
            return {
                "connectionDetails": {
                    "connectionProperties": {
                        "type": "ORACLE_ADW",
                        "tns": "aidp_low",
                        "user.name": "U101_AGENT_RO",
                        "credential_id": "memory-credential",
                    }
                }
            }
        requests.append((method, path, kwargs["payload"]))

    client._request = request

    catalog, credential, created = client._ensure_external_catalog("u101", database)

    properties = requests[0][2]["connectionDetails"]["connectionProperties"]
    assert created and catalog["key"] == "u101-external-catalog"
    assert credential["key"] == "u101-database-credential"
    assert set(properties) == {
        "ADW_WALLET_CONTENT_BASE64",
        "ADW_WALLET_PASSWORD",
        "ADW_USERNAME",
        "ADW_PASSWORD",
        "ADW_TNS_ALIAS",
    }
    rebound = requests[1][2]["connectionDetails"]["connectionProperties"]
    assert requests[1][:2] == ("PUT", "/catalogs/u101-external-catalog")
    assert rebound["credential_id"] == "u101-database-credential"
    assert rebound["tns"] == "aidp_low"


def test_database_credential_is_persistent_and_rotatable() -> None:
    client = bare_client()
    current = {"key": "credential-key", "displayName": credential_name("u101")}
    credentials = iter((None, current, current))
    client._database_credential = lambda *_args, **_kwargs: next(credentials)
    requests: list[tuple[str, str, dict]] = []
    client._request = lambda method, path, **kwargs: requests.append(
        (method, path, kwargs["payload"])
    )

    created = client._ensure_database_credential("u101", "first-password")
    rotated = client._ensure_database_credential("u101", "second-password")

    assert created == rotated == current
    assert requests[0][:2] == ("POST", "/credentials")
    assert requests[1][:2] == ("PUT", "/credentials/credential-key")
    assert requests[0][2]["credentialDetails"]["secretTokenPair"] == [
        {"secretKey": "password", "secretValue": "first-password"}
    ]
    assert requests[1][2]["credentialDetails"]["secretTokenPair"] == [
        {"secretKey": "password", "secretValue": "second-password"}
    ]


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


def test_missing_agent_deployment_is_created_on_shared_ai_compute() -> None:
    client = bare_client()
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
                "displayName": "u101_agent_data_governance_deployment",
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


def test_external_catalog_cleanup_deletes_the_exact_database_credential() -> None:
    client = bare_client()
    credential = {"key": "u101-database-credential"}
    credentials = iter((credential, None))
    client._catalog = lambda *_args, **_kwargs: None
    client._database_credential = lambda *_args, **_kwargs: next(credentials)
    requests: list[tuple[str, str]] = []
    client._request = lambda method, path, **_kwargs: requests.append((method, path))

    client._cleanup_external_catalog("u101", {})

    assert requests == [("DELETE", "/credentials/u101-database-credential")]


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
