import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import LOCAL_COOKIE_NAME, create_app
from app.security import hash_secret, issue_session, verify_session
from tests.api.test_api import make_client, login


@pytest.mark.parametrize("changed_identity", [None, "not-admin", "ocid", "invalid-app"])
def test_installation_reader_callback_revalidates_original_platform_administrator(tmp_path, monkeypatch, changed_identity):
    from app.modules.ai_gods_eye_view.runtime import api, module
    client = make_client(tmp_path, deployment_mode="production")
    app = client.app
    app.state.settings = replace(app.state.settings, viewer_oidc_app_name="viewer", gods_eye_view_group_id="readers")
    callbacks, grants, app_checks = [], [], []
    class Module:
        def __init__(self, settings, runtime, grant_administrator_reader):
            callbacks.append(grant_administrator_reader)
        async def status(self, deploy=False, administrator=None):
            return {"status": "available"}
    monkeypatch.setattr(module, "GodsEyeViewModule", Module)
    monkeypatch.setattr(api, "runtime_for", lambda _: SimpleNamespace())
    identity = app.state.test_identity
    async def group():
        return "readers"
    async def request(method, path, **kwargs):
        app_checks.append((method, path))
        assert method == "GET" and path == "/admin/v1/Apps"
        assert kwargs["params"]["filter"] == 'name eq "viewer"'
        return httpx.Response(200, request=httpx.Request(method, "https://identity.example.test" + path), json={
            "totalResults": 1, "Resources": [{"id": "app", "name": "viewer", "active": changed_identity != "invalid-app",
                "isOAuthClient": True, "clientType": "public", "allowedGrants": ["authorization_code"],
                "redirectUris": ["https://portal.example.test/api/auth/oci/callback"]}]})
    async def grant(user_id, enabled):
        grants.append((user_id, enabled))
    identity._gods_eye_view_group, identity._request, identity.grant_gods_eye_view = group, request, grant
    login(client)
    assert client.get("/api/admin/gods-eye-view/module").status_code == 200
    assert len(callbacks) == 1 and grants == app_checks == []
    administrator = {"id": "user-id", "ocid": "ocid1.user.oc1..ada"}
    if changed_identity == "not-admin":
        app.state.test_aidp.admin_ocids.clear()
        with pytest.raises(HTTPException) as error:
            asyncio.run(callbacks[0](administrator))
        assert error.value.status_code == 403
    elif changed_identity == "ocid":
        with pytest.raises(ValueError, match="identity has changed"):
            asyncio.run(callbacks[0]({**administrator, "ocid": "ocid1.user.oc1..other"}))
    elif changed_identity == "invalid-app":
        with pytest.raises(ValueError, match="Invalid viewer sign-in application"):
            asyncio.run(callbacks[0](administrator))
        assert app_checks == [("GET", "/admin/v1/Apps")] and not grants
    else:
        asyncio.run(callbacks[0](administrator))
        assert grants == [("user-id", True)] and app_checks == [("GET", "/admin/v1/Apps")]
    if changed_identity in {"not-admin", "ocid"}:
        assert grants == app_checks == []
    assert not identity.closed


def local_settings(tmp_path, **overrides):
    values = dict(
        local_development_mode=True, gods_eye_view_enabled=True, cookie_secure=False,
        local_identity_artifact_dir=str(tmp_path / "identity"),
        session_secret_file=str(tmp_path / "session.key"), aidp_settings_file=str(tmp_path / "settings.json"),
        admin_username="administrator", admin_password_hash=hash_secret("admin-test-password", iterations=1000),
        registration_code_hash=hash_secret("AIDP-2026", iterations=1000),
    )
    return Settings(**{**values, **overrides})


def admin_login(client):
    assert client.post("/api/admin/login", json={"username": "administrator", "password": "admin-test-password"}).status_code == 204


