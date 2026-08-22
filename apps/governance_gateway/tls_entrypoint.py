from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Callable

import oci
from cryptography import x509
from cryptography.hazmat.primitives import serialization


class TlsBootstrapError(RuntimeError):
    pass


def install_tls_bundle(
    secret_ocid: str,
    target: Path,
    *,
    signer_factory: Callable[[], Any] = oci.auth.signers.get_oke_workload_identity_resource_principal_signer,
    client_factory: Callable[[Any], Any] = lambda signer: oci.secrets.SecretsClient({}, signer=signer),
) -> tuple[Path, Path]:
    if not secret_ocid.startswith(("ocid1.vaultsecret.", "ocid1.secret.")):
        raise TlsBootstrapError("GOVERNANCE_TLS_SECRET_OCID is missing or invalid")
    try:
        bundle = client_factory(signer_factory()).get_secret_bundle(secret_ocid).data
        encoded = bundle.secret_bundle_content.content
        raw = base64.b64decode(encoded, validate=True)
        payload = json.loads(raw)
    except Exception as exc:
        raise TlsBootstrapError("The gateway TLS bundle could not be read from OCI Vault") from exc
    if not isinstance(payload, dict) or set(payload) != {"certificate_pem", "private_key_pem"}:
        raise TlsBootstrapError("The gateway TLS bundle has an invalid shape")
    certificate_pem = payload["certificate_pem"]
    private_key_pem = payload["private_key_pem"]
    if not isinstance(certificate_pem, str) or not isinstance(private_key_pem, str):
        raise TlsBootstrapError("The gateway TLS bundle has invalid values")
    try:
        certificate = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
        private_key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
        certificate_public = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_public = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TlsBootstrapError("The gateway TLS certificate or private key is invalid") from exc
    if certificate_public != key_public:
        raise TlsBootstrapError("The gateway TLS certificate and private key do not match")

    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    certificate_path = target / "tls.crt"
    key_path = target / "tls.key"
    _write_private(certificate_path, certificate_pem.encode("ascii"))
    _write_private(key_path, private_key_pem.encode("ascii"))
    return certificate_path, key_path


def _write_private(path: Path, value: bytes) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(value)
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def main() -> None:
    certificate, key = install_tls_bundle(
        os.environ.get("GOVERNANCE_TLS_SECRET_OCID", ""),
        Path("/tmp/governance-tls"),
    )
    os.execvp(
        "uvicorn",
        [
            "uvicorn",
            "governance_gateway.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8443",
            "--ssl-keyfile",
            str(key),
            "--ssl-certfile",
            str(certificate),
            "--proxy-headers",
            "--forwarded-allow-ips",
            "127.0.0.1",
        ],
    )


if __name__ == "__main__":
    main()
