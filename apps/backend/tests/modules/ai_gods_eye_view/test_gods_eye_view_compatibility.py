"""Retired client interfaces are rejected; current authorization remains required."""
import asyncio
import importlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.modules.ai_gods_eye_view.test_gods_eye_view_access import admin_login, local_settings


@pytest.mark.parametrize("canonical,legacy", [(None, "oci"), ("local", "oci"), ("oci", "local")])
def test_environment_ignores_retired_names(monkeypatch, canonical, legacy):
    for prefix in ("PRISMA", "TERRITORIAL"):
        monkeypatch.setenv(prefix + "_MODE", legacy)
        monkeypatch.setenv(prefix + "_VIEWER_ENABLED", "true")
    monkeypatch.delenv("GODS_EYE_VIEW_MODE", raising=False)
    monkeypatch.delenv("GODS_EYE_VIEW_ENABLED", raising=False)
    if canonical is not None:
        monkeypatch.setenv("GODS_EYE_VIEW_MODE", canonical)
    settings = Settings.from_env()
    assert settings.gods_eye_view_mode == canonical
    assert not settings.gods_eye_view_enabled
    monkeypatch.setenv("GODS_EYE_VIEW_ENABLED", "true")
    assert Settings.from_env().gods_eye_view_enabled


def test_only_canonical_routes_preserve_auth_and_retired_routes_are_not_mounted(tmp_path):
    app = create_app(local_settings(tmp_path))
    client = TestClient(app)
    assert client.get("/api/config").status_code == 200
    assert client.get("/api/public/config").status_code == 404
    for path in ("/api/gods-eye-view/snapshot", "/api/admin/gods-eye-view/sources"):
        assert client.get(path).status_code == 401
    retired = [f"/api/{admin}{prefix}/{tail}" for prefix in ("prisma", "territorial")
               for admin, tail in (("", "snapshot"), ("", "session"), ("", "identity"),
                                   ("admin/", "sources"), ("admin/", "identity"))]
    for path in retired:
        assert client.get(path).status_code == 404
    admin_login(client)
    assert client.get("/api/admin/gods-eye-view/sources").status_code == 200
    assert client.get("/api/gods-eye-view/snapshot").status_code == 200
    assert client.get("/api/gods-eye-view/session").headers["X-Gods-Eye-View-User"] == "administrator"
    for path in retired:
        assert client.get(path).status_code == 404
    for prefix in ("prisma", "territorial"):
        assert client.post(f"/api/admin/{prefix}/synthetic/reset", json={"confirm": False}).status_code == 404
    assert client.post("/api/admin/gods-eye-view/simulation", json={"action": "start"}).status_code == 404
    assert client.put("/api/admin/gods-eye-view/sources/x", json={"mode": "simulation"}).status_code == 422
    for method, suffix in (("post", ""), ("post", "/redeploy"), ("delete", "")):
        assert getattr(client, method)("/api/admin/users/unused/modules/ai_data_governance_vsc_extension" + suffix).status_code == 404


def test_retired_registration_field_is_rejected_before_creating_identity(tmp_path):
    app = create_app(local_settings(tmp_path))
    client = TestClient(app)
    payload = {"name": "Participant", "email": "participant@example.com", "lab_ids": ["banking"], "territorial_control": True}
    assert client.post("/api/register", json={**payload, "code": "AIDP-2026"}).status_code == 422
    admin_login(client)
    assert client.post("/api/admin/users", json=payload).status_code == 422
    assert app.state.identity_factory().users == {}


def test_source_tree_and_catalog_have_one_canonical_project_identity():
    from app.packages.catalog import APP_ROOT, PackageError, load_package, public_kit_catalog
    importlib.import_module("app.modules.ai_gods_eye_view.runtime.sensors")
    assert not (APP_ROOT / "territorial").exists() and not (APP_ROOT / "prisma").exists()
    assert (APP_ROOT / "modules/ai_data_governance/package/lab.json").is_file()
    with pytest.raises(PackageError):
        load_package("ai_data_governance_vsc_extension")
    module = load_package("ai_data_governance")
    assert module.lab_id == module.agent["name"] == "ai_data_governance"
    assert "ai_data_governance_vsc_extension" not in json.dumps(public_kit_catalog())


