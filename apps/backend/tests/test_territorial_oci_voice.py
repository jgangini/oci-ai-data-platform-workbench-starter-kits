import asyncio
import base64
from dataclasses import replace
import io
import json
from types import SimpleNamespace
import wave

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import oci
import pytest

from app.main import LOCAL_COOKIE_NAME, create_app
from app.territorial.oci_voice import AUDIO_LIMIT, OciVoice, VoiceTurn
from app.security import issue_session
from test_territorial_oci_provider import model, provider, recitation_response  # Reuse the isolated OCI SDK fakes.


def wav(seconds=.1, rate=16000, channels=1):
    result = io.BytesIO()
    with wave.open(result, "wb") as audio:
        audio.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * int(rate * seconds) * channels)
    return result.getvalue()


def payload(data=None):
    return {"audio_base64": base64.b64encode(wav() if data is None else data).decode(), "mime": "audio/wav"}


@pytest.fixture
def voice(provider, monkeypatch):
    text, catalog, inference, _ = provider
    item = model("ocid1.generativeaimodel.oc1.test.audio")
    item.display_name, item.vendor = "google.gemini-2.5-flash-lite", "Google"
    catalog.items.append(item)
    service = OciVoice(text.settings, text.runtime, text.aidp_factory)
    service.save(item.id, "eve")
    inference.answer = json.dumps({"transcript": "Muestra Bogotá", "answer": "Voy a mostrar Bogotá.",
        "actions": [{"name": "fly_to_location", "arguments": {"latitude": 4.6, "longitude": -74.1, "rangeM": 15000}}]})
    speech_calls = []
    monkeypatch.setattr(service, "_speak", lambda answer, chosen: speech_calls.append((answer, chosen)) or
                        {"audio_base64": payload()["audio_base64"], "mime": "audio/wav", "tts_request_id": "safe/tts"})
    return service, text, catalog, inference, speech_calls


async def connected():
    return False


def test_admin_bodyless_voice_test_checks_tts_audio_model_and_reply_without_microphone(voice):
    service, _, _, inference, speech_calls = voice
    app = create_app(service.settings)
    app.state.oci_voice_provider = service
    admin = TestClient(app)
    admin.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
    result = admin.post("/api/admin/territorial/oci-voice/test")
    assert result.status_code == 200 and result.json()["test"]["status"] == "success"
    assert len(inference.calls) == 1 and len(speech_calls) == 2
    assert speech_calls[0] == ("Hello. This is an OCI voice connection test.", "eve")
    assert service.status()["last_test"]["status"] == "success"


def test_bodyless_voice_test_records_tts_failure_without_calling_audio_model(voice, monkeypatch):
    service, _, _, inference, _ = voice
    def fail(*_):
        raise oci.exceptions.ServiceError(429, "Limit", {}, "PRIVATE raw error")
    monkeypatch.setattr(service, "_speak", fail)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.turn(None, connected, test=True))
    assert error.value.status_code == 429 and not inference.calls
    assert service.status()["last_test"]["error"]["code"] == "oci_rate_limited"
    assert "PRIVATE" not in json.dumps(service.status())


def test_bodyless_voice_test_requires_model_before_any_speech_request(voice):
    service, _, _, inference, speech_calls = voice
    service._state(lambda _: {"voice": "eve"})
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.turn(None, connected, test=True))
    assert error.value.status_code == 409 and error.value.detail["code"] == "oci_model_required"
    assert not inference.calls and not speech_calls


def test_vm_audio_default_resolves_model_without_overwriting_saved_voice(provider):
    text, catalog, inference, _ = provider
    settings = replace(text.settings, territorial_enabled=True)
    service = OciVoice(settings, text.runtime, text.aidp_factory)
    state = service.status()
    assert state["configured"] and state["model_id"] == "google.gemini-2.5-flash-lite"
    assert state["voice"] == "ara" and state["last_test"] is None and not inference.calls and not catalog.calls
    item = model("ocid1.generativeaimodel.oc1.test.audio")
    item.display_name, item.vendor = "google.gemini-2.5-flash-lite", "Google"
    catalog.items.append(item)
    assert service._selected_model(state["model_id"])["id"] == item.id
    service.save(item.id, "sal")
    service.settings = replace(settings, gods_eye_oci_voice_model="google.gemini-2.5-flash", gods_eye_oci_voice="eve")
    assert service.status()["voice"] == "sal" and service.status()["model_id"] == item.id
    service.aidp_factory = lambda: SimpleNamespace()
    assert not service.status()["configured"]


