from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest


MODULE_PATH = Path(__file__).parents[1] / "hooks" / "post_apply.py"
SPEC = importlib.util.spec_from_file_location("post_apply", MODULE_PATH)
post_apply = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = post_apply
SPEC.loader.exec_module(post_apply)


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []
        self.list_calls: list[tuple[str, dict | None]] = []
        self.actions: dict[str, dict] = {
            "/roles/platform-admin-key": {
                "assignees": [{"type": "USER", "target": "ocid1.user.oc1..operator"}]
            }
        }
        self.workspace_objects: dict[str, str] = {}
        self.resources = {
            "/workspaces": [
                {
                    "displayName": "ws",
                    "key": "ws-key",
                    "type": "DEFAULT",
                    "lifecycleState": "ACTIVE",
                }
            ],
            "/catalogs": [],
            "/schemas": [],
            "/volumes": [],
            "/roles": [{"displayName": "AI_DATA_PLATFORM_ADMIN", "key": "platform-admin-key"}],
            "/workspaces/ws-key/clusters": [],
            "/credentials": [],
        }

    def list_all(self, path: str, *, params=None) -> list[dict]:
        self.list_calls.append((path, params))
        if path.endswith("/permissions"):
            return list(self.actions.get(path, []))
        items = list(self.resources[path])
        if params and params.get("displayName"):
            items = [item for item in items if item.get("displayName") == params["displayName"]]
        return items

    def request(
        self,
        method: str,
        path: str,
        *,
        payload=None,
        params=None,
        data=None,
        headers=None,
    ):
        self.calls.append((method, path, payload, params))
        if method == "GET":
            return self._read_request(method, path)
        return self._write_request(method, path, payload, headers)

    def _write_request(self, method: str, path: str, payload, headers):
        if method == "POST" and path in self.resources:
            return self._create_resource(path, payload)
        if method == "PUT" and path.startswith("/credentials/"):
            return self._update_credential(path, payload)
        if method == "DELETE" and path.startswith("/roles/"):
            return self._delete_role(path)
        if method in {"POST", "PUT"} and "/actions/" in path:
            return self._apply_action(path, payload)
        if method == "POST" and path == "/workspaces/ws-key/objects":
            return self._create_workspace_object(headers)
        return post_apply.ApiResponse(200, {}, {})

    def _read_request(self, method: str, path: str):
        if path.startswith("/workspaces/ws-key/objects/"):
            return self._get_workspace_object(method, path)
        if path.startswith("/schemas/"):
            return self._get_named_resource(path, "/schemas/", "/schemas")
        if path.startswith("/volumes/") and not path.endswith("/permissions"):
            return self._get_named_resource(path, "/volumes/", "/volumes")
        return post_apply.ApiResponse(200, self.actions.get(path, {}), {})

    def _create_resource(self, path: str, payload: dict) -> post_apply.ApiResponse:
        item = dict(payload)
        name = payload["displayName"]
        if path == "/catalogs":
            item["key"] = f"{name}-key"
        elif path == "/schemas":
            item["key"] = f"{payload['catalogName']}.{name}"
        elif path == "/volumes":
            item["key"] = f"{payload['catalogName']}.{payload['schemaName']}.{name}"
        else:
            item["key"] = f"{name}-key"
        if path in {"/catalogs", "/schemas", "/volumes", "/workspaces/ws-key/clusters"}:
            item["lifecycleState"] = "ACTIVE"
        self.resources[path].append(item)
        return post_apply.ApiResponse(201, item, {})

    def _update_credential(self, path: str, payload: dict) -> post_apply.ApiResponse:
        key = path.removeprefix("/credentials/")
        current = next(item for item in self.resources["/credentials"] if item["key"] == key)
        current.update(payload)
        return post_apply.ApiResponse(200, current, {})

    def _delete_role(self, path: str) -> post_apply.ApiResponse:
        key = path.removeprefix("/roles/")
        self.resources["/roles"] = [
            item for item in self.resources["/roles"] if item.get("key") != key
        ]
        return post_apply.ApiResponse(204, {}, {})

    def _apply_action(self, path: str, payload: dict) -> post_apply.ApiResponse:
        base_path = path.split("/actions/", 1)[0]
        if path.endswith("/addMember"):
            self.actions[base_path] = {"assignees": payload["assignees"]}
        else:
            self._record_permissions(base_path, payload)
        return post_apply.ApiResponse(200, {}, {})

    def _record_permissions(self, base_path: str, payload: dict) -> None:
        details = next(iter(payload.values()))
        assignees = details["assignees"]
        targets = assignees["targets"]
        inspect_path = f"{base_path}/permissions"
        self.actions.setdefault(inspect_path, []).extend([
            {
                "grantee": target,
                "granteeName": target,
                "granteeType": assignees["type"],
                "granteePermissions": details["permissions"],
                "isPermissionsInheritable": details.get("isPermissionsInheritable"),
            }
            for target in targets
        ])
        resource_type, resource_key = self._permission_resource(base_path)
        for target in targets:
            role_key = f"{target}-key"
            self.actions.setdefault(f"/roles/{role_key}/permissions", []).append({
                "roleKey": role_key,
                "permissionsWithResourceDetails": {
                    "permissions": details["permissions"],
                    "resourceType": resource_type,
                    "resourceKey": resource_key,
                },
            })

    @staticmethod
    def _permission_resource(base_path: str) -> tuple[str, str]:
        if "/clusters/" in base_path:
            return "CLUSTER", "ws/aidp_cluster_shared_compute"
        if base_path.startswith("/schemas/"):
            return "SCHEMA", base_path.removeprefix("/schemas/")
        if "/objects/" in base_path:
            return "FOLDER", base_path.rsplit("/", 1)[-1]
        if base_path.startswith("/workspaces/"):
            return "WORKSPACE", "ws"
        return "CATALOG", "aidp_lab"

    def _create_workspace_object(self, headers: dict) -> post_apply.ApiResponse:
        object_path = headers["path"]
        self.workspace_objects[object_path] = "medallon-key"
        return post_apply.ApiResponse(201, None, {"object-key": "medallon-key"})

    def _get_workspace_object(self, method: str, path: str) -> post_apply.ApiResponse:
        object_path = unquote(path.rsplit("/", 1)[-1])
        object_key = self.workspace_objects.get(object_path)
        if not object_key:
            raise post_apply.ApiRequestError(method, path, 404, "request-id")
        return post_apply.ApiResponse(200, None, {"object-key": object_key})

    def _get_named_resource(self, path: str, prefix: str, collection: str) -> post_apply.ApiResponse:
        key = path.removeprefix(prefix)
        item = next(item for item in self.resources[collection] if item["key"] == key)
        return post_apply.ApiResponse(200, item, {})


