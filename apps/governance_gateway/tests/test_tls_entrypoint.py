import base64
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from governance_gateway.tls_entrypoint import TlsBootstrapError, install_tls_bundle


def _bundle(*, mismatch: bool = False) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certificate_key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if mismatch else key
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "governance.internal")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(certificate_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=30))
        .sign(certificate_key, hashes.SHA256())
    )
    payload = {
        "certificate_pem": certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        "private_key_pem": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii"),
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _client(encoded: str):
    content = SimpleNamespace(content=encoded)
    bundle = SimpleNamespace(secret_bundle_content=content)
    return SimpleNamespace(get_secret_bundle=lambda _ocid: SimpleNamespace(data=bundle))


def test_tls_bundle_is_validated_and_written_privately(tmp_path) -> None:
    certificate, key = install_tls_bundle(
        "ocid1.vaultsecret.oc1..tls",
        tmp_path / "tls",
        signer_factory=lambda: object(),
        client_factory=lambda _signer: _client(_bundle()),
    )

    assert certificate.read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert key.read_text().startswith("-----BEGIN PRIVATE KEY-----")
    if os.name != "nt":
        assert certificate.stat().st_mode & 0o777 == 0o600
        assert key.stat().st_mode & 0o777 == 0o600


def test_tls_bundle_rejects_a_mismatched_key(tmp_path) -> None:
    with pytest.raises(TlsBootstrapError, match="do not match"):
        install_tls_bundle(
            "ocid1.vaultsecret.oc1..tls",
            tmp_path / "tls",
            signer_factory=lambda: object(),
            client_factory=lambda _signer: _client(_bundle(mismatch=True)),
        )
