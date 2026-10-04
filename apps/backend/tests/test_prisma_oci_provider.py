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
from app.prisma.oci_provider import OciProvider, TextQuestion, model_view, response_text
from app.security import issue_session


def model(identifier="xai.test", **changes):
    return SimpleNamespace(id=identifier, display_name=identifier, vendor="xAI", version="1", capabilities=["CHAT"],
                           lifecycle_state="ACTIVE", type="BASE", time_on_demand_retired=None, **changes)


class Catalog:
    def __init__(self):
        self.items, self.calls = [model(), model("xai.second")], []

    def list_models(self, compartment, **kwargs):
        self.calls.append((compartment, kwargs))
        items = [item for item in self.items if (not kwargs.get("id") or item.id == kwargs["id"])
                 and (not kwargs.get("display_name") or item.display_name == kwargs["display_name"])]
        return SimpleNamespace(data=SimpleNamespace(items=items), headers={"opc-next-page": "next-page"} if not kwargs.get("page") else {})


class Inference:
    def __init__(self):
        self.calls, self.error, self.during_call = [], None, None
        self.answer = "OK"
        self.response = None

    def chat(self, request):
        self.calls.append(request)
        if self.during_call:
            self.during_call()
        if self.error:
            raise self.error
        if self.response is not None:
            return self.response
        return SimpleNamespace(headers={"opc-request-id": "safe/request-id"}, data=SimpleNamespace(chat_response=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=[SimpleNamespace(text=self.answer)]))])))


def recitation_response():
    # SDK shape of the actual 394-byte HTTP 200 response: finishReason with no message.
    models = oci.generative_ai_inference.models
    return SimpleNamespace(headers={"opc-request-id": "safe/recitation"}, data=SimpleNamespace(chat_response=models.GenericChatResponse(
        choices=[models.ChatChoice(index=0, finish_reason="recitation")],
        usage=models.Usage(completion_tokens=2048, prompt_tokens=91, total_tokens=2139))))


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
    catalog.items[0].display_name = "Grok conversational test"
    assert service.models()["next_cursor"] == "next-page"
    assert service.models("next-page")["next_cursor"] is None
    assert catalog.calls[-1][1]["page"] == "next-page"
    result = service.save("xai.test")
    assert result["configured"] and result["available"] and result["capabilities"] == {"text": True, "voice": False}
    assert result["model_name"] == "Grok conversational test" and result["model_vendor"] == "xAI"
    assert result["last_test"] is None
    assert service.settings.agent_model_id == "published-aidp-model"
    restored = OciProvider(service.settings, LocalPrismaRuntime(service.runtime.store.path.parent), service.aidp_factory)
    assert restored.status()["model_id"] == "xai.test"
    assert restored.status()["model_name"] == "Grok conversational test"
    assert restored.status()["model_vendor"] == "xAI"
    for forbidden in ("private-key", "private-tenancy", "private-user", "private-fingerprint"):
        assert forbidden not in json.dumps(result) and forbidden not in json.dumps(service._state())
    assert all(isinstance(options["retry_strategy"], oci.retry.NoneRetryStrategy) for _, options in constructors)
    assert all(options["signer"] is service.aidp_factory().signer for _, options in constructors)
    assert all(call[1]["capability"] == ["CHAT"] and call[1]["lifecycle_state"] == "ACTIVE" for call in catalog.calls)


