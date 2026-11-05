import ast
import asyncio
from types import SimpleNamespace

import pytest

from app.services.oci.aidp import AidpClient, AidpProvisionConflict, AidpProvisionError, AidpProvisionPending
from app.modules.ai_data_governance.constants import GOVERNANCE_AGENT_NAME, RUNTIME_FAMILY


USER_OCID = "ocid1.user.oc1..aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OPERATION_ID = "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43"


def module_client() -> AidpClient:
    client = object.__new__(AidpClient)
    client._locks = {}
    client.settings = SimpleNamespace(
        deployment_mode="production",
        agent_model_id="ocid1.generativeaimodel.oc1.us-chicago-1.example",
        aidp_region="us-chicago-1",
        compartment_id="ocid1.compartment.oc1..example",
        aidp_platform_id="ocid1.aidataplatform.oc1..example",
        objectstorage_namespace="namespace",
        artifacts_bucket_name="oci_artifacts",
    )
    client._role_user_ocids = lambda _role: {USER_OCID}
    client._workspace = lambda: {"key": "workspace"}
    return client


def manifest(operation_type: str, *, phase: str = "permissions") -> dict:
    return {
        "schema_version": 1,
        "runtime_family": RUNTIME_FAMILY,
        "control_module_id": "ai_data_governance",
        "module_id": "ai_data_governance",
        "status": "error",
        "phase": phase,
        "enabled": True,
        "previous_enabled": True,
        "operation": {"operation_id": OPERATION_ID, "type": operation_type, "phase": "error"},
        "resources": {"agent_key": "agent", "workflow_key": "job", "sync_requested_at": "2026-08-24T00:00:00+00:00"},
        "error_code": "deadbeefdeadbeef",
    }


@pytest.mark.parametrize(
    ("returned_path", "object_type", "accepted"),
    [
        ("/Workspace/", "FOLDER", True),
        ("/Workspace", "FOLDER", True),
        ("/Workspace/", "FILE", False),
        ("/Workspace//", "FOLDER", False),
        ("/Workspace/other", "FOLDER", False),
        ("/Workspace/../other", "FOLDER", False),
    ],
)
def test_workspace_object_key_validates_native_folder_path(
    returned_path: str, object_type: str, accepted: bool
) -> None:
    client = module_client()
    client._workspace_object = lambda *_args, **_kwargs: (
        None, {"folder": returned_path, "type": object_type}
    )
    if accepted:
        assert client._workspace_object_key("workspace", "/Workspace") == returned_path
    else:
        with pytest.raises(AidpProvisionError, match="mismatched workspace object path"):
            client._workspace_object_key("workspace", "/Workspace")


@pytest.mark.parametrize("object_type", ["FILE", "NOTEBOOK"])
@pytest.mark.parametrize("display_name", ["manifest.json", "other.json", "../manifest.json"])
def test_workspace_object_key_uses_native_file_name(
    object_type: str, display_name: str
) -> None:
    client = module_client()
    client._workspace_object = lambda *_args, **_kwargs: (
        {"arbitrary_content": True},
        {"folder": "/Workspace/.control/", "display-name": display_name, "type": object_type},
    )
    path = "/Workspace/.control/manifest.json"
    if display_name == "manifest.json":
        assert client._workspace_object_key("workspace", path) == path
    else:
        with pytest.raises(AidpProvisionError, match="mismatched workspace object path"):
            client._workspace_object_key("workspace", path)


def test_module_manifest_requires_a_canonical_uuid_operation_id() -> None:
    state = manifest("install")
    assert AidpClient._module_manifest_valid(state) is True
    for invalid in (
        "4AB88C5E-C9E3-47BF-8DCA-97F7EB7D0D43",
        "{4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43}",
        "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d4-",
        "4ab88c5ec9e347bf8dca97f7eb7d0d43",
        None,
    ):
        state["operation"]["operation_id"] = invalid
        assert AidpClient._module_manifest_valid(state) is False


def test_module_status_reflects_externally_disabled_governance_workflow() -> None:
    client = module_client()
    state = manifest("install")
    state.update(status="active", phase="active", enabled=True)
    client._module_manifest = lambda _workspace: state
    client._request = lambda *_args, **_kwargs: {
        "continuous": {"pauseStatus": "PAUSED"}
    }

    module = asyncio.run(client.list_modules())[0]

    assert module["enabled"] is False
    assert state["enabled"] is True


def test_module_status_compares_installed_and_bundled_versions() -> None:
    client = module_client()
    state = manifest("install")
    state.update(status="active", phase="active", pack_version="2.0.0")
    client._module_manifest = lambda _workspace: state
    client._request = lambda *_args, **_kwargs: {
        "continuous": {"pauseStatus": "UNPAUSED"}
    }

    module = asyncio.run(client.list_modules())[0]

    assert module["installed_version"] == "2.0.0"
    assert module["bundled_version"] == "4.0.0"
    assert module["update_available"] is True