def test_governance_schema_permissions_allow_only_admins_and_dedicated_jdbc_user() -> None:
    api = FakeApi()
    technical_user = "ocid1.user.oc1..governance"
    api.resources["/schemas"] = [
        {"displayName": "oci_control", "key": "aidp_lab.oci_control"}
    ]
    api.actions["/schemas/aidp_lab.oci_control/permissions"] = [
        {
            "grantee": "AI_DATA_PLATFORM_ADMIN",
            "granteeName": "AI_DATA_PLATFORM_ADMIN",
            "granteeType": "ROLE",
            "granteePermissions": ["ADMIN"],
            "isInherited": True,
        },
        {
            "grantee": technical_user,
            "granteeName": "governance-service",
            "granteeType": "USER",
            "granteePermissions": ["ADMIN"],
            "isInherited": False,
        },
    ]
    post_apply.verify_governance_schema_permissions(
        api, "aidp_lab-key", technical_user, attempts=1
    )


def test_governance_schema_permissions_fail_closed_for_developer_access() -> None:
    api = FakeApi()
    technical_user = "ocid1.user.oc1..governance"
    api.resources["/schemas"] = [
        {"displayName": "oci_control", "key": "aidp_lab.oci_control"}
    ]
    api.actions["/schemas/aidp_lab.oci_control/permissions"] = [
        {
            "grantee": technical_user,
            "granteeType": "USER",
            "granteePermissions": ["ADMIN"],
        },
        {
            "grantee": "AIDP_DEVELOPER",
            "granteeName": "AIDP_DEVELOPER",
            "granteeType": "ROLE",
            "granteePermissions": ["SELECT"],
            "isInherited": True,
        },
    ]
    with pytest.raises(post_apply.ReconcileError, match="unauthorized inherited grant"):
        post_apply.verify_governance_schema_permissions(
            api, "aidp_lab-key", technical_user, attempts=1
        )


def test_governance_runtime_gets_only_schema_admin_and_cluster_use() -> None:
    api = FakeApi()
    technical_user = "ocid1.user.oc1..governance"
    schema_key = post_apply.ensure_governance_control_access(
        api,
        "aidp_lab-key",
        "aidp_lab",
        "ws-key",
        "aidp_cluster_shared_compute-key",
        technical_user,
    )
    assert schema_key == "aidp_lab.oci_control"
    permissions = [
        (path, next(iter(payload.values()))["permissions"])
        for method, path, payload, _params in api.calls
        if method == "POST" and payload and "/actions/managePermission" in path
    ]
    assert permissions == [
        ("/schemas/aidp_lab.oci_control/actions/managePermission", ["ADMIN"]),
        (
            "/workspaces/ws-key/clusters/aidp_cluster_shared_compute-key/actions/managePermission",
            ["USE"],
        ),
    ]
    assert not any(path.startswith("/catalogs/") for path, _permissions in permissions)
    assert {item["target"] for item in api.actions["/roles/platform-admin-key"]["assignees"]} == {
        "ocid1.user.oc1..operator"
    }


def test_governance_jdbc_driver_is_validated_and_uploaded(tmp_path: Path, monkeypatch) -> None:
    driver = tmp_path / "aidp-jdbc.zip"
    with zipfile.ZipFile(driver, "w") as archive:
        archive.writestr("driver/sparkJDBC42.jar", b"jar")
    monkeypatch.setenv(post_apply.GOVERNANCE_JDBC_DRIVER_ENV, str(driver))
    assert post_apply.governance_jdbc_driver_path() == driver.resolve()

    uploaded: dict[str, object] = {}

    class ObjectStorage:
        @staticmethod
        def put_object(namespace, bucket, name, body, **kwargs):
            uploaded.update(
                namespace=namespace,
                bucket=bucket,
                name=name,
                body=body.read(),
                **kwargs,
            )

    post_apply.upload_governance_jdbc_driver(
        ObjectStorage(), "namespace", "bucket", ".governance/aidp-jdbc-driver.zip", driver
    )
    assert uploaded["body"] == driver.read_bytes()
    assert uploaded["content_length"] == driver.stat().st_size
    assert uploaded["content_type"] == "application/zip"
    assert isinstance(uploaded["content_md5"], str)


def test_governance_jdbc_credential_is_created_then_reused() -> None:
    fingerprint = ":".join(["aa"] * 16)
    state: dict[str, object] = {
        "keys": [],
        "secret": base64.b64encode(b'{"status":"bootstrap_pending"}').decode("ascii"),
    }

    class Identity:
        @staticmethod
        def list_api_keys(_user_id):
            return SimpleNamespace(data=list(state["keys"]))

        @staticmethod
        def upload_api_key(_user_id, details):
            assert details.key.startswith("-----BEGIN PUBLIC KEY-----")
            item = SimpleNamespace(fingerprint=fingerprint)
            state["keys"].append(item)
            return SimpleNamespace(data=item)

        @staticmethod
        def delete_api_key(_user_id, value):
            state["keys"] = [item for item in state["keys"] if item.fingerprint != value]

    class Secrets:
        @staticmethod
        def get_secret_bundle(_secret_id):
            return SimpleNamespace(
                data=SimpleNamespace(
                    secret_bundle_content=SimpleNamespace(content=state["secret"])
                )
            )

    class Vault:
        @staticmethod
        def update_secret(_secret_id, details):
            state["secret"] = details.secret_content.content

    models = SimpleNamespace(
        CreateApiKeyDetails=lambda **kwargs: SimpleNamespace(**kwargs),
        UpdateSecretDetails=lambda **kwargs: SimpleNamespace(**kwargs),
        Base64SecretContentDetails=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    oci_module = SimpleNamespace(
        identity=SimpleNamespace(IdentityClient=lambda *_args, **_kwargs: Identity(), models=models),
        secrets=SimpleNamespace(SecretsClient=lambda *_args, **_kwargs: Secrets()),
        vault=SimpleNamespace(VaultsClient=lambda *_args, **_kwargs: Vault(), models=models),
        exceptions=SimpleNamespace(ServiceError=RuntimeError),
    )
    outputs = {
        "governance_gateway_jdbc_user_ocid": "ocid1.user.oc1..governance",
        "governance_gateway_jdbc_secret_ocid": "ocid1.vaultsecret.oc1..governance",
    }
    config = {"tenancy": "ocid1.tenancy.oc1..tenant", "region": "us-chicago-1"}
    assert post_apply.ensure_governance_jdbc_credential(
        oci_module, config, object(), outputs, "us-chicago-1", "cluster-key", attempts=1
    )
    assert not post_apply.ensure_governance_jdbc_credential(
        oci_module, config, object(), outputs, "us-chicago-1", "cluster-key", attempts=1
    )
    document = json.loads(base64.b64decode(state["secret"]))
    assert document["purpose"] == post_apply.GOVERNANCE_JDBC_PURPOSE
    assert document["fingerprint"] == fingerprint
    assert document["user_ocid"] == outputs["governance_gateway_jdbc_user_ocid"]
    assert "private_key_pem" in document


def test_reconcile_leaves_legacy_catalog_empty_for_private_participant_catalogs(monkeypatch) -> None:
    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)
    api = FakeApi()
    outputs = {
        "default_workspace_name": "ws",
        "objectstorage_namespace": "namespace",
        "bucket_name": "aidp-data-test",
        "developer_group_ocid": "ocid1.group.developer",
        "pending_group_ocid": "ocid1.group.pending",
        "operator_user_ocid": "ocid1.user.oc1..operator",
    }
    reconciled, events = post_apply.reconcile(api, outputs)
    _assert_empty_legacy_catalog(api, reconciled, events)
    _assert_shared_compute(api, reconciled)
    _assert_workspace_permissions(api)


