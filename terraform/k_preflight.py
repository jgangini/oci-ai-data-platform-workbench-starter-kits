from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import oci
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from release_gate import compartment_target, validate_context, validate_source


E5_SHAPE = "VM.Standard.E5.Flex"
E4_SHAPE = "VM.Standard.E4.Flex"
E3_SHAPE = "VM.Standard.E3.Flex"
SUPPORTED_SHAPES = (E5_SHAPE, E4_SHAPE, E3_SHAPE)
ACTIVE_WORK_REQUEST_STATES = {"ACCEPTED", "IN_PROGRESS", "WAITING", "NEEDS_ATTENTION", "CANCELING"}
MODEL_TYPE_BASE = "BASE"


def _safe_error_message(exc: Exception) -> str:
    if isinstance(exc, oci.exceptions.ServiceError):
        return f"OCI {exc.status} {exc.code}"
    if isinstance(exc, (RuntimeError, ValueError)):
        return str(exc)[:256]
    return type(exc).__name__


def _home_region(identity: Any, tenancy_id: str) -> str:
    tenancy = identity.get_tenancy(tenancy_id).data
    home_key = str(tenancy.home_region_key).upper()
    subscriptions = identity.list_region_subscriptions(tenancy_id).data
    matches = [item for item in subscriptions if str(item.region_key).upper() == home_key]
    if len(matches) != 1 or not matches[0].region_name:
        raise RuntimeError("OCI did not return one unambiguous tenancy home region")
    return str(matches[0].region_name)


def _require_ready_region(identity: Any, tenancy_id: str, region: str) -> None:
    subscriptions = identity.list_region_subscriptions(tenancy_id).data
    match = next(
        (item for item in subscriptions if str(getattr(item, "region_name", "")) == region),
        None,
    )
    if match is None:
        raise RuntimeError(f"region {region} is not subscribed by this tenancy")
    status = str(getattr(match, "status", "READY") or "READY").upper()
    if status != "READY":
        raise RuntimeError(f"region {region} subscription is {status}, not READY")


def _model_is_selectable(model: Any) -> bool:
    capabilities = {str(value).upper() for value in (getattr(model, "capabilities", None) or [])}
    return (
        str(getattr(model, "lifecycle_state", "")).upper() == "ACTIVE"
        and "CHAT" in capabilities
        and str(getattr(model, "type", "")).upper() == MODEL_TYPE_BASE
        and not getattr(model, "base_model_id", None)
        and not getattr(model, "compartment_id", None)
        and not getattr(model, "time_deprecated", None)
        and not getattr(model, "time_on_demand_retired", None)
    )


def _require_agent_model(genai: Any, tenancy_id: str, model_id: str) -> str:
    models = _list_all(
        genai.list_models,
        compartment_id=tenancy_id,
        capability=["CHAT"],
        lifecycle_state="ACTIVE",
    )
    selected = next((model for model in models if str(getattr(model, "id", "")) == model_id), None)
    if selected is None or not _model_is_selectable(selected):
        raise RuntimeError("the selected Agent LLM is no longer an ACTIVE on-demand CHAT model in this region")
    return str(getattr(selected, "display_name", None) or model_id)


def _require_autonomous(
    database: Any,
    tenancy_id: str,
    inputs: dict[str, Any],
) -> str:
    mode = str(inputs.get("autonomous_database_mode") or "new")
    if mode == "existing":
        database_id = str(inputs.get("existing_autonomous_database_ocid") or "")
        if not database_id.startswith("ocid1.autonomousdatabase."):
            raise ValueError("existing Autonomous database OCID is required")
        item = database.get_autonomous_database(database_id).data
        if str(getattr(item, "db_version", "")) != "26ai":
            raise RuntimeError("the existing Autonomous database must use version 26ai")
        if str(getattr(item, "db_workload", "")).upper() != "DW":
            raise RuntimeError("the existing Autonomous database must use the Data Warehouse workload")
        state = str(getattr(item, "lifecycle_state", "")).upper()
        if state not in {"AVAILABLE", "STOPPED"}:
            raise RuntimeError(f"the existing Autonomous database is {state or 'not available'}")
        return f"existing Oracle AI Database 26ai DW is {state}"

    count = inputs.get("autonomous_database_compute_count", 4)
    if isinstance(count, bool) or not isinstance(count, (int, float)) or int(count) != count or not 2 <= int(count) <= 512:
        raise ValueError("new Autonomous database ECPU count must be an integer between 2 and 512")
    versions = _list_all(
        database.list_autonomous_db_versions,
        compartment_id=tenancy_id,
        db_workload="DW",
    )
    if not any(str(getattr(item, "db_version", "")) == "26ai" for item in versions):
        raise RuntimeError("Oracle AI Database 26ai DW is not available in the selected region")
    return f"new Oracle AI Database 26ai DW will use {int(count)} ECPU"