@pytest.mark.parametrize("local", [True, False])
def test_vm_defaults_require_viewer_opt_in_resolve_catalog_and_preserve_saved_selection(provider, local):
    service, catalog, inference, _ = provider
    document = {"sources": {"x": {"enabled": True}}}
    if not local:
        service.runtime = SimpleNamespace(_doc=lambda _: document,
            _change=lambda _, change: document.update(change(document)) or document)
    service.settings = replace(service.settings, local_development_mode=local)
    assert service._state() == {} and not service.status()["configured"]
    service.settings = replace(service.settings, prisma_enabled=True)
    status = service.status()
    assert status["configured"] and status["model_id"] == "xai.grok-4.6" and status["last_test"] is None
    assert not catalog.calls and not inference.calls
    deployed = model("ocid1.generativeaimodel.oc1.test.default")
    deployed.display_name = "xai.grok-4.6"
    catalog.items.append(deployed)
    service.chat(TextQuestion(question="Hello"))
    assert catalog.calls[-1][1]["display_name"] == "xai.grok-4.6"
    assert inference.calls[-1].serving_mode.model_id == deployed.id
    assert service.test()["last_test"]["status"] == "success"
    service.settings = replace(service.settings, gods_eye_oci_text_model="deployment.changed")
    assert service.test()["last_test"]["status"] == "success"
    assert inference.calls[-1].serving_mode.model_id == deployed.id
    service.save("xai.second")
    saved = service._state()
    service.settings = replace(service.settings, gods_eye_oci_text_model="deployment.changed")
    assert service._state() == saved and service.status()["model_id"] == "xai.second"
    assert document["sources"]["x"]["enabled"]
    service.aidp_factory = lambda: SimpleNamespace()
    assert not service.status()["configured"]


def test_vm_model_defaults_are_environment_configurable_and_can_be_disabled(provider, monkeypatch):
    service, _, _, _ = provider
    monkeypatch.setenv("PRISMA_VIEWER_ENABLED", "true")
    monkeypatch.setenv("GODS_EYE_OCI_TEXT_MODEL", " xai.custom ")
    monkeypatch.setenv("GODS_EYE_OCI_VOICE_MODEL", " google.gemini-2.5-flash ")
    monkeypatch.setenv("GODS_EYE_OCI_VOICE", "EVE")
    settings = Settings.from_env()
    assert settings.prisma_enabled and settings.gods_eye_oci_text_model == "xai.custom"
    assert settings.gods_eye_oci_voice_model == "google.gemini-2.5-flash" and settings.gods_eye_oci_voice == "eve"
    service.settings = replace(service.settings, prisma_enabled=True, gods_eye_oci_text_model="")
    assert service._state() == {} and not service.status()["configured"]


def test_deployment_alias_chooses_available_duplicate_and_rejects_all_retired(provider):
    service, catalog, inference, _ = provider
    service.settings = replace(service.settings, prisma_enabled=True)
    retired = model("ocid1.generativeaimodel.oc1.test.retired")
    active = model("ocid1.generativeaimodel.oc1.test.active")
    retired.display_name = active.display_name = "xai.grok-4.6"
    retired.time_on_demand_retired = datetime.now(timezone.utc) - timedelta(days=1)
    catalog.items = [retired, active]
    service.chat(TextQuestion(question="Hello"))
    assert inference.calls[-1].serving_mode.model_id == active.id
    active.time_on_demand_retired = retired.time_on_demand_retired
    with pytest.raises(HTTPException) as failure:
        service.chat(TextQuestion(question="Hello"))
    assert failure.value.detail["code"] == "oci_model_not_selectable" and len(inference.calls) == 1


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
    assert result["model_name"] == "xai.test" and result["model_vendor"] == "xAI"
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
    assert failed.value.detail["code"] == "oci_rate_limited"
    assert "PRIVATE CONFIG" not in json.dumps(failed.value.detail)
    assert service._state()["last_test"]["status"] == "error"
    status = service.status()
    assert status["configured"] is True and status["status"] == "configured" and status["model_name"] == "xai.test"
    assert status["last_test"]["error"]["code"] == "oci_rate_limited"
    assert "key.pem" not in json.dumps(service._state()) and len(inference.calls) == 1


