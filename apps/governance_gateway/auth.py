from __future__ import annotations

import json
from dataclasses import dataclass
from threading import Lock
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import jwt

from .policy import Principal


class AuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True)
class OidcSettings:
    issuer: str
    audience: str
    authority: str

    def __post_init__(self) -> None:
        if not self.issuer.startswith("https://") or not self.authority.startswith("https://") or not self.audience:
            raise ValueError("OIDC authority, issuer and audience are required; insecure identity fallback is disabled.")


class OidcAuthenticator:
    def __init__(self, settings: OidcSettings) -> None:
        self.settings = settings
        self._keys: jwt.PyJWKClient | None = None
        self._lock = Lock()

    def authenticate(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("A bearer token is required.")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            signing_key = self._key_client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except (jwt.PyJWTError, URLError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise AuthenticationError("The OCI identity token is invalid or expired.") from exc
        return Principal(
            subject=str(claims["sub"]),
            groups=_string_claims(claims.get("groups")),
            roles=_string_claims(claims.get("roles")),
            scopes=_string_claims(claims.get("scope") or claims.get("scp")),
        )

    def _key_client(self) -> jwt.PyJWKClient:
        if self._keys is not None:
            return self._keys
        with self._lock:
            if self._keys is not None:
                return self._keys
            authority = self.settings.authority.rstrip("/")
            document = _discovery_document(authority)
            if (
                not isinstance(document, dict)
                or _normalize_issuer(str(document.get("issuer") or "")) != _normalize_issuer(self.settings.issuer)
            ):
                raise ValueError("OIDC discovery returned an unexpected issuer.")
            jwks_uri = str(document.get("jwks_uri") or "")
            if not _same_https_origin(authority, jwks_uri):
                raise ValueError("OIDC discovery returned an unsafe JWKS URL.")
            self._keys = jwt.PyJWKClient(jwks_uri, cache_keys=True, timeout=15)
            return self._keys


def _string_claims(value: object) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset(item for item in value.split() if item)
    if isinstance(value, list):
        return frozenset(item for item in value if isinstance(item, str) and item)
    return frozenset()


def _normalize_issuer(value: str) -> str:
    return value.rstrip("/")


def _same_https_origin(authority: str, target: str) -> bool:
    expected = urlparse(authority)
    actual = urlparse(target)
    return actual.scheme == "https" and actual.netloc.casefold() == expected.netloc.casefold()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        return None


def _discovery_document(authority: str) -> dict[str, object]:
    request = Request(
        f"{authority}/.well-known/openid-configuration",
        headers={"accept": "application/json"}, method="GET",
    )
    with build_opener(_NoRedirect()).open(request, timeout=15) as response:
        if response.status != 200:
            raise ValueError("OIDC discovery failed.")
        payload = response.read(1_048_577)
    if len(payload) > 1_048_576:
        raise ValueError("OIDC discovery document is too large.")
    document = json.loads(payload)
    if not isinstance(document, dict):
        raise ValueError("OIDC discovery returned an invalid document.")
    return document
