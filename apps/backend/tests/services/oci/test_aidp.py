import asyncio
import hashlib
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services.oci.aidp import API_VERSION, AidpClient, AidpProvisionConflict, AidpProvisionError, AidpProvisionPending, LocalAidpClient, UserMaterial, participant_owner_key
from app.config import Settings
from app.packages.catalog import PackageAsset, load_package
from app.kits.naming import participant_folder, participant_key, table_name, workspace_participant_root, workspace_root


USER_OCID = "ocid1.user.oc1..aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
EMAIL = "ada@example.com"


def test_aidp_api_version_matches_current_platform() -> None:
    assert API_VERSION == "20260430"


class FakeResponse:
    def __init__(self, body=None, *, headers=None, status_code=200, content=None) -> None:
        self._body = body
        self.headers = headers or {}
        self.status_code = status_code
        self.content = content if content is not None else (
            json.dumps(body).encode() if body is not None else b""
        )

    def json(self):
        return self._body


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
        compartment_id="ocid1.compartment.oc1..example",
    )
    return client


@pytest.mark.parametrize('expected', [
    b'{"layout_version": 5, "participant_key": "u101"}',
    b'{\n  "z": [1, true],\n  "a": "Governance"\n}\n',
])
def test_uploaded_json_readback_preserves_content_across_native_json_parsing(expected):
    from unittest.mock import Mock

    client = bare_client()
    parsed = client._response_body(FakeResponse(json.loads(expected)))
    client._workspace_object = Mock(side_effect=[(None, {}), (parsed, {'type': 'FILE'})])
    client._request = Mock()
    assert client._upload_file('workspace', '/Workspace/agent-manifest.json', expected)
    assert client._request.call_count == 1
    assert client._request.call_args.kwargs['data'] == expected


@pytest.mark.parametrize('actual,expected', [
    ({'enabled': True}, b'{"enabled": 1}'),
    ({'version': 2}, b'{"version": 1}'),
    ({'name': 'agent'}, b'not JSON'),
    (b'{"a":1}', b'{ "a": 1 }'),
    ('source changed', b'source'),
])
def test_workspace_content_readback_rejects_material_or_raw_byte_drift(actual, expected):
    assert not AidpClient._content_matches(actual, expected)


def test_participant_code_names_technical_resources_and_email_names_workspace() -> None:
    owner = "u_" + hashlib.sha256(USER_OCID.encode()).hexdigest()[:16]
    assert participant_owner_key(USER_OCID) == owner
    assert participant_key(101) == "u101"
    assert workspace_participant_root("u101", "banking", EMAIL) == f"/Workspace/banking/u101_{EMAIL}"
    assert workspace_root("u101", "banking", EMAIL) == f"/Workspace/banking/u101_{EMAIL}"
    assert table_name("u101", "banking", "accounts") == "u101_banking_accounts"
    assert participant_folder("student/lab@example.com") == "student%2Flab@example.com"
    with pytest.raises(ValueError, match="valid OCI user OCID"):
        participant_owner_key(EMAIL)
    with pytest.raises(ValueError, match="starting at 101"):
        participant_key(100)
    with pytest.raises(ValueError, match="valid participant key"):
        workspace_participant_root(EMAIL, "banking", EMAIL)


def test_governed_access_fails_closed_when_direct_catalog_select_remains() -> None:
    client = bare_client()
    client._list = lambda *_args, **_kwargs: [{
        "grantee": USER_OCID, "granteeType": "USER", "granteePermissions": ["SELECT"],
    }]
    with pytest.raises(AidpProvisionError, match="revoke that permission"):
        client._assert_permission_absent("/catalogs/aidp-lab", USER_OCID, "SELECT")


def test_governed_access_accepts_catalog_without_direct_select() -> None:
    client = bare_client()
    client._list = lambda *_args, **_kwargs: []
    client._assert_permission_absent("/catalogs/aidp-lab", USER_OCID, "SELECT")