def test_audio_catalog_and_persistent_selection_are_separate_from_text(voice):
    service, text, _, _, _ = voice
    text.save("xai.test")
    result = service.models()
    assert [item["name"] for item in result["items"]] == ["google.gemini-2.5-flash-lite"]
    assert result["next_cursor"] == "next-page"
    status = OciVoice(service.settings, service.runtime, service.aidp_factory).status()
    assert status["configured"] and status["voice"] == "eve" and status["model_vendor"] == "Google"
    assert status["tts_model"] == "xai.grok-tts" and status["capabilities"]["realtime"] is False
    assert text.status()["model_id"] == "xai.test" and service.settings.agent_model_id == "published-aidp-model"
    with pytest.raises(HTTPException) as error:
        service.save("xai.test")
    assert error.value.detail["code"] == "oci_voice_model_not_selectable"
    assert service.status()["voice"] == "eve"


def test_grok_voice_from_catalog_is_visible_but_cannot_be_saved_until_realtime_is_verified(voice):
    service, _, catalog, _, _ = voice
    item = model("ocid1.generativeaimodel.oc1.test.realtime")
    item.display_name = "xai.grok-voice-agent"
    catalog.items.append(item)
    result = service.models()["items"]
    assert len(result) == 2 and result[0]["selectable"] is True
    assert result[1]["id"] == item.id and result[1]["selectable"] is False
    assert result[1]["reason"] == "Realtime connection not verified"
    with pytest.raises(HTTPException) as error:
        service.save(item.id)
    assert error.value.status_code == 422 and error.value.detail["code"] == "oci_voice_model_not_selectable"
    assert service.status()["model_id"] != item.id


