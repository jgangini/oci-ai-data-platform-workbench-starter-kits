from __future__ import annotations

import base64
import configparser
import hashlib
import json
import os
import stat
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.credential_bootstrap import (
    BootstrapSettings,
    CredentialBootstrapError,
    RUNTIME_KEY_FILE,
    bootstrap_credentials,
    main,
)


USER_OCID = "ocid1.user.oc1..operator"


class NotFound(Exception):
    status = 404


class FakeObjectStorage:
    def __init__(self, content: bytes, *, retain_after_delete: bool = False, delete_missing: bool = False) -> None:
        self.content = content
        self.retain_after_delete = retain_after_delete
        self.delete_missing = delete_missing
        self.deleted = False
        self.get_calls: list[tuple[str, str, str]] = []
        self.delete_calls: list[tuple[str, str, str]] = []

    def get_object(self, namespace: str, bucket: str, object_name: str):
        self.get_calls.append((namespace, bucket, object_name))
        if self.deleted and not self.retain_after_delete:
            raise NotFound()
        return SimpleNamespace(data=SimpleNamespace(content=self.content))

    def delete_object(self, namespace: str, bucket: str, object_name: str) -> None:
        self.delete_calls.append((namespace, bucket, object_name))
        self.deleted = True
        if self.delete_missing:
            raise NotFound()


def _private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _fingerprint(key: rsa.RSAPrivateKey) -> str:
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.md5(public_der, usedforsecurity=False).hexdigest()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))


def _envelope(
    bootstrap_key: rsa.RSAPrivateKey,
    operator_key: rsa.RSAPrivateKey,
    *,
    user: str = USER_OCID,
    fingerprint: str | None = None,
) -> bytes:
    key_text = _pem(operator_key)
    config_text = "\n".join(
        (
            "[DEFAULT]",
            f"user={user}",
            "tenancy=ocid1.tenancy.oc1..tenant",
            f"fingerprint={fingerprint or _fingerprint(operator_key)}",
            "key_file=/tmp/operator.pem",
            "region=us-chicago-1",
            "[EXTRA]",
            "token=must-not-persist",
            "",
        )
    )
    plaintext = json.dumps(
        {
            "config_text": config_text,
            "key_text": key_text,
            "wallet_zip_b64": base64.b64encode(_wallet()).decode(),
            "wallet_password": "WalletPassword123",
            "operator_username": "AIDP_LAB_OPERATOR",
            "operator_password": "OperatorPassword1234567890",
            "dsn": "aidp_low",
        },
        separators=(",", ":"),
    ).encode()
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, None)
    wrapped_key = bootstrap_key.public_key().encrypt(
        data_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return json.dumps(
        {
            "schema_version": 2,
            "wrapped_key_b64": base64.b64encode(wrapped_key).decode(),
            "nonce_b64": base64.b64encode(nonce).decode(),
            "ciphertext_b64": base64.b64encode(ciphertext).decode(),
        },
        separators=(",", ":"),
    ).encode()


def _settings(tmp_path: Path, bootstrap_key: rsa.RSAPrivateKey) -> BootstrapSettings:
    private_key = tmp_path / "bootstrap.pem"
    private_key.write_text(_pem(bootstrap_key), encoding="ascii")
    return BootstrapSettings(
        namespace="namespace",
        bucket="bucket",
        object_name="credentials/envelope.json",
        private_key=private_key,
        expected_user_ocid=USER_OCID,
        config_dir=tmp_path / "oci",
        autonomous_dir=tmp_path / "autonomous",
        region="us-chicago-1",
    )


def _wallet() -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in (
            ("tnsnames.ora", "aidp_low = (DESCRIPTION=(ADDRESS=(PROTOCOL=TCPS)))\n"),
            ("sqlnet.ora", "WALLET_LOCATION=(SOURCE=(METHOD=file))\n"),
        ):
            archive.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), content)
    return stream.getvalue()