def _candidate_shapes(preferred: str) -> list[str]:
    if preferred not in SUPPORTED_SHAPES:
        raise ValueError("preferred_vm_shape must be E5 Flex, E4 Flex, or E3 Flex")
    return list(SUPPORTED_SHAPES[SUPPORTED_SHAPES.index(preferred) :])


def _list_all(call: Callable[..., Any], **kwargs: Any) -> list[Any]:
    items: list[Any] = []
    while True:
        response = call(**kwargs)
        data = response.data
        items.extend(data if isinstance(data, list) else (getattr(data, "items", None) or []))
        page = (getattr(response, "headers", None) or {}).get("opc-next-page")
        if not page:
            return items
        kwargs["page"] = page


def _has_active_aidp_work_request(aidp: Any, compartment_ids: set[str]) -> bool:
    return any(
        str(getattr(item, "compartment_id", "")) in compartment_ids
        and str(getattr(item, "status", "")).upper() in ACTIVE_WORK_REQUEST_STATES
        for item in _list_all(aidp.list_work_requests)
    )


def _require_compartment_target(identity: Any, aidp: Any, tenancy_id: str, target: str, mode: str) -> str:
    compartments = _list_all(
        identity.list_compartments,
        compartment_id=tenancy_id,
        compartment_id_in_subtree=True,
        access_level="ANY",
    )
    matches = [
        item
        for item in compartments
        if str(getattr(item, "name", "")).casefold() == target.casefold()
    ]
    active = [item for item in matches if str(getattr(item, "lifecycle_state", "")).upper() == "ACTIVE"]
    if mode == "existing":
        if len(active) != 1:
            raise RuntimeError(f"existing compartment {target} was not found or is ambiguous")
        return f"{target} exists and is ACTIVE"
    occupied = [item for item in matches if str(getattr(item, "lifecycle_state", "")).upper() != "DELETED"]
    if occupied:
        raise RuntimeError(f"compartment {target} is not available to create")
    deleted_ids = {str(item.id) for item in matches if getattr(item, "id", None)}
    if deleted_ids and _has_active_aidp_work_request(aidp, deleted_ids):
        raise RuntimeError(f"a previous AIDP work request is still active for {target}")
    return f"{target} is available to create"


