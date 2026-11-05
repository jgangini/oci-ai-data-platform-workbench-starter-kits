"""Install an operator OCI profile delivered through an encrypted bootstrap object."""

from __future__ import annotations

import base64
import configparser
import hashlib
import hmac
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


RUNTIME_KEY_FILE = "/etc/aidp-lab/oci/key.pem"
_FINGERPRINT = re.compile(r"[0-9a-f]{32}")
BOOTSTRAP_VERSION = 2


class CredentialBootstrapError(RuntimeError):
    """The bootstrap payload could not be installed safely."""


class _BootstrapObjectNotFound(CredentialBootstrapError):
    pass


@dataclass(frozen=True, slots=True)
class BootstrapSettings:
    namespace: str
    bucket: str
    object_name: str
    private_key: Path
    expected_user_ocid: str
    config_dir: Path
    autonomous_dir: Path
    region: str

    @classmethod
    def from_env(cls) -> "BootstrapSettings":
        return cls(
            namespace=_required_env("OCI_BOOTSTRAP_NAMESPACE"),
            bucket=_required_env("OCI_BOOTSTRAP_BUCKET"),
            object_name=_required_env("OCI_BOOTSTRAP_OBJECT"),
            private_key=Path(_required_env("OCI_BOOTSTRAP_PRIVATE_KEY")),
            expected_user_ocid=_required_env("OCI_EXPECTED_USER_OCID"),
            config_dir=Path(_required_env("OCI_CONFIG_DIR")),
            autonomous_dir=Path(_required_env("AUTONOMOUS_RUNTIME_DIR")),
            region=_required_env("OCI_REGION"),
        )


def bootstrap_credentials(settings: BootstrapSettings, client: Any) -> None:
    """Fetch, validate, install, and remove one encrypted credential object."""
    try:
        envelope = _download(client, settings)
    except _BootstrapObjectNotFound:
        if _installed_credentials_valid(settings):
            return
        raise
    payload = _decrypt(envelope, settings.private_key)
    config_text = payload["config_text"]
    key_text = payload["key_text"]
    rendered_config = _validated_config(
        config_text,
        key_text,
        expected_user_ocid=settings.expected_user_ocid,
    )
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.config_dir.chmod(0o700)
    _atomic_write(settings.config_dir / "key.pem", key_text.encode("utf-8"))
    _atomic_write(settings.config_dir / "config", rendered_config.encode("utf-8"))
    _install_autonomous(payload, settings.autonomous_dir)
    _delete_and_verify(client, settings)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise CredentialBootstrapError(f"Required bootstrap setting is missing: {name}")
    return value


def _download(client: Any, settings: BootstrapSettings) -> bytes:
    try:
        response = client.get_object(settings.namespace, settings.bucket, settings.object_name)
    except Exception as exc:
        if _is_not_found(exc):
            raise _BootstrapObjectNotFound("Credential bootstrap object was not found") from exc
        raise CredentialBootstrapError("Credential bootstrap object could not be read") from exc

    data = response.data
    content = data.content if hasattr(data, "content") else data
    if not isinstance(content, bytes):
        raise CredentialBootstrapError("Credential bootstrap object has an invalid body")
    return content


def _decrypt(envelope_bytes: bytes, private_key_path: Path) -> dict[str, str]:
    envelope = _json_object(envelope_bytes, "credential envelope")
    if set(envelope) != {"schema_version", "wrapped_key_b64", "nonce_b64", "ciphertext_b64"}:
        raise CredentialBootstrapError("Credential envelope has an invalid shape")
    if envelope["schema_version"] != BOOTSTRAP_VERSION:
        raise CredentialBootstrapError("Credential envelope schema is not supported")

    wrapped_key = _decode_b64(envelope["wrapped_key_b64"], "wrapped key")
    nonce = _decode_b64(envelope["nonce_b64"], "nonce")
    ciphertext = _decode_b64(envelope["ciphertext_b64"], "ciphertext")
    if len(nonce) != 12:
        raise CredentialBootstrapError("Credential envelope nonce must be 12 bytes")

    try:
        key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise CredentialBootstrapError("Bootstrap private key is unavailable or invalid") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CredentialBootstrapError("Bootstrap private key must be RSA")

    try:
        data_key = key.decrypt(
            wrapped_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        plaintext = AESGCM(data_key).decrypt(nonce, ciphertext, None)
    except (InvalidTag, ValueError, TypeError) as exc:
        raise CredentialBootstrapError("Credential envelope authentication failed") from exc

    payload = _json_object(plaintext, "credential payload")
    expected = {
        "config_text",
        "key_text",
        "wallet_zip_b64",
        "wallet_password",
        "operator_username",
        "operator_password",
        "dsn",
    }
    if set(payload) != expected or not all(
        isinstance(payload[name], str) and payload[name] for name in expected
    ):
        raise CredentialBootstrapError("Credential payload has an invalid shape")
    return payload


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise CredentialBootstrapError(f"Duplicate field in {label}")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=pairs)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CredentialBootstrapError(f"Invalid {label}") from exc
    if not isinstance(value, dict):
        raise CredentialBootstrapError(f"Invalid {label}")
    return value


def _decode_b64(value: Any, label: str) -> bytes:
    if not isinstance(value, str):
        raise CredentialBootstrapError(f"Credential envelope {label} is invalid")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise CredentialBootstrapError(f"Credential envelope {label} is invalid") from exc


