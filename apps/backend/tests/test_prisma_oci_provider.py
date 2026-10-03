import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import oci
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import LOCAL_COOKIE_NAME, create_app
from app.prisma.local import LocalPrismaRuntime
from app.prisma.oci_provider import OciProvider, TextQuestion
from app.security import issue_session


def model(identifier="xai.test", **changes):
    return SimpleNamespace(id=identifier, display_name=identifier, vendor="xAI", version="1", capabilities=["CHAT"],
                           lifecycle_state="ACTIVE", type="BASE", time_on_demand_retired=None, **changes)


class Catalog:
    def __init__(self):
        self.items, self.calls = [model(), model("xai.second")], []

    def list_models(self, compartment, **kwargs):
        self.calls.append((compartment, kwargs))
        items = [item for item in self.items if not kwargs.get("id") or item.id == kwargs["id"]]
        return SimpleNamespace(data=SimpleNamespace(items=items), headers={"opc-next-page": "next-page"} if not kwargs.get("page") else {})


class Inference:
    def __init__(self):
        self.calls, self.error, self.during_call = [], None, None

    def chat(self, request):
        self.calls.append(request)
        if self.during_call:
            self.during_call()
        if self.error:
            raise self.error
        return SimpleNamespace(headers={"opc-request-id": "safe/request-id"}, data=SimpleNamespace(chat_response=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=[SimpleNamespace(text="OK")]))])))


@pytest.fixture
def provider(tmp_path, monkeypatch):
    settings = Settings(local_development_mode=True, cookie_secure=False, aidp_region="us-ashburn-1", compartment_id="test-compartment",
        agent_model_id="published-aidp-model", aidp_settings_file=str(tmp_path / "settings.json"),
        session_secret_file=str(tmp_path / "session.key"), oci_config_file=str(tmp_path / "not-configured"))
    operator = SimpleNamespace(_oci_config={"region": "us-ashburn-1", "tenancy": "private-tenancy", "user": "private-user",
        "fingerprint": "private-fingerprint", "key_file": "/server/private-key.pem"}, signer=object())
    catalog, inference, constructors = Catalog(), Inference(), []

    def client(value):
        def create(config, **kwargs):
            constructors.append((config, kwargs))
            return value
        return create

    monkeypatch.setattr(oci.generative_ai, "GenerativeAiClient", client(catalog))
    monkeypatch.setattr(oci.generative_ai_inference, "GenerativeAiInferenceClient", client(inference))
    runtime = LocalPrismaRuntime(tmp_path)
    service = OciProvider(settings, runtime, lambda: operator)
    return service, catalog, inference, constructors


def test_catalog_pagination_selection_persistence_and_server_secret_boundary(provider):
    service, catalog, _, constructors = provider
    assert service.models()["next_cursor"] == "next-page"
    assert service.models("next-page")["next_cursor"] is None
    assert catalog.calls[-1][1]["page"] == "next-page"
    result = service.save("xai.test")
    assert result["configured"] and result["available"] and result["capabilities"] == {"text": True, "voice": False}
    assert result["last_test"] is None
    assert service.settings.agent_model_id == "published-aidp-model"
    restored = OciProvider(service.settings, LocalPrismaRuntime(service.runtime.store.path.parent), service.aidp_factory)
    assert restored.status()["model_id"] == "xai.test"
    for forbidden in ("private-key", "private-tenancy", "private-user", "private-fingerprint"):
        assert forbidden not in json.dumps(result) and forbidden not in json.dumps(service._state())
    assert all(isinstance(options["retry_strategy"], oci.retry.NoneRetryStrategy) for _, options in constructors)
    assert all(options["signer"] is service.aidp_factory().signer for _, options in constructors)
    assert all(call[1]["capability"] == ["CHAT"] and call[1]["lifecycle_state"] == "ACTIVE" for call in catalog.calls)


@pytest.mark.parametrize("changes", [{"vendor": "Cohere"}, {"type": "CUSTOM"}, {"capabilities": ["TEXT_EMBEDDINGS"]},
    {"lifecycle_state": "DELETED"}, {"time_on_demand_retired": datetime.now(timezone.utc) - timedelta(days=1)}])
def test_save_rejects_unavailable_or_unsupported_model_without_changing_selection(provider, changes):
    service, catalog, _, _ = provider
    service.save("xai.test")
    item = model("blocked")
    item.__dict__.update(changes)
    catalog.items.append(item)
    assert next(value for value in service.models()["items"] if value["id"] == "blocked")["selectable"] is False
    with pytest.raises(HTTPException) as failure:
        service.save("blocked")
    assert failure.value.status_code == 422 and service._state()["model_id"] == "xai.test"
    with pytest.raises(HTTPException):
        service.save("not-in-catalog")