def _assert_empty_legacy_catalog(api: FakeApi, reconciled: dict, events: list[str]) -> None:
    assert reconciled["catalog_key"] == "aidp_lab-key"
    assert reconciled["catalog_name"] == "aidp_lab"
    assert reconciled["global_schema_count"] == 0
    assert reconciled["external_volume_count"] == 0
    schema_posts = [
        payload
        for method, path, payload, _ in api.calls
        if method == "POST" and path == "/schemas"
    ]
    assert schema_posts == []
    assert not any(method == "POST" and path == "/volumes" for method, path, _, _ in api.calls)
    schema_queries = [params for path, params in api.list_calls if path == "/schemas"]
    volume_queries = [params for path, params in api.list_calls if path == "/volumes"]
    assert schema_queries == [{"catalogKey": "aidp_lab-key"}]
    assert volume_queries == []
    role_queries = [params for path, params in api.list_calls if path == "/roles"]
    assert {query["displayName"] for query in role_queries} == {
        "AI_DATA_PLATFORM_ADMIN",
        "AIDP_DEVELOPER",
        "AIDP_LAB_DEVELOPER",
        "AIDP_LAB_PENDING",
    }
    assert any("zero legacy schemas and zero external volumes" in event for event in events)


def _assert_shared_compute(api: FakeApi, reconciled: dict) -> None:
    cluster_payloads = [payload for method, path, payload, _ in api.calls if method == "POST" and path.endswith("/clusters")]
    assert cluster_payloads[0]["displayName"] == "aidp_cluster_shared_compute"
    assert cluster_payloads[0]["type"] == "USER"
    assert cluster_payloads[0]["driverConfig"]["driverShape"] == "amd.generic"
    assert cluster_payloads[0]["workerConfig"]["maxWorkerCount"] == 10
    assert "autoTerminationMinutes" not in cluster_payloads[0]
    assert cluster_payloads[0]["clusterRuntimeConfig"]["sparkAdvancedConfigurations"] == {
        "spark.aidp.lineage.enabled": "true"
    }
    assert reconciled["shared_compute_key"] == "aidp_cluster_shared_compute-key"
    assert reconciled["root_object_key"] == "medallon-key"
    assert "shared_schema_keys" not in reconciled


def _assert_workspace_permissions(api: FakeApi) -> None:
    workspace_permissions = api.actions["/workspaces/ws-key/permissions"]
    assert {
        (item["grantee"], tuple(item["granteePermissions"]))
        for item in workspace_permissions
    } == {
        ("AIDP_DEVELOPER", ("USER",)),
        ("AIDP_LAB_PENDING", ("USER",)),
    }
    assert "/catalogs/aidp_lab-key/permissions" not in api.actions
    compute_permissions = api.actions[
        "/workspaces/ws-key/clusters/aidp_cluster_shared_compute-key/permissions"
    ]
    assert {item["grantee"] for item in compute_permissions} == {"AIDP_DEVELOPER"}
    assert "/workspaces/ws-key/objects/medallon-key/permissions" not in api.actions


def test_reconcile_retires_legacy_developer_role_only_after_equivalence(monkeypatch) -> None:
    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)
    api = FakeApi()
    legacy_key = "AIDP_LAB_DEVELOPER-key"
    api.resources["/roles"].append(
        {"displayName": "AIDP_LAB_DEVELOPER", "key": legacy_key}
    )
    api.actions[f"/roles/{legacy_key}"] = {
        "assignees": [{"type": "GROUP", "target": "ocid1.group.developer"}]
    }
    api.actions[f"/roles/{legacy_key}/permissions"] = [
        {
            "permissionsWithResourceDetails": {
                "resourceType": "WORKSPACE",
                "resourceKey": "ws",
                "permissions": ["USER"],
            }
        },
        {
            "permissionsWithResourceDetails": {
                "resourceType": "CLUSTER",
                "resourceKey": "ws/aidp_cluster_shared_compute",
                "permissions": ["USE"],
            }
        },
    ]

    _, events = post_apply.reconcile(
        api,
        {
            "default_workspace_name": "ws",
            "objectstorage_namespace": "namespace",
            "bucket_name": "aidp-data-test",
            "developer_group_ocid": "ocid1.group.developer",
            "pending_group_ocid": "ocid1.group.pending",
            "operator_user_ocid": "ocid1.user.oc1..operator",
        },
    )

    assert not any(
        item.get("displayName") == "AIDP_LAB_DEVELOPER"
        for item in api.resources["/roles"]
    )
    assert ("DELETE", f"/roles/{legacy_key}") in {
        (method, path) for method, path, _, _ in api.calls
    }
    assert any("AIDP_LAB_DEVELOPER retired" in event for event in events)


def test_legacy_developer_role_with_broader_permissions_is_not_retired(monkeypatch) -> None:
    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)
    api = FakeApi()
    legacy_key = "AIDP_LAB_DEVELOPER-key"
    api.resources["/roles"].append(
        {"displayName": "AIDP_LAB_DEVELOPER", "key": legacy_key}
    )
    api.actions[f"/roles/{legacy_key}"] = {
        "assignees": [{"type": "GROUP", "target": "ocid1.group.developer"}]
    }
    api.actions[f"/roles/{legacy_key}/permissions"] = [
        {
            "permissionsWithResourceDetails": {
                "resourceType": "MASTER_CATALOG",
                "resourceKey": "master",
                "permissions": ["ADMIN"],
            }
        }
    ]

    with pytest.raises(post_apply.ReconcileError, match="unexpected direct permissions"):
        post_apply.reconcile(
            api,
            {
                "default_workspace_name": "ws",
                "objectstorage_namespace": "namespace",
                "bucket_name": "aidp-data-test",
                "developer_group_ocid": "ocid1.group.developer",
                "pending_group_ocid": "ocid1.group.pending",
                "operator_user_ocid": "ocid1.user.oc1..operator",
            },
        )

    assert all(
        not (method == "DELETE" and path == f"/roles/{legacy_key}")
        for method, path, _, _ in api.calls
    )