@pytest.mark.parametrize("permission", ["MANAGE", "ADMIN"])
def test_shared_ancestor_audit_fails_closed_without_mutating_shared_permissions(
    permission: str,
) -> None:
    client = module_client()
    path = "/workspaces/workspace/objects/Workspace"
    permissions = [{
        "grantee": "AIDP_DEVELOPER",
        "granteeType": "ROLE",
        "granteePermissions": [permission],
    }]
    client._list = lambda request_path, **_kwargs: (
        permissions if request_path == f"{path}/permissions" else []
    )
    client._request = lambda *_args, **_kwargs: pytest.fail(
        "Shared ancestor permissions must never be mutated"
    )

    with pytest.raises(AidpProvisionError, match="shared workspace ancestor"):
        client._assert_no_forbidden_direct_permissions(
            path,
            {permission},
            allowed_grantees={("AI_DATA_PLATFORM_ADMIN", "ROLE")},
            inheritable_only=True,
        )

    permissions[0]["isPermissionsInheritable"] = False
    client._assert_no_forbidden_direct_permissions(
        path,
        {permission},
        allowed_grantees={("AI_DATA_PLATFORM_ADMIN", "ROLE")},
        inheritable_only=True,
    )

    permissions[:] = [{
        "grantee": "ocid1.group.oc1..admins",
        "granteeType": "GROUP",
        "granteePermissions": [permission],
    }]
    client._assert_no_forbidden_direct_permissions(
        path,
        {permission},
        allowed_grantees={
            ("AI_DATA_PLATFORM_ADMIN", "ROLE"),
            ("ocid1.group.oc1..admins", "GROUP"),
        },
        inheritable_only=True,
    )


def test_folder_permission_revoke_preserves_aidp_default_inheritance() -> None:
    client = module_client()
    payloads: list[dict] = []
    client._request = lambda *_args, payload, **_kwargs: payloads.append(payload) or {}

    client._revoke_direct_permission(
        "/workspaces/workspace/objects/folder",
        "revokeWorkspaceObjectPermissionDetails",
        {
            "grantee": "AIDP_DEVELOPER",
            "granteeType": "ROLE",
            "granteePermissions": ["MANAGE"],
        },
        include_columns=False,
        inheritable=True,
    )

    assert payloads == [{
        "revokeWorkspaceObjectPermissionDetails": {
            "assignees": {"type": "ROLE", "targets": ["AIDP_DEVELOPER"]},
            "permissions": ["MANAGE"],
            "isPermissionsInheritable": True,
        }
    }]


@pytest.mark.parametrize("expected", [(), (("AI_DATA_PLATFORM_ADMIN", "ADMIN", True),)])
def test_native_platform_admin_grants_without_inheritance_flag_are_preserved(expected) -> None:
    client = module_client()
    permissions = [
        {"grantee": "AI_DATA_PLATFORM_ADMIN", "granteeType": "ROLE", "granteePermissions": ["ADMIN"], "isPermissionsInheritable": True},
        {"grantee": USER_OCID, "granteeType": "USER", "granteePermissions": ["ADMIN"], "isPermissionsInheritable": True},
    ]
    client._list = lambda *_args, **_kwargs: permissions
    client._request = lambda *_args, **_kwargs: pytest.fail("Existing platform administrators must not be revoked or regranted")
    assert client._reconcile_role_permissions_exact(
        "/workspaces/workspace/objects/control", "assignWorkspaceObjectPermissionDetails",
        "revokeWorkspaceObjectPermissionDetails", expected, inheritable=True,
        platform_admin_grantees={("AI_DATA_PLATFORM_ADMIN", "ROLE"), (USER_OCID, "USER")},
    ) is False


def test_native_platform_admin_preservation_does_not_exempt_an_unverified_admin() -> None:
    client = module_client()
    trusted = {"grantee": USER_OCID, "granteeType": "USER", "granteePermissions": ["ADMIN"]}
    rogue = {"grantee": "unverified-user", "granteeType": "USER", "granteePermissions": ["ADMIN"]}
    permissions = [trusted, rogue]
    payloads = []
    client._list = lambda *_args, **_kwargs: list(permissions)

    def request(method, _path, *, payload, **_kwargs):
        assert method == "POST"
        details = payload["revokeClusterPermissionDetails"]
        assert details["assignees"] == {"type": "USER", "targets": ["unverified-user"]}
        payloads.append(payload)
        permissions.remove(rogue)

    client._request = request
    assert client._reconcile_role_permissions_exact(
        "/workspaces/workspace/clusters/compute", "assignClusterPermissionDetails",
        "revokeClusterPermissionDetails", (),
        platform_admin_grantees={(USER_OCID, "USER")},
    ) is True
    assert len(payloads) == 1
    assert permissions == [trusted]


