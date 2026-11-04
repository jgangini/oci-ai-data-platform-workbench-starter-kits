"""Replay the maintained consultative graph without native effects or credentials."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/backend"))


def model_failure_metadata(error):
    """Keep a typed transport diagnosis without provider messages or request data."""
    from oci.exceptions import ServiceError

    cause = error.__context__
    if not isinstance(cause, ServiceError):
        allowed_types = {"AttributeError", "ValueError", "TypeError", "ModuleNotFoundError",
            "ImportError", "InvalidConfig", "InvalidPrivateKey", "RequestException",
            "ConnectTimeout", "ReadTimeout", "ConnectionError", "SSLError", "TimeoutError"}
        kind = type(cause).__name__
        return {"complete": False, "failure_category": "model_adapter_or_transport",
            "cause_type": kind if kind in allowed_types else "unknown"}
    allowed = {"NotAuthenticated", "NotAuthorizedOrNotFound", "InvalidParameter",
        "InvalidRequest", "TooManyRequests", "InternalServerError", "ServiceUnavailable"}
    return {"complete": False, "failure_category": "oci_service_error",
        "http_status": cause.status if type(cause.status) is int else None,
        "service_code": cause.code if cause.code in allowed else "other"}


def configure_model_transport(model):
    """Reuse the existing real-node harness timeout; keep fixture mode credential-free."""
    from aidp_agent_flow.model import OCIModel

    if not isinstance(model, OCIModel) or model.client is not None:
        return
    timeout = model.settings.get("read_timeout_seconds", 180)
    if type(timeout) is not int or not 60 <= timeout <= 300:
        raise ValueError("The local OCI read timeout must be between60 and300 seconds")
    import oci
    from aidp_agent_flow.remote import signer_for

    config = oci.config.from_file(str(model.config_file), model.profile)
    model.client = oci.generative_ai_inference.GenerativeAiInferenceClient(
        {**config, "region": model.settings["region"]}, signer=signer_for(config),
        retry_strategy=oci.retry.NoneRetryStrategy(), timeout=(5, timeout))


def checked_candidate():
    """Bind imports outside the package to its exact, retained native source."""
    from app.ai_data_governance.governance_source import governance_agent_source, governance_entry_source

    config = tomllib.loads((ROOT / "aidp-agent.toml").read_text(encoding="utf-8"))
    validation = config["validation"]
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != validation["adapter_sha256"]:
        raise ValueError("The validation adapter changed; reconcile its configuration")
    entry = (ROOT / config["agent"]["entry"]).resolve()
    if not entry.is_relative_to(ROOT):
        raise ValueError("The retained entry escaped the project")
    source = (entry.parent / "governance_runtime.py").read_bytes()
    binding = next(ast.literal_eval(node.value) for node in ast.parse(source).body
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "CONFIG"
            for target in node.targets))
    revision = binding.pop("component_revision")
    if revision != validation["source_revision"] or binding["deployment_stage"] != "TEST":
        raise ValueError("The retained TEST revision changed")
    if source != governance_agent_source(binding) or entry.read_bytes() != governance_entry_source():
        raise ValueError("Maintained code no longer matches the retained candidate")
    return revision


def build_graph(runtime):
    from app.ai_data_governance.governance_graph import build_governance_graph, READ_OPERATIONS
    from app.ai_data_governance.inventory import canonical
    from app.ai_data_governance.native_governance import consultation_model_sources, restore_consultation_sources
    from app.ai_data_governance.prompts import governance_prompts

    if runtime.get("external_effects_allowed") is not False:
        raise ValueError("The replay adapter never enables external effects")
    checked_candidate()
    services, model = runtime["services"], runtime["model"]
    if isinstance(getattr(model, "response_metadata", None), list):
        configure_model_transport(model)
    prompts = governance_prompts()

    async def authorize(config, *, assets):
        result = await services.call("table", "authorize_central_metadata", {"assets": assets})
        if (not isinstance(result, dict) or set(result) != {"admitted", "access_mode"}
                or result["admitted"] is not True or result["access_mode"] != "CENTRAL_METADATA"):
            raise PermissionError("The recorded central metadata admission was rejected")

    async def resolve_assets(intent, config):
        return await services.call("table", "resolve_assets", {"intent": intent.model_dump(mode="json")})

    async def read(operation, assets, config):
        return await services.call("table", operation, {"assets": assets})

    async def revalidate(config, *, assets, evidence):
        result = await services.call("table", "revalidate_evidence", {"assets": assets, "evidence": evidence})
        if not isinstance(result, dict) or set(result) != {"verified"} or result["verified"] is not True:
            raise PermissionError("The recorded evidence readmission was rejected")

    async def reobserve(config, *, assets, evidence):
        return await services.call('table', 'reobserve_evidence', {'assets':assets,'evidence':evidence})

    async def call_model(phase, payload, schema, config):
        if phase not in {"G1", "G3", "G4", "G5", "G6"}:
            raise ValueError("Unsupported model phase")
        model_payload, aliases = consultation_model_sources(phase, payload)
        try:
            response = await model.complete(prompts["G0"] + "\n\n" + prompts[phase],
                canonical({"input": model_payload, "json_schema": schema}))
        except Exception as error:
            metadata = getattr(model, "response_metadata", None)
            if isinstance(metadata, list):
                metadata.append(model_failure_metadata(error))
            raise
        return restore_consultation_sources(phase, response, aliases)

    return build_governance_graph(authorize=authorize, resolve_assets=resolve_assets, read=read,
        call_model=call_model, capabilities=READ_OPERATIONS, checkpointer=runtime["checkpointer"],
        reauthorize_evidence=revalidate,reobserve_evidence=reobserve)
