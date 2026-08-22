from typing import Any

import pytest

from governance_gateway.auth import AuthenticationError, OidcAuthenticator, OidcSettings


def test_oidc_discovery_separates_identity_domain_authority_from_token_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "governance_gateway.auth._discovery_document",
        lambda *_args, **_kwargs: {
            "issuer": "https://identity.oraclecloud.com/",
            "jwks_uri": "https://idcs-example.identity.oraclecloud.com/admin/v1/SigningCert/jwk",
        },
    )

    class KeyClient:
        def __init__(self, url: str, **_kwargs: Any) -> None:
            self.url = url

    monkeypatch.setattr("governance_gateway.auth.jwt.PyJWKClient", KeyClient)
    authenticator = OidcAuthenticator(OidcSettings(
        "https://identity.oraclecloud.com", "governance-api",
        "https://idcs-example.identity.oraclecloud.com",
    ))
    client = authenticator._key_client()
    assert client.url.endswith("/admin/v1/SigningCert/jwk")  # type: ignore[attr-defined]


def test_oidc_discovery_rejects_cross_origin_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "governance_gateway.auth._discovery_document",
        lambda *_args, **_kwargs: {
            "issuer": "https://identity.oraclecloud.com",
            "jwks_uri": "https://attacker.example/keys",
        },
    )
    authenticator = OidcAuthenticator(OidcSettings(
        "https://identity.oraclecloud.com", "governance-api",
        "https://idcs-example.identity.oraclecloud.com",
    ))
    with pytest.raises(ValueError, match="unsafe JWKS"):
        authenticator._key_client()


def test_oidc_authentication_never_falls_back_without_a_token() -> None:
    authenticator = OidcAuthenticator(OidcSettings(
        "https://identity.oraclecloud.com", "governance-api",
        "https://idcs-example.identity.oraclecloud.com",
    ))
    with pytest.raises(AuthenticationError, match="bearer token"):
        authenticator.authenticate(None)
