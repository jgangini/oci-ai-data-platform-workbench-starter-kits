import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from governance_gateway.jdbc import (
    AidpJdbcRuntime, JdbcConfigurationError, JdbcDriverValidationError,
    _validate_driver_archive, _validate_driver_target, _write_oci_profile, bind_named_parameters,
)


def _private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def test_jdbc_profile_uses_only_a_dedicated_vault_credential(tmp_path: Path) -> None:
    url = _write_oci_profile(
        {
            "jdbc_url": "jdbc:spark://gateway.aidp.us-chicago-1.oci.oraclecloud.com/default;SparkServerType=AIDP;httpPath=cliservice/cluster",
            "tenancy_ocid": "ocid1.tenancy.oc1..example",
            "user_ocid": "ocid1.user.oc1..technical",
            "fingerprint": "11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
            "region": "us-chicago-1",
            "private_key_pem": _private_key(),
            "purpose": "AIDP_GOVERNANCE_GATEWAY",
        },
        tmp_path,
    )
    assert f"OCIConfigFile={tmp_path / 'config'}" in url
    assert "OCIProfile=AIDP_GATEWAY" in url
    assert "private_key_pem" not in (tmp_path / "config").read_text(encoding="utf-8")


def test_jdbc_profile_rejects_a_general_purpose_credential(tmp_path: Path) -> None:
    with pytest.raises(JdbcConfigurationError, match="not dedicated"):
        _write_oci_profile(
            {
                "jdbc_url": "jdbc:spark://gateway.aidp.us-chicago-1.oci.oraclecloud.com/default;SparkServerType=AIDP;httpPath=cliservice/cluster",
                "tenancy_ocid": "ocid1.tenancy.oc1..example",
                "user_ocid": "ocid1.user.oc1..person",
                "fingerprint": "11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
                "region": "us-chicago-1",
                "private_key_pem": _private_key(),
                "purpose": "DEPLOYMENT_OPERATOR",
            },
            tmp_path,
        )


def test_named_parameters_are_bound_without_sql_interpolation() -> None:
    sql, values = bind_named_parameters(
        "SELECT * FROM sales WHERE region = :region AND amount >= :minimum",
        {"region": "x' OR 1=1 --", "minimum": 10},
    )
    assert sql == "SELECT * FROM sales WHERE region = ? AND amount >= ?"
    assert values == ("x' OR 1=1 --", 10)


def test_named_parameters_fail_closed_on_contract_mismatch() -> None:
    with pytest.raises(ValueError, match="unexpected"):
        bind_named_parameters("SELECT 1", {"extra": "value"})


def test_jdbc_result_limit_is_enforced_before_results_leave_the_runtime() -> None:
    class Cursor:
        description = (("value",),)

        def execute(self, _statement: str, _values: tuple[object, ...]) -> None:
            return None

        def fetchmany(self, count: int) -> list[tuple[int]]:
            assert count == 3
            return [(1,), (2,), (3,)]

        def close(self) -> None:
            return None

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            return None

    runtime = AidpJdbcRuntime({"GOVERNANCE_MAX_RESULT_ROWS": "2"})
    runtime._connection = Connection
    with pytest.raises(JdbcConfigurationError, match="2-row limit"):
        runtime.execute("SELECT 1", {})


def test_jdbc_result_byte_limit_is_enforced_before_results_leave_the_runtime() -> None:
    class Cursor:
        description = (("value",),)

        def execute(self, _statement: str, _parameters: tuple[object, ...]) -> None:
            return None

        def fetchmany(self, _count: int) -> list[tuple[str]]:
            return [("x" * 2_000,)]

        def close(self) -> None:
            return None

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            return None

    runtime = AidpJdbcRuntime({"GOVERNANCE_MAX_RESULT_BYTES": "1024"})
    runtime._connection = Connection  # type: ignore[assignment]
    with pytest.raises(JdbcConfigurationError, match="response size limit"):
        runtime.execute("SELECT 1", {})


def test_driver_completion_rejects_non_zip_content(tmp_path: Path) -> None:
    archive = tmp_path / "driver.zip"
    archive.write_bytes(b"not-a-zip")
    with pytest.raises(JdbcDriverValidationError, match="not a ZIP"):
        _validate_driver_archive(archive)


