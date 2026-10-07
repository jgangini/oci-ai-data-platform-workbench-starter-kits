"""OCI Identity Domains authorization-code login for shared viewer members."""
import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from oci._vendor import jwt
from oci._vendor import requests

from .identity import IdentityPending, _scim_literal
from .security import issue_session

CALLBACK_PATH = "/api/auth/oci/callback"
LOGIN_TTL = 300


def _domain_endpoint(url, domain):
    parsed, expected = urlsplit(url), urlsplit(domain)
    if (parsed.scheme != "https" or parsed.netloc != expected.netloc or parsed.username
            or parsed.fragment or parsed.query):
        raise ValueError("Invalid Identity Domains endpoint")
    return url


async def _application(identity, settings):
    app_name = settings.viewer_oidc_app_name or settings.managed_viewer_app_name
    if not app_name or not await identity._gods_eye_view_group():
        raise HTTPException(503, "Deploy the portal's OCI viewer sign-in configuration first")
    response = await identity._request("GET", "/admin/v1/Apps", params={
        "filter": f"name eq {_scim_literal(app_name)}", "count": 2,
        "attributes": "id,name,active,isOAuthClient,clientType,allowedGrants,redirectUris"})
    response.raise_for_status()
    body = response.json()
    apps = body.get("Resources", [])
    if body.get("totalResults") != 1 or len(apps) != 1:
        raise ValueError("Viewer sign-in application is unavailable or ambiguous")
    application = apps[0]
    if (application.get("name") != app_name or not application.get("id")
            or application.get("active") is not True or application.get("isOAuthClient") is not True
            or application.get("clientType") != "public"
            or application.get("allowedGrants") != ["authorization_code"]):
        raise ValueError("Invalid viewer sign-in application")
    redirects = application.get("redirectUris", [])
    redirect = settings.viewer_oidc_redirect_uri or (redirects[0] if len(redirects) == 1 else "")
    uri = urlsplit(redirect)
    loopback = not settings.cookie_secure and uri.scheme == "http" and uri.hostname in {"localhost", "127.0.0.1"}
    if (redirect not in redirects or uri.path != CALLBACK_PATH or uri.query or uri.fragment or uri.username
            or not (uri.scheme == "https" or loopback)):
        raise ValueError("Invalid registered viewer callback")
    return application["name"], redirect


def _identity_claims(token, keys, discovery, client_id, nonce):
    header = jwt.get_unverified_header(token)
    matches = [key for key in keys["keys"] if key.get("kid") == header.get("kid") and key.get("kty") == "RSA"
               and key.get("use", "sig") == "sig" and key.get("alg", "RS256") == "RS256"]
    if header.get("alg") != "RS256" or len(matches) != 1:
        raise ValueError("Invalid identity signing key")
    claims = jwt.decode(token, jwt.PyJWK(matches[0]).key, algorithms=["RS256"],
                        audience=client_id, issuer=discovery["issuer"],
                        options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]})
    now = time.time()
    if (type(claims["exp"]) not in {int, float} or type(claims["iat"]) not in {int, float}
            or not 0 < claims["iat"] <= now + 30 or not claims["exp"] > max(now, claims["iat"])
            or not isinstance(claims["sub"], str) or not claims["sub"] or not isinstance(claims["nonce"], str)
            or not hmac.compare_digest(claims["nonce"], nonce)
            or (claims.get("azp") and claims["azp"] != client_id)
            or (isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id)):
        raise ValueError("Invalid identity claims")
    return claims