def test_participant_receives_select_only_on_owned_lab_tables() -> None:
    client = bare_client()
    pack = load_package("banking")
    first_layer, logical_names = next(iter(pack.tables.items()))
    owned_name = table_name("u101", "banking", logical_names[0])
    schemas = {
        layer: {"key": f"schema-{layer}", "displayName": f"oci_{layer}"}
        for layer in pack.tables
    }
    client._shared_schemas = lambda _catalog_key: schemas
    client._schema_tables = lambda _catalog_key, schema_key: [
        *([{"key": f"{schema_key}-owned", "displayName": owned_name}]
          if schema_key == f"schema-{first_layer}" else []),
        {"key": f"{schema_key}-other", "displayName": "u102_banking_accounts"},
    ]
    granted: list[tuple[str, str, str, str]] = []
    client._ensure_permission = lambda resource, action, principal, permission: granted.append(
        (resource, action, principal, permission)
    )

    client._ensure_lab_table_permissions("oci-medallion", "u101", "banking", USER_OCID)

    assert granted == [(
        f"/tables/schema-{first_layer}-owned",
        "assignTablePermissionDetails",
        USER_OCID,
        "SELECT",
    )]


def test_request_retry_token_covers_binary_content_and_identity_headers() -> None:
    client = bare_client()
    calls: list[dict] = []

    class Session:
        def request(self, _method, _url, **kwargs):
            calls.append(kwargs)
            return FakeResponse()

    client.session = Session()
    client._request("POST", "/objects", data=b"binary", headers={"path": "/one", "type": "FILE"})
    client._request("POST", "/objects", data=b"binary", headers={"path": "/one", "type": "FILE"})
    client._request("POST", "/objects", data=b"changed", headers={"path": "/one", "type": "FILE"})
    tokens = [call["headers"]["opc-retry-token"] for call in calls]
    assert tokens[0] == tokens[1]
    assert tokens[0] != tokens[2]


def test_request_error_identifies_safe_operation_without_disclosing_secrets() -> None:
    client = bare_client()

    class Session:
        def request(self, *_args, **_kwargs):
            return FakeResponse(
                {
                    "code": "InvalidParameter",
                    "message": (
                        "student+alias@example.com sent token=do-not-leak and "
                        "https://objectstorage.example/par-secret for "
                        "ocid1.user.oc1..secret"
                    ),
                },
                status_code=400,
            )

    client.session = Session()
    with pytest.raises(AidpProvisionError) as raised:
        client._request(
            "POST",
            "/workspaces/private-workspace/objects",
            data=b"private-content",
            headers={
                "path": "/Workspace/medallon/u_1111111111111111/banking/source/accounts.csv",
                "type": "FILE",
            },
            phase="content",
        )

    detail = str(raised.value)
    assert "POST /workspaces/{workspace}/objects during content" in detail
    assert "/Workspace/medallon/u_1111111111111111/banking/source/accounts.csv" in detail
    assert "400" in detail and "InvalidParameter" in detail
    assert "private-content" not in detail
    assert "private-workspace" not in detail
    assert "student+alias@example.com" not in detail
    assert "do-not-leak" not in detail
    assert "par-secret" not in detail
    assert "ocid1.user" not in detail


def test_list_follows_opc_next_page_and_preserves_filters() -> None:
    client = bare_client()
    calls: list[dict | None] = []

    def request(_method, _path, *, params=None, **_kwargs):
        calls.append(params)
        if len(calls) == 1:
            return {"items": [{"key": "one"}]}, {"opc-next-page": "next-token"}
        return [{"key": "two"}], {}

    client._request = request
    assert client._list("/schemas", params={"catalogKey": "catalog"}) == [
        {"key": "one"}, {"key": "two"}
    ]
    assert calls[1] == {
        "limit": "100", "catalogKey": "catalog", "page": "next-token"
    }


def test_list_fails_closed_on_repeated_page_token() -> None:
    client = bare_client()
    client._request = lambda *_args, **_kwargs: ({"items": []}, {"opc-next-page": "same"})
    with pytest.raises(AidpProvisionError, match="repeated pagination token"):
        client._list("/catalogs")


