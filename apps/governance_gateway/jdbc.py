from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


_PROFILE = "AIDP_GATEWAY"
MAX_JDBC_DRIVER_BYTES = 128 * 1024 * 1024
_JDBC_DRIVER_BUCKET = "oci_artifact"
_JDBC_DRIVER_OBJECT = "data_governance/runtime/aidp-jdbc-driver.zip"
_MAX_EXPANDED_BYTES = 512 * 1024 * 1024
_PARAMETER = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")
_DEFAULT_MAX_ROWS = 1_000
_DEFAULT_MAX_RESULT_BYTES = 5 * 1024 * 1024


class JdbcConfigurationError(RuntimeError):
    pass


class JdbcDriverValidationError(JdbcConfigurationError):
    pass


class AidpJdbcRuntime:
    """Materialize the licensed AIDP driver and a dedicated API-key profile at runtime."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._env = environ if environ is not None else os.environ
        self._lock = RLock()
        self._connection: Callable[[], Any] | None = None

    def initialize(self) -> None:
        self.connect().close()

    def connect(self) -> Any:
        with self._lock:
            if self._connection is None:
                self._connection = self._prepare()
            factory = self._connection
        return factory()

    def execute(self, statement: str, parameters: dict[str, Any]) -> tuple[list[str], list[tuple[Any, ...]]]:
        sql, values = bind_named_parameters(statement, parameters)
        connection = self.connect()
        cursor = connection.cursor()
        try:
            cursor.execute(sql, values)
            columns = [str(item[0]) for item in (cursor.description or ())]
            limit = _result_limit(self._env.get("GOVERNANCE_MAX_RESULT_ROWS", str(_DEFAULT_MAX_ROWS)))
            rows = [tuple(row) for row in cursor.fetchmany(limit + 1)]
            if len(rows) > limit:
                raise JdbcConfigurationError(f"The governed result exceeded the {limit}-row limit.")
            byte_limit = _result_byte_limit(
                self._env.get("GOVERNANCE_MAX_RESULT_BYTES", str(_DEFAULT_MAX_RESULT_BYTES))
            )
            if len(json.dumps(rows, default=str, ensure_ascii=False).encode("utf-8")) > byte_limit:
                raise JdbcConfigurationError("The governed result exceeded the configured response size limit.")
            return columns, rows
        finally:
            cursor.close()
            connection.close()

    def driver_installed(self) -> bool:
        oci, client, namespace, bucket, object_name = self._object_storage()
        try:
            response = client.head_object(namespace, bucket, object_name)
        except oci.exceptions.ServiceError as exc:
            if exc.status == 404:
                return False
            raise JdbcConfigurationError(f"JDBC driver status failed with OCI {exc.status}.") from exc
        size = int((getattr(response, "headers", None) or {}).get("content-length", 0))
        return 1 <= size <= MAX_JDBC_DRIVER_BYTES

    def create_driver_upload(self, size_bytes: int, sha256: str) -> dict[str, Any]:
        _validate_upload_contract(size_bytes, sha256)
        oci, client, namespace, bucket, object_name = self._object_storage()
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        details = oci.object_storage.models.CreatePreauthenticatedRequestDetails(
            name=f"aidp-jdbc-driver-{size_bytes}-{sha256}-{uuid.uuid4().hex}",
            access_type="ObjectWrite",
            object_name=object_name,
            time_expires=expires,
        )
        try:
            response = client.create_preauthenticated_request(namespace, bucket, details)
        except oci.exceptions.ServiceError as exc:
            raise JdbcConfigurationError(f"JDBC driver PAR creation failed with OCI {exc.status}.") from exc
        access_uri = str(getattr(response.data, "access_uri", "") or "")
        upload_id = str(getattr(response.data, "id", "") or "")
        if not access_uri.startswith("/") or re.search(r"[\x00-\x20]", access_uri):
            raise JdbcConfigurationError("Object Storage returned an invalid JDBC upload URL.")
        if not re.fullmatch(r"[A-Za-z0-9._~-]{1,512}", upload_id):
            raise JdbcConfigurationError("Object Storage returned an invalid JDBC upload identifier.")
        region = self._env["GOVERNANCE_OCI_REGION"].strip()
        return {
            "upload_id": upload_id,
            "upload_url": f"https://objectstorage.{region}.oraclecloud.com{access_uri}",
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
        }

    def complete_driver_upload(self, upload_id: str, size_bytes: int, sha256: str) -> None:
        _validate_upload_contract(size_bytes, sha256)
        oci, client, namespace, bucket, object_name = self._object_storage()
        _revoke_driver_upload(
            oci, client, namespace, bucket, object_name, upload_id, size_bytes, sha256
        )
        temporary: Path | None = None
        try:
            try:
                response = client.get_object(namespace, bucket, object_name)
            except oci.exceptions.ServiceError as exc:
                if exc.status == 404:
                    raise JdbcDriverValidationError("The JDBC driver upload was not found.") from exc
                raise JdbcConfigurationError(f"JDBC driver verification failed with OCI {exc.status}.") from exc
            declared = int((getattr(response, "headers", None) or {}).get("content-length", 0))
            if declared != size_bytes or not 1 <= declared <= MAX_JDBC_DRIVER_BYTES:
                raise JdbcDriverValidationError("The uploaded JDBC driver size does not match the request.")
            digest = hashlib.sha256()
            with tempfile.NamedTemporaryFile(prefix="jdbc-verify-", suffix=".zip", delete=False) as handle:
                temporary = Path(handle.name)
                remaining = MAX_JDBC_DRIVER_BYTES + 1
                while remaining:
                    chunk = response.data.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            if temporary.stat().st_size != size_bytes or digest.hexdigest() != sha256:
                raise JdbcDriverValidationError("The uploaded JDBC driver digest does not match the request.")
            _validate_driver_archive(temporary)
        except JdbcDriverValidationError:
            try:
                client.delete_object(namespace, bucket, object_name)
            except oci.exceptions.ServiceError as cleanup:
                raise JdbcConfigurationError(
                    f"The invalid JDBC driver could not be removed (OCI {cleanup.status})."
                ) from cleanup
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        with self._lock:
            self._connection = None

    def _object_storage(self) -> tuple[Any, Any, str, str, str]:
        required = {
            name: self._env.get(name, "").strip()
            for name in (
                "GOVERNANCE_OCI_REGION",
                "GOVERNANCE_OBJECT_STORAGE_NAMESPACE",
                "GOVERNANCE_JDBC_DRIVER_BUCKET",
                "GOVERNANCE_JDBC_DRIVER_OBJECT",
            )
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise JdbcConfigurationError(f"Missing governance JDBC setting(s): {', '.join(missing)}")
        _validate_driver_target(
            required["GOVERNANCE_JDBC_DRIVER_BUCKET"],
            required["GOVERNANCE_JDBC_DRIVER_OBJECT"],
        )
        import oci

        signer = oci.auth.signers.get_oke_workload_identity_resource_principal_signer()
        client = oci.object_storage.ObjectStorageClient(
            {},
            signer=signer,
            service_endpoint=f"https://objectstorage.{required['GOVERNANCE_OCI_REGION']}.oraclecloud.com",
            retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
        )
        return (
            oci,
            client,
            required["GOVERNANCE_OBJECT_STORAGE_NAMESPACE"],
            required["GOVERNANCE_JDBC_DRIVER_BUCKET"],
            required["GOVERNANCE_JDBC_DRIVER_OBJECT"],
        )

    def _prepare(self) -> Callable[[], Any]:
        required = {
            name: self._env.get(name, "").strip()
            for name in (
                "GOVERNANCE_OCI_REGION",
                "GOVERNANCE_JDBC_SECRET_OCID",
                "GOVERNANCE_JDBC_USER_OCID",
                "GOVERNANCE_JDBC_DRIVER_OBJECT",
                "GOVERNANCE_OBJECT_STORAGE_NAMESPACE",
                "GOVERNANCE_JDBC_DRIVER_BUCKET",
            )
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise JdbcConfigurationError(f"Missing governance JDBC setting(s): {', '.join(missing)}")
        _validate_driver_target(
            required["GOVERNANCE_JDBC_DRIVER_BUCKET"], required["GOVERNANCE_JDBC_DRIVER_OBJECT"]
        )

        import oci

        signer = oci.auth.signers.get_oke_workload_identity_resource_principal_signer()
        secret = _read_secret(oci, signer, required["GOVERNANCE_JDBC_SECRET_OCID"], required["GOVERNANCE_OCI_REGION"])
        directory = Path(tempfile.mkdtemp(prefix="governance-jdbc-", dir="/tmp"))
        if os.name == "posix":
            directory.chmod(stat.S_IRWXU)
        jdbc_url = _write_oci_profile(secret, directory)
        if secret["user_ocid"] != required["GOVERNANCE_JDBC_USER_OCID"]:
            raise JdbcConfigurationError("The JDBC Vault secret does not belong to the configured technical user.")
        jars = _download_driver(
            oci,
            signer,
            required["GOVERNANCE_OCI_REGION"],
            required["GOVERNANCE_OBJECT_STORAGE_NAMESPACE"],
            required["GOVERNANCE_JDBC_DRIVER_BUCKET"],
            required["GOVERNANCE_JDBC_DRIVER_OBJECT"],
            directory,
        )
        if secret["region"] != required["GOVERNANCE_OCI_REGION"]:
            raise JdbcConfigurationError("The JDBC credential and OKE workload must use the same OCI region.")
        driver_class = str(secret.get("driver_class") or "com.simba.spark.jdbc.Driver")
        if driver_class != "com.simba.spark.jdbc.Driver":
            raise JdbcConfigurationError("Only the Oracle-provided Simba Spark JDBC driver is allowed.")

        import jaydebeapi

        return lambda: jaydebeapi.connect(driver_class, jdbc_url, jars=[str(path) for path in jars])


def _read_secret(oci: Any, signer: Any, secret_id: str, region: str) -> dict[str, Any]:
    response = oci.secrets.SecretsClient({}, signer=signer, service_endpoint=f"https://secrets.vaults.{region}.oci.oraclecloud.com").get_secret_bundle(secret_id)
    encoded = response.data.secret_bundle_content.content
    try:
        decoded = base64.b64decode(encoded, validate=True)
        if len(decoded) > 64 * 1024:
            raise ValueError("secret too large")
        value = json.loads(decoded)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise JdbcConfigurationError("The JDBC Vault secret is not valid base64 JSON.") from exc
    if not isinstance(value, dict):
        raise JdbcConfigurationError("The JDBC Vault secret must contain a JSON object.")
    return value


def _write_oci_profile(secret: dict[str, Any], directory: Path) -> str:
    _require_profile_strings(secret)
    _validate_profile_identity(secret)
    url = _validated_jdbc_url(str(secret["jdbc_url"]))
    _validate_private_key(str(secret["private_key_pem"]))

    key_file = directory / "api_key.pem"
    config_file = directory / "config"
    key_file.write_text(str(secret["private_key_pem"]), encoding="utf-8")
    config_file.write_text(
        "\n".join((
            f"[{_PROFILE}]",
            f"tenancy={secret['tenancy_ocid']}",
            f"user={secret['user_ocid']}",
            f"fingerprint={secret['fingerprint']}",
            f"region={secret['region']}",
            f"key_file={key_file}",
            "",
        )),
        encoding="utf-8",
    )
    if os.name == "posix":
        key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
        config_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return f"{url};OCIConfigFile={config_file};OCIProfile={_PROFILE}"


def _require_profile_strings(secret: dict[str, Any]) -> None:
    required = ("jdbc_url", "tenancy_ocid", "user_ocid", "fingerprint", "region", "private_key_pem", "purpose")
    missing = [name for name in required if not isinstance(secret.get(name), str) or not str(secret[name]).strip()]
    if missing:
        raise JdbcConfigurationError(f"The JDBC Vault secret is missing: {', '.join(missing)}")


def _validate_profile_identity(secret: dict[str, Any]) -> None:
    if secret["purpose"] != "AIDP_GOVERNANCE_GATEWAY":
        raise JdbcConfigurationError("The Vault secret is not dedicated to the governance gateway.")
    if not str(secret["tenancy_ocid"]).startswith("ocid1.tenancy.") or not str(secret["user_ocid"]).startswith("ocid1.user."):
        raise JdbcConfigurationError("The JDBC Vault secret contains invalid OCI principals.")
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){15}[0-9a-fA-F]{2}", str(secret["fingerprint"])):
        raise JdbcConfigurationError("The JDBC Vault secret contains an invalid API-key fingerprint.")
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", str(secret["region"])):
        raise JdbcConfigurationError("The JDBC Vault secret contains an invalid OCI region.")


def _validated_jdbc_url(value: str) -> str:
    url = value.strip().rstrip(";")
    lowered = url.casefold()
    if not url.startswith("jdbc:spark://") or ";sparkservertype=aidp" not in lowered or ";httppath=cliservice/" not in lowered:
        raise JdbcConfigurationError("The JDBC URL is not an Oracle AI Data Platform compute endpoint.")
    if any(marker in lowered for marker in ("ociprofile=", "ociconfigfile=", "authmech=", "token=")):
        raise JdbcConfigurationError("Authentication options must not be embedded in the JDBC URL.")
    return url


def _validate_private_key(value: str) -> None:
    try:
        key = serialization.load_pem_private_key(value.encode("utf-8"), password=None)
    except (TypeError, ValueError) as exc:
        raise JdbcConfigurationError("The JDBC Vault secret does not contain a valid unencrypted private key.") from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise JdbcConfigurationError("The JDBC API key must be RSA with at least 2048 bits.")


def _download_driver(
    oci: Any,
    signer: Any,
    region: str,
    namespace: str,
    bucket: str,
    object_name: str,
    directory: Path,
) -> tuple[Path, ...]:
    client = oci.object_storage.ObjectStorageClient(
        {}, signer=signer, service_endpoint=f"https://objectstorage.{region}.oraclecloud.com"
    )
    response = client.get_object(namespace, bucket, object_name)
    content_length = int(response.headers.get("content-length", 0))
    if content_length <= 0 or content_length > MAX_JDBC_DRIVER_BYTES:
        raise JdbcConfigurationError("The JDBC driver object size is missing or outside the allowed limit.")
    archive = directory / "driver-download"
    with archive.open("wb") as handle:
        remaining = MAX_JDBC_DRIVER_BYTES + 1
        while remaining:
            chunk = response.data.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            handle.write(chunk)
            remaining -= len(chunk)
    if archive.stat().st_size != content_length or archive.stat().st_size > MAX_JDBC_DRIVER_BYTES:
        raise JdbcConfigurationError("The JDBC driver object did not match its declared size.")

    if object_name.casefold().endswith(".jar"):
        jar = directory / "sparkJDBC42.jar"
        archive.replace(jar)
        return (jar,)
    if not zipfile.is_zipfile(archive):
        raise JdbcConfigurationError("The JDBC driver object must be a JAR or ZIP bundle.")
    jars = _extract_driver_archive(archive, directory / "driver", depth=0)
    if not jars:
        raise JdbcConfigurationError("The JDBC driver bundle contained no JAR files.")
    return tuple(sorted(jars))


def _validate_upload_contract(size: int, sha256: str) -> None:
    if not 1 <= size <= MAX_JDBC_DRIVER_BYTES or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise JdbcDriverValidationError("The JDBC driver upload metadata is invalid.")


def _revoke_driver_upload(
    oci: Any,
    client: Any,
    namespace: str,
    bucket: str,
    object_name: str,
    upload_id: str,
    size_bytes: int,
    sha256: str,
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._~-]{1,512}", upload_id):
        raise JdbcDriverValidationError("The JDBC driver upload identifier is invalid.")
    try:
        response = client.get_preauthenticated_request(namespace, bucket, upload_id)
    except oci.exceptions.ServiceError as exc:
        if exc.status == 404:
            raise JdbcDriverValidationError("The JDBC driver upload has expired or does not exist.") from exc
        raise JdbcConfigurationError(f"JDBC driver PAR lookup failed with OCI {exc.status}.") from exc
    details = response.data
    name = str(getattr(details, "name", "") or "")
    managed = (
        name.startswith("aidp-jdbc-driver-")
        and str(getattr(details, "access_type", "") or "") == "ObjectWrite"
        and str(getattr(details, "object_name", "") or "") == object_name
    )
    if not managed:
        raise JdbcDriverValidationError("The JDBC driver upload identifier is not managed by this gateway.")
    try:
        # ponytail: revoke before reading the canonical object so a completed upload cannot be overwritten.
        client.delete_preauthenticated_request(namespace, bucket, upload_id)
    except oci.exceptions.ServiceError as exc:
        raise JdbcConfigurationError(f"JDBC driver PAR revocation failed with OCI {exc.status}.") from exc
    expected = re.escape(f"aidp-jdbc-driver-{size_bytes}-{sha256}-") + r"[0-9a-f]{32}"
    if not re.fullmatch(expected, name):
        raise JdbcDriverValidationError("The JDBC driver upload does not match the original request.")


def _validate_driver_target(bucket: str, object_name: str) -> None:
    if bucket != _JDBC_DRIVER_BUCKET or object_name != _JDBC_DRIVER_OBJECT:
        raise JdbcConfigurationError(
            "The governance JDBC target must be "
            f"{_JDBC_DRIVER_BUCKET}/{_JDBC_DRIVER_OBJECT}."
        )


def _validate_driver_archive(archive: Path) -> None:
    if archive.stat().st_size <= 0 or archive.stat().st_size > MAX_JDBC_DRIVER_BYTES:
        raise JdbcDriverValidationError("The JDBC driver ZIP size is invalid.")
    if not zipfile.is_zipfile(archive):
        raise JdbcDriverValidationError("The uploaded JDBC driver is not a ZIP archive.")
    try:
        with tempfile.TemporaryDirectory(prefix="jdbc-validation-") as directory:
            jars = _extract_driver_archive(archive, Path(directory), depth=0)
    except (OSError, zipfile.BadZipFile, JdbcConfigurationError) as exc:
        raise JdbcDriverValidationError("The uploaded JDBC driver ZIP is invalid.") from exc
    if not jars:
        raise JdbcDriverValidationError("The uploaded JDBC driver ZIP contains no JAR files.")


def _extract_driver_archive(archive: Path, destination: Path, depth: int) -> list[Path]:
    if depth > 2:
        raise JdbcConfigurationError("The JDBC driver archive is nested too deeply.")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    jars: list[Path] = []
    expanded = 0
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise JdbcConfigurationError("The JDBC driver archive contains an unsafe path.")
            expanded += member.file_size
            if expanded > _MAX_EXPANDED_BYTES:
                raise JdbcConfigurationError("The JDBC driver archive exceeds the expanded-size limit.")
            if member.is_dir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            suffix = target.suffix.casefold()
            if suffix == ".jar":
                jars.append(target)
            elif suffix == ".zip":
                jars.extend(_extract_driver_archive(target, target.with_suffix(""), depth + 1))
    return jars


def bind_named_parameters(statement: str, parameters: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    names = _PARAMETER.findall(statement)
    expected = set(names)
    supplied = set(parameters)
    if expected != supplied:
        missing = sorted(expected - supplied)
        unexpected = sorted(supplied - expected)
        detail = "; ".join(filter(None, (
            f"missing: {', '.join(missing)}" if missing else "",
            f"unexpected: {', '.join(unexpected)}" if unexpected else "",
        )))
        raise ValueError(f"Registered query parameters do not match ({detail}).")
    values: list[Any] = []

    def replace(match: re.Match[str]) -> str:
        value = parameters[match.group(1)]
        if value is not None and (isinstance(value, (dict, list, tuple, set, bytes)) or not isinstance(value, (str, int, float, bool))):
            raise TypeError(f"Unsupported value for parameter {match.group(1)}.")
        values.append(value)
        return "?"

    return _PARAMETER.sub(replace, statement), tuple(values)


def _result_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise JdbcConfigurationError("GOVERNANCE_MAX_RESULT_ROWS must be an integer.") from exc
    if not 1 <= limit <= 10_000:
        raise JdbcConfigurationError("GOVERNANCE_MAX_RESULT_ROWS must be between 1 and 10000.")
    return limit


def _result_byte_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise JdbcConfigurationError("GOVERNANCE_MAX_RESULT_BYTES must be an integer.") from exc
    if not 1_024 <= limit <= 50 * 1024 * 1024:
        raise JdbcConfigurationError("GOVERNANCE_MAX_RESULT_BYTES must be between 1024 and 52428800.")
    return limit
