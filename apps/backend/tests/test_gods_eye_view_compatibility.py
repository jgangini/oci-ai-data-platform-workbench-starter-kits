"""Canonical names preserve authenticated clients and existing runtime configuration."""
import importlib
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from test_gods_eye_view_access import admin_login, local_settings


@pytest.mark.parametrize("canonical,legacy,expected", [(None, "oci", "oci"), ("local", "oci", "local"), ("oci", "local", "oci")])
def test_environment_prefers_canonical_but_preserves_existing_deployments(monkeypatch, canonical, legacy, expected):
    monkeypatch.setenv("PRISMA_MODE", legacy)
    monkeypatch.setenv("PRISMA_VIEWER_ENABLED", "true")
    monkeypatch.delenv("TERRITORIAL_MODE", raising=False)
    monkeypatch.delenv("GODS_EYE_VIEW_MODE", raising=False)
    monkeypatch.delenv("GODS_EYE_VIEW_ENABLED", raising=False)
    monkeypatch.delenv("TERRITORIAL_VIEWER_ENABLED", raising=False)
    if canonical is not None:
        monkeypatch.setenv("GODS_EYE_VIEW_MODE", canonical)
    settings = Settings.from_env()
    assert settings.gods_eye_view_mode == expected
    assert settings.gods_eye_view_local_mode == (expected == "local")
    assert settings.gods_eye_view_enabled
    monkeypatch.setenv("GODS_EYE_VIEW_ENABLED", "false")
    assert not Settings.from_env().gods_eye_view_enabled


def test_canonical_and_legacy_routes_preserve_auth_dependencies_and_state(tmp_path):
    app = create_app(local_settings(tmp_path))
    client = TestClient(app)
    for prefix in ("gods-eye-view", "prisma", "territorial"):
        assert client.get(f"/api/{prefix}/snapshot").status_code == 401
        assert client.get(f"/api/admin/{prefix}/sources").status_code == 401
        assert client.post(f"/api/admin/{prefix}/synthetic/reset", json={}).status_code == 401
    admin_login(client)
    assert client.get("/api/admin/gods-eye-view/sources").json() == client.get("/api/admin/prisma/sources").json()
    assert client.get("/api/gods-eye-view/snapshot").json() == client.get("/api/prisma/snapshot").json()
    for prefix in ("prisma", "territorial"):
        assert client.post(f"/api/admin/{prefix}/synthetic/reset", json={"confirm": False}).status_code == 422
        response = client.get(f"/api/{prefix}/session")
        assert response.headers["X-Territorial-User"] == response.headers["X-PRISMA-User"] == "administrator"
    assert (tmp_path / "prisma.sqlite3").exists()
    assert not (tmp_path / "territorial.sqlite3").exists()


def test_source_tree_and_catalog_have_one_canonical_project_identity():
    from app.lab_packs import load_lab_pack, public_lab_catalog

    canonical = importlib.import_module("app.gods_eye_view.sensors")
    app_root = Path(canonical.__file__).parent.parent
    assert not (app_root / "territorial").exists()
    assert not (app_root / "prisma").exists()
    assert not (app_root / "labs" / "ai_data_governance_vsc_extension").exists()
    assert (app_root / "labs" / "ai_data_governance" / "lab.json").is_file()
    module = load_lab_pack("ai_data_governance_vsc_extension")
    assert module.lab_id == module.agent["name"] == "ai_data_governance"
    assert module.pack_version == "3.0.3"
    assert "ai_data_governance_vsc_extension" not in json.dumps(public_lab_catalog())


@pytest.mark.parametrize("api", ["gods-eye-view", "prisma", "territorial"])
@pytest.mark.parametrize("header", ["x-gods-eye-view-user", "x-prisma-user", "x-territorial-user"])
@pytest.mark.parametrize("subject", ["local-gods-eye-view:participant", "local-prisma:participant", "local-territorial:participant"])
def test_viewer_aliases_and_identity_headers_preserve_participant_boundary(monkeypatch, api, header, subject):
    from test_gods_eye_view_bridge import bridge, SNAPSHOT

    forwarded = []
    async def snapshot(_request):
        return SNAPSHOT
    async def forward(*args):
        forwarded.append(args)
        return {}
    monkeypatch.setattr(bridge, "snapshot_for", snapshot)
    monkeypatch.setattr(bridge, "admin_request", forward)
    client = TestClient(bridge.app)
    base = f"/api/{api}"
    assert client.get(base + "/snapshot").status_code == 401
    headers = {header: subject, "cookie": "session=fixture", "origin": "http://testserver", "x-gev-origin": "http://testserver"}
    response = client.get(base + "/snapshot", headers=headers)
    assert response.status_code == 200
    assert response.json()["can_admin"] is response.json()["can_review"] is False
    location = {"lat": 4.6, "lon": -74.1, "expected_lat": 4.5, "expected_lon": -74.0}
    assert client.post(base + "/sensors/station-1/location", headers=headers, json=location).status_code == 403
    assert client.post(base + "/oci-voice/turn", headers={**headers, "origin": "https://foreign.example"}, json={}).status_code == 403
    assert not forwarded


