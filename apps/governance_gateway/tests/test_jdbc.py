from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from governance_gateway.jdbc import AidpJdbcRuntime, JdbcConfigurationError, _write_oci_profile, bind_named_parameters


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
