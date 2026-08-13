import asyncio
import hashlib
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.aidp import (
    AidpClient,
    AidpProvisionConflict,
    AidpProvisionError,
    LocalAidpClient,
    UserMaterial,
    participant_key,
)
from app.config import Settings
from app.lab_packs import LabAsset, load_lab_pack
from app.notebooks import participant_folder, workspace_participant_root, workspace_root


USER_OCID = "ocid1.user.oc1..aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
EMAIL = "ada@example.com"


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
    client.base = "https://aidp.example.invalid/20240831/dataLakes/platform"
    client.signer = object()
    client._session_lock = threading.Lock()
    client._locks = {}
    client.settings = SimpleNamespace(
        bucket_name="bucket", objectstorage_namespace="namespace"
    )
    return client


def test_participant_key_is_stable_opaque_and_workspace_uses_only_the_key() -> None:
    expected = "u_" + hashlib.sha256(USER_OCID.encode()).hexdigest()[:16]
    assert participant_key(USER_OCID) == expected
    assert EMAIL not in expected
    assert len(expected) == 18
    assert workspace_participant_root(expected) == f"/Workspace/medallon/{expected}"
    assert workspace_root(expected, "banking") == f"/Workspace/medallon/{expected}/banking"
    assert participant_folder("student/lab@example.com") == "student%2Flab@example.com"
    with pytest.raises(ValueError, match="valid OCI user OCID"):
        participant_key(EMAIL)
    with pytest.raises(ValueError, match="valid participant key"):
        workspace_participant_root(EMAIL)


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


def test_job_contract_is_derived_from_pack_and_accepts_a_sixth_notebook() -> None:
    client = bare_client()
    pack = load_lab_pack("banking")
    extra = LabAsset(
        "06_extension_banking.ipynb",
        pack.notebooks[-1].path,
        pack.notebooks[-1].sha256,
        task_key="06_extension_banking",
        depends_on=(pack.notebooks[-1].task_key or "",),
    )
    extended = replace(pack, notebooks=(*pack.notebooks, extra))
    parameters = {
        "participant_key": participant_key(USER_OCID),
        "lab_id": "banking",
        "workspace_root": workspace_root(participant_key(USER_OCID), "banking"),
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
    pack = load_lab_pack("banking")

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
        client._ensure_job = lambda _workspace, _compute, participant, _pack, job_root, **_kwargs: (
            jobs.append((participant, job_root)) or (f"wf_{participant}_banking", "job", False)
        )
        client._ensure_participant_content(
            "workspace", "compute", key, pack, root, True
        )
        files.pop("/lab-manifest.json")
        return files, notebooks, jobs

    first = capture("u_1111111111111111", "/Workspace/medallon/u_1111111111111111/banking")
    second = capture("u_2222222222222222", "/Workspace/medallon/u_2222222222222222/banking")
    assert first[:2] == second[:2]
    assert first[2] != second[2]


def test_v2_manifest_migrates_to_v3_without_updating_assets() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    old_root = f"/Workspace/medallon/{participant_folder(EMAIL)}/banking"
    manifest = {
        "layout_version": 2,
        "participant_key": key,
        "industry": "banking",
        "workspace_path": old_root,
        "phase": "active",
    }
    writes: list[dict] = []
    client._manifest = lambda _workspace, _key: manifest if not writes else writes[-1]
    client._write_manifest = lambda _workspace, _key, value: writes.append(json.loads(json.dumps(value)))
    migrated = client._ensure_manifest("workspace", key, EMAIL, "banking")
    assert migrated["layout_version"] == 3
    assert migrated["labs"]["banking"] == {
        "pack_version": "legacy-v2",
        "pack_hash": "",
        "workspace_path": old_root,
        "job_name": f"wf_{key}_banking_medallion",
        "phase": "active",
        "operation": None,
    }

    client._workspace = lambda: {"key": "workspace"}
    client._ensure_workspace_layout = lambda *_args: False
    client._shared_compute = lambda *_args: (_ for _ in ()).throw(
        AssertionError("active v2 assets must not be rewritten")
    )
    assert client._provision_lab(USER_OCID, EMAIL, "banking", migrated).pack_version == "legacy-v2"


def test_new_v3_manifest_uses_opaque_participant_workspace_paths() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    writes: list[dict] = []
    client._manifest = lambda _workspace, _key: None
    client._write_manifest = lambda _workspace, _key, value: writes.append(
        json.loads(json.dumps(value))
    )

    manifest = client._ensure_manifest(
        "workspace", key, "student+alias@example.com", ("banking", "retail")
    )

    assert writes == [manifest]
    assert {
        lab_id: state["workspace_path"]
        for lab_id, state in manifest["labs"].items()
    } == {
        "banking": workspace_root(key, "banking"),
        "retail": workspace_root(key, "retail"),
    }
    assert "student" not in json.dumps(manifest)


def test_local_multi_lab_lifecycle_is_idempotent_and_protects_last_lab() -> None:
    async def run() -> None:
        client = LocalAidpClient(Settings(local_development_mode=True))
        materials = await client.provision_user(
            USER_OCID, EMAIL, ["banking", "retail"]
        )
        assert isinstance(materials, tuple)
        assert [material.lab_id for material in materials] == ["banking", "retail"]
        assert materials == await client.provision_user(
            USER_OCID, EMAIL, ["banking", "retail"]
        )
        await client.add_lab(USER_OCID, EMAIL, "healthcare")
        operation_id = "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43"
        first = await client.redeploy_lab(USER_OCID, EMAIL, "banking", operation_id)
        assert first == await client.redeploy_lab(USER_OCID, EMAIL, "banking", operation_id)
        await client.delete_lab(USER_OCID, "retail", operation_id)
        await client.delete_lab(USER_OCID, "healthcare", operation_id)
        with pytest.raises(AidpProvisionConflict, match="last lab"):
            await client.delete_lab(USER_OCID, "banking", operation_id)
        with pytest.raises(ValueError, match="available"):
            await client.add_lab(USER_OCID, EMAIL, "agent")
        await client.cleanup_user(USER_OCID)
        assert client.users == {}

    asyncio.run(run())


def test_cleanup_lab_targets_only_declared_lab_resources() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    state = {
        "pack_version": "1.0.0",
        "pack_hash": load_lab_pack("banking").pack_sha256,
        "workspace_path": workspace_root(key, "banking"),
        "job_name": f"wf_{key}_banking",
        "phase": "active",
        "operation": None,
    }
    calls: list[tuple[str, ...]] = []
    client._cleanup_lab_job = lambda workspace, job: calls.append(("job", workspace, job))
    client._catalog = lambda: {"key": "catalog"}
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
        ("workspace", "workspace", workspace_root(key, "banking")),
    ]


