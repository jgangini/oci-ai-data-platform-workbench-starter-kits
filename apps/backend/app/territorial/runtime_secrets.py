"""Runtime helpers shared by the Spark job and AIDP code agent."""
import base64
import hashlib
import io
import json
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

OCI_CREDENTIALS = ("AidpDataGovernanceExtension", "PrismaWriterRuntime")


def identity_hash(config):
    identity = [config.get(key) for key in ("tenancy", "user", "fingerprint")]
    if any(not isinstance(value, str) or not value for value in identity):
        raise RuntimeError("OCI runtime identity incomplete")
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def shared_credential(credentials):
    """Choose once at deployment; an invalid preferred credential never triggers fallback."""
    for name in OCI_CREDENTIALS:
        matches = [item for item in credentials if item.get("displayName") == name
                   and (item.get("lifecycleState") or item.get("lifeCycleState") or item.get("state")) != "DELETED"]
        if not matches:
            continue
        if len(matches) != 1:
            raise RuntimeError("Duplicate shared OCI credentials")
        item = matches[0]
        if (item.get("type") or item.get("credentialType")) != "SECRET_TOKEN":
            raise RuntimeError("Shared OCI credential has an incompatible type")
        if (item.get("lifecycleState") or item.get("lifeCycleState") or item.get("state")) != "ACTIVE" or not (item.get("key") or item.get("id")):
            raise RuntimeError("Shared OCI credential is not ready")
        return item
    return None


def values(secret_get, name, keys):
    result = {key: secret_get(name=name, key=key) for key in keys}
    if any(not isinstance(value, str) or not value for value in result.values()):
        raise RuntimeError("Territorial runtime credential incomplete")
    return result


@contextmanager
def database_connection(secret_get, credential):
    import oracledb
    config = values(secret_get, credential, ("db_user", "db_password", "dsn", "wallet", "wallet_password"))
    with tempfile.TemporaryDirectory(prefix="territorial-wallet-") as directory:
        target = Path(directory).resolve()
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(config["wallet"], validate=True))) as archive:
            if sum(item.file_size for item in archive.infolist()) > 2_000_000:
                raise ValueError("Wallet exceeds size limit")
            for item in archive.infolist():
                if not (target / item.filename).resolve().is_relative_to(target):
                    raise ValueError("Unsafe wallet path")
            archive.extractall(target)
        with oracledb.connect(user=config["db_user"], password=config["db_password"], dsn=config["dsn"],
                             config_dir=directory, wallet_location=directory, wallet_password=config["wallet_password"]) as connection:
            yield connection


def signer(secret_get, credential_name="PrismaWriterRuntime", expected_identity=""):
    config = _oci_values(secret_get, credential_name, expected_identity)
    return _signer(config)


def _oci_values(secret_get, credential_name, expected_identity):
    if credential_name not in OCI_CREDENTIALS:
        raise RuntimeError("Unsupported OCI runtime credential")
    config = values(secret_get, credential_name, ("tenancy", "user", "fingerprint"))
    if expected_identity and identity_hash(config) != expected_identity:
        raise RuntimeError("OCI runtime credential identity mismatch")
    config.update(values(secret_get, credential_name, ("private_key",)))
    return config


def _signer(config):
    import oci
    return oci.signer.Signer(tenancy=config["tenancy"], user=config["user"], fingerprint=config["fingerprint"],
                            private_key_file_location=None, private_key_content=config["private_key"])


def runtime_auth(secret_get, region, credential_name="PrismaWriterRuntime", expected_identity=""):
    """Ordinary OCI Signer clients still validate a complete SDK config; keep it only in memory."""
    credential = _oci_values(secret_get, credential_name, expected_identity)
    config = {key: credential[key] for key in ("tenancy", "user", "fingerprint")}
    config.update(region=region, key_content=credential["private_key"])
    return config, _signer(credential)
