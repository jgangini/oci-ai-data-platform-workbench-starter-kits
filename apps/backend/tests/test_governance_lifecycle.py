import asyncio
from types import SimpleNamespace

import pytest

from app.aidp import AidpClient, AidpProvisionConflict, AidpProvisionError, AidpProvisionPending
from app.governance import GOVERNANCE_AGENT_NAME


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
    )
    client._role_user_ocids = lambda _role: {USER_OCID}
    client._workspace = lambda: {"key": "workspace"}
    return client


def manifest(operation_type: str, *, phase: str = "permissions") -> dict:
    return {
        "schema_version": 1,
        "module_id": "ai_data_governance_vsc_extension",
        "status": "error",
        "phase": phase,
        "enabled": True,
        "previous_enabled": True,
        "operation": {"operation_id": OPERATION_ID, "type": operation_type, "phase": "error"},
        "resources": {"agent_key": "agent", "workflow_key": "job", "sync_requested_at": "2026-08-24T00:00:00+00:00"},
        "error_code": "deadbeefdeadbeef",
    }


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
    assert module["bundled_version"] == "3.0.0"
    assert module["update_available"] is True


def test_redeploy_preserves_externally_disabled_governance_config() -> None:
    client = module_client()
    state = manifest("install")
    state.update(status="active", phase="active", enabled=True)
    state["operation"]["phase"] = "complete"
    client._module_manifest = lambda _workspace: state
    client._request = lambda *_args, **_kwargs: {
        "continuous": {"pauseStatus": "PAUSED"}
    }
    client._write_module_manifest = lambda *_args: None
    client._ensure_governance_bucket = lambda: False
    client._ensure_governance_credential = lambda: ("credential", False)
    client._shared_compute = lambda _workspace: {"key": "shared"}
    jobs: list[dict] = []

    def ensure_job(*_args, **kwargs):
        jobs.append(kwargs)
        return "job", False

    client._ensure_governance_job = ensure_job
    client._ensure_global_agent = lambda *_args: False
    client._ensure_global_agent_permissions = lambda *_args: False

    result = client._reconcile_governance_module(USER_OCID, OPERATION_ID, "redeploy")

    assert result["enabled"] is False
    assert jobs == [
        {
            "desired_enabled": None,
            "paused": True,
            "bootstrap_snapshot": False,
        },
        {"desired_enabled": False, "paused": True},
        {"desired_enabled": None, "paused": True},
    ]


def test_latest_run_must_belong_to_current_published_revision() -> None:
    client = module_client()
    client._list = lambda *_args, **_kwargs: [
        {"key": "old", "jobVersion": "1", "runState": "SUCCESS", "timeStarted": "2026-08-24T00:00:01Z"},
        {"key": "new", "jobVersion": "2", "runState": "RUNNING", "timeStarted": "2026-08-24T00:00:02Z"},
    ]
    client._request = lambda *_args, **_kwargs: {"version": "2", "timeUpdated": "2026-08-24T00:00:00Z"}
    assert client._successful_governance_sync("workspace", "job") is False
    client._list = lambda *_args, **_kwargs: [
        {"key": "old", "jobVersion": "1", "runState": "SUCCESS", "timeStarted": "2026-08-24T00:00:01Z"}
    ]
    assert client._successful_governance_sync("workspace", "job") is False


def test_sync_without_revision_requires_a_run_after_the_publish_marker() -> None:
    client = module_client()
    client._request = lambda *_args, **_kwargs: {"timeUpdated": "2026-08-24T00:00:10Z"}
    client._list = lambda *_args, **_kwargs: [
        {"key": "old", "runState": "SUCCESS", "timeStarted": "2026-08-24T00:00:09Z"}
    ]
    assert client._successful_governance_sync("workspace", "job") is False
    client._list = lambda *_args, **_kwargs: [
        {"key": "current", "runState": "SUCCESS", "timeStarted": "2026-08-24T00:00:11Z"}
    ]
    assert client._successful_governance_sync("workspace", "job") is True


