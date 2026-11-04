import ast
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.ai_data_governance import governance_graph
from app.ai_data_governance.governance_source import governance_agent_source, governance_entry_source
from test_ai_dg_activity import activity_fixture


SPEC = importlib.util.spec_from_file_location(
    "ai_dg_test_adapter", Path(__file__).resolve().parents[3] / "scripts/ai_dg_test_adapter.py")
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    binding, _, _ = activity_fixture()
    binding.pop("component_revision")
    binding["storage_root"] = "oci://oci_artifacts@test/oci_artifacts"
    source = governance_agent_source(binding)
    config = next(ast.literal_eval(node.value) for node in ast.parse(source).body
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "CONFIG"
            for target in node.targets))
    package = tmp_path / "agent"
    package.mkdir()
    (package / "governance_runtime.py").write_bytes(source)
    (package / "governance_agent.py").write_bytes(governance_entry_source())
    manifest = tmp_path / "aidp-agent.toml"
    manifest.write_text('[agent]\nentry="agent/governance_agent.py"\n[validation]\n'
        + 'source_revision=' + json.dumps(config["component_revision"]) + '\n'
        + 'adapter_sha256=' + json.dumps(hashlib.sha256(Path(adapter.__file__).read_bytes()).hexdigest())
        + '\n', encoding="utf-8")
    monkeypatch.setattr(adapter, "ROOT", tmp_path)
    return manifest, package, config["component_revision"]


def test_replay_candidate_matches_maintained_native_source(candidate):
    assert adapter.checked_candidate() == candidate[2]


@pytest.mark.parametrize("drift", ["adapter", "source", "entry", "escape"])
def test_replay_refuses_adapter_or_native_package_drift(candidate, drift):
    manifest, package, _ = candidate
    if drift == "adapter":
        text = manifest.read_text(encoding="utf-8")
        digest = hashlib.sha256(Path(adapter.__file__).read_bytes()).hexdigest()
        manifest.write_text(text.replace(digest, "0" * 64), encoding="utf-8")
    elif drift == "escape":
        manifest.write_text(manifest.read_text(encoding="utf-8").replace(
            "agent/governance_agent.py", "../outside.py"), encoding="utf-8")
    else:
        target = package / ("governance_runtime.py" if drift == "source" else "governance_agent.py")
        target.write_bytes(target.read_bytes() + b"\n# changed\n")
    with pytest.raises(ValueError):
        adapter.checked_candidate()


@pytest.mark.parametrize("flag", [True, 0, None, "false"])
def test_replay_never_enables_external_effects(monkeypatch, flag):
    checked = AsyncMock()
    monkeypatch.setattr(adapter, "checked_candidate", checked)
    with pytest.raises(ValueError, match="never enables external effects"):
        adapter.build_graph({"external_effects_allowed": flag})
    checked.assert_not_called()


@pytest.mark.parametrize("operation,valid", [
    ("authorize", {"admitted": True, "access_mode": "CENTRAL_METADATA"}),
    ("revalidate", {"verified": True}),
])
@pytest.mark.parametrize("value", [True, 1, 1.0, False, None, "true", "extra"])
def test_recorded_admission_requires_exact_boolean_and_keys(monkeypatch, operation, valid, value):
    monkeypatch.setattr(adapter, "checked_candidate", lambda: "verified")
    monkeypatch.setattr(governance_graph, "build_governance_graph", lambda **hooks: hooks)
    receipt = {**valid, **({"unexpected": True} if value == "extra" else {
        "admitted" if operation == "authorize" else "verified": value})}
    services = type("Services", (), {"call": AsyncMock(return_value=receipt)})()
    hooks = adapter.build_graph({"external_effects_allowed": False, "services": services,
        "model": object(), "checkpointer": object()})
    callback = hooks["authorize" if operation == "authorize" else "reauthorize_evidence"]
    kwargs = {"assets": []} if operation == "authorize" else {"assets": [], "evidence": []}
    if value is True:
        asyncio.run(callback({}, **kwargs))
    else:
        with pytest.raises(PermissionError):
            asyncio.run(callback({}, **kwargs))


@pytest.mark.parametrize("code,expected", [("InvalidParameter", "InvalidParameter"), ("private-provider-text", "other")])
def test_model_error_metadata_never_includes_provider_body(code, expected):
    from oci.exceptions import ServiceError

    try:
        try:
            raise ServiceError(400, code, {"private-header": "secret"}, "private prompt and response")
        except ServiceError:
            raise RuntimeError("generic adapter error") from None
    except RuntimeError as error:
        assert adapter.model_failure_metadata(error) == {"complete": False,
            "failure_category": "oci_service_error", "http_status": 400, "service_code": expected}


def test_model_error_metadata_does_not_guess_authentication_from_untyped_error():
    assert adapter.model_failure_metadata(RuntimeError("private")) == {
        "complete": False, "failure_category": "model_adapter_or_transport", "cause_type": "unknown"}


def test_model_error_metadata_only_reports_allowlisted_cause_types():
    try:
        try:
            raise AttributeError("private attribute detail")
        except AttributeError:
            raise RuntimeError("private") from None
    except RuntimeError as error:
        assert adapter.model_failure_metadata(error) == {"complete": False,
            "failure_category": "model_adapter_or_transport", "cause_type": "AttributeError"}


@pytest.fixture
def local_model_transport(monkeypatch):
    import oci

    class OCIModel:
        client = None
        settings = {"region": "us-chicago-1", "read_timeout_seconds": 180}
        config_file = "protected-reference-only"
        profile = "DEFAULT"

    package = ModuleType("aidp_agent_flow")
    package.__path__ = []
    signer = Mock(return_value=object())
    monkeypatch.setitem(sys.modules, "aidp_agent_flow", package)
    monkeypatch.setitem(sys.modules, "aidp_agent_flow.model", SimpleNamespace(OCIModel=OCIModel))
    monkeypatch.setitem(sys.modules, "aidp_agent_flow.remote", SimpleNamespace(signer_for=signer))
    read_config = Mock(return_value={"region": "us-chicago-1"})
    client = Mock(return_value=object())
    monkeypatch.setattr(oci.config, "from_file", read_config)
    monkeypatch.setattr(oci.generative_ai_inference, "GenerativeAiInferenceClient", client)
    return OCIModel(), read_config, client, signer


def test_real_transport_retains_configured_identity_timeout_and_no_retry(local_model_transport):
    import oci

    model, read_config, client, signer = local_model_transport
    adapter.configure_model_transport(model)
    read_config.assert_called_once_with(model.config_file, model.profile)
    signer.assert_called_once_with(read_config.return_value)
    assert client.call_args.kwargs["timeout"] == (5, 180)
    assert client.call_args.kwargs["signer"] is signer.return_value
    assert isinstance(client.call_args.kwargs["retry_strategy"], oci.retry.NoneRetryStrategy)
    assert model.client is client.return_value
    adapter.configure_model_transport(model)
    client.assert_called_once()


@pytest.mark.parametrize("timeout", [True, 59, 301, "180"])
def test_invalid_transport_timeout_fails_before_credentials(local_model_transport, timeout):
    model, read_config, client, _ = local_model_transport
    model.settings = {**model.settings, "read_timeout_seconds": timeout}
    with pytest.raises(ValueError):
        adapter.configure_model_transport(model)
    read_config.assert_not_called()
    client.assert_not_called()


def test_fixture_transport_does_not_read_credentials(local_model_transport):
    _, read_config, client, _ = local_model_transport
    adapter.configure_model_transport(object())
    read_config.assert_not_called()
    client.assert_not_called()