def test_governance_redeploy_revokes_drift_before_restoring_exact_agent_roles() -> None:
    client = module_client()
    path = "/workspaces/workspace/agents/agent"
    permissions = [
        {
            "grantee": "AIDP_DEVELOPER",
            "granteeType": "ROLE",
            "granteePermissions": ["ADMIN"],
            "columns": [],
            "excludeColumns": [],
        },
        {
            "grantee": "ocid1.user.oc1..unexpected",
            "granteeType": "USER",
            "granteePermissions": ["MANAGE"],
            "columns": [],
            "excludeColumns": [],
        },
    ]
    payloads: list[dict] = []

    def list_permissions(request_path: str, **_kwargs):
        assert request_path == f"{path}/permissions"
        return [dict(item) for item in permissions]

    def request(_method: str, request_path: str, *, payload: dict, **_kwargs):
        assert request_path == f"{path}/actions/managePermission"
        payloads.append(payload)
        key, details = next(iter(payload.items()))
        target = details["assignees"]["targets"][0]
        if key == "revokeAgentPermissionDetails":
            permissions[:] = [item for item in permissions if item["grantee"] != target]
        else:
            permissions.append({
                "grantee": target,
                "granteeType": "ROLE",
                "granteePermissions": details["permissions"],
                "columns": details["includeColumns"],
                "excludeColumns": details["excludeColumns"],
            })
        return {}

    client._list = list_permissions
    client._request = request

    assert client._reconcile_role_permissions_exact(
        path,
        "assignAgentPermissionDetails",
        "revokeAgentPermissionDetails",
        (
            ("AIDP_DEVELOPER", "USE", None),
            ("AI_DATA_PLATFORM_ADMIN", "ADMIN", None),
        ),
        include_columns=True,
        reject_inherited_editors=True,
    ) is True
    assert [next(iter(payload)) for payload in payloads] == [
        "revokeAgentPermissionDetails",
        "revokeAgentPermissionDetails",
        "assignAgentPermissionDetails",
        "assignAgentPermissionDetails",
    ]
    assert all(
        details["includeColumns"] == [] and details["excludeColumns"] == []
        for payload in payloads
        for details in payload.values()
    )
    assert {
        (item["grantee"], frozenset(item["granteePermissions"]))
        for item in permissions
    } == {
        ("AIDP_DEVELOPER", frozenset({"USE"})),
        ("AI_DATA_PLATFORM_ADMIN", frozenset({"ADMIN"})),
    }


def test_governance_rbac_fails_closed_on_inherited_non_admin_edit_access() -> None:
    client = module_client()
    client._list = lambda *_args, **_kwargs: [{
        "grantee": "AIDP_DEVELOPER",
        "granteeType": "ROLE",
        "granteePermissions": ["MANAGE"],
        "isInherited": True,
    }]
    with pytest.raises(AidpProvisionError, match="inherits governance edit access"):
        client._reconcile_role_permissions_exact(
            "/workspaces/workspace/agents/agent",
            "assignAgentPermissionDetails",
            "revokeAgentPermissionDetails",
            (),
            include_columns=True,
            reject_inherited_editors=True,
        )


def test_platform_admin_principals_expand_group_and_nested_role_assignments() -> None:
    client = module_client()
    client._role = lambda _name: {"key": "root-role"}
    assignments = {
        "root-role": [
            {"type": "USER", "target": USER_OCID},
            {"type": "GROUP", "target": "ocid1.group.oc1..admins"},
            {"type": "ROLE", "target": "nested-role"},
        ],
        "nested-role": [
            {"type": "GROUP", "target": "ocid1.group.oc1..security-admins"},
            {"type": "ROLE", "target": "root-role"},
        ],
    }
    client._request = lambda _method, path, **_kwargs: {
        "assignees": assignments[path.rsplit("/", 1)[-1]]
    }

    assert client._role_principal_ocids("AI_DATA_PLATFORM_ADMIN") == (
        {USER_OCID},
        {"ocid1.group.oc1..admins", "ocid1.group.oc1..security-admins"},
    )


def test_concurrent_install_reuses_the_manifest_operation_without_reconciliation() -> None:
    client = module_client()
    state = manifest("install", phase="sync")
    state["status"] = "installing"
    client._module_manifest = lambda _workspace: state
    with pytest.raises(AidpProvisionPending, match="existing global governance installation") as raised:
        client._reconcile_governance_module(
            USER_OCID, "98cc3eec-fab0-461f-97ed-f95e7f6c8992", "install"
        )
    assert raised.value.phase == "sync"
    assert state["operation"]["operation_id"] == OPERATION_ID