@pytest.mark.parametrize("invalid", ["not base64", "", base64.b64encode(b"RIFFprivate unsupported data").decode(),
    base64.b64encode(wav(30.01)).decode(), base64.b64encode(wav(channels=2)).decode(),
    base64.b64encode(wav()[:-2]).decode(), "A" * (4 * ((AUDIO_LIMIT + 2) // 3) + 1)],
    ids=["base64", "empty", "format", "duration", "channels", "truncated", "size"])
def test_untrusted_audio_rejected_without_echoing_content_or_calling_sdk(voice, invalid):
    _, _, _, inference, speech_calls = voice
    with pytest.raises(HTTPException) as error:
        VoiceTurn(audio_base64=invalid)
    assert error.value.status_code == 422 and error.value.detail["code"] == "oci_invalid_audio"
    assert "private unsupported data" not in json.dumps(error.value.detail)
    assert not inference.calls and not speech_calls


def test_real_turn_contract_uses_inline_audio_and_bounded_native_actions(voice):
    service, _, _, inference, speech_calls = voice
    result = asyncio.run(service.turn(VoiceTurn(**payload()), connected, test=True))
    request = inference.calls[0].chat_request
    assert request.is_stream is False and request.max_tokens == 2048 and request.response_format.type == "JSON_SCHEMA"
    schema = request.response_format.json_schema.schema
    assert request.response_format.json_schema.name == "gods_eye_voice_reply"
    assert schema["required"] == ["transcript", "answer", "actions"]
    variants = schema["properties"]["actions"]["items"]["anyOf"]
    assert [variant["properties"]["name"]["enum"] for variant in variants] == [
        ["fly_to_location"], ["adjust_camera_zoom"], ["zoom_to_globe"], ["set_layer_visibility"], ["ask_aidp"],
    ]
    arguments = variants[3]["properties"]["arguments"]
    assert arguments["required"] == ["layerId", "enabled"]
    assert set(arguments["properties"]) == {"layerId", "enabled"} and arguments["additionalProperties"] is False
    assert arguments["properties"]["layerId"]["enum"] == [
        "flights", "military", "earthquakes", "satellites", "rocket-launches", "traffic", "cctv", "radio", "bikeshare",
        "ais-live-vessels", "local-datacenters", "local-dams", "telegeography-submarine-cables", "local-firms",
        "fire-perimeters", "alpr-cameras", "local-adsb", "territorial-events", "sensors",
    ]
    assert arguments["properties"]["enabled"]["type"] == "boolean"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["transcript"] == {"type": "string", "minLength": 1, "maxLength": 2000}
    assert schema["properties"]["answer"] == {"type": "string", "minLength": 1, "maxLength": 600}
    assert schema["properties"]["actions"]["maxItems"] == 4
    assert all(key not in json.dumps(schema) for key in ('"$defs"', '"$ref"', '"const"'))
    assert '"properties"' not in request.messages[0].content[0].text
    instruction, content = request.messages[-1].content
    assert instruction.type == "TEXT" and "Transcribe this audio" in instruction.text
    assert content.type == "AUDIO" and content.audio_url.url.startswith("data:audio/wav;base64,")
    assert base64.b64decode(content.audio_url.url.split(",", 1)[1]) == wav()
    assert result["mode"] == "real" and result["scope"] == "general_voice" and result["mime"] == "audio/wav"
    assert result["model_name"] == "google.gemini-2.5-flash-lite" and result["voice"] == "eve"
    assert result["actions"][0]["name"] == "fly_to_location" and speech_calls == [(result["answer"], "eve")]
    assert service.status()["last_test"]["status"] == "success"
    assert "audio_base64" not in json.dumps(service._state()) and "transcript" not in json.dumps(service._state())


@pytest.mark.parametrize("name,arguments,required,allowed", [
    ("fly_to_location", {"latitude": 4.6, "longitude": -74.1}, {"latitude", "longitude"}, {"latitude", "longitude", "viewMode", "rangeM"}),
    ("adjust_camera_zoom", {"direction": "in", "amount": "little"}, {"direction", "amount"}, {"direction", "amount"}),
    ("zoom_to_globe", {}, set(), set()),
    ("set_layer_visibility", {"layerId": "territorial-events", "enabled": True}, {"layerId", "enabled"}, {"layerId", "enabled"}),
    ("ask_aidp", {"question": "Compare Synthetic sensor readings with social evidence."}, {"question"}, {"question"}),
])
def test_each_action_has_its_own_required_and_allowed_arguments(voice, name, arguments, required, allowed):
    service, _, _, inference, _ = voice
    inference.answer = json.dumps({"transcript": "Please change the map", "answer": "I'll request that change.",
                                  "actions": [{"name": name, "arguments": arguments}]})
    result = asyncio.run(service.turn(VoiceTurn(**payload()), connected))
    variants = inference.calls[0].chat_request.response_format.json_schema.schema["properties"]["actions"]["items"]["anyOf"]
    variant = next(item for item in variants if item["properties"]["name"]["enum"] == [name])
    assert variant["required"] == ["name", "arguments"] and variant["additionalProperties"] is False
    schema = variant["properties"]["arguments"]
    assert set(schema.get("required", [])) == required
    assert set(schema["properties"]) == allowed and schema["additionalProperties"] is False
    assert result["actions"][0]["name"] == name
    assert arguments.items() <= result["actions"][0]["arguments"].items()


@pytest.mark.parametrize("transcript,layer_id,enabled", [
    ("Habilitar capa de datos de redes sociales", "territorial-events", True),
    ("Oculta redes sociales", "territorial-events", False),
    ("Show the earthquakes layer", "earthquakes", True),
    ("Disable satellites", "satellites", False),
    ("Habilita sensores", "sensors", True),
])
def test_valid_model_layer_commands_reach_client_unchanged(voice, transcript, layer_id, enabled):
    service, _, _, inference, speech_calls = voice
    action = {"name": "set_layer_visibility", "arguments": {"layerId": layer_id, "enabled": enabled}}
    inference.answer = json.dumps({"transcript": transcript, "answer": "I'll request that layer change.", "actions": [action]})
    result = asyncio.run(service.turn(VoiceTurn(**payload()), connected))
    assert result["actions"] == [action] and result["actions"][0]["arguments"]["enabled"] is enabled
    assert speech_calls == [(result["answer"], "eve")]
    prompt = inference.calls[0].chat_request.messages[0].content[0].text
    assert "you MUST return set_layer_visibility" in prompt
    assert "'Social networks', 'redes sociales'" in prompt and "all mean territorial-events" in prompt
    assert "Distinguish showing a place" in prompt and "never claim you executed an action" in prompt


@pytest.mark.parametrize("transcript", ["¿Qué capas están disponibles?", "Me interesa la información de redes sociales."])
def test_noncommand_model_reply_preserves_empty_actions(voice, transcript):
    service, _, _, inference, _ = voice
    inference.answer = json.dumps({"transcript": transcript, "answer": "You can explore the Social networks layer.", "actions": []})
    result = asyncio.run(service.turn(VoiceTurn(**payload()), connected))
    assert result["actions"] == []
    prompt = inference.calls[0].chat_request.messages[0].content[0].text
    assert "available layers or how to use a layer" in prompt
    assert "statements that do not request a change, have no actions" in prompt


@pytest.mark.parametrize("actions", [[{"name": "fetch_url", "arguments": {"url": "https://other.invalid"}}],
    [{"name": "fly_to_location", "arguments": {"latitude": 91, "longitude": 0}}],
    [{"name": "fly_to_location", "arguments": {"latitude": 4, "longitude": -74, "query": "private URL"}}],
    [{"name": "set_layer_visibility", "arguments": {"layerId": "unknown", "enabled": True}}],
    [{"name": "set_layer_visibility", "arguments": {"layerId": "territorial-events", "enabled": "true"}}],
    [{"name": "set_layer_visibility", "arguments": {"layerId": "territorial-events", "enabled": "false"}}],
    [{"name": "set_layer_visibility", "arguments": {"layerId": "flights", "enabled": 1}}],
    [{"name": "set_layer_visibility", "arguments": {"layerId": "flights"}}],
    [{"name": "set_layer_visibility", "arguments": {"layerId": "territorial-events", "viewMode": "overview"}}],
    [{"name": "set_layer_visibility", "arguments": {"layerId": "territorial-events", "enabled": True, "viewMode": "overview"}}],
    [{"name": "set_layer_visibility", "arguments": {"layerId": "flights", "enabled": True, "url": "private URL"}}],
    [{"name": "set_layer_visibility", "arguments": {"layerId": "flights", "enabled": True}, "extra": "private value"}],
    [{"name": "ask_aidp", "arguments": {"question": " "}}],
    [{"name": "ask_aidp", "arguments": {"question": "x" * 2001}}],
    [{"name": "ask_aidp", "arguments": {"question": "Validate it", "validate": True}}],
    [{"name": "zoom_to_globe", "arguments": {}}] * 5])
def test_invalid_model_actions_fail_closed_before_speech(voice, actions):
    service, _, _, inference, speech_calls = voice
    inference.answer = json.dumps({"transcript": "hello", "answer": "hello", "actions": actions})
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.turn(VoiceTurn(**payload()), connected))
    assert error.value.status_code == 502 and error.value.detail["code"] == "oci_invalid_voice_response"
    assert error.value.detail["voice_stage"] == "analysis_schema" and error.value.detail["voice_reason"] == "invalid_schema"
    assert error.value.detail["request_id"] == "safe/request-id"
    assert not speech_calls


def test_compound_request_preserves_map_actions_and_delegates_sensor_corroboration(voice):
    service, _, _, inference, _ = voice
    actions = [
        {"name": "set_layer_visibility", "arguments": {"layerId": "sensors", "enabled": True}},
        {"name": "set_layer_visibility", "arguments": {"layerId": "territorial-events", "enabled": True}},
        {"name": "adjust_camera_zoom", "arguments": {"direction": "in", "amount": "little"}},
        {"name": "ask_aidp", "arguments": {"question": "Compare sensor readings and social reports in Kennedy."}},
    ]
    inference.answer = json.dumps({"transcript": "Activa sensores y redes sociales, acerca el mapa y compara la evidencia.",
                                  "answer": "Consultaré Agent Flow.", "actions": actions})
    result = asyncio.run(service.turn(VoiceTurn(**payload()), connected))
    assert result["actions"] == actions
    prompt = inference.calls[0].chat_request.messages[0].content[0].text
    assert "ask_aidp last" in prompt and "never real observations" in prompt
    assert "does not confirm an incident or replace human review" in prompt


@pytest.mark.parametrize("answer,reason", [('{"transcript":"PRIVATE incomplete', "invalid_json"),
    (json.dumps({"answer": "PRIVATE missing transcript"}), "invalid_schema"),
    (json.dumps({"transcript": "hello", "answer": "x" * 601, "actions": []}), "invalid_schema"),
    ("", "empty_or_oversized"), ("x" * 12001, "empty_or_oversized")])
def test_malformed_reply_has_safe_stage_and_request_id_without_replaying_audio(voice, answer, reason):
    service, _, _, inference, speech_calls = voice
    inference.answer = answer
    with pytest.raises(HTTPException) as failure:
        asyncio.run(service.turn(VoiceTurn(**payload()), connected, test=True))
    detail = failure.value.detail
    assert detail["voice_reason"] == reason and detail["request_id"] == "safe/request-id"
    assert detail["voice_stage"] == ("analysis_response" if reason == "empty_or_oversized" else "analysis_schema")
    assert "PRIVATE" not in json.dumps(detail) and "audio_base64" not in json.dumps(detail)
    assert len(inference.calls) == 1 and not speech_calls
    assert service.status()["last_test"]["error"] == detail


def test_follow_up_keeps_bounded_history_and_rejects_malformed_third_turn_before_tts(voice):
    service, _, _, inference, speech_calls = voice
    history = []
    for _ in range(2):
        result = asyncio.run(service.turn(VoiceTurn(**payload(), history=history), connected))
        history += [{"role": "user", "content": result["transcript"]}, {"role": "assistant", "content": result["answer"]}]
    inference.answer = json.dumps({"transcript": "continue", "answer": "PRIVATE invalid follow-up", "actions": None})
    with pytest.raises(HTTPException) as failure:
        asyncio.run(service.turn(VoiceTurn(**payload(), history=history), connected))
    request = inference.calls[-1].chat_request
    assert [message.role for message in request.messages] == ["SYSTEM", "USER", "ASSISTANT", "USER", "ASSISTANT", "USER"]
    assert [message.content[0].text for message in request.messages[1:-1]] == [message["content"] for message in history]
    assert [part.type for part in request.messages[-1].content] == ["TEXT", "AUDIO"]
    assert request.response_format.type == "JSON_SCHEMA"
    assert failure.value.detail["voice_reason"] == "invalid_schema"
    assert len(inference.calls) == 3 and len(speech_calls) == 2
    assert "transcript" not in json.dumps(service._state()) and "history" not in json.dumps(service._state())


def test_provider_429_is_safe_persisted_and_does_not_unconfigure_voice(voice):
    service, _, _, inference, speech_calls = voice
    inference.error = oci.exceptions.ServiceError(429, "TooManyRequests", {"opc-request-id": "safe/429"}, "PRIVATE KEY")
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.turn(VoiceTurn(**payload()), connected, test=True))
    assert error.value.detail["code"] == "oci_rate_limited" and error.value.detail["request_id"] == "safe/429"
    state = service.status()
    assert state["configured"] and state["last_test"]["status"] == "error" and not speech_calls
    assert "PRIVATE KEY" not in json.dumps(state)


def test_http_200_recitation_without_message_never_calls_speech_or_unconfigures_voice(voice):
    service, _, _, inference, speech_calls = voice
    inference.response = recitation_response()
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.turn(VoiceTurn(**payload()), connected, test=True))
    assert error.value.status_code == 502 and error.value.detail == {
        "code": "oci_invalid_voice_response", "message": "OCI blocked the model response; no answer was returned", "request_id": "safe/recitation", "response_blocked": True,
        "voice_stage": "analysis_response", "voice_reason": "blocked"}
    state = service.status()
    assert state["configured"] and state["available"] and state["last_test"]["status"] == "error"
    assert not speech_calls