@pytest.mark.parametrize("api", ["gods-eye-view", "prisma", "territorial"])
@pytest.mark.parametrize("header", ["x-gods-eye-view-user", "x-prisma-user", "x-territorial-user"])
@pytest.mark.parametrize("subject", ["local-gods-eye-view:participant", "local-prisma:participant", "local-territorial:participant"])
def test_only_canonical_route_header_and_subject_allow_reader_access(monkeypatch, api, header, subject):
    from tests.modules.ai_gods_eye_view.test_gods_eye_view_bridge import bridge, SNAPSHOT
    async def snapshot(_request): return SNAPSHOT
    monkeypatch.setattr(bridge, "snapshot_for", snapshot)
    client = TestClient(bridge.app)
    headers = {header: subject, "x-gods-eye-view-role": "reader", "cookie": "session=fixture",
               "origin": "http://testserver", "x-gev-origin": "http://testserver"}
    expected = 404 if api != "gods-eye-view" else 401 if header != "x-gods-eye-view-user" or not subject.startswith("local-gods-eye-view:") else 200
    result = client.get(f"/api/{api}/snapshot", headers=headers)
    assert result.status_code == expected
    if expected == 200:
        assert result.json()["can_admin"] is result.json()["can_review"] is False
        location = {"lat": 4.6, "lon": -74.1, "expected_lat": 4.5, "expected_lon": -74.0}
        assert client.post(f"/api/{api}/sensors/station-1/location", headers=headers, json=location).status_code == 403
        assert client.post(f"/api/{api}/oci-voice/turn", headers={**headers, "origin": "https://foreign.example"}, json={}).status_code == 403


@pytest.mark.parametrize("header", ["x-territorial-user", "x-prisma-user"])
def test_retired_identity_headers_cannot_authenticate(header):
    from tests.modules.ai_gods_eye_view.test_gods_eye_view_bridge import bridge
    request = Request({"type": "http", "headers": [(b"cookie", b"session=fixture"), (header.encode(), b"operator")]})
    with pytest.raises(HTTPException) as error:
        bridge.principal(request)
    assert error.value.status_code == 401


@pytest.mark.parametrize("method,path,status", [
    ("GET", "/api/gods-eye-view/snapshot?date_from=2026-10-05", 200),
    ("POST", "/api/gods-eye-view/chat", 200), ("POST", "/api/gods-eye-view/chat", 404),
    ("GET", "/api/admin/session", 401),
    *(("GET", "/api/gods-eye-view/identity", code) for code in (404, 401, 403, 503)),
    ("POST", "/api/gods-eye-view/identity", 404), ("GET", "/api/admin/gods-eye-view/identity", 404),
])
def test_private_backend_uses_canonical_path_without_retries_or_error_masking(monkeypatch, method, path, status):
    from tests.modules.ai_gods_eye_view.test_gods_eye_view_bridge import bridge
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"version": "one"})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(respond), **kw))
    request = Request({"type": "http", "headers": [(b"cookie", b"session=fixture")]})
    payload = {"question": "fixture", "version": "one"} if method == "POST" else None
    if status < 400:
        assert asyncio.run(bridge.admin_request(request, method, path, payload)) == {"version": "one"}
    else:
        with pytest.raises(HTTPException) as error:
            asyncio.run(bridge.admin_request(request, method, path, payload))
        assert error.value.status_code == status
    assert len(calls) == 1 and calls[0].url.raw_path.decode() == path
    assert calls[0].method == method and calls[0].headers["cookie"] == "session=fixture"
    if payload is not None: assert json.loads(calls[0].content) == payload


@pytest.mark.parametrize("backend_status", [404, 401, 403, 503])
def test_native_html_propagates_backend_identity_failure(monkeypatch, backend_status):
    from tests.modules.ai_gods_eye_view.test_gods_eye_view_bridge import bridge, HEADERS
    monkeypatch.setenv("GODS_EYE_NATIVE_ENABLED", "true")
    calls = []
    def respond(request):
        calls.append(request)
        assert request.url.path == "/api/gods-eye-view/identity" and request.headers["cookie"] == HEADERS["cookie"]
        return httpx.Response(backend_status, json={"detail": "fixture"})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(respond), **kw))
    result = TestClient(bridge.app).get("/", headers={**HEADERS, "x-gev-origin": "http://testserver"})
    assert result.status_code == backend_status and len(calls) == 1


def test_viewer_environment_ignores_retired_aliases(monkeypatch):
    from tests.modules.ai_gods_eye_view.test_gods_eye_view_bridge import bridge
    monkeypatch.delenv("GODS_EYE_VIEW_MODE", raising=False)
    for name in ("PRISMA_MODE", "TERRITORIAL_MODE"):
        monkeypatch.setenv(name, "old-client")
        assert bridge.viewer_env("MODE", "default") == "default"
    monkeypatch.setenv("GODS_EYE_VIEW_MODE", "local")
    assert bridge.viewer_env("MODE") == "local"


@pytest.mark.parametrize("method", ["get", "post", "put", "delete"])
@pytest.mark.parametrize("path", ["/prisma", "/prisma/view", "/admin/prisma", "/local/prisma/login", "/api/prisma/snapshot", "/api/admin/territorial/sources"])
def test_private_viewer_rejects_retired_routes_before_spa_or_method_fallback(method, path):
    from tests.modules.ai_gods_eye_view.test_gods_eye_view_bridge import bridge, HEADERS
    client = TestClient(bridge.app)
    assert getattr(client, method)(path).status_code == 404
    assert getattr(client, method)(path, headers=HEADERS).status_code == 404
