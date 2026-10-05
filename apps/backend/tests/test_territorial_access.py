import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import LOCAL_COOKIE_NAME, create_app
from app.security import hash_secret, issue_session, verify_session


def local_settings(tmp_path, **overrides):
    values = dict(
        local_development_mode=True, territorial_enabled=True, cookie_secure=False,
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
    assert participant.post("/api/local/territorial/login", json=credentials).status_code == 401
    media_url = "/api/gods-eye-view/media/post-0001/image-01.svg"
    assert participant.get(media_url).status_code == 401
    assert admin.put(f"/api/admin/territorial/users/{user_id}", json={"enabled": True}).status_code == 200
    assert participant.post("/api/local/territorial/login", json=credentials).status_code == 204
    session = participant.get("/api/territorial/session")
    assert session.status_code == 200
    assert session.headers["X-Territorial-User"] == session.headers["X-PRISMA-User"] == f"local-prisma:{user_id}"
    assert verify_session(participant.cookies.get(LOCAL_COOKIE_NAME), app.state.session_key) == f"local-prisma:{user_id}"
    legacy = TestClient(app)
    for prefix in ("local-prisma:", "local-territorial:"):
        legacy.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, f"{prefix}{user_id}"))
        session = legacy.get("/api/prisma/session")
        assert session.status_code == 200
        assert session.headers["X-Territorial-User"] == session.headers["X-PRISMA-User"] == f"local-prisma:{user_id}"
        assert legacy.get("/api/territorial/snapshot").status_code == 200
        assert legacy.get("/api/local/prisma/workspace").status_code == 200
        assert legacy.get("/api/admin/prisma/sources").status_code == 401
    assert participant.get("/api/territorial/snapshot").status_code == 200
    media = participant.get(media_url)
    assert media.status_code == 200 and media.headers["content-type"] == "image/webp"
    assert media.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert admin.get("/api/admin/territorial/media/post-0001/image-01.svg").headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert participant.get("/api/territorial/snapshot").headers["content-security-policy"].startswith("default-src 'self';")
    assert media.headers["x-content-type-options"] == "nosniff" and media.headers["cache-control"] == "no-store"
    assert media.content[:4] == b"RIFF" and media.content[8:12] == b"WEBP"
    assert participant.get(media_url.replace(".svg", ".png")).content == media.content
    assert participant.get(media_url.replace(".svg", ".jpg")).status_code == 404
    assert participant.get("/api/gods-eye-view/media/post-0007/image-01.svg").status_code == 404
    assert participant.get("/api/gods-eye-view/media/post-0001/private.pem").status_code == 404
    assert participant.get("/api/admin/territorial/media/post-0001/image-01.svg").status_code == 401
    workspace = participant.get("/api/local/territorial/workspace")
    assert workspace.status_code == 200 and workspace.json()["user"]["material"]["labs"][0]["lab_id"] == "banking"
    assert workspace.json()["user"]["territorial_access"] is workspace.json()["user"]["prisma_access"] is True
    assert "territorial_access" not in app.state.identity_factory().users[user_id]
    assert "password" not in workspace.text
    assert participant.get("/api/admin/territorial/sources").status_code == 401
    assert participant.post("/api/admin/territorial/simulation", json={"action": "start"}).status_code == 401
    assert participant.put(f"/api/admin/territorial/users/{user_id}", json={"enabled": True}).status_code == 401
    assert participant.post("/api/territorial/incidents/any/review", json={"status": "validated"}).status_code == 401
    assert participant.post("/api/territorial/sensors/any/location", json={"lat": 4.6, "lon": -74.1,
        "expected_lat": 4.6, "expected_lon": -74.1}).status_code == 401

    restarted = create_app(settings)
    restarted_admin, restarted_participant = TestClient(restarted), TestClient(restarted)
    restarted_participant.cookies.update(participant.cookies)
    assert restarted_participant.get("/api/local/territorial/workspace").status_code == 200
    admin_login(restarted_admin)
    restored = restarted_admin.get("/api/admin/users").json()["users"][0]
    assert restored["labs"][0]["lab_id"] == "banking"
    assert restarted_admin.post(f"/api/admin/users/{user_id}/labs", json={"lab_id": "retail"}).status_code == 200
    updated_material = restarted_participant.get("/api/local/territorial/workspace").json()["user"]["material"]
    assert {lab["lab_id"] for lab in updated_material["labs"]} == {"banking", "retail"}
    assert restarted_admin.put(f"/api/admin/territorial/users/{user_id}", json={"enabled": False}).status_code == 200
    assert restarted_participant.get("/api/territorial/snapshot").status_code == 401  # Existing cookie loses access immediately.
    restarted_legacy = TestClient(restarted)
    restarted_legacy.cookies.update(legacy.cookies)
    assert restarted_legacy.get("/api/prisma/snapshot").status_code == 401
    assert restarted_participant.get(media_url).status_code == 401
    assert restarted_participant.post("/api/local/territorial/login", json=credentials).status_code == 401
    assert restarted_admin.put(f"/api/admin/territorial/users/{user_id}", json={"enabled": True}).status_code == 200
    assert restarted_admin.delete(f"/api/admin/users/{user_id}").status_code == 204
    assert restarted_participant.get("/api/territorial/session").status_code == 401
    assert restarted_participant.post("/api/local/territorial/login", json=credentials).status_code == 401
    assert not welcome_path.exists()


def test_cloud_does_not_mount_local_identity_endpoints(tmp_path):
    client = TestClient(create_app(local_settings(tmp_path, local_development_mode=False, local_identity_artifact_dir="")))
    assert client.post("/api/local/territorial/login", json={"username": "any", "password": "any"}).status_code == 404
    assert client.get("/api/local/territorial/workspace").status_code == 404
    assert client.put("/api/admin/territorial/users/any", json={"enabled": True}).status_code == 404


def test_public_registration_explicit_local_grant(tmp_path):
    client = TestClient(create_app(local_settings(tmp_path)))
    result = client.post("/api/register", json={
        "name": "Analista Bogotá", "email": "analista@example.com", "lab_ids": ["banking"],
        "code": "AIDP-2026", "territorial_control": True,
    })
    assert result.status_code == 201
    welcome = json.loads(next((tmp_path / "identity").glob("welcome-*.json")).read_text(encoding="utf-8"))
    assert client.post("/api/local/territorial/login", json={key: welcome[key] for key in ("username", "password")}).status_code == 204