def test_reconcile_rejects_operator_without_platform_admin_membership(monkeypatch) -> None:
    monkeypatch.setattr(post_apply, "_sleep", lambda _: None)
    api = FakeApi()
    api.actions["/roles/platform-admin-key"] = {
        "assignees": [{"type": "USER", "target": "ocid1.user.oc1..another"}]
    }

    with pytest.raises(post_apply.ReconcileError, match="not an AI_DATA_PLATFORM_ADMIN member"):
        post_apply.reconcile(api, {"operator_user_ocid": "ocid1.user.oc1..operator"})


def test_operator_platform_admin_membership_retries_eventual_consistency(monkeypatch) -> None:
    api = FakeApi()
    checks = iter((False, True))
    monkeypatch.setattr(post_apply, "role_has_member", lambda *args: next(checks))
    sleeps: list[int] = []
    monkeypatch.setattr(post_apply, "_sleep", sleeps.append)

    post_apply.assert_operator_platform_admin(
        api,
        "ocid1.user.oc1..operator",
        attempts=2,
    )

    assert sleeps == [5]


def test_fresh_only_rejects_legacy_overlapping_volume_without_deleting() -> None:
    api = FakeApi()
    api.resources["/schemas"] = [
        {"displayName": "legacy", "key": "legacy-schema"}
    ]
    api.resources["/volumes"] = [
        {
            "displayName": "landing_data",
            "key": "legacy-volume",
            "volumeType": "EXTERNAL",
            "storageLocation": "oci://aidp-data-test@namespace/01_landing/",
        }
    ]
    with pytest.raises(post_apply.ReconcileError, match="overlapping medallion paths"):
        post_apply.assert_fresh_catalog(
            api, "aidp_lab-key", "namespace", "aidp-data-test"
        )
    assert ("/volumes", {"catalogKey": "aidp_lab-key", "schemaKey": "legacy-schema"}) in api.list_calls
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


def test_fresh_only_rejects_global_medallion_schema_without_deleting() -> None:
    api = FakeApi()
    api.resources["/schemas"] = [
        {"displayName": "landing", "key": "legacy-schema"}
    ]
    with pytest.raises(post_apply.ReconcileError, match="legacy global schemas"):
        post_apply.assert_fresh_catalog(
            api, "aidp_lab-key", "namespace", "aidp-data-test"
        )
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


def test_object_prefixes_remain_virtual_until_first_workload_write() -> None:
    assert post_apply.describe_object_prefixes() == [
        "Object Storage prefix 01_landing/ is virtual",
        "Object Storage prefix 02_bronze/ is virtual",
        "Object Storage prefix 03_silver/ is virtual",
        "Object Storage prefix 04_gold/ is virtual",
    ]


def test_existing_incompatible_catalog_is_never_replaced() -> None:
    api = FakeApi()
    api.resources["/catalogs"] = [
        {"displayName": "aidp_lab", "catalogType": "EXTERNAL", "key": "bad", "lifecycleState": "ACTIVE"}
    ]
    try:
        post_apply.ensure_resource(
            api,
            "/catalogs",
            "catalog",
            "aidp_lab",
            {"displayName": "aidp_lab", "catalogType": "INTERNAL"},
            {"catalogType": "INTERNAL"},
            wait_for_active=True,
        )
    except post_apply.ReconcileError as exc:
        assert "incompatible" in str(exc)
    else:
        raise AssertionError("incompatible catalog must fail")
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


def test_async_resource_waits_for_active(monkeypatch) -> None:
    api = FakeApi()
    api.resources["/catalogs"] = [
        {"displayName": "aidp_lab", "catalogType": "INTERNAL", "key": "catalog", "lifecycleState": "CREATING"}
    ]
    sleeps: list[int] = []

    def activate(_: int) -> None:
        sleeps.append(1)
        if len(sleeps) == 25:
            api.resources["/catalogs"][0]["lifecycleState"] = "ACTIVE"

    monkeypatch.setattr(post_apply.time, "sleep", activate)
    resource, created = post_apply.ensure_resource(
        api,
        "/catalogs",
        "catalog",
        "aidp_lab",
        {"displayName": "aidp_lab", "catalogType": "INTERNAL"},
        {"catalogType": "INTERNAL"},
        wait_for_active=True,
    )
    assert resource["lifecycleState"] == "ACTIVE"
    assert created is False
    assert len(sleeps) == 25


def test_async_resource_terminal_state_fails() -> None:
    api = FakeApi()
    api.resources["/volumes"] = [{"displayName": "landing_data", "lifecycleState": "DELETED"}]
    try:
        post_apply.ensure_resource(
            api,
            "/volumes",
            "volume",
            "landing_data",
            {"displayName": "landing_data"},
            {},
            wait_for_active=True,
        )
    except post_apply.ReconcileError as exc:
        assert "terminal state DELETED" in str(exc)
    else:
        raise AssertionError("terminal lifecycle state must fail")


def test_permission_action_must_be_observable(monkeypatch) -> None:
    api = FakeApi()
    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)
    inspect_path = "/catalogs/key/permissions"
    changed = post_apply.ensure_action(
        api,
        "POST",
        "/catalogs/key/actions/managePermission",
        {
            "assignCatalogPermissionDetails": {
                "assignees": {"type": "ROLE", "targets": ["AIDP_DEVELOPER"]},
                "permissions": ["SELECT"],
            }
        },
        lambda: post_apply.permission_is_assigned(
            api, inspect_path, "AIDP_DEVELOPER", "SELECT"
        ),
        attempts=1,
    )
    assert changed is True


def test_schema_admin_can_be_added_over_inherited_catalog_select() -> None:
    class Api:
        @staticmethod
        def list_all(_path):
            return [
                {
                    "grantee": "AIDP_DEVELOPER",
                    "granteeType": "ROLE",
                    "granteePermissions": ["SELECT"],
                }
            ]

    assert not post_apply.permission_is_assigned(
        Api(), "/schemas/schema/permissions", "AIDP_DEVELOPER", "ADMIN"
    )


def test_new_role_with_null_assignees_has_no_group() -> None:
    class Api:
        @staticmethod
        def request(method, path):
            return post_apply.ApiResponse(200, {"assignees": None}, {})

    assert not post_apply.role_has_group(Api(), "role-key", "group-id")


def test_role_readiness_rejects_extra_member() -> None:
    class Api:
        @staticmethod
        def request(method, path):
            return post_apply.ApiResponse(
                200,
                {
                    "assignees": [
                        {"type": "GROUP", "target": "expected"},
                        {"type": "USER", "target": "unexpected"},
                    ]
                },
                {},
            )

    with pytest.raises(post_apply.ReconcileError, match="unexpected members"):
        post_apply.assert_role_members_exact(
            Api(), "role-key", "AIDP_DEVELOPER", "GROUP", "expected"
        )


