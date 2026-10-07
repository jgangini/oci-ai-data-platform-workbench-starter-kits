"""Native membership API contract with SCIM responses doubled; no OCI resources created."""
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from oci._vendor import requests

from app.main import LOCAL_COOKIE_NAME, create_app
from app.security import issue_session
from test_identity_gods_eye_view import native_identity


GRANT = "/api/admin/gods-eye-view/users/participant"
SESSION = "/api/gods-eye-view/session"
READY = {"installed": True, "enabled": True, "status": "ready"}


def cloud_client(tmp_path, *, active=True, pending=False, module=None, app_name="viewer-app", admin_name="admin"):
    identity, members, patches = native_identity(active=active, pending=pending)
    settings = replace(identity.settings, admin_username=admin_name, cookie_secure=False, gods_eye_view_mode="local",
                       gods_eye_view_enabled=True, viewer_oidc_app_name=app_name,
                       session_secret_file=str(tmp_path / "session.key"),
                       aidp_settings_file=str(tmp_path / "settings.json"))
    identity.settings = settings
    app = create_app(settings)
    app.state.identity_factory = lambda: identity
    app.state.gods_eye_view_status = AsyncMock(return_value=READY if module is None else module)
    return TestClient(app), identity, members, patches


def sign_in(client, subject):
    client.cookies.set(LOCAL_COOKIE_NAME, issue_session(client.app.state.session_key, subject))


def test_grant_requires_administrator_and_ignores_identity_headers(tmp_path):
    client, _, members, patches = cloud_client(tmp_path)
    spoofed = {"X-Gods-Eye-View-User": "admin", "X-Gods-Eye-View-Role": "admin"}
    assert client.put(GRANT, json={"enabled": True}, headers=spoofed).status_code == 401
    members.add("readers")
    sign_in(client, "viewer:participant")
    assert client.get(SESSION).json()["role"] == "reader"
    assert client.put(GRANT, json={"enabled": True}, headers=spoofed).status_code == 401
    assert patches == []


def test_native_reader_grant_session_and_revoke_without_module_redeployment(tmp_path):
    admin, _, members, patches = cloud_client(tmp_path)
    sign_in(admin, "admin")
    grant = admin.put(GRANT, json={"enabled": True})
    assert grant.status_code == 200 and grant.json() == {"enabled": True, "mode": "OCI"}
    assert members == {"readers"}
    participant = TestClient(admin.app)
    sign_in(participant, "viewer:participant")
    session = participant.get(SESSION)
    assert session.status_code == 200
    assert session.json() == {"username": "viewer:participant", "role": "reader"}
    assert session.headers["X-Gods-Eye-View-Role"] == "reader"
    assert session.headers["Cache-Control"] == "no-store"
    assert participant.get("/api/admin/users").status_code == 401
    assert participant.get("/api/admin/gods-eye-view/sources").status_code == 401
    assert participant.post("/api/admin/gods-eye-view/module/deploy").status_code == 401
    admin.app.state.gods_eye_view_status.reset_mock()
    admin.app.state.gods_eye_view_status.return_value = {"installed": False, "enabled": False, "status": "not_installed"}
    revoke = admin.put(GRANT, json={"enabled": False})
    assert revoke.status_code == 200 and revoke.json()["enabled"] is False
    admin.app.state.gods_eye_view_status.assert_not_awaited()
    assert participant.get(SESSION).status_code == 401  # The existing cookie loses access immediately.
    assert members == set() and len(patches) == 2


@pytest.mark.parametrize("module", [
    {"installed": False, "enabled": False, "status": "not_installed"},
    {"installed": True, "enabled": False, "status": "activating"},
    {"installed": True, "enabled": True, "status": "failed"},
    {"installed": "false", "enabled": True, "status": "ready"},
    {"installed": True, "enabled": 1, "status": "ready"},
])
def test_grant_requires_verified_installed_module(tmp_path, module):
    client, _, members, patches = cloud_client(tmp_path, module=module)
    sign_in(client, "admin")
    assert client.put(GRANT, json={"enabled": True}).status_code == 409
    assert members == set() and patches == []


@pytest.mark.parametrize("active,pending", [(False, False), (True, True)])
def test_inactive_or_pending_native_account_cannot_be_granted_but_can_be_revoked(tmp_path, active, pending):
    client, _, members, patches = cloud_client(tmp_path, active=active, pending=pending)
    members.add("readers")
    sign_in(client, "admin")
    rejected = client.put(GRANT, json={"enabled": True})
    assert rejected.status_code == 409
    assert "Only active users" in rejected.json()["detail"]
    assert patches == []
    assert client.put(GRANT, json={"enabled": False}).status_code == 200
    assert "readers" not in members


def test_grant_requires_native_account_strict_boolean_and_configured_signin(tmp_path):
    client, _, _, patches = cloud_client(tmp_path, app_name="")
    sign_in(client, "admin")
    assert client.put(GRANT.replace("participant", "missing"), json={"enabled": True}).status_code == 404
    assert client.put(GRANT, json={"enabled": "true"}).status_code == 422
    assert client.put(GRANT, json={"enabled": True, "role": "admin"}).status_code == 422
    assert client.put(GRANT, json={"enabled": True}).status_code == 503
    assert patches == []


def test_membership_mutation_rejects_foreign_origin(tmp_path):
    client, _, _, patches = cloud_client(tmp_path)
    sign_in(client, "admin")
    assert client.put(GRANT, json={"enabled": True}, headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert patches == []
    assert client.put(GRANT, json={"enabled": True}, headers={"Origin": "http://testserver"}).status_code == 200


@pytest.mark.parametrize("method,path,identity_method", [
    ("PUT", GRANT, "get_gods_eye_view_account"),
    ("GET", SESSION, "gods_eye_view_user"),
])
@pytest.mark.parametrize("failure", [
    httpx.ConnectError("PRIVATE upstream credentials"),
    httpx.HTTPStatusError("PRIVATE upstream credentials", request=httpx.Request("GET", "https://identity.example.test"),
                         response=httpx.Response(403)),
    requests.exceptions.ConnectionError("PRIVATE upstream credentials"),
])
def test_native_transport_failures_deny_access_with_sanitized_503(tmp_path, method, path, identity_method, failure):
    client, identity, _, patches = cloud_client(tmp_path)
    setattr(identity, identity_method, AsyncMock(side_effect=failure))
    sign_in(client, "admin" if method == "PUT" else "viewer:participant")
    response = client.request(method, path, **({"json": {"enabled": True}} if method == "PUT" else {}))
    assert response.status_code == 503 and "PRIVATE" not in response.text
    assert patches == []


def test_unknown_reader_and_unsigned_identity_headers_cannot_start_session(tmp_path):
    client, _, _, _ = cloud_client(tmp_path)
    assert client.get(SESSION, headers={"X-Gods-Eye-View-User": "viewer:participant", "X-Gods-Eye-View-Role": "admin"}).status_code == 401
    sign_in(client, "viewer:missing")
    assert client.get(SESSION).status_code == 401


def test_reader_subject_matching_configured_administrator_is_never_administrative(tmp_path):
    client, _, members, patches = cloud_client(tmp_path, admin_name="viewer:participant")
    members.add("readers")
    sign_in(client, "viewer:participant")
    assert client.get("/api/admin/session").status_code == 401
    assert client.put(GRANT, json={"enabled": True}).status_code == 401
    assert client.get(SESSION).json()["role"] == "reader"
    members.clear()
    assert client.get(SESSION).status_code == 401
    assert patches == []