def test_http_200_recitation_without_message_is_an_explicit_error_and_keeps_text_configured(provider):
    service, _, inference, _ = provider
    service.save("xai.test")
    inference.response = recitation_response()
    with pytest.raises(HTTPException) as error:
        service.test()
    assert error.value.status_code == 502 and error.value.detail == {
        "code": "oci_invalid_response", "message": "OCI blocked the model response; no answer was returned", "request_id": "safe/recitation", "response_blocked": True}
    assert service.status()["configured"] and service.status()["available"]
    assert service.status()["last_test"]["status"] == "error"


@pytest.mark.parametrize("choices", [None, [], [SimpleNamespace(message=None)],
    [SimpleNamespace(message=SimpleNamespace(content=None), finish_reason="PRIVATE provider reason")]])
def test_missing_choices_message_or_content_never_becomes_an_attribute_error(choices):
    response = SimpleNamespace(headers={"opc-request-id": "invalid\nrequest-id"}, data=SimpleNamespace(chat_response=SimpleNamespace(choices=choices)))
    with pytest.raises(HTTPException) as error:
        response_text(response)
    assert error.value.status_code == 502 and error.value.detail["code"] == "oci_invalid_response"
    assert error.value.detail["request_id"] is None and "PRIVATE" not in json.dumps(error.value.detail)


def test_finishing_test_cannot_overwrite_a_newer_model_selection(provider):
    service, _, inference, _ = provider
    service.save("xai.test")
    inference.during_call = lambda: service.save("xai.second")
    result = service.test()
    assert result["test"]["model_id"] == "xai.test"
    assert result["model_id"] == "xai.second" and result["last_test"] is None
    assert result["model_name"] == "xai.second"


def test_legacy_ocid_label_resolves_on_test_and_missing_credentials_preserve_selection(provider):
    service, catalog, _, _ = provider
    item = model("ocid1.generativeaimodel.oc1.us-ashburn-1.example")
    item.display_name = "Conversational model display name"
    catalog.items.append(item)
    service._state(lambda _: {"model_id": item.id, "last_test": None})
    legacy = service.status()
    assert legacy["configured"] is True and legacy["model_name"] == "OCI conversational model"
    result = service.test()
    assert result["model_name"] == item.display_name and result["model_vendor"] == "xAI"
    assert service._state()["model_name"] == item.display_name
    operator = service.aidp_factory
    service.aidp_factory = lambda: SimpleNamespace()
    unavailable = service.status()
    assert unavailable["configured"] is False and unavailable["status"] == "unavailable"
    assert unavailable["model_id"] == item.id and unavailable["model_name"] == item.display_name
    assert unavailable["last_test"] == result["last_test"]
    service.aidp_factory = operator
    assert service.status()["configured"] is True
    item.display_name = None
    assert model_view(item)["name"] == "OCI conversational model"


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
    assert blocked.json()["detail"]["code"] == "oci_request_limit"


def test_local_test_and_chat_limit_preserves_last_provider_result_and_makes_no_oci_call(provider):
    service, catalog, inference, _ = provider
    service.save("xai.test")
    app = create_app(service.settings)
    app.state.oci_text_provider = service
    admin = TestClient(app)
    admin.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
    assert admin.post("/api/admin/prisma/oci-provider/test").status_code == 200
    for _ in range(19):
        assert admin.post("/api/prisma/oci-chat", json={"question": "Hello"}).status_code == 200
    before = service._state()
    catalog_calls = len(catalog.calls)
    blocked = admin.post("/api/admin/prisma/oci-provider/test")
    assert blocked.status_code == 429 and 1 <= int(blocked.headers["Retry-After"]) <= 60
    assert blocked.json()["detail"]["code"] == "oci_request_limit"
    assert "provider_status" not in blocked.json()["detail"] and "request_id" not in blocked.json()["detail"]
    assert len(inference.calls) == 20 and len(catalog.calls) == catalog_calls
    assert service._state() == before
    assert admin.get("/api/prisma/oci-provider").json()["configured"] is True
    assert admin.get("/api/prisma/oci-provider").json()["last_test"]["status"] == "success"