def test_job_contract_is_derived_from_pack_and_accepts_a_sixth_notebook() -> None:
    client = bare_client()
    pack = load_package("banking")
    extra = PackageAsset(
        "06_extension_banking.ipynb",
        pack.notebooks[-1].path,
        pack.notebooks[-1].sha256,
        task_key="06_extension_banking",
        depends_on=(pack.notebooks[-1].task_key or "",),
    )
    extended = replace(pack, notebooks=(*pack.notebooks, extra))
    parameters = {
        "participant_key": participant_key(101),
        "lab_id": "banking",
        "workspace_root": workspace_root(participant_key(101), "banking", EMAIL),
        "bucket_name": "bucket",
        "objectstorage_namespace": "namespace",
    }
    tasks = client._job_tasks("/Workspace/lab", "compute", extended.notebooks, parameters)
    assert len(tasks) == 6
    assert tasks[-1]["taskKey"] == "06_extension_banking"
    assert tasks[-1]["dependsOn"] == [{"taskKey": "05_lineage_banking"}]
    assert tasks[-1]["parameters"] == [
        {"name": name, "value": value} for name, value in parameters.items()
    ]
    assert client._job_tasks_match(tasks, tasks, "compute")


def test_two_participants_upload_identical_pack_assets_with_isolated_job_identity() -> None:
    client = bare_client()
    pack = load_package("banking")

    def capture(key: str, root: str):
        files: dict[str, bytes] = {}
        notebooks: dict[str, bytes] = {}
        jobs: list[tuple[str, str]] = []
        client._upload_file = lambda _workspace, path, content, **_kwargs: (
            files.__setitem__(path.removeprefix(root), content) or False
        )
        client._upload_notebook = lambda _workspace, path, notebook, **_kwargs: (
            notebooks.__setitem__(
                path.removeprefix(root),
                json.dumps(notebook, sort_keys=True, separators=(",", ":")).encode(),
            )
            or False
        )
        client._ensure_job = lambda _workspace, _compute, participant, _pack, job_root, _catalog, **_kwargs: (
            jobs.append((participant, job_root)) or (f"wf_{participant}_banking", "job", False)
        )
        client._ensure_participant_content(
            "workspace", "compute", key, pack, root, f"{key}_aidp", True
        )
        files.pop("/lab-manifest.json")
        return files, notebooks, jobs

    first = capture("u_1111111111111111", "/Workspace/medallon/u_1111111111111111/banking")
    second = capture("u_2222222222222222", "/Workspace/medallon/u_2222222222222222/banking")
    assert first[:2] == second[:2]
    assert first[2] != second[2]


@pytest.mark.parametrize("version", [2, 3, 4])
def test_historical_participant_manifests_are_rejected_without_writes(version):
    client = bare_client()
    owner = participant_owner_key(USER_OCID)
    manifest = {"layout_version": version, "participant_key": owner, "industry": "banking"}
    writes = []
    client._manifest = lambda *_: manifest
    client._write_manifest = lambda *args: writes.append(args)
    with pytest.raises(Exception, match="Unsupported participant layout"):
        client._ensure_manifest("workspace", owner, EMAIL, 101, "banking")
    assert writes == []


def test_new_v5_manifest_uses_code_and_email_workspace_paths() -> None:
    client = bare_client()
    owner_key = participant_owner_key(USER_OCID)
    key = participant_key(101)
    writes: list[dict] = []
    client._manifest = lambda _workspace, _key: None
    client._write_manifest = lambda _workspace, _key, value: writes.append(
        json.loads(json.dumps(value))
    )

    manifest = client._ensure_manifest(
        "workspace", owner_key, "student+alias@example.com", 101, ("banking", "retail")
    )

    assert writes == [manifest]
    assert {
        lab_id: state["workspace_path"]
        for lab_id, state in manifest["labs"].items()
    } == {
        "banking": workspace_root(key, "banking", "student+alias@example.com"),
        "retail": workspace_root(key, "retail", "student+alias@example.com"),
    }
    assert manifest["owner_key"] == owner_key
    assert manifest["participant_code"] == 101
    assert manifest["participant_email"] == "student+alias@example.com"
    assert manifest["layout_version"] == 5
    assert manifest["catalog"]["name"] == "oci_medallion"