def test_full_cleanup_delegates_to_each_lab_without_cross_lab_discovery() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    roots = {
        lab_id: {
            "workspace_path": workspace_root(key, lab_id),
            "job_name": f"wf_{key}_{lab_id}",
        }
        for lab_id in ("banking", "retail")
    }
    client._workspace = lambda: {"key": "workspace"}
    client._manifest = lambda _workspace, _key: {
        "layout_version": 3,
        "participant_key": key,
        "labs": roots,
    }
    calls: list[tuple[str, str]] = []
    client._cleanup_lab = lambda _workspace, _key, lab_id, state: calls.append(
        (lab_id, state["workspace_path"])
    )
    client._catalog = lambda: {"key": "catalog"}
    client._cleanup_legacy_tables = lambda *_args: None
    client._cleanup_legacy_schemas = lambda *_args: None
    client._delete_workspace_path = lambda *_args: None

    client._cleanup_user(key)

    assert calls == [
        ("banking", workspace_root(key, "banking")),
        ("retail", workspace_root(key, "retail")),
    ]


def test_cleanup_rejects_untrusted_workspace_path() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    with pytest.raises(Exception, match="exact lab workspace path"):
        client._cleanup_lab(
            "workspace",
            key,
            "banking",
            {
                "workspace_path": "/Workspace/Shared/not-ours/banking",
                "job_name": f"wf_{key}_banking",
            },
        )


def test_v3_manifest_rejects_email_scoped_workspace_path() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    with pytest.raises(Exception, match="exact lab workspace path"):
        client._manifest_labs(
            {
                "layout_version": 3,
                "participant_key": key,
                "labs": {
                    "banking": {
                        "pack_version": "1.0.0",
                        "workspace_path": f"/Workspace/medallon/{EMAIL}/banking",
                    }
                },
            },
            key,
        )
