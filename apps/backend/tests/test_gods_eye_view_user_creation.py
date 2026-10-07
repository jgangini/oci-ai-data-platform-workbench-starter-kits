"""Administrator-selected shared access; native SCIM and AIDP are test doubles."""
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.identity import IdentityClient, IdentityPending
from app.security import hash_secret, issue_session
from test_api import FakeAidp


USER = {"id": "participant", "ocid": "ocid1.user.oc1..participant", "userName": "participant@example.com",
        "displayName": "Participant", "active": True, "externalId": "lab"}
PAYLOAD = {"name": "Participant", "email": "participant@example.com", "lab_ids": [], "gods_eye_view": True}
READY = {"installed": True, "enabled": True, "status": "ready"}


def creation_client(tmp_path, *, existing=False, members=(), app_available=True, group_available=True, local=False):
    state = {"user": dict(USER) if existing else None, "members": set(members), "events": [], "grant_failure": False}
    application = {"id": "app", "name": "viewer-app", "active": app_available, "isOAuthClient": True,
                   "clientType": "public", "allowedGrants": ["authorization_code"],
                   "redirectUris": ["https://portal.example.test/api/auth/oci/callback"]}

    def handler(request):
        path, method = request.url.path, request.method
        route = (method, path)
        state["events"].append(route)
        if route == ("GET", "/admin/v1/Apps"):
            return httpx.Response(200, json={"Resources": [application], "totalResults": 1})
        if route == ("GET", "/admin/v1/Groups/readers"):
            return httpx.Response(200, json={"id": "readers", "externalId": "lab:gods_eye_view"})
        if route == ("GET", "/admin/v1/Users/participant"):
            return httpx.Response(200, json=state["user"]) if state["user"] else httpx.Response(404)
        if route == ("GET", "/admin/v1/Users"):
            expression = request.url.params["filter"]
            found = not "groups.value eq" in expression or any(f'groups.value eq "{group}"' in expression for group in state["members"])
            rows = [state["user"]] if found and state["user"] else []
            return httpx.Response(200, json={"Resources": rows, "totalResults": len(rows)})
        if route == ("POST", "/admin/v1/Users"):
            assert state["user"] is None
            state["user"] = {**json.loads(request.content), "id": "participant", "ocid": USER["ocid"]}
            return httpx.Response(201, json=state["user"])
        if route == ("PUT", "/admin/v1/UserActivationInitiator/participant"):
            return httpx.Response(204)
        if route == ("DELETE", "/admin/v1/Users/participant"):
            state["user"] = None
            state["members"].clear()
            return httpx.Response(204)
        if method == "PATCH":
            group = path.rsplit("/", 1)[-1]
            if group == "readers" and state["grant_failure"]:
                return httpx.Response(503, json={"detail": "PRIVATE native error"})
            operation = json.loads(request.content)["Operations"][0]
            (state["members"].add if operation["op"] == "add" else state["members"].discard)(group)
            return httpx.Response(204)
        raise AssertionError(f"Unexpected native request: {method} {path}")

    settings = Settings(cookie_secure=False, local_development_mode=local, gods_eye_view_mode="local",
                        identity_domain_url="https://identity.example.test", developer_group_id="developers",
                        pending_group_id="pending", gods_eye_view_group_id="readers" if group_available else "",
                        viewer_oidc_app_name="viewer-app", lab_marker="lab", gods_eye_view_enabled=True,
                        aidp_platform_id="ocid1.aidataplatform.oc1..test", aidp_workspace_name="workspace",
                        aidp_region="us-chicago-1", objectstorage_namespace="namespace", bucket_name="bucket",
                        registration_code_hash=hash_secret("AIDP-2026", iterations=1000),
                        session_secret_file=str(tmp_path / "session.key"), aidp_settings_file=str(tmp_path / "settings.json"),
                        local_identity_artifact_dir=str(tmp_path / "identity"))
    app = main.create_app(settings)
    identity = IdentityClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    if not local:
        app.state.identity_factory = lambda: identity
    app.state.gods_eye_view_status = AsyncMock(return_value=READY)
    client = TestClient(app)
    client.cookies.set(main.LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
    return client, state


def test_god_only_creates_reader_without_aidp_material_and_deletion_preserves_shared_module(tmp_path):
    client, state = creation_client(tmp_path)
    client.app.state.aidp_factory = Mock(side_effect=AssertionError("God-only must not invoke AIDP"))
    response = client.post("/api/admin/users", json=PAYLOAD)
    assert response.status_code == 201
    assert response.json() == {"status": "active", "email": "participant@example.com", "labs": [],
                               "gods_eye_view_access": True, "viewer_url": "/viewer/login"}
    assert state["members"] == {"readers"}
    assert ("PATCH", "/admin/v1/Groups/developers") not in state["events"]
    assert state["events"].index(("GET", "/admin/v1/Apps")) < state["events"].index(("POST", "/admin/v1/Users"))
    assert state["events"].index(("GET", "/admin/v1/Groups/readers")) < state["events"].index(("POST", "/admin/v1/Users"))
    client.app.state.aidp_factory.assert_not_called()
    aidp = FakeAidp()
    client.app.state.aidp_factory = lambda: aidp
    client.app.state.gods_eye_view_status.reset_mock()
    assert client.delete("/api/admin/users/participant").status_code == 204
    assert state["user"] is None and state["members"] == set()
    assert aidp.cleaned == [USER["ocid"]]
    client.app.state.gods_eye_view_status.assert_not_awaited()
    assert client.app.state.gods_eye_view_status.return_value == READY


def test_mixed_access_provisions_selected_kits_and_both_developer_and_reader_groups(tmp_path):
    client, state = creation_client(tmp_path)
    aidp = FakeAidp()
    aidp.provision_user = AsyncMock(wraps=aidp.provision_user)
    client.app.state.aidp_factory = lambda: aidp
    response = client.post("/api/admin/users", json={**PAYLOAD, "lab_ids": ["retail"]})
    assert response.status_code == 201 and response.json()["gods_eye_view_access"] is True
    assert [lab["lab_id"] for lab in response.json()["labs"]] == ["retail"]
    assert state["members"] == {"developers", "readers"}
    assert aidp.provision_user.await_args.args[2] == ["retail"]


@pytest.mark.parametrize("members", [("readers",), ("developers",)])
def test_god_only_retry_preserves_existing_access_without_resending_activation(tmp_path, members):
    client, state = creation_client(tmp_path, existing=True, members=members)
    client.app.state.aidp_factory = Mock(side_effect=AssertionError("God-only must preserve existing labs"))
    for _ in range(2):
        assert client.post("/api/admin/users", json=PAYLOAD).status_code == 200
    assert state["members"] == {*members, "readers"}
    assert ("POST", "/admin/v1/Users") not in state["events"]
    assert ("PUT", "/admin/v1/UserActivationInitiator/participant") not in state["events"]
    assert ("PATCH", "/admin/v1/Groups/pending") not in state["events"]
    assert ("PATCH", "/admin/v1/Groups/developers") not in state["events"]


@pytest.mark.parametrize("unavailable", ["module", "application", "group"])
def test_selected_shared_access_is_validated_before_native_user_mutation(tmp_path, unavailable):
    client, state = creation_client(tmp_path, app_available=unavailable != "application", group_available=unavailable != "group")
    if unavailable == "module":
        client.app.state.gods_eye_view_status.return_value = {"installed": False, "enabled": False, "status": "not_installed"}
    response = client.post("/api/admin/users", json=PAYLOAD)
    assert response.status_code == (409 if unavailable == "module" else 503)
    assert state["user"] is None and state["members"] == set()
    assert all(method == "GET" for method, _ in state["events"])


@pytest.mark.parametrize("field,value", [("installed", "false"), ("enabled", 1)])
def test_malformed_module_readiness_denies_creation_before_identity_mutation(tmp_path, field, value):
    client, state = creation_client(tmp_path)
    client.app.state.gods_eye_view_status.return_value = {**READY, field: value}
    assert client.post("/api/admin/users", json=PAYLOAD).status_code == 409
    assert state["user"] is None and state["events"] == []


@pytest.mark.parametrize("body", [
    {**PAYLOAD, "gods_eye_view": False}, {**PAYLOAD, "gods_eye_view": "true"},
    {**PAYLOAD, "lab_ids": ["gods_eye_view"]},
])
def test_empty_or_invalid_selection_never_mutates_native_users(tmp_path, body):
    client, state = creation_client(tmp_path)
    assert client.post("/api/admin/users", json=body).status_code == 422
    assert state["events"] == []


def test_public_registration_still_requires_a_kit_and_cannot_self_grant_native_reader_access(tmp_path):
    client, state = creation_client(tmp_path)
    assert client.post("/api/register", json={**PAYLOAD, "code": "AIDP-2026"}).status_code == 422
    assert client.post("/api/register", json={**PAYLOAD, "lab_ids": ["banking"], "code": "AIDP-2026"}).status_code == 403
    assert state["events"] == []


def test_reader_grant_transport_failure_is_sanitized_and_retries_same_identity(tmp_path):
    client, state = creation_client(tmp_path)
    state["grant_failure"] = True
    response = client.post("/api/admin/users", json=PAYLOAD)
    assert response.status_code == 503 and "PRIVATE" not in response.text
    assert state["user"]["id"] == "participant" and "developers" not in state["members"]
    state["grant_failure"] = False
    assert client.post("/api/admin/users", json=PAYLOAD).status_code == 200
    assert state["members"] == {"readers"}
    assert state["events"].count(("POST", "/admin/v1/Users")) == 1


def test_pending_reader_grant_retries_without_creating_another_identity(tmp_path):
    client, state = creation_client(tmp_path)
    identity = client.app.state.identity_factory()
    grant = identity.grant_gods_eye_view

    async def eventually_grant(user_id, enabled):
        identity.grant_gods_eye_view = grant
        raise IdentityPending("Reader membership has not propagated yet")

    identity.grant_gods_eye_view = eventually_grant
    pending = client.post("/api/admin/users", json=PAYLOAD)
    assert pending.status_code == 202 and pending.json()["phase"] == "permissions"
    assert state["members"] == set()
    assert client.post("/api/admin/users", json=PAYLOAD).status_code == 200
    assert state["members"] == {"readers"}
    assert state["events"].count(("POST", "/admin/v1/Users")) == 1


def test_first_native_kit_is_provisioned_before_developer_access_is_activated(tmp_path):
    client, state = creation_client(tmp_path)
    assert client.post("/api/admin/users", json=PAYLOAD).status_code == 201
    aidp = FakeAidp()

    async def provision_with_reader_only(*args):
        assert state["members"] == {"readers"}
        return await FakeAidp.provision_user(aidp, *args)

    aidp.provision_user = AsyncMock(side_effect=provision_with_reader_only)
    aidp.add_lab = AsyncMock(side_effect=AssertionError("The first kit must provision the participant"))
    client.app.state.aidp_factory = lambda: aidp
    response = client.post("/api/admin/users/participant/labs", json={"lab_id": "retail"})
    assert response.status_code == 200 and response.json()["labs"][0]["lab_id"] == "retail"
    assert state["members"] == {"developers", "readers"}
    aidp.provision_user.assert_awaited_once()
    aidp.add_lab.assert_not_awaited()


def test_god_only_local_account_and_first_kit_transition_keep_simulated_access(tmp_path):
    client, _ = creation_client(tmp_path, local=True)
    created = client.post("/api/admin/users", json=PAYLOAD)
    assert created.status_code == 201 and created.json()["labs"] == []
    identity = client.app.state.identity_factory()
    user_id = next(iter(identity.users))
    assert identity.users[user_id]["developer_access"] is False
    assert "participant_code" not in created.json()
    added = client.post(f"/api/admin/users/{user_id}/labs", json={"lab_id": "retail"})
    assert added.status_code == 200 and added.json()["labs"][0]["lab_id"] == "retail"
    assert identity.users[user_id]["developer_access"] is True
    assert identity.users[user_id]["gods_eye_view_access"] is True
    welcome = json.loads((tmp_path / "identity" / f"welcome-{user_id}.json").read_text(encoding="utf-8"))
    client.cookies.clear()
    assert client.post("/api/local/gods-eye-view/login", json={key: welcome[key] for key in ("username", "password")}).status_code == 204
