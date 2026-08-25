from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


TERRAFORM_ROOT = Path(__file__).parents[1]
RELEASE_GATE_PATH = TERRAFORM_ROOT / "release_gate.py"
RELEASE_GATE_SPEC = importlib.util.spec_from_file_location("release_gate", RELEASE_GATE_PATH)
assert RELEASE_GATE_SPEC and RELEASE_GATE_SPEC.loader
release_gate = importlib.util.module_from_spec(RELEASE_GATE_SPEC)
sys.modules[RELEASE_GATE_SPEC.name] = release_gate
RELEASE_GATE_SPEC.loader.exec_module(release_gate)

MODULE_PATH = TERRAFORM_ROOT / "k_preflight.py"
SPEC = importlib.util.spec_from_file_location("aidp_deploy_preflight", MODULE_PATH)
assert SPEC and SPEC.loader
preflight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


class Identity:
    def get_user(self, _user_ocid: str) -> Any:
        return SimpleNamespace(data=SimpleNamespace(name="joel.ganggini@oracle.com"))

    def get_tenancy(self, _tenancy_id: str) -> Any:
        return SimpleNamespace(data=SimpleNamespace(home_region_key="IAD"))

    def list_region_subscriptions(self, _tenancy_id: str) -> Any:
        return SimpleNamespace(
            data=[
                SimpleNamespace(region_key="ORD", region_name="us-chicago-1", is_home_region=False, status="READY"),
                SimpleNamespace(region_key="IAD", region_name="us-ashburn-1", is_home_region=True, status="READY"),
            ]
        )

    def list_availability_domains(self, _tenancy_id: str) -> Any:
        return SimpleNamespace(data=[SimpleNamespace(name="AD-1")])

    def list_compartments(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(data=[], headers={})

class Compute:
    def __init__(self, statuses: dict[str, tuple[str, str]]) -> None:
        self.statuses = statuses
        self.details: Any = None

    def create_compute_capacity_report(self, details: Any) -> Any:
        self.details = details
        return SimpleNamespace(
            data=SimpleNamespace(
                shape_availabilities=[
                    SimpleNamespace(instance_shape=shape, availability_status=status, available_count=count)
                    for shape, (status, count) in self.statuses.items()
                ]
            )
        )


class Aidp:
    def __init__(self, work_requests: list[Any] | None = None) -> None:
        self.work_requests = work_requests or []

    def list_work_requests(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(data=SimpleNamespace(items=self.work_requests), headers={})


class Database:
    def list_autonomous_db_versions(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(data=[SimpleNamespace(version="26ai")], headers={})

    def get_autonomous_database(self, _database_id: str) -> Any:
        return SimpleNamespace(
            data=SimpleNamespace(db_version="26ai", db_workload="DW", lifecycle_state="AVAILABLE")
        )


class Genai:
    def __init__(self, models: list[Any] | None = None) -> None:
        self.models = models or [
            SimpleNamespace(
                id="ocid1.generativeaimodel.oc1..chat",
                display_name="Test Chat",
                lifecycle_state="ACTIVE",
                capabilities=["CHAT"],
                type="BASE",
                base_model_id=None,
                compartment_id=None,
                time_deprecated=None,
                time_on_demand_retired=None,
            )
        ]

    def list_models(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(data=SimpleNamespace(items=self.models), headers={})


class ObjectStorage:
    def __init__(self, bucket_exists: bool = False) -> None:
        self.bucket_exists = bucket_exists

    def get_namespace(self) -> Any:
        return SimpleNamespace(data="testnamespace")

    def get_bucket(self, **_kwargs: Any) -> Any:
        if self.bucket_exists:
            return SimpleNamespace(data=SimpleNamespace(name="oci_artifacts"))
        raise preflight.oci.exceptions.ServiceError(
            status=404,
            code="BucketNotFound",
            headers={},
            message="bucket not found",
        )


def _select(
    statuses: dict[str, tuple[str, str]],
    *,
    identity: Identity | None = None,
    aidp: Aidp | None = None,
    compartment: str = "oci-aidp-cloud-migration-lab-5",
    compartment_mode: str = "new",
    input_overrides: dict[str, Any] | None = None,
    object_storage: ObjectStorage | None = None,
) -> tuple[dict[str, Any], Compute]:
    compute = Compute(statuses)
    identity = identity or Identity()
    aidp = aidp or Aidp()
    inputs = {
        "agent_model_id": "ocid1.generativeaimodel.oc1..chat",
        "autonomous_database_mode": "new",
        "autonomous_database_compute_count": 4,
        **(input_overrides or {}),
    }
    result = preflight.select_inputs(
        {
            "region": "us-chicago-1",
            "compartment": compartment,
            "compartment_mode": compartment_mode,
            "inputs": inputs,
        },
        {"tenancy": "ocid1.tenancy.oc1..test", "user": "ocid1.user.oc1..operator", "region": "us-chicago-1"},
        identity_factory=lambda _config: identity,
        compute_factory=lambda _config: compute,
        aidp_factory=lambda _config: aidp,
        database_factory=lambda _config: Database(),
        genai_factory=lambda _config: Genai(),
        object_storage_factory=lambda _config: object_storage or ObjectStorage(),
    )
    return result, compute


def test_preflight_selects_e5_and_discovers_home_region() -> None:
    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    result, compute = _select({preflight.E5_SHAPE: (available, "1"), preflight.E4_SHAPE: (available, "2")})
    assert result["inputs"] == {
        "home_region": "us-ashburn-1",
        "operator_user_ocid": "ocid1.user.oc1..operator",
        "operator_username": "joel.ganggini@oracle.com",
        "preferred_vm_shape": preflight.E5_SHAPE,
        "availability_domain_index": 0,
    }
    assert compute.details.compartment_id == "ocid1.tenancy.oc1..test"
    assert compute.details.availability_domain == "AD-1"
    assert [item.instance_shape for item in compute.details.shape_availabilities] == list(preflight.SUPPORTED_SHAPES)
    assert compute.details.shape_availabilities[0].instance_shape_config.ocpus == 2
    assert compute.details.shape_availabilities[0].instance_shape_config.memory_in_gbs == 16


def test_preflight_rejects_creating_the_fixed_artifacts_bucket_when_it_exists() -> None:
    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    with pytest.raises(RuntimeError, match="oci_artifacts already exists"):
        _select(
            {preflight.E5_SHAPE: (available, "1")},
            input_overrides={"artifacts_bucket_mode": "new"},
            object_storage=ObjectStorage(bucket_exists=True),
        )


def test_preflight_rejects_occupied_new_compartment_across_pages() -> None:
    class PagedIdentity(Identity):
        def list_compartments(self, **kwargs: Any) -> Any:
            if kwargs.get("page") == "next":
                return SimpleNamespace(
                    data=[SimpleNamespace(id="ocid1.compartment.oc1..active", name="OCI-AIDP-CLOUD-MIGRATION-LAB-5", lifecycle_state="ACTIVE")],
                    headers={},
                )
            return SimpleNamespace(
                data=[SimpleNamespace(id="ocid1.compartment.oc1..deleted", name="oci-aidp-cloud-migration-lab-5", lifecycle_state="DELETED")],
                headers={"opc-next-page": "next"},
            )

    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    with pytest.raises(RuntimeError, match="is not available"):
        _select({preflight.E5_SHAPE: (available, "1")}, identity=PagedIdentity())


def test_preflight_rejects_active_work_request_from_deleted_target() -> None:
    class DeletedIdentity(Identity):
        def list_compartments(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                data=[SimpleNamespace(id="ocid1.compartment.oc1..deleted", name="oci-aidp-cloud-migration-lab-5", lifecycle_state="DELETED")],
                headers={},
            )

    work_request = SimpleNamespace(
        compartment_id="ocid1.compartment.oc1..deleted",
        status="IN_PROGRESS",
    )
    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    with pytest.raises(RuntimeError, match="work request is still active"):
        _select(
            {preflight.E5_SHAPE: (available, "1")},
            identity=DeletedIdentity(),
            aidp=Aidp([work_request]),
        )


def test_preflight_accepts_one_active_existing_compartment() -> None:
    class ExistingIdentity(Identity):
        def list_compartments(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                data=[SimpleNamespace(id="ocid1.compartment.oc1..existing", name="Shared_Lab.2026", lifecycle_state="ACTIVE")],
                headers={},
            )

    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    result, _ = _select(
        {preflight.E5_SHAPE: (available, "1")},
        identity=ExistingIdentity(),
        compartment="Shared_Lab.2026",
        compartment_mode="existing",
    )
    event = next(item for item in result["events"] if item["name"] == "Compartment availability")
    assert event["message"] == "Shared_Lab.2026 exists and is ACTIVE"


def test_preflight_accepts_active_existing_compartment_ocid() -> None:
    compartment_id = "ocid1.compartment.oc1..existing"

    class ExistingIdentity(Identity):
        def list_compartments(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                data=[SimpleNamespace(id=compartment_id, name="Shared_Lab.2026", lifecycle_state="ACTIVE")],
                headers={},
            )

    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    result, _ = _select(
        {preflight.E5_SHAPE: (available, "1")},
        identity=ExistingIdentity(),
        compartment=compartment_id,
        compartment_mode="existing",
    )
    event = next(item for item in result["events"] if item["name"] == "Compartment availability")
    assert event["message"] == "selected compartment exists and is ACTIVE"


def test_preflight_rejects_ocid_as_new_compartment_name() -> None:
    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    with pytest.raises(ValueError, match="must use a name"):
        _select(
            {preflight.E5_SHAPE: (available, "1")},
            compartment="ocid1.compartment.oc1..new",
            compartment_mode="new",
        )


def test_preflight_reports_selected_new_name_is_available() -> None:
    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    result, _ = _select({preflight.E5_SHAPE: (available, "1")}, compartment="custom.lab_2026")
    event = next(item for item in result["events"] if item["name"] == "Compartment availability")
    assert event["message"] == "custom.lab_2026 is available to create"


def test_preflight_accepts_available_status_without_a_count() -> None:
    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    result, _ = _select({preflight.E5_SHAPE: (available, None)})
    assert result["inputs"]["preferred_vm_shape"] == preflight.E5_SHAPE


def test_preflight_selects_e4_when_e5_is_not_available() -> None:
    model = preflight.oci.core.models.CapacityReportShapeAvailability
    result, _ = _select(
        {
            preflight.E5_SHAPE: (model.AVAILABILITY_STATUS_OUT_OF_HOST_CAPACITY, "0"),
            preflight.E4_SHAPE: (model.AVAILABILITY_STATUS_AVAILABLE, "1"),
        }
    )
    assert result["inputs"]["preferred_vm_shape"] == preflight.E4_SHAPE


def test_preflight_falls_back_to_e4_without_user_shape_input() -> None:
    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    result, compute = _select({preflight.E4_SHAPE: (available, "1")})
    assert result["inputs"]["preferred_vm_shape"] == preflight.E4_SHAPE
    assert [item.instance_shape for item in compute.details.shape_availabilities] == list(preflight.SUPPORTED_SHAPES)


def test_preflight_falls_back_to_authorized_e3_when_e5_and_e4_are_unavailable() -> None:
    model = preflight.oci.core.models.CapacityReportShapeAvailability
    result, _ = _select(
        {
            preflight.E5_SHAPE: (model.AVAILABILITY_STATUS_OUT_OF_HOST_CAPACITY, "0"),
            preflight.E4_SHAPE: (model.AVAILABILITY_STATUS_OUT_OF_HOST_CAPACITY, "0"),
            preflight.E3_SHAPE: (model.AVAILABILITY_STATUS_AVAILABLE, "1"),
        }
    )
    assert result["inputs"]["preferred_vm_shape"] == preflight.E3_SHAPE


def test_preflight_checks_all_availability_domains_for_standard_shape() -> None:
    model = preflight.oci.core.models.CapacityReportShapeAvailability

    class MultiAdIdentity(Identity):
        def list_availability_domains(self, _tenancy_id: str) -> Any:
            return SimpleNamespace(data=[SimpleNamespace(name="AD-1"), SimpleNamespace(name="AD-2")])

    class MultiAdCompute:
        def create_compute_capacity_report(self, details: Any) -> Any:
            available = details.availability_domain == "AD-2"
            return SimpleNamespace(
                data=SimpleNamespace(
                    shape_availabilities=[
                        SimpleNamespace(
                            instance_shape=preflight.E5_SHAPE,
                            availability_status=(model.AVAILABILITY_STATUS_AVAILABLE if available else model.AVAILABILITY_STATUS_OUT_OF_HOST_CAPACITY),
                            available_count=1 if available else 0,
                        )
                    ]
                )
            )

    result = preflight.select_inputs(
        {
            "region": "us-chicago-1",
            "compartment": "aidp-lab",
            "compartment_mode": "new",
            "inputs": {
                "agent_model_id": "ocid1.generativeaimodel.oc1..chat",
                "autonomous_database_mode": "new",
                "autonomous_database_compute_count": 4,
            },
        },
        {"tenancy": "ocid1.tenancy.oc1..test", "user": "ocid1.user.oc1..operator", "region": "us-chicago-1"},
        identity_factory=lambda _config: MultiAdIdentity(),
        compute_factory=lambda _config: MultiAdCompute(),
        aidp_factory=lambda _config: Aidp(),
        database_factory=lambda _config: Database(),
        genai_factory=lambda _config: Genai(),
        object_storage_factory=lambda _config: ObjectStorage(),
    )
    assert result["inputs"]["availability_domain_index"] == 1


def test_preflight_rejects_zero_or_missing_capacity() -> None:
    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    unsupported = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_HARDWARE_NOT_SUPPORTED
    with pytest.raises(RuntimeError, match="no capacity"):
        _select({preflight.E5_SHAPE: (available, "0"), preflight.E4_SHAPE: (unsupported, "0")})


def test_preflight_accepts_any_available_fault_domain() -> None:
    model = preflight.oci.core.models.CapacityReportShapeAvailability

    class FaultDomainCompute:
        @staticmethod
        def create_compute_capacity_report(_details: Any) -> Any:
            return SimpleNamespace(
                data=SimpleNamespace(
                    shape_availabilities=[
                        SimpleNamespace(
                            instance_shape=preflight.E5_SHAPE,
                            availability_status=model.AVAILABILITY_STATUS_OUT_OF_HOST_CAPACITY,
                            available_count=0,
                        ),
                        SimpleNamespace(
                            instance_shape=preflight.E5_SHAPE,
                            availability_status=model.AVAILABILITY_STATUS_AVAILABLE,
                            available_count=1,
                        ),
                    ]
                )
            )

    result = preflight.select_inputs(
        {
            "region": "us-chicago-1",
            "compartment": "aidp-lab",
            "compartment_mode": "new",
            "inputs": {
                "agent_model_id": "ocid1.generativeaimodel.oc1..chat",
                "autonomous_database_mode": "new",
                "autonomous_database_compute_count": 4,
            },
        },
        {"tenancy": "ocid1.tenancy.oc1..test", "user": "ocid1.user.oc1..operator", "region": "us-chicago-1"},
        identity_factory=lambda _config: Identity(),
        compute_factory=lambda _config: FaultDomainCompute(),
        aidp_factory=lambda _config: Aidp(),
        database_factory=lambda _config: Database(),
        genai_factory=lambda _config: Genai(),
        object_storage_factory=lambda _config: ObjectStorage(),
    )
    assert result["inputs"]["preferred_vm_shape"] == preflight.E5_SHAPE


def _private_key_pem(*, encrypted_with: bytes | None = None) -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encryption = (
        serialization.BestAvailableEncryption(encrypted_with)
        if encrypted_with
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        encryption,
    )


def test_preflight_uses_runner_private_key_path(tmp_path, monkeypatch) -> None:
    config = {"tenancy": "ocid1.tenancy.oc1..test", "region": "us-chicago-1", "key_file": "stale"}
    key_path = tmp_path / "private-uploaded-key.pem"
    key_path.write_bytes(_private_key_pem())
    monkeypatch.setenv("DEPLOY_STUDIO_OCI_CONFIG", "uploaded-config")
    monkeypatch.setenv("DEPLOY_STUDIO_OCI_KEY", str(key_path))
    monkeypatch.setattr(preflight.oci.config, "from_file", lambda path, profile: dict(config))
    monkeypatch.setattr(preflight.oci.config, "validate_config", lambda value: None)
    loaded = preflight._load_sdk_config()
    assert loaded["key_file"] == str(key_path)


@pytest.mark.parametrize("config_has_pass_phrase", [False, True])
def test_preflight_rejects_encrypted_or_passphrase_config_before_oci(
    tmp_path,
    monkeypatch,
    config_has_pass_phrase: bool,
) -> None:
    pass_phrase = b"must-not-appear"
    key_path = tmp_path / "private-uploaded-key.pem"
    key_path.write_bytes(
        _private_key_pem(encrypted_with=None if config_has_pass_phrase else pass_phrase)
    )
    config = {"tenancy": "ocid1.tenancy.oc1..test", "region": "us-chicago-1"}
    if config_has_pass_phrase:
        config["pass_phrase"] = pass_phrase.decode()
    monkeypatch.setenv("DEPLOY_STUDIO_OCI_CONFIG", "uploaded-config")
    monkeypatch.setenv("DEPLOY_STUDIO_OCI_KEY", str(key_path))
    monkeypatch.setattr(preflight.oci.config, "from_file", lambda path, profile: dict(config))
    monkeypatch.setattr(
        preflight.oci.config,
        "validate_config",
        lambda value: pytest.fail("OCI config validation must not run after the key gate fails"),
    )

    with pytest.raises(ValueError, match="unencrypted") as error:
        preflight._load_sdk_config()

    assert pass_phrase.decode() not in str(error.value)


def test_safe_error_message_keeps_capacity_and_oci_errors_actionable() -> None:
    assert preflight._safe_error_message(RuntimeError("OCI reports no capacity")) == "OCI reports no capacity"
    error = preflight.oci.exceptions.ServiceError(status=429, code="TooManyRequests", headers={}, message="hidden detail")
    assert preflight._safe_error_message(error) == "OCI 429 TooManyRequests"
