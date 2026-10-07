"""Exercise real signed OIDC tokens and the HTTP login boundary without OCI writes."""
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from oci._vendor import jwt

from app.config import Settings
from app.main import create_app
from app.security import verify_session

DOMAIN = "https://identity.example.test"
CLIENT_ID = "aidp_viewer_test"
REDIRECT = "https://portal.example.test/api/auth/oci/callback"
USER = {"id": "native-user", "email": "member@example.test", "active": True}


@pytest.fixture
def flow(tmp_path, request):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public.update(kid="signing-key", use="sig", alg="RS256")
    state = {"member": True, "reader_group": "readers", "claims": {}, "keys": [public], "app": {
        "id": "application-id", "name": CLIENT_ID, "active": True, "isOAuthClient": True,
        "clientType": "public", "allowedGrants": ["authorization_code"], "redirectUris": [REDIRECT]},
        "discovery": {"issuer": DOMAIN, "authorization_endpoint": DOMAIN + "/oauth2/v1/authorize",
                      "token_endpoint": DOMAIN + "/oauth2/v1/token", "jwks_uri": DOMAIN + "/admin/v1/SigningCert/jwk"},
        "requests": []}

    class Identity:
        async def _gods_eye_view_group(self):
            return state["reader_group"]

        async def _request(self, method, path, **kwargs):
            assert method == "GET" and path == "/admin/v1/Apps"
            assert kwargs["params"]["filter"] == f'name eq "{CLIENT_ID}"'
            return httpx.Response(200, json={"totalResults": 1, "Resources": [state["app"]]},
                                  request=httpx.Request(method, DOMAIN + path))

        async def gods_eye_view_user(self, user_id):
            return USER if state["member"] and user_id == USER["id"] else None

        async def _users_matching(self, expression):
            assert expression == 'userName eq "member@example.test"'
            return [USER]

    def transport(request):
        state["requests"].append(request)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=state["discovery"])
        if request.url.path == "/admin/v1/SigningCert/jwk":
            return httpx.Response(200, json={"keys": state["keys"]})
        assert request.url.path == "/oauth2/v1/token"
        state["exchange"] = parse_qs(request.content.decode())
        claims = {"iss": DOMAIN, "aud": CLIENT_ID, "iat": int(time.time()), "exp": int(time.time()) + 300,
                  "sub": USER["email"], "user_id": USER["id"], "nonce": state["nonce"], **state["claims"]}
        token = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "signing-key"})
        return httpx.Response(200, json={"id_token": state.get("token", token)})

    legacy = getattr(request, "param", False)
    settings = Settings(admin_username="admin", cookie_secure=True, identity_domain_url=DOMAIN,
                        lab_marker="aidp-lab-test", gods_eye_view_group_id="" if legacy else "readers",
                        viewer_oidc_app_name="" if legacy else CLIENT_ID,
                        aidp_settings_file=str(tmp_path / "settings.json"), session_secret_file=str(tmp_path / "session.key"))
    app = create_app(settings)
    app.state.identity_factory = Identity
    app.state.viewer_oidc_client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(transport))
    client = TestClient(app, base_url="https://portal.example.test", follow_redirects=False)
    return client, app, state


def start(flow):
    client, _, state = flow
    response = client.get("/api/auth/oci/login")
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"] and query["scope"] == ["openid profile"]
    assert "code_verifier" not in query
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=lax" in response.headers["set-cookie"]
    state["nonce"] = query["nonce"][0]
    return query


def finish(flow, query):
    return flow[0].get("/api/auth/oci/callback", params={"state": query["state"][0], "code": "one-use-code"})


def test_member_login_reader_permission_revocation_and_replay(flow):
    query = start(flow)
    client, app, state = flow
    assert finish(flow, query).headers["location"] == "/gods-eye-view/"
    assert state["exchange"]["redirect_uri"] == [REDIRECT]
    assert state["exchange"]["client_id"] == [CLIENT_ID]
    assert len(state["exchange"]["code_verifier"][0]) >= 43
    token = client.cookies.get("__Host-aidp_lab_admin")
    assert verify_session(token, app.state.session_key) == "viewer:native-user"
    session = client.get("/api/gods-eye-view/session")
    assert session.json() == {"username": "viewer:native-user", "role": "reader"}
    assert session.headers["x-gods-eye-view-role"] == "reader"
    assert client.get("/api/admin/session").status_code == 401
    state["member"] = False
    assert client.get("/api/gods-eye-view/session").status_code == 401
    before = len(state["requests"])
    assert finish(flow, query).headers["location"] == "/viewer/login?error=sign_in_failed"
    assert len(state["requests"]) == before