def test_disconnect_between_providers_prevents_tts_and_preserves_last_test(voice):
    service, _, _, inference, speech_calls = voice
    checks = []
    async def disconnected():
        checks.append(True)
        return len(checks) > 1
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.turn(VoiceTurn(**payload()), disconnected, test=True))
    assert len(inference.calls) == 1 and not speech_calls and service.status()["last_test"] is None


def test_concurrent_voice_selection_cannot_be_overwritten_by_finishing_test(voice):
    service, _, _, inference, _ = voice
    inference.during_call = lambda: service.save(service._state()["model_id"], "sal")
    response = asyncio.run(service.turn(VoiceTurn(**payload()), connected, test=True))
    assert response["voice"] == "eve" and response["test"]["status"] == "success"
    assert service.status()["voice"] == "sal" and service.status()["last_test"] is None


def test_signed_tts_uses_only_fixed_oci_url_and_normalizes_streaming_wav(voice, monkeypatch):
    service, _, _, _, _ = voice
    operator = service.aidp_factory()
    operator.signer = lambda prepared: prepared.headers.update({"Authorization": "test-only-signed"})
    calls, options = [], []
    data = bytearray(wav(rate=24000))
    data[4:8] = data[40:44] = b"\xff" * 4
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=bytes(data), headers={"opc-request-id": "safe/speech"})
    original = httpx.Client
    def client(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, "Client", client)
    result = OciVoice._speak(service, "Muestra Bogotá.", "ara")
    assert str(calls[0].url) == "https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com/openai/v1/audio/speech"
    assert calls[0].headers["Authorization"] == "test-only-signed" and calls[0].headers["CompartmentId"] == "test-compartment"
    assert json.loads(calls[0].content)["model"] == "xai.grok-tts" and len(calls) == 1
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert base64.b64decode(result["audio_base64"]) == wav(rate=24000)
    assert result["tts_request_id"] == "safe/speech"


