import httpx
import pytest
from fastapi.testclient import TestClient

from test_gods_eye_native_proxy import AUTH, proxy
from test_territorial_bridge import bridge
from test_viewer_identity import HTML


@pytest.mark.parametrize("path", ["/", "/index.html"])
def test_native_html_gets_saved_identity_before_any_script_and_never_cache_stale(path, monkeypatch):
    monkeypatch.setenv("GODS_EYE_NATIVE_ENABLED", "true")
    seen = []
    identity = {"name": "Territorio <seguro>", "description": "Comunidad & datos"}
    async def settings(_request, method, endpoint):
        assert (method, endpoint) == ("GET", "/api/territorial/identity")
        return identity
    monkeypatch.setattr(bridge, "admin_request", settings)
    def respond(request):
        seen.append(request)
        return httpx.Response(200, content=HTML.encode(), headers={"content-type": "text/html; charset=utf-8",
            "etag": '"old"', "cache-control": "public, max-age=3600", "content-security-policy": "default-src 'self'"})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    with TestClient(bridge.app) as client:
        response = client.get(path, headers={**AUTH, "if-none-match": '"old"', "range": "bytes=0-10"})
        assert response.status_code == 200
        assert response.text.count("Territorio &lt;seguro&gt;") == 3
        assert '<p class="subtitle">Comunidad &amp; datos</p>' in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-security-policy"] == "default-src 'self'"
        assert int(response.headers["content-length"]) == len(response.content)
        assert "etag" not in response.headers
        identity.update(name="", description="")
        assert client.get(path, headers=AUTH).text == HTML
    assert all("if-none-match" not in request.headers and "range" not in request.headers and "cookie" not in request.headers for request in seen)


@pytest.mark.parametrize("document,content_type", [(b"not html", "text/plain"), (b"x" * 2_000_001, "text/html"), (b"\xff", "text/html"), (b"<title>changed upstream</title>", "text/html")], ids=["content-type", "size", "encoding", "changed-template"])
def test_native_identity_rejects_invalid_upstream_and_closes_stream(document, content_type, monkeypatch):
    monkeypatch.setenv("GODS_EYE_NATIVE_ENABLED", "true")
    closed = []
    class Data(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield document
        async def aclose(self):
            closed.append(True)
    async def settings(*_args):
        return {"name": "Custom", "description": ""}
    monkeypatch.setattr(bridge, "admin_request", settings)
    client_type = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": content_type}, stream=Data())), **kwargs))
    with TestClient(bridge.app) as client:
        response = client.get("/", headers=AUTH)
    assert response.status_code == 503 and closed
    assert response.json()["detail"] == "The native viewer identity is unavailable"


def test_static_assets_never_request_identity(monkeypatch):
    monkeypatch.setenv("GODS_EYE_NATIVE_ENABLED", "true")
    async def forbidden(*_args):
        pytest.fail("Asset requested branding settings")
    monkeypatch.setattr(bridge, "admin_request", forbidden)
    client_type = httpx.AsyncClient
    class Data(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"asset"
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Data(), headers={"content-type": "text/css"})), **kwargs))
    with TestClient(bridge.app) as client:
        assert client.get("/assets/styles.css", headers=AUTH).text == "asset"


def test_viewer_identity_route_keeps_auth_and_delegates_to_backend(monkeypatch):
    calls = []
    async def settings(request, method, endpoint):
        calls.append((method, endpoint))
        return {"name": "Saved", "description": "Read only"}
    monkeypatch.setattr(bridge, "admin_request", settings)
    with TestClient(bridge.app) as client:
        assert client.get("/api/territorial/identity").status_code == 401
        assert calls == []
        response = client.get("/api/territorial/identity", headers=AUTH)
        assert response.json() == {"name": "Saved", "description": "Read only"}
        assert calls == [("GET", "/api/territorial/identity")]