def test_local_multi_lab_lifecycle_is_idempotent_and_protects_last_lab() -> None:
    async def run() -> None:
        client = LocalAidpClient(Settings(local_development_mode=True))
        materials = await client.provision_user(
            USER_OCID, EMAIL, ["banking", "retail"], 101
        )
        assert isinstance(materials, tuple)
        assert [material.lab_id for material in materials] == ["banking", "retail"]
        assert materials == await client.provision_user(
            USER_OCID, EMAIL, ["banking", "retail"], 101
        )
        await client.add_lab(USER_OCID, EMAIL, "healthcare")
        operation_id = "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43"
        first = await client.redeploy_lab(USER_OCID, EMAIL, "banking", operation_id)
        assert first == await client.redeploy_lab(USER_OCID, EMAIL, "banking", operation_id)
        await client.delete_lab(USER_OCID, "retail", operation_id)
        await client.delete_lab(USER_OCID, "healthcare", operation_id)
        with pytest.raises(AidpProvisionConflict, match="last lab"):
            await client.delete_lab(USER_OCID, "banking", operation_id)
        await client.cleanup_user(USER_OCID)
        assert client.users == {}

    asyncio.run(run())


def test_cleanup_lab_targets_only_declared_lab_resources() -> None:
    client = bare_client()
    key = participant_key(101)
    state = {
        "pack_version": "1.0.0",
        "pack_hash": load_package("banking").pack_sha256,
        "workspace_path": workspace_root(key, "banking", EMAIL),
        "job_name": f"wf_{key}_banking",
        "phase": "active",
        "operation": None,
    }
    calls: list[tuple[str, ...]] = []
    client._cleanup_lab_job = lambda workspace, job: calls.append(("job", workspace, job))
    client._catalog = lambda _name, **_kwargs: {"key": "catalog"}
    client._cleanup_lab_tables = lambda catalog, participant, lab: calls.append(
        ("tables", catalog, participant, lab)
    )
    client._cleanup_lab_object_storage = lambda participant, lab: calls.append(
        ("objects", participant, lab)
    )
    client._delete_workspace_path = lambda workspace, path, _message: calls.append(
        ("workspace", workspace, path)
    )
    client._cleanup_lab("workspace", key, "banking", state)
    assert calls == [
        ("job", "workspace", f"wf_{key}_banking"),
        ("tables", "catalog", key, "banking"),
        ("objects", key, "banking"),
        ("workspace", "workspace", workspace_root(key, "banking", EMAIL)),
    ]


def test_full_cleanup_removes_participant_object_storage_markers() -> None:
    client = bare_client()
    deleted: list[str] = []
    client._delete_object_storage_prefix = deleted.append
    client._object_storage_prefix_exists = lambda _prefix: False

    client._cleanup_participant_object_storage("u101")

    assert deleted == [
        "01_landing/users/u101/",
        "02_bronze/users/u101/",
        "03_silver/users/u101/",
        "04_gold/users/u101/",
    ]


def test_redeploy_cleanup_preserves_workspace_container_for_in_place_repair() -> None:
    client = bare_client()
    key = participant_key(101)
    state = {
        "workspace_path": workspace_root(key, "banking", EMAIL),
        "job_name": f"wf_{key}_banking",
    }
    calls: list[tuple[str, ...]] = []
    client._cleanup_lab_job = lambda workspace, job: calls.append(("job", workspace, job))
    client._catalog = lambda _name, **_kwargs: {"key": "catalog"}
    client._cleanup_lab_tables = lambda catalog, participant, lab: calls.append(
        ("tables", catalog, participant, lab)
    )
    client._cleanup_lab_object_storage = lambda participant, lab: calls.append(
        ("objects", participant, lab)
    )
    client._delete_workspace_path = lambda *_args: calls.append(("workspace",))

    client._cleanup_lab(
        "workspace", key, "banking", state, preserve_workspace=True
    )

    assert calls == [
        ("job", "workspace", f"wf_{key}_banking"),
        ("tables", "catalog", key, "banking"),
        ("objects", key, "banking"),
    ]


