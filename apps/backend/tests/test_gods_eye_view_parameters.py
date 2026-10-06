import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import LOCAL_COOKIE_NAME, create_app
from app.security import issue_session
from test_gods_eye_view_oci_provider import provider


@pytest.fixture
def parameters(provider, monkeypatch):
    service, *_ = provider
    app = create_app(service.settings)
    client = TestClient(app)
    client.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    metadata = {"revision": "environment", "public_key": key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
        "providers": [{"id": "openai", "label": "OpenAI", "configured": False,
                       "fields": [{"id": "OPENAI_API_KEY", "label": "API key", "configured": False, "secret": True}]}]}
    calls, received = [], []
    def handle(request):
        calls.append(request)
        assert request.headers["cookie"] and request.url.host == "private-viewer"
        if request.method == "GET":
            return httpx.Response(200, json=metadata)
        sealed = json.loads(request.content)
        raw = lambda field: base64.b64decode(sealed[field])
        data_key = key.decrypt(raw("wrapped_key_b64"), padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        received.append(json.loads(AESGCM(data_key).decrypt(raw("nonce_b64"), raw("ciphertext_b64"), None)))
        return httpx.Response(200, json={"ok": True, "status": "success", "message": "Access verified"}
                              if request.method == "POST" else {**metadata, "revision": "saved"})
    original = httpx.AsyncClient
    monkeypatch.setenv("PRISMA_VIEWER_URL", "http://private-viewer:8081")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    return app, client, calls, received, metadata


def test_admin_boundary_status_redaction_and_encrypted_save_and_draft_test(parameters):
    app, client, calls, received, _ = parameters
    unauthorized = TestClient(app)
    assert unauthorized.get("/api/admin/gods-eye-view/parameters").status_code == 401
    assert unauthorized.put("/api/admin/gods-eye-view/parameters/openai", json={}).status_code == 401
    unauthorized.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "local-prisma:participant"))
    assert unauthorized.post("/api/admin/gods-eye-view/parameters/openai/test", json={}).status_code == 401
    assert not calls
    status = client.get("/api/admin/gods-eye-view/parameters")
    assert status.status_code == 200 and "public_key" not in status.json()
    assert status.headers["cache-control"] == "no-store"
    payload = {"expected_revision": "environment", "values": {"OPENAI_API_KEY": "private-draft-key"}}
    for method, suffix in (("POST", "/test"), ("PUT", "")):
        response = client.request(method, "/api/admin/gods-eye-view/parameters/openai" + suffix, json=payload)
        assert response.status_code == 200 and "private-draft-key" not in response.text and "public_key" not in response.text
        assert received[-1] == {"provider_id": "openai", **payload}
        assert b"private-draft-key" not in calls[-1].content
    assert [r.method for r in calls] == ["GET", "GET", "POST", "GET", "PUT"]


@pytest.mark.parametrize("changes", [{"values": {"endpoint": "PRIVATE"}}, {"values": {"OPENAI_API_KEY": "PRIVATE\nKEY"}},
    {"values": {"OPENAI_API_KEY": "PRIVATE" * 100}}, {"values": {"OPENAI_API_KEY": None}},
    {"expected_revision": "PRIVATE/../"}, {"extra": "PRIVATE"}])
def test_invalid_payload_never_echoes_credentials_or_reaches_apply(parameters, changes):
    _, client, calls, received, _ = parameters
    response = client.put("/api/admin/gods-eye-view/parameters/openai", json={"expected_revision": "environment", "values": {}, **changes})
    assert response.status_code == 422 and "PRIVATE" not in response.text and not received
    assert all(call.method == "GET" for call in calls)


def test_cross_site_and_path_injection_rejected(parameters):
    _, client, _, received, _ = parameters
    payload = {"expected_revision": "environment", "values": {}}
    assert client.post("/api/admin/gods-eye-view/parameters/openai/test", json=payload, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.put("/api/admin/gods-eye-view/parameters/unknown", json=payload).status_code == 422
    assert client.put("/api/admin/gods-eye-view/parameters/openai", json=payload, headers={"Origin": "https://other.invalid"}).status_code == 403
    assert not received


def test_no_configuration_backend_and_upstream_error_never_leak(parameters, monkeypatch):
    _, client, calls, _, metadata = parameters
    metadata["public_key"] = "PRIVATE invalid key"
    response = client.put("/api/admin/gods-eye-view/parameters/openai", json={"expected_revision": "environment", "values": {}})
    assert response.status_code == 503 and "PRIVATE" not in response.text
    from app.gods_eye_view.parameters import parameter_response
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        parameter_response(httpx.Response(409, json={"detail": "PRIVATE provider payload"}))
    assert error.value.status_code == 409 and "PRIVATE" not in error.value.detail
    monkeypatch.delenv("PRISMA_VIEWER_URL")
    before = len(calls)
    assert client.get("/api/admin/gods-eye-view/parameters").status_code == 503 and len(calls) == before


def test_native_tests_are_explicit_bounded_and_do_not_save(parameters):
    _, client, calls, received, _ = parameters
    payload = {"expected_revision": "environment", "values": {}}
    for _ in range(10):
        assert client.post("/api/admin/gods-eye-view/parameters/openai/test", json=payload).status_code == 200
    result = client.post("/api/admin/gods-eye-view/parameters/openai/test", json=payload)
    assert result.status_code == 429 and int(result.headers["retry-after"]) > 0
    assert len(received) == 10 and not any(call.method == "PUT" for call in calls)