def test_canonical_viewer_identity_header_precedes_legacy():
    from test_gods_eye_view_bridge import bridge

    request = Request({"type": "http", "headers": [
        (b"cookie", b"session=fixture"), (b"x-gods-eye-view-user", b"local-gods-eye-view:participant"),
        (b"x-territorial-user", b"operator"),
        (b"x-prisma-user", b"operator")]})
    assert bridge.principal(request) == "local-gods-eye-view:participant"


@pytest.mark.parametrize("method,path,wire,status", [
    ("GET", "/api/gods-eye-view/snapshot?date_from=2026-10-05", "/api/prisma/snapshot?date_from=2026-10-05", 200),
    ("POST", "/api/admin/gods-eye-view/chat", "/api/admin/prisma/chat", 200),
    ("POST", "/api/admin/gods-eye-view/chat", "/api/admin/prisma/chat", 404),
    ("GET", "/api/admin/session", "/api/admin/session", 401),
    ("GET", "/api/gods-eye-view/identity", "/api/prisma/identity", 404),
    ("GET", "/api/gods-eye-view/identity", "/api/prisma/identity", 401),
    ("GET", "/api/gods-eye-view/identity", "/api/prisma/identity", 403),
    ("GET", "/api/gods-eye-view/identity", "/api/prisma/identity", 503),
    ("POST", "/api/gods-eye-view/identity", "/api/prisma/identity", 404),
    ("GET", "/api/admin/gods-eye-view/identity", "/api/admin/prisma/identity", 404),
])
def test_private_backend_wire_compatibility_never_retries_or_masks_auth(monkeypatch, method, path, wire, status):
    from test_gods_eye_view_bridge import bridge
    from app.viewer_identity import DEFAULT_IDENTITY

    calls = []
    payload = {"question": "fixture", "version": "one"} if method == "POST" else None
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"version": "one"})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    request = Request({"type": "http", "headers": [(b"cookie", b"session=fixture")]})
    fallback = method == "GET" and path == "/api/gods-eye-view/identity" and status == 404
    if status < 400 or fallback:
        result = asyncio.run(bridge.admin_request(request, method, path, payload))
        assert result == (DEFAULT_IDENTITY if fallback else {"version": "one"})
    else:
        with pytest.raises(HTTPException) as error:
            asyncio.run(bridge.admin_request(request, method, path, payload))
        assert error.value.status_code == status
    assert len(calls) == 1
    assert calls[0].url.raw_path.decode() == wire
    assert calls[0].method == method and calls[0].headers["cookie"] == "session=fixture"
    if payload is not None:
        assert json.loads(calls[0].content) == payload


@pytest.mark.parametrize("backend_status", [404, 401, 403, 503])
def test_native_html_uses_defaults_only_when_older_backend_lacks_identity(monkeypatch, backend_status):
    from test_gods_eye_view_bridge import bridge, HEADERS
    from test_viewer_identity import HTML

    monkeypatch.setenv("GODS_EYE_NATIVE_ENABLED", "true")
    calls = []
    def respond(request):
        calls.append(request)
        if request.url.path == "/api/prisma/identity":
            assert request.headers["cookie"] == HEADERS["cookie"]
            return httpx.Response(backend_status, json={"detail": "fixture"})
        assert str(request.url) == bridge.native_proxy.NATIVE_ORIGIN + "/"
        assert "cookie" not in request.headers
        return httpx.Response(200, headers={"content-type": "text/html"}, content=HTML.encode())
    client_type = httpx.AsyncClient
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    result = TestClient(bridge.app).get("/", headers={**HEADERS, "x-gev-origin": "http://testserver"})
    assert result.status_code == (200 if backend_status == 404 else backend_status)
    assert len(calls) == (2 if backend_status == 404 else 1)
    if backend_status == 404:
        assert result.text == HTML and result.headers["cache-control"] == "no-store"


def test_viewer_environment_keeps_exact_alias_precedence(monkeypatch):
    from test_gods_eye_view_bridge import bridge

    for name in ("GODS_EYE_VIEW_MODE", "TERRITORIAL_MODE", "PRISMA_MODE"):
        monkeypatch.delenv(name, raising=False)
    assert bridge.viewer_env("MODE", "default") == "default"
    for name, value in (("PRISMA_MODE", "legacy-client"), ("TERRITORIAL_MODE", "prior-client"), ("GODS_EYE_VIEW_MODE", "canonical-client")):
        monkeypatch.setenv(name, value)
        assert bridge.viewer_env("MODE") == value