def test_driver_archive_requires_and_accepts_a_jar(tmp_path: Path) -> None:
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("README.txt", "no driver")
    with pytest.raises(JdbcDriverValidationError, match="contains no JAR"):
        _validate_driver_archive(empty)
    driver = tmp_path / "driver.zip"
    with zipfile.ZipFile(driver, "w") as archive:
        archive.writestr("lib/aidp-jdbc.jar", b"test-jar")
    _validate_driver_archive(driver)


def test_driver_target_is_fixed_to_the_governance_artifact() -> None:
    _validate_driver_target("oci_artifact", "data_governance/runtime/aidp-jdbc-driver.zip")
    with pytest.raises(JdbcConfigurationError, match="must be oci_artifact"):
        _validate_driver_target("other", "data_governance/runtime/aidp-jdbc-driver.zip")
    with pytest.raises(JdbcConfigurationError, match="must be oci_artifact"):
        _validate_driver_target("oci_artifact", "other.zip")


class FakeServiceError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


class FakeDriverClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.details: object | None = None
        self.events: list[str] = []

    def create_preauthenticated_request(
        self, _namespace: str, _bucket: str, details: object,
    ) -> SimpleNamespace:
        self.details = details
        return SimpleNamespace(data=SimpleNamespace(
            id="opaque-upload-id", access_uri="/p/opaque-secret",
        ))

    def get_preauthenticated_request(
        self, _namespace: str, _bucket: str, upload_id: str,
    ) -> SimpleNamespace:
        assert upload_id == "opaque-upload-id"
        self.events.append("inspect-par")
        return SimpleNamespace(data=self.details)

    def delete_preauthenticated_request(
        self, _namespace: str, _bucket: str, upload_id: str,
    ) -> None:
        assert upload_id == "opaque-upload-id"
        self.events.append("revoke-par")

    def get_object(self, _namespace: str, _bucket: str, _object_name: str) -> SimpleNamespace:
        self.events.append("read-object")
        return SimpleNamespace(
            headers={"content-length": str(len(self.payload))}, data=BytesIO(self.payload),
        )

    def delete_object(self, _namespace: str, _bucket: str, _object_name: str) -> None:
        self.events.append("delete-object")


def driver_runtime(payload: bytes) -> tuple[AidpJdbcRuntime, FakeDriverClient]:
    models = SimpleNamespace(
        CreatePreauthenticatedRequestDetails=lambda **values: SimpleNamespace(**values),
    )
    oci = SimpleNamespace(
        object_storage=SimpleNamespace(models=models),
        exceptions=SimpleNamespace(ServiceError=FakeServiceError),
    )
    client = FakeDriverClient(payload)
    runtime = AidpJdbcRuntime({"GOVERNANCE_OCI_REGION": "us-chicago-1"})
    runtime._object_storage = lambda: (  # type: ignore[method-assign]
        oci, client, "namespace", "oci_artifact", "data_governance/runtime/aidp-jdbc-driver.zip",
    )
    return runtime, client


def test_driver_par_is_bound_to_the_request_and_revoked_before_validation() -> None:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("lib/aidp-jdbc.jar", b"test-jar")
    payload = output.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    runtime, client = driver_runtime(payload)
    upload = runtime.create_driver_upload(len(payload), digest)
    assert upload["upload_id"] == "opaque-upload-id"
    runtime.complete_driver_upload(upload["upload_id"], len(payload), digest)
    assert client.events == ["inspect-par", "revoke-par", "read-object"]


def test_driver_par_is_revoked_and_object_deleted_when_validation_fails() -> None:
    payload = b"not-a-zip"
    digest = hashlib.sha256(payload).hexdigest()
    runtime, client = driver_runtime(payload)
    upload = runtime.create_driver_upload(len(payload), digest)
    with pytest.raises(JdbcDriverValidationError, match="not a ZIP"):
        runtime.complete_driver_upload(upload["upload_id"], len(payload), digest)
    assert client.events == ["inspect-par", "revoke-par", "read-object", "delete-object"]