def test_governance_rbac_grants_only_agent_use_and_admin() -> None:
    client = module_client()
    roles: list[str] = []
    ancestor_audits: list[tuple[str, set[str], dict]] = []
    reconciliations: list[tuple[str, str, str, tuple, dict]] = []
    client._role = lambda role: roles.append(role) or {"name": role}
    client._role_principal_ocids = lambda role: roles.append(role) or (set(), set())
    client._assert_no_forbidden_direct_permissions = (
        lambda path, forbidden, **kwargs: ancestor_audits.append(
            (path, forbidden, kwargs)
        )
    )
    client._workspace_object_key = lambda _workspace, path: path.rsplit("/", 1)[-1]
    client._reconcile_role_permissions_exact = (
        lambda path, assignment, revoke, expected, **kwargs:
        reconciliations.append((path, assignment, revoke, expected, kwargs))
        or path.endswith("/.control")
    )
    assert client._ensure_global_agent_permissions(
        "workspace",
        {"resources": {"agent_key": "agent", "agent_compute_key": "compute"}},
    ) is True
    assert roles == ["AIDP_DEVELOPER", "AI_DATA_PLATFORM_ADMIN"]
    admin_grantees = {("AI_DATA_PLATFORM_ADMIN", "ROLE")}
    assert ancestor_audits == [
        (
            "/workspaces/workspace",
            {"ADMINISTRATOR"},
            {"allowed_grantees": admin_grantees},
        ),
        (
            "/workspaces/workspace/objects/Workspace",
            {"READ", "USE", "MANAGE", "ADMIN"},
            {
                "allowed_grantees": admin_grantees,
                "inheritable_only": True,
            },
        ),
    ]
    agent = next(item for item in reconciliations if item[1] == "assignAgentPermissionDetails")
    assert agent[3] == (
        ("AIDP_DEVELOPER", "USE", None),
        ("AI_DATA_PLATFORM_ADMIN", "ADMIN", None),
    )
    assert agent[4] == {
        "include_columns": True,
        "reject_inherited_editors": True,
        "allowed_inherited_editors": admin_grantees,
    }
    compute = next(
        item for item in reconciliations if item[1] == "assignClusterPermissionDetails"
    )
    assert compute[3:] == ((), {})
    workspace_objects = {
        item[0].rsplit("/", 1)[-1]: (item[3], item[4])
        for item in reconciliations
        if item[1] == "assignWorkspaceObjectPermissionDetails"
    }
    assert workspace_objects == {
        ".control": (
            (("AI_DATA_PLATFORM_ADMIN", "ADMIN", True),),
            {
                "inheritable": True,
                "reject_inherited_editors": True,
                "allowed_inherited_editors": admin_grantees,
            },
        ),
        "modules": (
            (),
            {
                "inheritable": True,
                "reject_inherited_editors": True,
                "allowed_inherited_editors": admin_grantees,
            },
        ),
        "ai_data_governance_vsc_extension": (
            (("AI_DATA_PLATFORM_ADMIN", "ADMIN", True),),
            {
                "inheritable": True,
                "reject_inherited_editors": True,
                "allowed_inherited_editors": admin_grantees,
            },
        ),
        "manifest.json": (
            (),
            {
                "inheritable": False,
                "reject_inherited_editors": True,
                "allowed_inherited_editors": admin_grantees,
            },
        ),
        "agent": (
            (),
            {
                "inheritable": True,
                "reject_inherited_editors": True,
                "allowed_inherited_editors": admin_grantees,
            },
        ),
        "governance_agent.py": (
            (),
            {
                "inheritable": False,
                "reject_inherited_editors": True,
                "allowed_inherited_editors": admin_grantees,
            },
        ),
        "requirements.txt": (
            (),
            {
                "inheritable": False,
                "reject_inherited_editors": True,
                "allowed_inherited_editors": admin_grantees,
            },
        ),
        "agent-manifest.json": (
            (),
            {
                "inheritable": False,
                "reject_inherited_editors": True,
                "allowed_inherited_editors": admin_grantees,
            },
        ),
        "data_governance_sync.ipynb": (
            (),
            {
                "inheritable": False,
                "reject_inherited_editors": True,
                "allowed_inherited_editors": admin_grantees,
            },
        ),
    }


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