def test_redeploy_uses_in_place_cleanup_before_provisioning() -> None:
    client = bare_client()
    owner_key = participant_owner_key(USER_OCID)
    key = participant_key(101)
    state = {
        "pack_version": "1.0.0",
        "pack_hash": load_package("banking").pack_sha256,
        "workspace_path": workspace_root(key, "banking", EMAIL),
        "job_name": f"wf_{key}_banking",
        "phase": "active",
        "operation": None,
    }
    manifest = {
        "layout_version": 5,
        "owner_key": owner_key,
        "participant_key": key,
        "participant_code": 101,
        "participant_email": EMAIL,
        "labs": {"banking": state},
    }
    calls: list[tuple[str, bool]] = []
    client._workspace = lambda: {"key": "workspace"}
    client._manifest = lambda *_args: manifest
    client._write_manifest = lambda *_args: None
    client._cleanup_lab = lambda _workspace, _key, lab, _state, *, preserve_workspace=False: (
        calls.append((lab, preserve_workspace))
    )
    client._provision_lab = lambda *_args: UserMaterial(
        EMAIL,
        "banking",
        key,
        workspace_root(key, "banking", EMAIL),
        f"wf_{key}_banking",
        "1.0.0",
    )

    client._redeploy_lab(
        USER_OCID, EMAIL, "banking", "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43"
    )

    assert calls == [("banking", True)]


def test_full_cleanup_delegates_to_each_lab_without_cross_lab_discovery() -> None:
    client = bare_client()
    owner_key = participant_owner_key(USER_OCID)
    key = participant_key(101)
    roots = {
        lab_id: {
            "workspace_path": workspace_root(key, lab_id, EMAIL),
            "job_name": f"wf_{key}_{lab_id}",
        }
        for lab_id in ("banking", "retail")
    }
    client._workspace = lambda: {"key": "workspace"}
    client._manifest = lambda _workspace, _key: {
        "layout_version": 5,
        "owner_key": owner_key,
        "participant_key": key,
        "participant_code": 101,
        "participant_email": EMAIL,
        "labs": roots,
    }
    calls: list[tuple[str, str]] = []
    client._cleanup_lab = lambda _workspace, _key, lab_id, state: calls.append(
        (lab_id, state["workspace_path"])
    )
    client._catalog = lambda _name, **_kwargs: {"key": "catalog"}
    client._cleanup_participant_object_storage = lambda participant: calls.append(
        ("objects", participant)
    )
    client._delete_workspace_path = lambda *_args: None

    client._cleanup_user(owner_key)

    assert calls == [
        ("banking", workspace_root(key, "banking", EMAIL)),
        ("retail", workspace_root(key, "retail", EMAIL)),
        ("objects", key),
    ]


def test_cleanup_rejects_untrusted_workspace_path() -> None:
    client = bare_client()
    key = participant_key(101)
    with pytest.raises(Exception, match="exact (kit|lab) workspace path"):
        client._cleanup_lab(
            "workspace",
            key,
            "banking",
            {
                "workspace_path": "/Workspace/Shared/not-ours/banking",
                "job_name": f"wf_{key}_banking",
            },
        )


def test_current_manifest_rejects_workspace_for_a_different_email() -> None:
    client = bare_client()
    owner_key = participant_owner_key(USER_OCID)
    key = participant_key(101)
    with pytest.raises(Exception, match="exact (kit|lab) workspace path"):
        client._manifest_labs(
            {
                "layout_version": 5,
                "owner_key": owner_key,
                "participant_key": key,
                "participant_code": 101,
                "participant_email": EMAIL,
                "labs": {
                    "banking": {
                        "pack_version": "1.0.0",
                        "workspace_path": workspace_root(key, "banking", "other@example.com"),
                    }
                },
            },
            owner_key,
        )


def test_workspace_selection_uses_key_without_name_fallback():
    client = bare_client()
    client.settings = Settings(aidp_workspace_key="selected", aidp_workspace_name="old-name")
    client._list = lambda *args, **kwargs: [
        {"key": "wrong", "displayName": "old-name", "lifecycleState": "ACTIVE"},
        {"key": "selected", "displayName": "renamed", "lifecycleState": "ACTIVE"},
    ]
    assert client._workspace()["key"] == "selected"
    client.settings = replace(client.settings, aidp_workspace_key="not-in-platform")
    with pytest.raises(AidpProvisionPending):
        client._workspace()


def test_workspace_name_resolution_is_preserved_for_new_platform():
    client = bare_client()
    client.settings = Settings(aidp_workspace_name="default")
    client._list = lambda *args, **kwargs: [{"key": "default-key", "displayName": "default", "lifecycleState": "ACTIVE"}]
    assert client._workspace()["key"] == "default-key"