def test_role_readiness_rejects_master_catalog_or_broader_permissions() -> None:
    class Api:
        @staticmethod
        def list_all(path, *, params=None):
            assert params == {"permissionScope": "DIRECT"}
            return [
                {
                    "roleKey": "role-key",
                    "permissionsWithResourceDetails": {
                        "permissions": ["ADMIN"],
                        "resourceType": "MASTER_CATALOG",
                        "resourceKey": "master",
                    },
                }
            ]

    with pytest.raises(post_apply.ReconcileError, match="unexpected direct permissions"):
        post_apply.assert_role_permissions_exact(
            Api(),
            "role-key",
            "AIDP_DEVELOPER",
            {("CATALOG", "catalog-key", frozenset({"SELECT"}))},
        )


def test_resource_readiness_rejects_broader_direct_permission() -> None:
    class Api:
        @staticmethod
        def list_all(path):
            return [
                {
                    "grantee": "AIDP_DEVELOPER",
                    "granteeType": "ROLE",
                    "granteePermissions": ["USE", "ADMIN"],
                }
            ]

    with pytest.raises(post_apply.ReconcileError, match="conflicting direct permission"):
        post_apply.permission_is_assigned(
            Api(), "/clusters/key/permissions", "AIDP_DEVELOPER", "USE"
        )


def test_permission_conflict_is_not_treated_as_success(monkeypatch) -> None:
    class ConflictApi:
        @staticmethod
        def request(method, path, *, payload=None):
            raise post_apply.ApiRequestError(method, path, 409, "request-id")

    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)
    try:
        post_apply.ensure_action(
            ConflictApi(),
            "POST",
            "/catalogs/key/actions/managePermission",
            {"permissions": ["SELECT"]},
            lambda: False,
            attempts=1,
        )
    except post_apply.ReconcileError as exc:
        assert "did not converge" in str(exc)
    else:
        raise AssertionError("unverified 409 must fail")


def test_api_retries_429(monkeypatch) -> None:
    class Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.headers = {}
            self.content = b"{}"

        @staticmethod
        def json() -> dict:
            return {}

    class Session:
        def __init__(self) -> None:
            self.statuses = [429, 200]

        def request(self, *args, **kwargs):
            return Response(self.statuses.pop(0))

    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)
    api = post_apply.AidpApi("us-chicago-1", "platform", object(), "deployment")
    api.session = Session()
    response = api.request("GET", "/catalogs")
    assert response.status_code == 200


def test_post_retry_token_uses_canonical_payload_hash() -> None:
    observed: list[str] = []

    class Response:
        status_code = 200
        headers = {}
        content = b"{}"

        @staticmethod
        def json() -> dict:
            return {}

    class Session:
        @staticmethod
        def request(*args, **kwargs):
            observed.append(kwargs["headers"]["opc-retry-token"])
            return Response()

    api = post_apply.AidpApi("us-chicago-1", "platform", object(), "deployment")
    api.session = Session()
    api.request("POST", "/schemas", payload={"displayName": "landing", "catalogName": "aidp_lab"})
    api.request("POST", "/schemas", payload={"catalogName": "aidp_lab", "displayName": "landing"})
    api.request("POST", "/schemas", payload={"displayName": "bronze", "catalogName": "aidp_lab"})
    api.request(
        "POST",
        "/schemas",
        payload={"displayName": "bronze", "catalogName": "aidp_lab"},
        headers={"type": "FOLDER"},
    )
    assert observed[0] == observed[1]
    assert observed[0] != observed[2]
    assert observed[2] != observed[3]


def test_governance_api_uses_current_ai_data_platform_contract() -> None:
    api = post_apply.AidpApi(
        "us-chicago-1",
        "platform",
        object(),
        "deployment",
        api_version=post_apply.GOVERNANCE_API_VERSION,
        resource_segment="aiDataPlatforms",
    )

    assert api.base == (
        "https://datalake.us-chicago-1.oci.oraclecloud.com/20260430/"
        "aiDataPlatforms/platform"
    )


def test_governance_operator_credential_is_created_and_rotated() -> None:
    api = FakeApi()
    config = {
        "tenancy": "ocid1.tenancy.oc1..test",
        "user": "ocid1.user.oc1..operator",
        "fingerprint": "aa:bb:cc",
    }

    assert post_apply.ensure_governance_operator_credential(
        api, config, "private-key", "us-chicago-1"
    ) is True
    credential = api.resources["/credentials"][0]
    pairs = credential["credentialDetails"]["secretTokenPair"]
    assert credential["displayName"] == post_apply.GOVERNANCE_CREDENTIAL_NAME
    assert credential["type"] == "SECRET_TOKEN"
    assert [pair["secretKey"] for pair in pairs] == [
        "tenancy",
        "user",
        "fingerprint",
        "region",
        "private_key",
    ]
    credential["credentialType"] = credential.pop("type")

    assert post_apply.ensure_governance_operator_credential(
        api, config, "rotated-private-key", "us-chicago-1"
    ) is False
    assert api.resources["/credentials"][0]["credentialDetails"]["secretTokenPair"][-1] == {
        "secretKey": "private_key",
        "secretValue": "rotated-private-key",
    }


def test_governance_operator_credential_rejects_duplicates() -> None:
    api = FakeApi()
    api.resources["/credentials"] = [
        {"displayName": post_apply.GOVERNANCE_CREDENTIAL_NAME, "key": "one"},
        {"displayName": post_apply.GOVERNANCE_CREDENTIAL_NAME, "key": "two"},
    ]

    with pytest.raises(post_apply.ReconcileError, match="multiple resources"):
        post_apply.ensure_governance_operator_credential(
            api,
            {
                "tenancy": "ocid1.tenancy.oc1..test",
                "user": "ocid1.user.oc1..operator",
                "fingerprint": "aa:bb:cc",
            },
            "private-key",
            "us-chicago-1",
        )


def test_stopped_shared_compute_is_reusable_after_auto_termination() -> None:
    assert post_apply.is_active_or_raise({"lifecycleState": "STOPPED"}, "shared compute")


def test_workspace_object_key_accepts_live_folder_path_header() -> None:
    response = post_apply.ApiResponse(
        200,
        "",
        {"folder": "/Workspace/medallon", "type": "FOLDER"},
    )

    assert post_apply.workspace_object_key(response, "/Workspace/medallon") == "/Workspace/medallon"


def test_workspace_object_key_rejects_mismatched_folder_path() -> None:
    response = post_apply.ApiResponse(
        200,
        "",
        {"folder": "/Workspace/medallon", "type": "FOLDER"},
    )

    with pytest.raises(post_apply.ReconcileError, match="mismatched path"):
        post_apply.workspace_object_key(
            response, "/Workspace/medallon/participant@example.com"
        )