def test_terminal_error_persists_only_sanitized_code_and_pending_does_not() -> None:
    client = module_client()
    state = manifest("install")
    state["status"] = "installing"
    writes: list[dict] = []
    client._module_manifest = lambda _workspace: state
    client._write_module_manifest = lambda _workspace, value: writes.append(dict(value))

    async def terminal() -> None:
        def fail(*_args):
            raise RuntimeError("sensitive upstream detail")

        with pytest.raises(AidpProvisionError, match="failed closed"):
            await client._run_module_operation(fail, USER_OCID, OPERATION_ID, "install")

    asyncio.run(terminal())
    assert state["status"] == "error"
    assert len(state["error_code"]) == 16
    assert "sensitive" not in str(state)

    writes.clear()
    state["status"] = "installing"

    async def pending() -> None:
        def wait(*_args):
            raise AidpProvisionPending("still working", "sync")

        with pytest.raises(AidpProvisionPending):
            await client._run_module_operation(wait, USER_OCID, OPERATION_ID, "install")

    asyncio.run(pending())
    assert writes == []


def test_job_contract_repairs_concurrency_drift() -> None:
    client = module_client()
    task = {
        "type": "NOTEBOOK_TASK", "taskKey": "sync", "dependsOn": [], "runIf": "ALL_SUCCESS",
        "notebookPath": "/Workspace/sync.ipynb", "cluster": {"clusterKey": "compute"}, "parameters": [],
    }
    payload = {
        "name": "sync", "path": "/Workspace", "maxConcurrentRuns": 1,
        "continuous": {"pauseStatus": "UNPAUSED"}, "jobClusters": [{"clusterKey": "compute"}], "tasks": [task],
    }
    details = dict(payload, maxConcurrentRuns=2)
    assert client._job_contract_is_visible(details, payload, "compute") is False


def test_global_deployment_name_and_retry_scope_are_deterministic() -> None:
    client = module_client()
    client._list = lambda *_args, **_kwargs: []
    captured: dict = {}

    def request(_method, _path, **kwargs):
        captured.update(kwargs)
        return {"key": "deployment"}

    client._request = request
    client._ensure_agent_deployment("workspace", "agent", "compute", GOVERNANCE_AGENT_NAME)
    assert captured["payload"]["displayName"] == f"{GOVERNANCE_AGENT_NAME}_deployment"
    assert captured["retry_scope"] == "agent-deploy:agent"


def test_governance_manifest_reads_only_the_canonical_path():
    from app.services.oci.aidp import MODULE_ROOT
    client = module_client()
    reads = []
    client._workspace_json = lambda workspace, path, message: reads.append(path)
    assert client._module_manifest("workspace") is None
    assert reads == [MODULE_ROOT + "/manifest.json"]


@pytest.mark.parametrize("agents", [[], [{"key": "unexpected-agent"}], [{"id": "unexpected-agent"}], [{"key": ""}]])
def test_governance_redeploy_never_replaces_a_recorded_agent(agents):
    client = module_client()
    client._upload_file = lambda *_args, **_kwargs: False
    client._agents = lambda *_args: agents
    client._request = lambda *_args, **_kwargs: pytest.fail("A recorded agent must not be replaced or updated by identity drift")
    with pytest.raises(AidpProvisionError, match="recorded governance Agent identity"):
        client._ensure_agent("workspace", "compute", GOVERNANCE_AGENT_NAME, "/Workspace/agent", b"source", b"{}",
            repair_drift=True, expected_key="retained-agent")


@pytest.mark.parametrize("paused", [False, True])
def test_module_status_reads_native_schedule(paused):
    client = module_client()
    state = manifest("install")
    state.update(status="active", phase="active", enabled=True)
    client._module_manifest = lambda _workspace: state
    client._request = lambda *_args, **_kwargs: {
        "continuous": None, "schedule": {"pauseStatus": "PAUSED" if paused else "UNPAUSED"}}
    assert asyncio.run(client.list_modules())[0]["enabled"] is (not paused)


@pytest.mark.parametrize("retired_state", ["INACTIVE", "DELETED"])
def test_agent_redeploy_ignores_retired_deployment_history(retired_state):
    client = module_client()
    current = {"key": "new", "lifecycleState": "ACTIVE"}
    client._list = lambda *_args, **_kwargs: [
        {"key": "old", "lifecycleState": retired_state}, current]
    assert client._agent_deployments("/deployments") == [current]


def test_duplicate_active_agent_deployments_still_fail_closed():
    client = module_client()
    client._list = lambda *_args, **_kwargs: [
        {"key": "one", "lifecycleState": "ACTIVE"},
        {"key": "two", "lifecycleState": "ACTIVE"}]
    with pytest.raises(AidpProvisionError, match="duplicate"):
        client._agent_deployments("/deployments")
