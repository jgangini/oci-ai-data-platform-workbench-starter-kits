from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from app.aidp import AidpClient, AidpProvisionPending, participant_owner_key
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
    client.base = "https://aidp.example.invalid/20260430/aiDataPlatforms/platform"
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
        True,
    ) if key == "u101" and value == database else (_ for _ in ()).throw(
        AssertionError("unexpected participant database")
    )
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
        "database_schema": "U101_AGENT",
    }
    assert writes[-1]["labs"]["agent"]["external_catalog_key"] == "u101-external-catalog"


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