def test_run_command_returns_only_bootstrap_public_key() -> None:
    public_key = "-----BEGIN PUBLIC KEY-----\nQUJD\n-----END PUBLIC KEY-----\n"

    class Model:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    models = SimpleNamespace(
        CreateInstanceAgentCommandDetails=Model,
        InstanceAgentCommandTarget=Model,
        InstanceAgentCommandContent=Model,
        InstanceAgentCommandSourceViaTextDetails=Model,
        InstanceAgentCommandOutputViaTextDetails=Model,
    )
    oci_module = SimpleNamespace(
        compute_instance_agent=SimpleNamespace(models=models),
        exceptions=SimpleNamespace(ServiceError=RuntimeError),
    )

    class Client:
        details = None

        def create_instance_agent_command(self, details):
            self.details = details
            return SimpleNamespace(data=SimpleNamespace(id="command-id"))

        @staticmethod
        def get_instance_agent_command_execution(command_id, instance_id):
            assert (command_id, instance_id) == ("command-id", "instance-id")
            return SimpleNamespace(
                data=SimpleNamespace(
                    lifecycle_state="SUCCEEDED",
                    content=SimpleNamespace(text=public_key),
                )
            )

    client = Client()
    assert post_apply.fetch_bootstrap_public_key(
        client, oci_module, "compartment-id", "instance-id"
    ) == public_key
    assert "exec sudo /usr/local/sbin/aidp-lab-bootstrap-public-key" in client.details.content.source.text
    assert client.details.execution_time_out_in_seconds == 660
    assert "PRIVATE" not in client.details.content.source.text


def test_run_command_accepts_existing_runtime_ready_marker() -> None:
    assert post_apply.parse_public_key_output(
        f"\n{post_apply.BOOTSTRAP_READY}\n"
    ) == post_apply.BOOTSTRAP_READY


def test_run_command_retries_submission_during_iam_propagation(monkeypatch) -> None:
    class ServiceError(Exception):
        status = 403

    class Model:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    models = SimpleNamespace(
        CreateInstanceAgentCommandDetails=Model,
        InstanceAgentCommandTarget=Model,
        InstanceAgentCommandContent=Model,
        InstanceAgentCommandSourceViaTextDetails=Model,
        InstanceAgentCommandOutputViaTextDetails=Model,
    )
    oci_module = SimpleNamespace(
        compute_instance_agent=SimpleNamespace(models=models),
        exceptions=SimpleNamespace(ServiceError=ServiceError),
    )

    class Client:
        submissions = 0

        def create_instance_agent_command(self, details):
            self.submissions += 1
            if self.submissions < 3:
                raise ServiceError()
            return SimpleNamespace(data=SimpleNamespace(id="command-id"))

        @staticmethod
        def get_instance_agent_command_execution(command_id, instance_id):
            return SimpleNamespace(
                data=SimpleNamespace(
                    lifecycle_state="SUCCEEDED",
                    content=SimpleNamespace(text=post_apply.BOOTSTRAP_READY),
                )
            )

    monkeypatch.setattr(post_apply, "_sleep", lambda _: None)
    client = Client()
    assert post_apply.fetch_bootstrap_public_key(
        client,
        oci_module,
        "compartment-id",
        "instance-id",
        attempts=1,
        create_attempts=3,
    ) == post_apply.BOOTSTRAP_READY
    assert client.submissions == 3


def test_run_command_waits_for_instance_agent_policy_propagation(monkeypatch) -> None:
    class ServiceError(Exception):
        status = 404

    class Model:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    models = SimpleNamespace(
        CreateInstanceAgentCommandDetails=Model,
        InstanceAgentCommandTarget=Model,
        InstanceAgentCommandContent=Model,
        InstanceAgentCommandSourceViaTextDetails=Model,
        InstanceAgentCommandOutputViaTextDetails=Model,
    )
    oci_module = SimpleNamespace(
        compute_instance_agent=SimpleNamespace(models=models),
        exceptions=SimpleNamespace(ServiceError=ServiceError),
    )

    class Client:
        polls = 0

        @staticmethod
        def create_instance_agent_command(details):
            return SimpleNamespace(data=SimpleNamespace(id="command-id"))

        def get_instance_agent_command_execution(self, command_id, instance_id):
            self.polls += 1
            if self.polls < 3:
                raise ServiceError()
            return SimpleNamespace(
                data=SimpleNamespace(
                    lifecycle_state="SUCCEEDED",
                    content=SimpleNamespace(
                        text="-----BEGIN PUBLIC KEY-----\nQUJD\n-----END PUBLIC KEY-----\n"
                    ),
                )
            )

    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)
    client = Client()
    post_apply.fetch_bootstrap_public_key(
        client, oci_module, "compartment-id", "instance-id", attempts=3
    )
    assert client.polls == 3


def test_bootstrap_envelope_uses_rsa_oaep_sha256_and_aes_gcm() -> None:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    envelope = json.loads(
        post_apply.encrypt_bootstrap_credentials(
            public_key,
            "[DEFAULT]\nuser=operator\n",
            "private-key",
            b"wallet",
            "wallet-password",
            "AIDP_LAB_OPERATOR",
            "operator-password",
            "aidp_low",
        )
    )

    assert set(envelope) == {
        "schema_version",
        "wrapped_key_b64",
        "nonce_b64",
        "ciphertext_b64",
    }
    assert envelope["schema_version"] == 2
    nonce = base64.b64decode(envelope["nonce_b64"], validate=True)
    assert len(nonce) == 12
    data_key = private_key.decrypt(
        base64.b64decode(envelope["wrapped_key_b64"], validate=True),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    plaintext = AESGCM(data_key).decrypt(
        nonce,
        base64.b64decode(envelope["ciphertext_b64"], validate=True),
        None,
    )
    assert json.loads(plaintext) == {
        "config_text": "[DEFAULT]\nuser=operator\n",
        "key_text": "private-key",
        "wallet_zip_b64": base64.b64encode(b"wallet").decode("ascii"),
        "wallet_password": "wallet-password",
        "operator_username": "AIDP_LAB_OPERATOR",
        "operator_password": "operator-password",
        "dsn": "aidp_low",
    }


def test_autonomous_governance_bootstrap_installs_allowlisted_operator(monkeypatch) -> None:
    statements: list[tuple[str, dict[str, str]]] = []
    connections: list[dict[str, str]] = []

    class Cursor:
        def execute(self, statement: str, **parameters: str) -> None:
            statements.append((statement, parameters))

        @staticmethod
        def fetchone() -> tuple[int]:
            return (0,)

    class Connection:
        def __enter__(self) -> "Connection":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def cursor() -> Cursor:
            return Cursor()

        @staticmethod
        def commit() -> None:
            return None

    def connect(**kwargs: str) -> Connection:
        connections.append(kwargs)
        return Connection()

    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(connect=connect))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("tnsnames.ora", "aidp_high=(DESCRIPTION=high)\naidp_low=(DESCRIPTION=low)\n")
        archive.writestr("sqlnet.ora", "SSL_SERVER_DN_MATCH=yes\n")

    username, password, dsn = post_apply.bootstrap_autonomous_governance(
        stream.getvalue(), "wallet-password", "admin-password"
    )

    assert username == "AIDP_LAB_OPERATOR"
    assert len(password) == 32
    assert dsn == "aidp_low"
    assert connections[0]["user"] == "ADMIN"
    assert connections[0]["password"] == "admin-password"
    executed = [statement for statement, _parameters in statements]
    assert post_apply.GOVERNANCE_PACKAGE_SPEC in executed
    assert post_apply.GOVERNANCE_PACKAGE_BODY in executed
    assert "PROCEDURE PUT_METRIC" in post_apply.GOVERNANCE_PACKAGE_SPEC
    assert "PROCEDURE PUT_LINEAGE" in post_apply.GOVERNANCE_PACKAGE_SPEC
    assert "MERGE INTO " in post_apply.GOVERNANCE_PACKAGE_BODY
    assert "IF SQLCODE != -28007 THEN" in post_apply.GOVERNANCE_PACKAGE_BODY
    assert "RAISE;" in post_apply.GOVERNANCE_PACKAGE_BODY
    assert any("CREATE USER AIDP_LAB_OPERATOR" in statement for statement in executed)
    assert executed[-2:] == [
        "GRANT CREATE SESSION TO AIDP_LAB_OPERATOR",
        "GRANT EXECUTE ON ADMIN.AIDP_LAB_GOVERNANCE TO AIDP_LAB_OPERATOR",
    ]