@pytest.mark.parametrize("flow", [True], indirect=True)
def test_legacy_host_supports_signin_with_fresh_native_validation(flow, monkeypatch):
    client, app, state = flow
    assert app.state.settings.viewer_oidc_app_name == app.state.settings.gods_eye_view_group_id == ""
    with monkeypatch.context() as patch:
        patch.setattr(app.state, "identity_factory", lambda: pytest.fail("Public configuration must not invoke OCI"))
        assert client.get("/api/public/config").json()["viewer_signin_enabled"] is True
    query = start(flow)
    assert query["client_id"] == [CLIENT_ID]
    assert finish(flow, query).headers["location"] == "/gods-eye-view/"
    state["reader_group"] = None
    assert client.get("/api/public/config").json()["viewer_signin_enabled"] is True
    assert client.get("/api/auth/oci/login").status_code == 503
    state["reader_group"] = "readers"
    state["app"]["name"] = "foreign-client"
    assert client.get("/api/public/config").json()["viewer_signin_enabled"] is True
    assert client.get("/api/auth/oci/login").status_code == 503


@pytest.mark.parametrize("claims", [{"iss": "https://wrong.example.test"}, {"aud": "wrong-client"},
    {"nonce": "wrong-nonce"}, {"exp": int(time.time()) - 1}, {"iat": int(time.time()) + 600},
    {"iat": True}, {"exp": "9999999999"}, {"exp": float("inf")}, {"iat": float("inf")},
    {"exp": float("nan")}, {"sub": "other@example.test"}, {"user_id": "other-user"},
    {"azp": "other-client"}, {"aud": [CLIENT_ID, "other-client"]}])
def test_invalid_token_cannot_create_session(flow, claims):
    query = start(flow)
    flow[2]["claims"] = claims
    assert finish(flow, query).headers["location"].startswith("/viewer/login?error=")
    assert not flow[0].cookies.get("__Host-aidp_lab_admin")


def test_no_grant_and_bad_signature_fail_closed(flow):
    query = start(flow)
    flow[2]["member"] = False
    assert finish(flow, query).headers["location"] == "/viewer/login?error=access_denied"
    query = start(flow)
    flow[2]["token"] = "invalid.token.signature"
    assert finish(flow, query).headers["location"] == "/viewer/login?error=sign_in_failed"


def test_username_subject_resolves_exact_native_account(flow):
    query = start(flow)
    flow[2]["claims"] = {"user_id": None}
    assert finish(flow, query).headers["location"] == "/gods-eye-view/"


@pytest.mark.parametrize("change", [{"active": False}, {"clientType": "confidential"},
    {"allowedGrants": ["authorization_code", "password"]}, {"name": "other"},
    {"redirectUris": ["https://evil.example.test/wrong"]}])
def test_invalid_application_cannot_start(flow, change):
    flow[2]["app"].update(change)
    response = flow[0].get("/api/auth/oci/login")
    assert response.status_code == 503 and "set-cookie" not in response.headers


def test_host_injection_and_cross_domain_discovery_rejected(flow):
    response = flow[0].get("/api/auth/oci/login", headers={"Host": "evil.example.test"})
    assert parse_qs(urlsplit(response.headers["location"]).query)["redirect_uri"] == [REDIRECT]
    flow[2]["discovery"]["token_endpoint"] = "https://evil.example.test/token"
    assert flow[0].get("/api/auth/oci/login").status_code == 503


def test_unbound_unicode_duplicate_or_failed_state_never_exchanges_code(flow):
    query = start(flow)
    before = len(flow[2]["requests"])
    for params in ({"state": "árbol", "code": "secret"}, {"state": "wrong", "code": "secret"},
                   [("state", query["state"][0]), ("state", "wrong"), ("code", "secret")]):
        assert flow[0].get("/api/auth/oci/callback", params=params).headers["location"] == "/viewer/login?error=sign_in_failed"
    assert len(flow[2]["requests"]) == before


def test_expired_transaction_and_login_rate_limit(flow, monkeypatch):
    query = start(flow)
    import app.viewer_auth as auth
    monkeypatch.setattr(auth.time, "time", lambda: 9999999999)
    assert finish(flow, query).headers["location"] == "/viewer/login?error=sign_in_failed"
    for _ in range(4):
        flow[0].get("/api/auth/oci/login")
    assert flow[0].get("/api/auth/oci/login").status_code == 429
