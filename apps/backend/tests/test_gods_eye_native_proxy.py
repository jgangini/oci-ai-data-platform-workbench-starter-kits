"""The native feed bridge authenticates first and never forwards browser credentials."""
import asyncio

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from test_prisma_bridge import bridge, HEADERS

proxy = bridge.native_proxy
PUBLIC = "http://localhost:18081"
AUTH = {**HEADERS, "x-gev-origin": PUBLIC}


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setenv("GODS_EYE_NATIVE_ENABLED", "true")
    with TestClient(bridge.app) as client:
        yield client


def test_native_routes_require_session_before_network(native, monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("Unauthenticated request reached the native provider")
    monkeypatch.setattr(proxy, "proxy", forbidden)
    for route in ("/", "/api/weather", "/api/setup/status", "/api/setup/browser", "/api/prisma/oci-provider"):
        assert native.get(route).status_code == 401


@pytest.mark.parametrize("method,extra,status", [
    ("GET", {}, 200), ("GET", {"origin": PUBLIC}, 200),
    ("POST", {"origin": PUBLIC}, 200), ("POST", {}, 403),
    ("GET", {"origin": "https://untrusted.example"}, 403),
    ("GET", {"sec-fetch-site": "same-site"}, 403),
    ("GET", {"sec-fetch-site": "cross-site"}, 403),
    ("GET", {"x-gev-origin": ""}, 403),
])
def test_native_origin_boundary(method, extra, status):
    headers = {**AUTH, **extra}
    request = Request({"type": "http", "method": method, "headers": [(key.encode(), value.encode()) for key, value in headers.items()]})
    if status == 200:
        proxy.check_origin(request)
    else:
        with pytest.raises(HTTPException) as error:
            proxy.check_origin(request)
        assert error.value.status_code == status


def test_native_provider_stream_preserves_range_but_not_secrets(native, monkeypatch):
    seen, closed = [], []
    class Data(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            yield b"second"
        async def aclose(self):
            closed.append(True)
    def respond(request):
        seen.append(request)
        return httpx.Response(206, headers={"Content-Type": "application/octet-stream", "Content-Range": "bytes 0-10/20",
            "Set-Cookie": "secret=must-not-propagate", "Location": "https://untrusted.example"}, stream=Data())
    client_type = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    response = native.get("/api/weather?lat=4.66&lon=-74.08", headers={**AUTH, "range": "bytes=0-10", "authorization": "private", "x-forwarded-for": "untrusted"})
    assert response.status_code == 206 and response.content == b"firstsecond" and closed
    assert response.headers["content-range"] == "bytes 0-10/20"
    assert "set-cookie" not in response.headers and "location" not in response.headers
    assert str(seen[0].url) == proxy.NATIVE_ORIGIN + "/api/weather?lat=4.66&lon=-74.08"
    assert seen[0].headers["origin"] == proxy.NATIVE_ORIGIN
    assert seen[0].headers["range"] == "bytes=0-10"
    for key in ("cookie", "authorization", "x-forwarded-for", "x-prisma-user", "x-gev-origin"):
        assert key not in seen[0].headers


@pytest.mark.parametrize("path", ["api/setup/keys", "api/mcp", "api/admin/users", "__native_setup", "../.env", "/api/weather"])
def test_native_proxy_rejects_nonpublic_paths(native, path):
    request = Request({"type": "http", "method": "GET", "headers": [(b"x-gev-origin", PUBLIC.encode())]})
    with pytest.raises(HTTPException) as error:
        asyncio.run(proxy.proxy(request, path))
    assert error.value.status_code == 404


def test_native_body_limit_and_malformed_chat(native):
    assert native.post("/api/realtime/token", headers={**AUTH, "origin": PUBLIC}, content=b"x" * 1_000_001).status_code == 413
    assert native.post("/api/prisma/oci-chat", headers={**AUTH, "origin": PUBLIC, "content-type": "application/json"}, content="invalid").status_code == 422


def test_native_health_checks_both_processes(native, monkeypatch):
    monkeypatch.setattr(proxy, "native_json", lambda _path: {"status": "failed"})
    assert native.get("/health").status_code == 503
    monkeypatch.setattr(proxy, "native_json", lambda _path: {"status": "ok"})
    assert native.get("/health").json()["status"] == "ok"


def test_native_status_does_not_expose_development_key_writer(native, monkeypatch):
    monkeypatch.setattr(proxy, "native_json", lambda _path: {"keys": [{"id": "openai", "set": False}], "total": 1, "setCount": 0})
    value = native.get("/api/setup/status", headers=AUTH).json()
    assert value["server_managed"] is True and value["keys"][0]["set"] is False
    assert native.post("/api/setup/keys", headers={**AUTH, "origin": PUBLIC}, json={"OPENAI_API_KEY": "fixture"}).status_code == 404


def test_browser_config_exposes_only_the_two_intentionally_browser_side_keys(native, monkeypatch):
    monkeypatch.setattr(proxy, "native_json", lambda _path: {"googleApiKey": "public-map-key", "cesiumToken": "public-map-token", "OPENAI_API_KEY": "secret", "OCI_CONFIG_FILE": "/private"})
    assert native.get("/api/setup/browser", headers=AUTH).json() == {"googleApiKey": "public-map-key", "cesiumToken": "public-map-token"}
