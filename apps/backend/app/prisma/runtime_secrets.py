"""Runtime helpers shared by the Spark job and AIDP code agent."""
import base64
import io
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path


def values(secret_get, name, keys):
    result = {key: secret_get(name=name, key=key) for key in keys}
    if any(not isinstance(value, str) or not value for value in result.values()):
        raise RuntimeError("PRISMA runtime credential incomplete")
    return result


@contextmanager
def database_connection(secret_get, credential):
    import oracledb
    config = values(secret_get, credential, ("db_user", "db_password", "dsn", "wallet", "wallet_password"))
    with tempfile.TemporaryDirectory(prefix="prisma-wallet-") as directory:
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


def signer(secret_get):
    config = values(secret_get, "PrismaWriterRuntime", ("tenancy", "user", "fingerprint", "private_key"))
    return _signer(config)


def _signer(config):
    import oci
    return oci.signer.Signer(tenancy=config["tenancy"], user=config["user"], fingerprint=config["fingerprint"],
                            private_key_file_location=None, private_key_content=config["private_key"])


def runtime_auth(secret_get, region):
    """Ordinary OCI Signer clients still validate a complete SDK config; keep it only in memory."""
    credential = values(secret_get, "PrismaWriterRuntime", ("tenancy", "user", "fingerprint", "private_key"))
    config = {key: credential[key] for key in ("tenancy", "user", "fingerprint")}
    config.update(region=region, key_content=credential["private_key"])
    return config, _signer(credential)