def test_bootstrap_installs_exact_operator_profile_and_deletes_object(tmp_path: Path) -> None:
    bootstrap_key = _private_key()
    operator_key = _private_key()
    settings = _settings(tmp_path, bootstrap_key)
    storage = FakeObjectStorage(_envelope(bootstrap_key, operator_key))

    bootstrap_credentials(settings, storage)

    assert (settings.config_dir / "key.pem").read_text(encoding="utf-8") == _pem(operator_key)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(settings.config_dir / "config")
    assert parser["DEFAULT"]["user"] == USER_OCID
    assert parser["DEFAULT"]["key_file"] == RUNTIME_KEY_FILE
    assert parser.sections() == []
    assert storage.get_calls == [
        (settings.namespace, settings.bucket, settings.object_name),
        (settings.namespace, settings.bucket, settings.object_name),
    ]
    assert storage.delete_calls == [(settings.namespace, settings.bucket, settings.object_name)]
    assert (settings.autonomous_dir / "wallet.zip").read_bytes() == _wallet()
    runtime = json.loads((settings.autonomous_dir / "runtime.json").read_text())
    assert runtime["bootstrap_version"] == 2
    assert runtime["operator_username"] == "AIDP_LAB_OPERATOR"
    if os.name != "nt":
        assert stat.S_IMODE(settings.config_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((settings.config_dir / "config").stat().st_mode) == 0o600
        assert stat.S_IMODE((settings.config_dir / "key.pem").stat().st_mode) == 0o600


@pytest.mark.parametrize("bad_field", ["user", "fingerprint"])
def test_bootstrap_rejects_wrong_operator_identity_without_deleting(
    tmp_path: Path,
    bad_field: str,
) -> None:
    bootstrap_key = _private_key()
    operator_key = _private_key()
    settings = _settings(tmp_path, bootstrap_key)
    kwargs = {"user": "ocid1.user.oc1..other"} if bad_field == "user" else {
        "fingerprint": _fingerprint(_private_key())
    }
    storage = FakeObjectStorage(_envelope(bootstrap_key, operator_key, **kwargs))

    with pytest.raises(CredentialBootstrapError):
        bootstrap_credentials(settings, storage)

    assert not settings.config_dir.exists()
    assert storage.delete_calls == []


def test_bootstrap_rejects_tampered_ciphertext_without_writing(tmp_path: Path) -> None:
    bootstrap_key = _private_key()
    settings = _settings(tmp_path, bootstrap_key)
    envelope = json.loads(_envelope(bootstrap_key, _private_key()))
    ciphertext = bytearray(base64.b64decode(envelope["ciphertext_b64"]))
    ciphertext[-1] ^= 1
    envelope["ciphertext_b64"] = base64.b64encode(ciphertext).decode()
    storage = FakeObjectStorage(json.dumps(envelope).encode())

    with pytest.raises(CredentialBootstrapError, match="authentication failed"):
        bootstrap_credentials(settings, storage)

    assert not settings.config_dir.exists()
    assert storage.delete_calls == []


def test_bootstrap_accepts_delete_404_only_after_verifying_absence(tmp_path: Path) -> None:
    bootstrap_key = _private_key()
    settings = _settings(tmp_path, bootstrap_key)
    storage = FakeObjectStorage(_envelope(bootstrap_key, _private_key()), delete_missing=True)

    bootstrap_credentials(settings, storage)

    assert len(storage.get_calls) == 2


def test_missing_object_is_idempotent_when_installed_credentials_are_valid(tmp_path: Path) -> None:
    bootstrap_key = _private_key()
    settings = _settings(tmp_path, bootstrap_key)
    storage = FakeObjectStorage(_envelope(bootstrap_key, _private_key()))
    bootstrap_credentials(settings, storage)
    delete_calls = list(storage.delete_calls)

    bootstrap_credentials(settings, storage)

    assert storage.delete_calls == delete_calls
    assert len(storage.get_calls) == 3


def test_missing_object_still_fails_when_installed_credentials_are_invalid(tmp_path: Path) -> None:
    bootstrap_key = _private_key()
    settings = _settings(tmp_path, bootstrap_key)
    storage = FakeObjectStorage(_envelope(bootstrap_key, _private_key()))
    storage.deleted = True
    settings.config_dir.mkdir()
    (settings.config_dir / "config").write_text(
        "[DEFAULT]\nuser=ocid1.user.oc1..another\nfingerprint=00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00\n"
        f"key_file={RUNTIME_KEY_FILE}\n",
        encoding="utf-8",
    )
    (settings.config_dir / "key.pem").write_text(_pem(_private_key()), encoding="utf-8")

    with pytest.raises(CredentialBootstrapError, match="object was not found"):
        bootstrap_credentials(settings, storage)

    assert storage.delete_calls == []


def test_bootstrap_fails_closed_when_object_remains_after_delete(tmp_path: Path) -> None:
    bootstrap_key = _private_key()
    settings = _settings(tmp_path, bootstrap_key)
    storage = FakeObjectStorage(_envelope(bootstrap_key, _private_key()), retain_after_delete=True)

    with pytest.raises(CredentialBootstrapError, match="still exists"):
        bootstrap_credentials(settings, storage)

    assert (settings.config_dir / "config").is_file()
    assert len(storage.get_calls) == 2


def test_main_uses_instance_principal_and_all_environment_settings(tmp_path: Path, monkeypatch) -> None:
    bootstrap_key = _private_key()
    operator_key = _private_key()
    settings = _settings(tmp_path, bootstrap_key)
    storage = FakeObjectStorage(_envelope(bootstrap_key, operator_key))
    signer = object()
    clients: list[tuple[dict[str, str], object]] = []

    monkeypatch.setenv("OCI_BOOTSTRAP_NAMESPACE", settings.namespace)
    monkeypatch.setenv("OCI_BOOTSTRAP_BUCKET", settings.bucket)
    monkeypatch.setenv("OCI_BOOTSTRAP_OBJECT", settings.object_name)
    monkeypatch.setenv("OCI_BOOTSTRAP_PRIVATE_KEY", str(settings.private_key))
    monkeypatch.setenv("OCI_EXPECTED_USER_OCID", settings.expected_user_ocid)
    monkeypatch.setenv("OCI_CONFIG_DIR", str(settings.config_dir))
    monkeypatch.setenv("AUTONOMOUS_RUNTIME_DIR", str(settings.autonomous_dir))
    monkeypatch.setenv("OCI_REGION", settings.region)
    monkeypatch.setattr(
        "oci.auth.signers.InstancePrincipalsSecurityTokenSigner",
        lambda: signer,
    )
    monkeypatch.setattr(
        "oci.object_storage.ObjectStorageClient",
        lambda config, *, signer: clients.append((config, signer)) or storage,
    )

    assert main() == 0
    assert clients == [({"region": settings.region}, signer)]
    assert (settings.config_dir / "config").is_file()


def test_main_failure_does_not_print_credential_contents(tmp_path: Path, monkeypatch, capsys) -> None:
    bootstrap_key = _private_key()
    settings = _settings(tmp_path, bootstrap_key)
    storage = FakeObjectStorage(
        _envelope(bootstrap_key, _private_key(), user="ocid1.user.oc1..unexpected")
    )

    monkeypatch.setenv("OCI_BOOTSTRAP_NAMESPACE", settings.namespace)
    monkeypatch.setenv("OCI_BOOTSTRAP_BUCKET", settings.bucket)
    monkeypatch.setenv("OCI_BOOTSTRAP_OBJECT", settings.object_name)
    monkeypatch.setenv("OCI_BOOTSTRAP_PRIVATE_KEY", str(settings.private_key))
    monkeypatch.setenv("OCI_EXPECTED_USER_OCID", settings.expected_user_ocid)
    monkeypatch.setenv("OCI_CONFIG_DIR", str(settings.config_dir))
    monkeypatch.setenv("AUTONOMOUS_RUNTIME_DIR", str(settings.autonomous_dir))
    monkeypatch.setenv("OCI_REGION", settings.region)
    monkeypatch.setattr("oci.auth.signers.InstancePrincipalsSecurityTokenSigner", object)
    monkeypatch.setattr(
        "oci.object_storage.ObjectStorageClient",
        lambda _config, *, signer: storage,
    )

    assert main() == 1
    output = capsys.readouterr()
    assert "does not match the expected operator" in output.err
    assert "unexpected" not in output.err
    assert "BEGIN PRIVATE KEY" not in output.err