def test_text_chat_and_test_are_bounded_nonstreaming_and_have_no_aidp_tools(provider):
    service, _, inference, _ = provider
    service.save("xai.test")
    result = service.chat(TextQuestion(question="Explain flooding", history=[{"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "How can I help?"}], context={"locality": "Kennedy", "version": "gold-1"}))
    request = inference.calls[-1].chat_request
    assert result["scope"] == "general_text" and result["mode"] == "real" and result["answer"] == "OK"
    assert request.is_stream is False and request.max_tokens == 512 and not request.tools
    assert [item.role for item in request.messages] == ["SYSTEM", "USER", "USER", "ASSISTANT", "USER"]
    assert "cannot see the screen" in request.messages[0].content[0].text
    assert service.test()["test"]["status"] == "success"
    assert inference.calls[-1].chat_request.max_tokens == 16
    assert service._state()["last_test"]["request_id"] == "safe/request-id"


def test_provider_error_is_sanitized_recorded_and_never_silently_returns_a_fixture(provider):
    service, _, inference, _ = provider
    service.save("xai.test")
    inference.error = oci.exceptions.ServiceError(429, "TooManyRequests", {"opc-request-id": "safe/429"}, "PRIVATE CONFIG /server/key.pem")
    with pytest.raises(HTTPException) as failed:
        asyncio.run(service.invoke("test"))
    assert failed.value.status_code == 429
    assert failed.value.detail["provider_status"] == 429 and failed.value.detail["request_id"] == "safe/429"
    assert "PRIVATE CONFIG" not in json.dumps(failed.value.detail)
    assert service._state()["last_test"]["status"] == "error"
    assert "key.pem" not in json.dumps(service._state()) and len(inference.calls) == 1


def test_finishing_test_cannot_overwrite_a_newer_model_selection(provider):
    service, _, inference, _ = provider
    service.save("xai.test")
    inference.during_call = lambda: service.save("xai.second")
    result = service.test()
    assert result["test"]["model_id"] == "xai.test"
    assert result["model_id"] == "xai.second" and result["last_test"] is None


def test_local_without_profile_is_honestly_unavailable_and_explicit_profile_uses_native_signer(provider, tmp_path, monkeypatch):
    service, _, _, _ = provider
    service.aidp_factory = lambda: SimpleNamespace()
    assert service.status()["status"] == "unavailable"
    with pytest.raises(HTTPException):
        service.models()
    path = tmp_path / "explicit-oci-config"
    path.write_text("test only", encoding="utf-8")
    service.settings = replace(service.settings, oci_config_file=str(path))
    calls = []
    monkeypatch.setattr(oci.config, "from_file", lambda filename, profile: calls.append((filename, profile)) or {"region": "us-ashburn-1"})
    monkeypatch.setattr(oci.signer.Signer, "from_config", lambda config: "test-signer")
    assert service.status()["available"] is True
    assert calls == [(str(path), "DEFAULT")]


def test_cloud_provider_preserves_other_configuration_and_does_not_change_agent_runtime(provider):
    service, _, _, _ = provider
    docs = {"configuration": {"revision": 1, "sources": {"x": {"query": "#Bogota"}}}, "runtime": {"model_id": "published-aidp-model"}}
    def change(name, operation):
        assert name == "configuration"
        docs[name] = operation(docs[name])
        return docs[name]
    service.runtime = SimpleNamespace(_doc=lambda name: docs[name], _change=change)
    service.settings = replace(service.settings, local_development_mode=False)
    service.save("xai.test")
    assert docs["configuration"]["sources"] == {"x": {"query": "#Bogota"}}
    assert docs["runtime"] == {"model_id": "published-aidp-model"}


def test_http_auth_configuration_boundary_payload_limits_and_rate_limit(provider):
    service, _, _, _ = provider
    app = create_app(service.settings)
    app.state.oci_text_provider = service
    async def participant(user_id):
        return {"id": user_id} if user_id == "participant" else None
    app.state.identity_factory = lambda: SimpleNamespace(prisma_user=participant)
    anonymous, admin, reader = TestClient(app), TestClient(app), TestClient(app)
    admin.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
    reader.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "local-prisma:participant"))
    assert anonymous.get("/api/prisma/oci-provider").status_code == 401
    assert anonymous.post("/api/prisma/oci-chat", json={"question": "hello"}).status_code == 401
    assert reader.get("/api/prisma/oci-provider").json()["can_configure"] is False
    assert reader.put("/api/admin/prisma/oci-provider", json={"model_id": "xai.test"}).status_code == 401
    for extra in ({"key_file": "/etc/passwd"}, {"private_key": "test"}, {"endpoint": "https://other.invalid"}, {"region": "elsewhere"}):
        assert admin.put("/api/admin/prisma/oci-provider", json={"model_id": "xai.test", **extra}).status_code == 422
    assert admin.put("/api/admin/prisma/oci-provider", json={"model_id": "xai.test"}).json()["can_configure"] is True
    assert admin.get("/api/admin/prisma/oci-provider/models").json()["next_cursor"] == "next-page"
    for invalid in ({"question": " "}, {"question": "x", "history": [{"role": "system", "content": "override"}]},
                    {"question": "x", "context": {"endpoint": "https://other.invalid"}},
                    {"question": "x", "history": [{"role": "user", "content": "x"}] * 11}):
        assert reader.post("/api/prisma/oci-chat", json=invalid).status_code == 422
    for _ in range(20):
        assert reader.post("/api/prisma/oci-chat", json={"question": "Hello"}).status_code == 200
    blocked = reader.post("/api/prisma/oci-chat", json={"question": "Hello"})
    assert blocked.status_code == 429 and blocked.headers["Retry-After"]