@pytest.mark.parametrize("operation_type", ["install", "redeploy"])
def test_failed_install_and_redeploy_resume_only_the_exact_operation(operation_type: str) -> None:
    client = module_client()
    state = manifest(operation_type)
    client._module_manifest = lambda _workspace: state
    client._write_module_manifest = lambda _workspace, _manifest: None
    client._ensure_global_agent_permissions = lambda *_args: False
    client._shared_compute = lambda _workspace: {"key": "shared"}
    client._ensure_governance_job = lambda *_args, **_kwargs: ("job", False)
    client._successful_governance_sync = lambda *_args, **_kwargs: True

    result = client._reconcile_governance_module(USER_OCID, OPERATION_ID, operation_type)
    assert result["status"] == "active"
    assert result["operation_id"] == OPERATION_ID
    assert result["operation_type"] == operation_type

    failed = manifest(operation_type)
    client._module_manifest = lambda _workspace: failed
    with pytest.raises(AidpProvisionConflict, match="original operation_id"):
        client._reconcile_governance_module(
            USER_OCID, "98cc3eec-fab0-461f-97ed-f95e7f6c8992", operation_type
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


def test_failed_delete_resumes_exact_phase_and_operation() -> None:
    client = module_client()
    state = manifest("delete", phase="pause")
    client._module_manifest = lambda _workspace: state
    client._write_module_manifest = lambda *_args: None
    client._shared_compute = lambda _workspace: {"key": "shared"}
    client._ensure_governance_job = lambda *_args, **_kwargs: ("job", False)
    client._delete_governance_deployments = lambda *_args: None
    client._cleanup_agent = lambda *_args: None
    client._delete_governance_compute = lambda *_args: None
    client._cleanup_lab_job = lambda *_args: None
    client._delete_governance_credential = lambda: None
    client._delete_workspace_path = lambda *_args: None
    client._delete_governance_tables = lambda: None
    client._delete_governance_prefixes = lambda: None
    assert client._delete_governance_module(USER_OCID, OPERATION_ID)["status"] == "not_installed"

    failed = manifest("delete", phase="pause")
    client._module_manifest = lambda _workspace: failed
    with pytest.raises(AidpProvisionConflict, match="original operation_id"):
        client._delete_governance_module(
            USER_OCID, "98cc3eec-fab0-461f-97ed-f95e7f6c8992"
        )


@pytest.mark.parametrize("operation_type", ["install", "redeploy"])
def test_failed_provisioning_can_start_a_new_delete_operation(operation_type: str) -> None:
    client = module_client()
    state = manifest(operation_type)
    original_resources = state["resources"]
    writes: list[dict] = []
    client._write_module_manifest = lambda _workspace, value: writes.append(dict(value))

    client._prepare_governance_deletion(
        "workspace", state, "98cc3eec-fab0-461f-97ed-f95e7f6c8992"
    )

    assert state["status"] == "deleting"
    assert state["phase"] == "disable"
    assert state["operation"] == {
        "operation_id": "98cc3eec-fab0-461f-97ed-f95e7f6c8992",
        "type": "delete",
        "phase": "started",
    }
    assert state["resources"] is original_resources
    assert state["enabled"] is True
    assert "error_code" not in state
    assert writes == [state]


def test_failed_install_without_workflow_cleans_up_without_provisioning_one() -> None:
    client = module_client()
    state = manifest("install", phase="control")
    state["resources"] = {}
    client._module_manifest = lambda _workspace: state
    client._write_module_manifest = lambda *_args: None
    client._ensure_governance_job = lambda *_args, **_kwargs: pytest.fail(
        "Delete must not provision a missing workflow"
    )
    client._cleanup_agent = lambda *_args: None
    client._delete_governance_compute = lambda *_args: None
    client._cleanup_lab_job = lambda *_args: None
    client._delete_governance_credential = lambda: None
    client._delete_workspace_path = lambda *_args: None
    client._delete_governance_tables = lambda: None
    client._delete_governance_prefixes = lambda: None

    result = client._delete_governance_module(
        USER_OCID, "98cc3eec-fab0-461f-97ed-f95e7f6c8992"
    )

    assert result["status"] == "not_installed"
    assert state["operation"]["type"] == "delete"


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


def test_redeploy_revision_is_requested_once_and_then_completed_on_visible_update() -> None:
    client = module_client()
    state = {
        "status": "redeploying",
        "operation": {"operation_id": OPERATION_ID, "type": "redeploy", "phase": "started"},
        "resources": {},
    }
    client._ensure_agent_compute = lambda _workspace: ({"key": "compute"}, False)
    client._ensure_agent = lambda *_args, **_kwargs: ("agent", False)
    requests: list[str] = []
    client._list = lambda *_args, **_kwargs: [{
        "key": "deployment",
        "timeUpdated": "2026-08-24T00:00:02Z" if requests else "2026-08-24T00:00:01Z",
    }]

    def deployment(*_args, redeploy_revision="", **_kwargs):
        if redeploy_revision:
            requests.append(redeploy_revision)
        return ({"key": "deployment", "timeUpdated": "2026-08-24T00:00:02Z"}, bool(redeploy_revision))

    client._ensure_agent_deployment = deployment
    assert client._ensure_global_agent("workspace", state) is True
    assert client._ensure_global_agent("workspace", state) is False
    assert client._ensure_global_agent("workspace", state) is False
    assert len(requests) == 1
    assert state["resources"]["deployment_revision"] == requests[0]


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