@pytest.mark.parametrize("status,body,code,reason", [(429, b"PRIVATE provider error body", "oci_rate_limited", None),
    (200, b"not wav", "oci_invalid_voice_response", "invalid_audio"),
    (200, b"x" * (4 * 1024 * 1024 + 1), "oci_invalid_voice_response", "audio_too_large")], ids=["remote-limit", "invalid-audio", "oversized-audio"])
def test_tts_failure_is_safe_and_never_returns_false_audio_success(voice, monkeypatch, status, body, code, reason):
    service, _, _, _, _ = voice
    service.aidp_factory().signer = lambda prepared: None
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=body, headers={"opc-request-id": "safe/tts-failure"})
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(HTTPException) as error:
        OciVoice._speak(service, "test", "ara")
    assert error.value.detail["code"] == code and len(calls) == 1
    assert error.value.detail["request_id"] == "safe/tts-failure"
    if reason:
        assert error.value.detail["voice_stage"] == "speech_response" and error.value.detail["voice_reason"] == reason
    assert "PRIVATE" not in json.dumps(error.value.detail)


def test_http_auth_and_shared_cost_limit_do_not_call_oci_for_blocked_voice(voice):
    service, text, _, inference, speech_calls = voice
    app = create_app(service.settings)
    app.state.oci_voice_provider, app.state.oci_text_provider = service, text
    async def participant(user_id):
        return {"id": user_id} if user_id == "participant" else None
    app.state.identity_factory = lambda: SimpleNamespace(territorial_user=participant)
    anonymous, admin, reader = TestClient(app), TestClient(app), TestClient(app)
    admin.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
    reader.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "local-prisma:participant"))
    assert anonymous.post("/api/territorial/oci-voice/turn", json=payload()).status_code == 401
    assert reader.get("/api/territorial/oci-voice").json()["can_configure"] is False
    assert reader.put("/api/admin/territorial/oci-voice", json={"model_id": "xai.test"}).status_code == 401
    for extra in ({"endpoint": "https://other.invalid"}, {"private_key": "x"}, {"voice": "other"}):
        assert admin.put("/api/admin/territorial/oci-voice", json={"model_id": service._state()["model_id"], **extra}).status_code == 422
    for _ in range(20):
        assert reader.post("/api/territorial/oci-voice/turn", json=payload()).status_code == 200
    blocked = reader.post("/api/territorial/oci-voice/turn", json=payload())
    assert blocked.status_code == 429 and blocked.json()["detail"]["code"] == "oci_request_limit"
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60 and len(inference.calls) == len(speech_calls) == 20