def test_local_project_grant_restart_revocation_and_admin_boundary(tmp_path):
    settings = local_settings(tmp_path)
    app = create_app(settings)
    admin, participant = TestClient(app), TestClient(app)
    admin_login(admin)
    created = admin.post("/api/admin/users", json={
        "name": "Analista Bogotá", "email": "analista@example.com", "lab_ids": ["banking"],
    })
    assert created.status_code == 201
    user_id = admin.get("/api/admin/users").json()["users"][0]["id"]
    welcome_path = tmp_path / "identity" / f"welcome-{user_id}.json"
    welcome = json.loads(welcome_path.read_text(encoding="utf-8"))
    credentials = {key: welcome[key] for key in ("username", "password")}
    assert participant.post("/api/local/gods-eye-view/login", json=credentials).status_code == 401
    media_url = "/api/gods-eye-view/media/post-0001/image-01.webp"
    assert participant.get(media_url).status_code == 401
    assert admin.put(f"/api/admin/gods-eye-view/users/{user_id}", json={"enabled": True}).status_code == 200
    assert participant.post("/api/local/gods-eye-view/login", json=credentials).status_code == 204
    session = participant.get("/api/gods-eye-view/session")
    assert session.status_code == 200
    assert session.headers["X-Gods-Eye-View-User"] == f"local-gods-eye-view:{user_id}"
    assert "X-Territorial-User" not in session.headers and "X-PRISMA-User" not in session.headers
    assert verify_session(participant.cookies.get(LOCAL_COOKIE_NAME), app.state.session_key) == f"local-gods-eye-view:{user_id}"
    legacy = TestClient(app)
    for prefix in ("local-prisma:", "local-territorial:"):
        legacy.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, f"{prefix}{user_id}"))
        assert legacy.get("/api/prisma/session").status_code == 404
        assert legacy.get("/api/gods-eye-view/session").status_code == 401
        assert legacy.get("/api/gods-eye-view/snapshot").status_code == 401
        assert legacy.get("/api/local/prisma/workspace").status_code == 404
        assert legacy.get("/api/admin/prisma/sources").status_code == 404
    assert participant.get("/api/gods-eye-view/snapshot").status_code == 200
    media = participant.get(media_url)
    assert media.status_code == 200 and media.headers["content-type"] == "image/webp"
    assert media.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert admin.get("/api/admin/gods-eye-view/media/post-0001/image-01.webp").headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert participant.get("/api/gods-eye-view/snapshot").headers["content-security-policy"].startswith("default-src 'self';")
    assert media.headers["x-content-type-options"] == "nosniff" and media.headers["cache-control"] == "no-store"
    assert media.content[:4] == b"RIFF" and media.content[8:12] == b"WEBP"
    assert participant.get(media_url.replace(".webp", ".png")).status_code == 404
    assert participant.get(media_url.replace(".webp", ".jpg")).status_code == 404
    assert participant.get("/api/gods-eye-view/media/post-0007/image-01.webp").status_code == 404
    assert participant.get("/api/gods-eye-view/media/post-0001/private.pem").status_code == 404
    assert participant.get("/api/admin/gods-eye-view/media/post-0001/image-01.webp").status_code == 401
    workspace = participant.get("/api/local/gods-eye-view/workspace")
    assert workspace.status_code == 200 and workspace.json()["user"]["material"]["labs"][0]["lab_id"] == "banking"
    assert workspace.json()["user"]["gods_eye_view_access"] is True
    assert "territorial_access" not in workspace.json()["user"] and "prisma_access" not in workspace.json()["user"]
    assert "territorial_access" not in app.state.identity_factory().users[user_id]
    assert "password" not in workspace.text
    assert participant.get("/api/admin/gods-eye-view/sources").status_code == 401
    assert participant.post("/api/admin/gods-eye-view/simulation", json={"action": "start"}).status_code == 404
    assert participant.put(f"/api/admin/gods-eye-view/users/{user_id}", json={"enabled": True}).status_code == 401
    assert participant.post("/api/gods-eye-view/incidents/any/review", json={"status": "validated"}).status_code == 401
    assert participant.post("/api/gods-eye-view/sensors/any/location", json={"lat": 4.6, "lon": -74.1,
        "expected_lat": 4.6, "expected_lon": -74.1}).status_code == 401

    restarted = create_app(settings)
    restarted_admin, restarted_participant = TestClient(restarted), TestClient(restarted)
    restarted_participant.cookies.update(participant.cookies)
    assert restarted_participant.get("/api/local/gods-eye-view/workspace").status_code == 200
    admin_login(restarted_admin)
    restored = restarted_admin.get("/api/admin/users").json()["users"][0]
    assert restored["labs"][0]["lab_id"] == "banking"
    assert restarted_admin.post(f"/api/admin/users/{user_id}/labs", json={"lab_id": "retail"}).status_code == 200
    updated_material = restarted_participant.get("/api/local/gods-eye-view/workspace").json()["user"]["material"]
    assert {lab["lab_id"] for lab in updated_material["labs"]} == {"banking", "retail"}
    assert restarted_admin.put(f"/api/admin/gods-eye-view/users/{user_id}", json={"enabled": False}).status_code == 200
    assert restarted_participant.get("/api/gods-eye-view/snapshot").status_code == 401  # Existing cookie loses access immediately.
    restarted_legacy = TestClient(restarted)
    restarted_legacy.cookies.update(legacy.cookies)
    assert restarted_legacy.get("/api/prisma/snapshot").status_code == 404
    assert restarted_participant.get(media_url).status_code == 401
    assert restarted_participant.post("/api/local/gods-eye-view/login", json=credentials).status_code == 401
    assert restarted_admin.put(f"/api/admin/gods-eye-view/users/{user_id}", json={"enabled": True}).status_code == 200
    assert restarted_admin.delete(f"/api/admin/users/{user_id}").status_code == 204
    assert restarted_participant.get("/api/gods-eye-view/session").status_code == 401
    assert restarted_participant.post("/api/local/gods-eye-view/login", json=credentials).status_code == 401
    assert not welcome_path.exists()


def test_cloud_does_not_mount_local_identity_endpoints(tmp_path):
    client = TestClient(create_app(local_settings(tmp_path, local_development_mode=False, local_identity_artifact_dir="")))
    assert client.post("/api/local/gods-eye-view/login", json={"username": "any", "password": "any"}).status_code == 404
    assert client.get("/api/local/gods-eye-view/workspace").status_code == 404
    assert client.put("/api/admin/gods-eye-view/users/any", json={"enabled": True}).status_code == 401


def test_public_registration_explicit_local_grant(tmp_path):
    client = TestClient(create_app(local_settings(tmp_path)))
    result = client.post("/api/register", json={
        "name": "Analista Bogotá", "email": "analista@example.com", "lab_ids": ["banking"],
        "code": "AIDP-2026", "gods_eye_view": True,
    })
    assert result.status_code == 201
    welcome = json.loads(next((tmp_path / "identity").glob("welcome-*.json")).read_text(encoding="utf-8"))
    assert client.post("/api/local/gods-eye-view/login", json={key: welcome[key] for key in ("username", "password")}).status_code == 204