def test_runtime_oci_config_is_unencrypted_verified_and_sanitized(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    fingerprint_hex = post_apply.hashlib.md5(
        private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
        usedforsecurity=False,
    ).hexdigest()
    fingerprint = ":".join(
        fingerprint_hex[index : index + 2]
        for index in range(0, len(fingerprint_hex), 2)
    )
    config_path = tmp_path / "config"
    config_path.write_text(
        "\n".join(
            (
                "[DEFAULT]",
                "tenancy=ocid1.tenancy.oc1..tenant",
                "user=ocid1.user.oc1..operator",
                f"fingerprint={fingerprint}",
                "region=us-chicago-1",
                "key_file=C:/ignored.pem",
                "[OTHER]",
                "user=ocid1.user.oc1..other",
                "",
            )
        ),
        encoding="utf-8",
    )

    config = post_apply.load_oci_config(str(config_path), str(key_path))
    rendered = post_apply.render_runtime_oci_config(config)

    assert "[OTHER]" not in rendered
    assert "ocid1.user.oc1..other" not in rendered
    assert "key_file=/etc/aidp-lab/oci/key.pem" in rendered


def test_hook_rejects_operator_config_passphrase_without_disclosing_it(tmp_path: Path) -> None:
    config_path = tmp_path / "config"
    config_path.write_text(
        "[DEFAULT]\ntenancy=t\nuser=u\nfingerprint=f\nregion=us-chicago-1\npass_phrase=do-not-log\n",
        encoding="utf-8",
    )

    with pytest.raises(post_apply.ReconcileError, match="unencrypted RSA PEM") as raised:
        post_apply.load_oci_config(str(config_path), str(tmp_path / "missing.pem"))

    assert "do-not-log" not in str(raised.value)


def test_operator_credentials_are_delivered_to_exact_bootstrap_object(monkeypatch) -> None:
    run_client = object()
    oci_module = SimpleNamespace(
        compute_instance_agent=SimpleNamespace(
            ComputeInstanceAgentClient=lambda config, *, signer: run_client
        ),
        exceptions=SimpleNamespace(ServiceError=RuntimeError),
    )
    uploaded: list[tuple[str, str, str, bytes, str, str]] = []

    class ObjectStorage:
        @staticmethod
        def put_object(namespace, bucket, name, body, *, content_type, if_none_match):
            uploaded.append((namespace, bucket, name, body, content_type, if_none_match))

    monkeypatch.setattr(post_apply, "fetch_bootstrap_public_key", lambda *args: "public-key")
    monkeypatch.setattr(post_apply, "encrypt_bootstrap_credentials", lambda *args: b"encrypted-envelope")
    monkeypatch.setattr(post_apply, "delete_bootstrap_object", lambda *args: None)
    monkeypatch.setattr(
        post_apply,
        "bootstrap_autonomous_governance",
        lambda *args: ("AIDP_LAB_OPERATOR", "operator-password", "aidp_low"),
    )
    outputs = {
        "operator_user_ocid": "ocid1.user.oc1..operator",
        "compartment_ocid": "ocid1.compartment.oc1..lab",
        "instance_id": "ocid1.instance.oc1..vm",
        "objectstorage_namespace": "namespace",
        "bucket_name": "aidp-data-test",
    }

    assert post_apply.deliver_operator_credentials(
        oci_module,
        {"user": outputs["operator_user_ocid"]},
        object(),
        outputs,
        "us-chicago-1",
        ObjectStorage(),
        "config-text",
        "key-text",
        b"wallet",
        "wallet-password",
        "admin-password",
    ) is True

    assert uploaded == [
        (
            "namespace",
            "aidp-data-test",
            ".bootstrap/operator-credentials.json",
            b"encrypted-envelope",
            "application/json",
            "*",
        )
    ]


def test_operator_credential_delivery_is_idempotent_after_vm_ready(monkeypatch) -> None:
    run_client = object()
    oci_module = SimpleNamespace(
        compute_instance_agent=SimpleNamespace(
            ComputeInstanceAgentClient=lambda config, *, signer: run_client
        ),
        exceptions=SimpleNamespace(ServiceError=RuntimeError),
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        post_apply,
        "fetch_bootstrap_public_key",
        lambda *args: post_apply.BOOTSTRAP_READY,
    )
    monkeypatch.setattr(
        post_apply,
        "delete_bootstrap_object",
        lambda *args: deleted.append(post_apply.BOOTSTRAP_OBJECT_NAME),
    )
    outputs = {
        "operator_user_ocid": "ocid1.user.oc1..operator",
        "compartment_ocid": "ocid1.compartment.oc1..lab",
        "instance_id": "ocid1.instance.oc1..vm",
        "objectstorage_namespace": "namespace",
        "bucket_name": "aidp-data-test",
    }

    assert post_apply.deliver_operator_credentials(
        oci_module,
        {"user": outputs["operator_user_ocid"]},
        object(),
        outputs,
        "us-chicago-1",
        object(),
        "config-text",
        "key-text",
        b"wallet",
        "wallet-password",
        "admin-password",
    ) is False
    assert deleted == [post_apply.BOOTSTRAP_OBJECT_NAME]


def test_operator_credential_delivery_rejects_a_different_config_user() -> None:
    outputs = {
        "operator_user_ocid": "ocid1.user.oc1..operator",
        "objectstorage_namespace": "namespace",
        "bucket_name": "aidp-data-test",
    }

    with pytest.raises(post_apply.ReconcileError, match="does not match the uploaded OCI config"):
        post_apply.deliver_operator_credentials(
            SimpleNamespace(),
            {"user": "ocid1.user.oc1..another"},
            object(),
            outputs,
            "us-chicago-1",
            object(),
            "config-text",
            "key-text",
            b"wallet",
            "wallet-password",
            "admin-password",
        )


def test_bootstrap_consumption_and_cleanup_use_the_exact_object(monkeypatch) -> None:
    class ServiceError(Exception):
        def __init__(self, status: int) -> None:
            self.status = status

    oci_module = SimpleNamespace(exceptions=SimpleNamespace(ServiceError=ServiceError))
    calls: list[tuple[str, str, str, str]] = []

    class ObjectStorage:
        polls = 0

        def head_object(self, namespace, bucket, name):
            calls.append(("HEAD", namespace, bucket, name))
            self.polls += 1
            if self.polls == 2:
                raise ServiceError(404)

        @staticmethod
        def delete_object(namespace, bucket, name):
            calls.append(("DELETE", namespace, bucket, name))

    outputs = {"objectstorage_namespace": "namespace", "bucket_name": "aidp-data-test"}
    storage = ObjectStorage()
    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)

    post_apply.wait_for_bootstrap_consumed(oci_module, storage, outputs, attempts=2)
    post_apply.delete_bootstrap_object(oci_module, storage, outputs)

    assert calls == [
        ("HEAD", "namespace", "aidp-data-test", ".bootstrap/operator-credentials.json"),
        ("HEAD", "namespace", "aidp-data-test", ".bootstrap/operator-credentials.json"),
        ("DELETE", "namespace", "aidp-data-test", ".bootstrap/operator-credentials.json"),
    ]