def mount_viewer_login(app, cookie_name, client_ip):
    settings = app.state.settings
    flow_cookie = "__Host-aidp_viewer_login" if settings.cookie_secure else "aidp_viewer_login"
    # ponytail: bounded single-worker login transactions; use a shared expiring store before replicating the API.
    pending = {}
    app.state.viewer_oidc_client_factory = lambda: httpx.AsyncClient(timeout=15, follow_redirects=False)

    @app.get("/api/auth/oci/login")
    async def login(request: Request):
        retry = app.state.login_limiter.consume(client_ip(request))
        if retry:
            raise HTTPException(429, "Too many sign-in attempts; try again shortly", headers={"Retry-After": str(retry)})
        identity = app.state.identity_factory()
        try:
            client_id, redirect = await _application(identity, settings)
            async with app.state.viewer_oidc_client_factory() as client:
                response = await client.get(_domain_endpoint(settings.identity_domain_url + "/.well-known/openid-configuration", settings.identity_domain_url))
                response.raise_for_status()
                discovery = response.json()
            endpoints = {name: _domain_endpoint(discovery[name], settings.identity_domain_url)
                         for name in ("authorization_endpoint", "token_endpoint", "jwks_uri")}
            if not isinstance(discovery["issuer"], str) or urlsplit(discovery["issuer"]).scheme != "https":
                raise ValueError("Invalid issuer")
        except HTTPException:
            raise
        except (httpx.HTTPError, requests.exceptions.RequestException, ValueError, KeyError, TypeError, AttributeError, IdentityPending):
            raise HTTPException(503, "OCI viewer sign-in could not be verified") from None
        now = time.time()
        for state in tuple(pending):
            if pending[state]["expires"] <= now:
                del pending[state]
        if len(pending) >= 1024:
            raise HTTPException(429, "Too many sign-in attempts; try again shortly")
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        pending[state] = {"expires": now + LOGIN_TTL, "nonce": nonce, "verifier": verifier,
                          "client_id": client_id, "redirect": redirect, "discovery": discovery, "endpoints": endpoints}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        response = RedirectResponse(endpoints["authorization_endpoint"] + "?" + urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": redirect, "scope": "openid profile",
            "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}), status_code=302)
        response.set_cookie(flow_cookie, state, max_age=LOGIN_TTL, secure=settings.cookie_secure,
                            httponly=True, samesite="lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get(CALLBACK_PATH)
    async def callback(request: Request):
        params = request.query_params
        # Uvicorn must not log authorization codes or state query strings.
        request.scope["query_string"] = b""
        state = params.get("state", "")
        flow = None
        cookie_state = request.cookies.get(flow_cookie, "")
        if state and state.isascii() and cookie_state.isascii() and hmac.compare_digest(state, cookie_state):
            flow = pending.pop(state, None)
        error = "sign_in_failed"
        subject = None
        try:
            if (not flow or flow["expires"] <= time.time() or params.get("error") or not params.get("code")
                    or len(params.getlist("state")) != 1 or len(params.getlist("code")) != 1):
                raise ValueError("Invalid login transaction")
            async with app.state.viewer_oidc_client_factory() as client:
                result = await client.post(flow["endpoints"]["token_endpoint"], data={
                    "grant_type": "authorization_code", "client_id": flow["client_id"], "code": params["code"],
                    "redirect_uri": flow["redirect"], "code_verifier": flow["verifier"]})
                result.raise_for_status()
                token = result.json()["id_token"]
                result = await client.get(flow["endpoints"]["jwks_uri"])
                result.raise_for_status()
                claims = _identity_claims(token, result.json(), flow["discovery"], flow["client_id"], flow["nonce"])
            identity = app.state.identity_factory()
            user_id = claims.get("user_id")
            if not user_id:
                matches = await identity._users_matching(f"userName eq {_scim_literal(claims['sub'])}")
                if len(matches) != 1:
                    raise ValueError("Ambiguous native identity")
                user_id = matches[0]["id"]
            if not isinstance(user_id, str) or not user_id or "/" in user_id or ":" in user_id:
                raise ValueError("Invalid native identity")
            user = await identity.gods_eye_view_user(user_id)
            if not user:
                error = "access_denied"
                raise ValueError("Viewer membership is required")
            if user["email"].casefold() != claims["sub"].casefold():
                raise ValueError("Identity subject mismatch")
            subject = "viewer:" + user_id
        except (httpx.HTTPError, requests.exceptions.RequestException, jwt.PyJWTError, ValueError, KeyError, TypeError, AttributeError, OverflowError, IdentityPending):
            pass
        response = RedirectResponse("/gods-eye-view/" if subject else "/viewer/login?error=" + error, status_code=302)
        response.delete_cookie(flow_cookie, secure=settings.cookie_secure, httponly=True, samesite="lax", path="/")
        if subject:
            response.set_cookie(cookie_name, issue_session(app.state.session_key, subject), max_age=28_800,
                                secure=settings.cookie_secure, httponly=True, samesite="strict", path="/")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