def _installed_credentials_valid(settings: BootstrapSettings) -> bool:
    try:
        _validated_config(
            (settings.config_dir / "config").read_text(encoding="utf-8"),
            (settings.config_dir / "key.pem").read_text(encoding="utf-8"),
            expected_user_ocid=settings.expected_user_ocid,
            require_runtime_key_file=True,
        )
        runtime = _json_object(
            (settings.autonomous_dir / "runtime.json").read_bytes(),
            "Autonomous runtime",
        )
        if runtime.get("bootstrap_version") != BOOTSTRAP_VERSION:
            raise CredentialBootstrapError("Autonomous runtime bootstrap version is stale")
        if not (settings.autonomous_dir / "wallet.zip").is_file():
            raise CredentialBootstrapError("Autonomous wallet is missing")
    except (CredentialBootstrapError, OSError, UnicodeError):
        return False
    return True


def _validated_config(
    config_text: str,
    key_text: str,
    *,
    expected_user_ocid: str,
    require_runtime_key_file: bool = False,
) -> str:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(config_text)
    except configparser.Error as exc:
        raise CredentialBootstrapError("OCI config is invalid") from exc

    profile = parser[parser.default_section]
    missing = [name for name in ("tenancy", "user", "fingerprint", "region") if not profile.get(name, "").strip()]
    if missing:
        raise CredentialBootstrapError("OCI config is missing required runtime fields")
    if not hmac.compare_digest(profile.get("user", ""), expected_user_ocid):
        raise CredentialBootstrapError("OCI config user does not match the expected operator")
    if require_runtime_key_file and profile.get("key_file", "") != RUNTIME_KEY_FILE:
        raise CredentialBootstrapError("Installed OCI config does not select the runtime key")

    configured_fingerprint = profile.get("fingerprint", "").replace(":", "").lower()
    if not _FINGERPRINT.fullmatch(configured_fingerprint):
        raise CredentialBootstrapError("OCI config fingerprint is invalid")
    try:
        signing_key = serialization.load_pem_private_key(key_text.encode("utf-8"), password=None)
        if not isinstance(signing_key, rsa.RSAPrivateKey):
            raise TypeError
        public_der = signing_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except (ValueError, TypeError) as exc:
        raise CredentialBootstrapError("OCI API private key is invalid") from exc
    actual_fingerprint = hashlib.md5(public_der, usedforsecurity=False).hexdigest()
    if not hmac.compare_digest(configured_fingerprint, actual_fingerprint):
        raise CredentialBootstrapError("OCI API private key does not match the config fingerprint")

    from io import StringIO

    runtime_parser = configparser.ConfigParser(interpolation=None)
    runtime_parser["DEFAULT"] = {
        name: profile[name]
        for name in ("tenancy", "user", "fingerprint", "region")
    }
    runtime_parser["DEFAULT"]["key_file"] = RUNTIME_KEY_FILE
    rendered = StringIO()
    runtime_parser.write(rendered, space_around_delimiters=False)
    return rendered.getvalue()


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _install_autonomous(payload: dict[str, str], target: Path) -> None:
    wallet = _decode_b64(payload["wallet_zip_b64"], "Autonomous wallet")
    if len(wallet) > 10 * 1024 * 1024:
        raise CredentialBootstrapError("Autonomous wallet exceeds the 10 MiB safety limit")
    try:
        with zipfile.ZipFile(io.BytesIO(wallet)) as archive:
            members = archive.infolist()
            if not members or any(
                item.filename.startswith(("/", "\\"))
                or ".." in Path(item.filename.replace("\\", "/")).parts
                or item.file_size > 5 * 1024 * 1024
                for item in members
            ):
                raise CredentialBootstrapError("Autonomous wallet archive is unsafe")
            extracted = {
                item.filename: archive.read(item)
                for item in members
                if not item.is_dir()
            }
    except zipfile.BadZipFile as exc:
        raise CredentialBootstrapError("Autonomous wallet archive is invalid") from exc
    if "tnsnames.ora" not in extracted:
        raise CredentialBootstrapError("Autonomous wallet has no tnsnames.ora")

    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    _atomic_write(target / "wallet.zip", wallet)
    for name, content in extracted.items():
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.chmod(0o700)
        _atomic_write(destination, content)
    runtime = {
        "schema_version": 1,
        "bootstrap_version": BOOTSTRAP_VERSION,
        "operator_username": payload["operator_username"],
        "operator_password": payload["operator_password"],
        "wallet_password": payload["wallet_password"],
        "dsn": payload["dsn"],
    }
    _atomic_write(
        target / "runtime.json",
        (json.dumps(runtime, sort_keys=True) + "\n").encode("utf-8"),
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _delete_and_verify(client: Any, settings: BootstrapSettings) -> None:
    try:
        client.delete_object(settings.namespace, settings.bucket, settings.object_name)
    except Exception as exc:
        if not _is_not_found(exc):
            raise CredentialBootstrapError("Credential bootstrap object could not be deleted") from exc

    try:
        client.get_object(settings.namespace, settings.bucket, settings.object_name)
    except Exception as exc:
        if _is_not_found(exc):
            return
        raise CredentialBootstrapError("Credential bootstrap object deletion could not be verified") from exc
    raise CredentialBootstrapError("Credential bootstrap object still exists after deletion")


def _is_not_found(exc: Exception) -> bool:
    return getattr(exc, "status", None) == 404


def main() -> int:
    import oci

    try:
        settings = BootstrapSettings.from_env()
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        client = oci.object_storage.ObjectStorageClient({"region": settings.region}, signer=signer)
        bootstrap_credentials(settings, client)
        return 0
    except CredentialBootstrapError as exc:
        print(f"Credential bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