def select_inputs(
    context: dict[str, Any],
    sdk_config: dict[str, Any],
    identity_factory: Callable[[dict[str, Any]], Any] = oci.identity.IdentityClient,
    compute_factory: Callable[[dict[str, Any]], Any] = oci.core.ComputeClient,
    aidp_factory: Callable[[dict[str, Any]], Any] = oci.ai_data_platform.AiDataPlatformClient,
    database_factory: Callable[[dict[str, Any]], Any] = oci.database.DatabaseClient,
    genai_factory: Callable[[dict[str, Any]], Any] = oci.generative_ai.GenerativeAiClient,
) -> dict[str, Any]:
    target, mode = compartment_target(context)
    region = str(context.get("region") or sdk_config.get("region") or "").strip()
    tenancy_id = str(sdk_config.get("tenancy") or "").strip()
    operator_user_ocid = str(sdk_config.get("user") or "").strip()
    if not region or not tenancy_id or not operator_user_ocid.startswith("ocid1.user."):
        raise ValueError("preflight requires deployment region, tenancy OCID, and operator user OCID")

    candidates = _candidate_shapes(E5_SHAPE)
    ocpus = 2.0
    memory = 16.0
    regional_config = dict(sdk_config)
    regional_config["region"] = region
    identity = identity_factory(regional_config)
    _require_ready_region(identity, tenancy_id, region)
    inputs = context.get("inputs") if isinstance(context.get("inputs"), dict) else {}
    model_id = str(inputs.get("agent_model_id") or "").strip()
    if not model_id:
        raise ValueError("agent_model_id is required")
    model_name = _require_agent_model(genai_factory(regional_config), tenancy_id, model_id)
    database_message = _require_autonomous(database_factory(regional_config), tenancy_id, inputs)
    compartment_message = _require_compartment_target(
        identity,
        aidp_factory(regional_config),
        tenancy_id,
        target,
        mode,
    )
    home_region = _home_region(identity, tenancy_id)
    availability_domains = identity.list_availability_domains(tenancy_id).data
    compute = compute_factory(regional_config)
    for availability_domain_index, domain in enumerate(availability_domains):
        availability_domain = str(domain.name)
        details = oci.core.models.CreateComputeCapacityReportDetails(
            compartment_id=tenancy_id,
            availability_domain=availability_domain,
            shape_availabilities=[
                oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                    instance_shape=shape,
                    instance_shape_config=oci.core.models.CapacityReportInstanceShapeConfig(
                        ocpus=ocpus,
                        memory_in_gbs=memory,
                    ),
                )
                for shape in SUPPORTED_SHAPES
            ],
        )
        report = compute.create_compute_capacity_report(details).data
        selected = next(
            (
                shape
                for shape in candidates
                if any(
                    str(item.instance_shape) == shape
                    and item.availability_status
                    == oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
                    and (item.available_count is None or int(item.available_count) >= 1)
                    for item in report.shape_availabilities
                )
            ),
            None,
        )
        if selected:
            return {
                "inputs": {
                    "home_region": home_region,
                    "operator_user_ocid": operator_user_ocid,
                    "preferred_vm_shape": selected,
                    "availability_domain_index": availability_domain_index,
                },
                "events": [
                    {
                        "name": "Immutable v3.0.0 source",
                        "status": "passed",
                        "message": "v3.0.0 source context and deployment source passed",
                    },
                    {
                        "name": "Compartment availability",
                        "status": "passed",
                        "message": compartment_message,
                    },
                    {"name": "OCI tenancy home region", "status": "passed", "message": home_region},
                    {"name": "Regional Agent LLM", "status": "passed", "message": model_name},
                    {"name": "Autonomous AI Database 26ai", "status": "passed", "message": database_message},
                    {
                        "name": "Compute capacity preflight",
                        "status": "passed",
                        "message": f"{selected} available in {availability_domain}",
                    },
                ],
            }
    raise RuntimeError("OCI reports no capacity for the supported E5/E4/E3 Flex shapes in any Availability Domain")


def _read_json_env(name: str) -> dict[str, Any]:
    return json.loads(Path(os.environ[name]).read_text(encoding="utf-8"))


def _write_result(payload: dict[str, Any]) -> None:
    path = Path(os.environ["DEPLOY_STUDIO_RESULT"])
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    path.chmod(0o600)


def _require_unencrypted_private_key(config: dict[str, Any], key_path: str) -> None:
    if config.get("pass_phrase"):
        raise ValueError("Deploy Studio requires an unencrypted OCI API private key")
    try:
        private_key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    except TypeError as exc:
        raise ValueError("Deploy Studio requires an unencrypted OCI API private key") from exc
    except (OSError, ValueError) as exc:
        raise ValueError("OCI API private key is unreadable or invalid") from exc
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("OCI API private key must be RSA")


def _load_sdk_config() -> dict[str, Any]:
    config = oci.config.from_file(os.environ["DEPLOY_STUDIO_OCI_CONFIG"], "DEFAULT")
    config["key_file"] = os.environ["DEPLOY_STUDIO_OCI_KEY"]
    _require_unencrypted_private_key(config, config["key_file"])
    oci.config.validate_config(config)
    return config


def main() -> int:
    try:
        context = _read_json_env("DEPLOY_STUDIO_CONTEXT")
        validate_context(context)
        validate_source(Path(__file__).parent)
        _write_result(select_inputs(context, _load_sdk_config()))
        return 0
    except Exception as exc:  # The runner receives a bounded, secret-free failure event.
        _write_result(
            {
                "inputs": {},
                "events": [
                    {
                        "name": "OCI deployment preflight",
                        "status": "failed",
                        "message": _safe_error_message(exc),
                    }
                ],
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