def test_global_post_apply_deadline_stops_nested_retries(monkeypatch) -> None:
    monkeypatch.setattr(post_apply.time, "monotonic", lambda: 100.0)
    previous = post_apply._post_apply_deadline
    post_apply._post_apply_deadline = 100.5
    try:
        with pytest.raises(post_apply.ReconcileError, match="safe execution deadline"):
            post_apply._sleep(1)
    finally:
        post_apply._post_apply_deadline = previous


def test_permission_verification_paginates_and_correlates_one_item() -> None:
    class Response:
        status_code = 200
        content = b"{}"

        def __init__(self, items: list[dict], next_page: str | None = None) -> None:
            self._items = items
            self.headers = {"opc-next-page": next_page} if next_page else {}

        def json(self) -> dict:
            return {"items": self._items}

    class Session:
        def request(self, *args, **kwargs):
            if kwargs["params"].get("page") == "second":
                return Response(
                    [
                        {
                            "grantee": "ANOTHER_ROLE",
                            "granteeName": "ANOTHER_ROLE",
                            "granteeType": "ROLE",
                            "granteePermissions": ["SELECT"],
                        }
                    ]
                )
            return Response(
                [
                    {
                        "grantee": "AIDP_DEVELOPER",
                        "granteeName": "AIDP_DEVELOPER",
                        "granteeType": "ROLE",
                        "granteePermissions": ["READ"],
                    }
                ],
                "second",
            )

    api = post_apply.AidpApi("us-chicago-1", "platform", object(), "deployment")
    api.session = Session()
    with pytest.raises(post_apply.ReconcileError, match="conflicting direct permission"):
        post_apply.permission_is_assigned(
            api, "/catalogs/key/permissions", "AIDP_DEVELOPER", "SELECT"
        )


def test_workspace_waits_until_active(monkeypatch) -> None:
    api = FakeApi()
    api.resources["/workspaces"][0]["lifecycleState"] = "CREATING"
    sleeps: list[int] = []

    def activate(_: int) -> None:
        sleeps.append(1)
        if len(sleeps) == 25:
            api.resources["/workspaces"][0]["lifecycleState"] = "ACTIVE"

    monkeypatch.setattr(post_apply.time, "sleep", activate)
    workspace = post_apply.wait_for_existing_active(
        api,
        "/workspaces",
        "workspace",
        "ws",
        {"type": "DEFAULT"},
    )
    assert workspace["lifecycleState"] == "ACTIVE"
    assert len(sleeps) == 25


def test_operator_admin_waits_for_data_plane_visibility(monkeypatch) -> None:
    api = FakeApi()
    list_all = api.list_all
    attempts = 0

    def eventually_visible(path: str, *, params=None) -> list[dict]:
        nonlocal attempts
        if path == "/roles" and attempts < 2:
            attempts += 1
            raise post_apply.ApiRequestError("GET", path, 404, f"request-{attempts}")
        return list_all(path, params=params)

    api.list_all = eventually_visible
    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)

    post_apply.assert_operator_platform_admin(
        api, "ocid1.user.oc1..operator", attempts=3
    )

    assert attempts == 2


def test_operator_admin_reports_last_data_plane_request(monkeypatch) -> None:
    api = FakeApi()

    def unavailable(path: str, **_) -> list[dict]:
        raise post_apply.ApiRequestError("GET", path, 404, "request-final")

    api.list_all = unavailable
    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)

    with pytest.raises(post_apply.ReconcileError, match="request-final"):
        post_apply.assert_operator_platform_admin(
            api, "ocid1.user.oc1..operator", attempts=2
        )


def test_aidp_api_uses_workbench_data_plane_endpoint() -> None:
    api = post_apply.AidpApi("us-chicago-1", "ocid1.aidataplatform.test", object(), "deployment")
    assert api.base == (
        "https://datalake.us-chicago-1.oci.oraclecloud.com/20240831/"
        "dataLakes/ocid1.aidataplatform.test"
    )


def test_application_health_uses_self_signed_https(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, str]:
            return {"status": "ok"}

    class Session:
        @staticmethod
        def get(url, *, timeout, verify):
            observed.update(url=url, timeout=timeout, verify=verify)
            return Response()

    monkeypatch.setattr(post_apply.requests, "Session", Session)
    post_apply.wait_for_application("https://192.0.2.10")
    assert observed["url"] == "https://192.0.2.10/api/health"
    assert observed["verify"] is False


def test_workbench_url_uses_oci_web_socket_endpoint() -> None:
    assert post_apply.workbench_url(
        {
            "aidp_web_socket_endpoint": "1yjfbzshsbc4glmdavcord",
            "tenancy_name": "oci-deploy-1",
            "identity_domain_name": "Default",
        }
    ) == "https://1yjfbzshsbc4glmdavcord.datalake.oci.oraclecloud.com#?tenant=oci-deploy-1&domain=Default"


def test_workbench_url_falls_back_to_control_plane_alias() -> None:
    assert post_apply.workbench_url(
        {
            "aidp_web_socket_endpoint": "",
            "aidp_alias_endpoint": "nxjjum1xu8a1iw51nzhord",
            "tenancy_name": "oci-deploy-1",
            "identity_domain_name": "Default",
        }
    ) == "https://nxjjum1xu8a1iw51nzhord.datalake.oci.oraclecloud.com#?tenant=oci-deploy-1&domain=Default"


def test_aidp_alias_endpoint_uses_oci_region_key() -> None:
    assert (
        post_apply.aidp_alias_endpoint("nxjjum1xu8a1iw51nzh", "us-chicago-1")
        == "nxjjum1xu8a1iw51nzhord"
    )
