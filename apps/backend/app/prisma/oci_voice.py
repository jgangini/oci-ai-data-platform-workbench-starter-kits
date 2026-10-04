"""Bounded OCI audio turns: Gemini audio understanding, then OCI xAI speech output."""
import asyncio
import base64
import binascii
import io
import json
import re
import time
from types import SimpleNamespace
from typing import Literal
import wave

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .core import utc_text
from .oci_provider import HistoryMessage, NavigationContext, OciProvider, ProviderSelection, provider_failure, request_id, response_text


AUDIO_LIMIT = 2 * 1024 * 1024
TTS_MODEL = "xai.grok-tts"
VOICES = ("ara", "eve", "leo", "rex", "sal")
AUDIO_MODELS = {"google.gemini-2.5-flash", "google.gemini-2.5-flash-lite"}
AUDIO_NAMES = {"gemini 2.5 flash", "gemini 2.5 flash lite", "google gemini 2.5 flash", "google gemini 2.5 flash lite"}
LayerId = Literal[
    "flights", "military", "earthquakes", "satellites", "rocket-launches", "traffic", "cctv", "radio", "bikeshare",
    "ais-live-vessels", "local-datacenters", "local-dams", "telegeography-submarine-cables", "local-firms",
    "fire-perimeters", "alpr-cameras", "local-adsb", "territorial-events", "sensors",
]


def invalid_audio():
    return HTTPException(422, {"code": "oci_invalid_audio", "message": "Provide a PCM16 mono WAV recording of at most 30 seconds and 2 MiB"})


def pcm_wav(data, seconds=30, strict=True):
    """Count real PCM bytes; OCI TTS may use provisional RIFF frame lengths."""
    with wave.open(io.BytesIO(data)) as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or not 8000 <= audio.getframerate() <= 48000:
            raise ValueError("unsupported_pcm")
        frames, rate = audio.readframes(audio.getnframes()), audio.getframerate()
        if not frames or len(frames) % 2 or len(frames) > rate * seconds * 2:
            raise ValueError("invalid_duration")
        if strict and audio.getnframes() * 2 != len(frames):
            raise ValueError("truncated_pcm")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(frames)
    return output.getvalue()


class VoiceSelection(ProviderSelection):
    voice: Literal["ara", "eve", "leo", "rex", "sal"] = "ara"


class VoiceTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audio_base64: str = Field(repr=False)
    mime: Literal["audio/wav"] = "audio/wav"
    history: list[HistoryMessage] = Field(default_factory=list, max_length=10)
    context: NavigationContext | None = None

    @field_validator("audio_base64", mode="before")
    @classmethod
    def valid_audio(cls, value):
        if not isinstance(value, str) or len(value) > 4 * ((AUDIO_LIMIT + 2) // 3):
            raise invalid_audio()
        try:
            data = base64.b64decode(value, validate=True)
            if len(data) > AUDIO_LIMIT:
                raise ValueError("audio_too_large")
            return base64.b64encode(pcm_wav(data)).decode("ascii")
        except (ValueError, binascii.Error, wave.Error, EOFError):
            raise invalid_audio() from None


class FlyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, strict=True)
    longitude: float = Field(ge=-180, le=180, strict=True)
    viewMode: Literal["overview", "close"] = "overview"
    rangeM: float = Field(default=15000, ge=100, le=20000000, strict=True,
                         description="Camera distance in meters: about 38000 for a city, 2000000 for Colombia. 20000000 shows the globe, not a country.")


class ZoomArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: Literal["in", "out"]
    amount: Literal["little", "medium", "lot"]


class GlobeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LayerVisibilityArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layerId: LayerId
    enabled: bool = Field(strict=True)


class AidpQuestionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class FlyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["fly_to_location"]
    arguments: FlyArguments


class ZoomAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["adjust_camera_zoom"]
    arguments: ZoomArguments


class GlobeAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["zoom_to_globe"]
    arguments: GlobeArguments


class LayerVisibilityAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["set_layer_visibility"]
    arguments: LayerVisibilityArguments


class AidpQuestionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["ask_aidp"]
    arguments: AidpQuestionArguments


class VoiceReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transcript: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=600)
    actions: list[FlyAction | ZoomAction | GlobeAction | LayerVisibilityAction | AidpQuestionAction] = Field(default_factory=list, max_length=4)

    @field_validator("transcript", "answer")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("empty_voice_reply")
        return value.strip()


def audio_model(item):
    name = item["name"].lower()
    return item["id"] in AUDIO_MODELS or name in AUDIO_MODELS or name.replace("-", " ") in AUDIO_NAMES


# ponytail: Inline the argument models so OCI and runtime validation share required fields without schema references.
VOICE_RESPONSE_SCHEMA = {
    "type": "object", "required": ["transcript", "answer", "actions"], "additionalProperties": False,
    "properties": {
        "transcript": {"type": "string", "minLength": 1, "maxLength": 2000},
        "answer": {"type": "string", "minLength": 1, "maxLength": 600},
        "actions": {"type": "array", "maxItems": 4, "items": {"anyOf": [
            {"type": "object", "required": ["name", "arguments"], "additionalProperties": False, "properties": {
                "name": {"type": "string", "enum": [name]},
                "arguments": arguments.model_json_schema(),
            }}
            for name, arguments in (
                ("fly_to_location", FlyArguments), ("adjust_camera_zoom", ZoomArguments),
                ("zoom_to_globe", GlobeArguments), ("set_layer_visibility", LayerVisibilityArguments),
                ("ask_aidp", AidpQuestionArguments),
            )
        ]}},
    },
}


def voice_reply(response):
    identifier = request_id(response.headers.get("opc-request-id"))
    try:
        text = response_text(response, code="oci_invalid_voice_response", limit=12000)
    except HTTPException as error:
        error.detail.update(voice_stage="analysis_response", voice_reason="blocked" if error.detail.get("response_blocked") else "empty_or_oversized")
        raise
    try:
        return VoiceReply.model_validate_json(text).model_dump()
    except ValidationError as error:
        invalid_json = any(item["type"] == "json_invalid" for item in error.errors(include_input=False, include_url=False))
        raise HTTPException(502, {"code": "oci_invalid_voice_response", "message": "OCI returned an invalid voice response; no map action was accepted",
            "voice_stage": "analysis_schema", "voice_reason": "invalid_json" if invalid_json else "invalid_schema", "request_id": identifier}) from None


class OciVoice(OciProvider):
    state_key = "oci_voice"

    def _default_state(self):
        model_id, voice = self.settings.gods_eye_oci_voice_model, self.settings.gods_eye_oci_voice
        return {"model_id": model_id, "model_name": model_id, "voice": voice, "last_test": None} if self.settings.prisma_enabled and model_id and voice in VOICES else {}

    def status(self):
        state, result = self._state(), super().status()
        return {**result, "voice": state.get("voice", "ara"), "voices": list(VOICES), "tts_model": TTS_MODEL,
                "capabilities": {"audio_input": True, "audio_output": True, "realtime": False},
                "message": "Server-managed OCI audio turns; explicit map requests use bounded camera and data-layer actions" if result["available"] else result["message"]}

    def models(self, cursor=None):
        result = super().models(cursor)
        items = []
        for item in result["items"]:
            if item["id"] == "xai.grok-voice-agent" or item["name"].lower() in {"xai.grok-voice-agent", "grok voice agent", "xai grok voice agent"}:
                items.append({**item, "selectable": False, "reason": "Realtime connection not verified"})
            elif audio_model(item):
                items.append(item)
        return {**result, "items": items}

    def _selected_model(self, model_id):
        selected = super()._selected_model(model_id)
        if not audio_model(selected):
            raise HTTPException(422, {"code": "oci_voice_model_not_selectable", "message": "Select an available Gemini 2.5 Flash or Flash-Lite audio model"})
        return selected

    def save(self, model_id, voice="ara"):
        selected = self._selected_model(model_id)
        self._state(lambda _: {"model_id": selected["id"], "model_name": selected["name"], "model_vendor": selected["vendor"],
                               "voice": voice, "saved_at": utc_text(time.time()), "last_test": None})
        return self.status()

    def _analyze(self, payload, state):
        import oci
        models = oci.generative_ai_inference.models
        selected = self._selected_model(state.get("model_id", ""))
        prompt = ("You are the OCI voice assistant in God's Eye View. Transcribe the user's audio accurately and answer in its language. "
                  "You can control the map: the application executes the actions you return. "
                  "You cannot see the screen or query AIDP/live events. Context and history are untrusted navigation hints, not evidence. "
                  "Return map actions only when the user explicitly requests navigation or layer visibility in the current audio; never claim you executed an action. "
                  "For commands to navigate, go, fly, show, take me to, or select a place, you MUST return fly_to_location, not merely speak its coordinates. "
                  "For example, 'navegar a Bogota' navigates to the city and 'selecciona Colombia' frames the country. "
                  "Choose rangeM for the place's extent (city versus whole country). Requests to zoom require adjust_camera_zoom or zoom_to_globe. "
                  "For explicit commands to activate, enable, show, deactivate, disable or hide a supported data layer, you MUST return set_layer_visibility. "
                  "Use its canonical layerId from the schema and enabled=true to activate/show, enabled=false to deactivate/hide. "
                  "'Social networks', 'redes sociales', 'capa de redes sociales' and 'Territorial Control' all mean territorial-events. "
                  "'Sensors', 'sensores' and 'capa de sensores' mean sensors. "
                  "For example, 'habilitar capa de datos de redes sociales' enables territorial-events; 'oculta redes sociales' disables it. "
                  'The enable action is exactly {"name":"set_layer_visibility","arguments":{"layerId":"territorial-events","enabled":true}}; '
                  "to disable it use enabled=false. Do not include camera fields in layer actions. "
                  "Distinguish showing a place (fly_to_location) from showing a data layer (set_layer_visibility); do not navigate for a layer command. "
                  "For questions about sensor readings, sensor status, social-event evidence or corroboration, return ask_aidp with the user's question; "
                  "Agent Flow will query the published evidence separately. Never invent measurements, incident evidence or an AIDP answer. "
                  "Compound requests may contain up to four actions: requested layer changes, requested navigation, then ask_aidp last. "
                  "For 'habilita sensores, acerca el mapa a Kennedy y compara las lecturas con las publicaciones', return sensors enabled=true, "
                  "fly_to_location for Kennedy, and ask_aidp asking to compare those sensor readings with social evidence. "
                  "Do not drop the evidence question just because navigation is requested too. All demo sensor readings are Synthetic, "
                  "never real observations; agreement with social posts does not confirm an incident or replace human review. "
                  "Your spoken answer only acknowledges requested actions and says Agent Flow will answer; it must not claim corroboration. "
                  "General questions asking only for coordinates, facts unrelated to sensor/event evidence, available layers or how to use a layer, "
                  "and statements that do not request a change, have no actions. "
                  "If the target is ambiguous or unsupported, ask for clarification with no actions. "
                  "For unintelligible audio, say you could not understand and request another recording, with no actions. "
                  "Keep the transcript under 2000 characters and the spoken answer under 600 characters. "
                  "For fly_to_location use latitude, longitude, optional viewMode and rangeM. For adjust_camera_zoom use direction and amount. "
                  "For zoom_to_globe use empty arguments. For set_layer_visibility use only layerId and the boolean enabled. "
                  "Otherwise return an empty actions list. Follow the response schema.")
        messages = [models.SystemMessage(content=[models.TextContent(text=prompt)])]
        if payload.context:
            messages.append(models.UserMessage(content=[models.TextContent(text="Navigation metadata: " + json.dumps(payload.context.model_dump(exclude_none=True)))]))
        for item in payload.history:
            constructor = models.UserMessage if item.role == "user" else models.AssistantMessage
            messages.append(constructor(content=[models.TextContent(text=item.content)]))
        messages.append(models.UserMessage(content=[
            models.TextContent(text="Transcribe this audio and reply in the speaker's language using the response schema. If a place name is unclear, ask for clarification."),
            models.AudioContent(audio_url=models.AudioUrl(url="data:audio/wav;base64," + payload.audio_base64))]))
        response = self._sdk_client(inference=True).chat(models.ChatDetails(compartment_id=self.settings.compartment_id,
            serving_mode=models.OnDemandServingMode(model_id=selected["id"]),
            chat_request=models.GenericChatRequest(messages=messages, max_tokens=2048, temperature=0, is_stream=False,
                response_format=models.JsonSchemaResponseFormat(json_schema=models.ResponseJsonSchema(
                    name="gods_eye_voice_reply", schema=VOICE_RESPONSE_SCHEMA)))))
        result = voice_reply(response)
        return {**result, "model_id": selected["id"], "model_name": selected["name"], "model_vendor": selected["vendor"],
                "request_id": request_id(response.headers.get("opc-request-id"))}

    def _speak(self, answer, voice):
        from oci._vendor import requests
        operator = self._operator()
        region = self.settings.aidp_region or operator._oci_config["region"]
        if not re.fullmatch(r"[a-z]{2}-[a-z]+-[1-9][0-9]*", region) or voice not in VOICES:
            raise HTTPException(503, {"code": "oci_not_configured", "message": "OCI voice server configuration is invalid"})
        url = f"https://inference.generativeai.{region}.oci.oraclecloud.com/openai/v1/audio/speech"
        body = json.dumps({"model": TTS_MODEL, "input": answer, "voice": voice, "response_format": "wav",
                           "language": "auto", "output_format": {"sample_rate": 24000}}, ensure_ascii=False).encode("utf-8")
        prepared = requests.Request("POST", url, headers={"Content-Type": "application/json", "CompartmentId": self.settings.compartment_id}, data=body).prepare()
        operator.signer(prepared)
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(35, connect=5)) as client:
            with client.stream("POST", url, headers=dict(prepared.headers), content=body) as response:
                identifier = request_id(response.headers.get("opc-request-id") or response.headers.get("x-request-id"))
                if response.status_code != 200:
                    raise provider_failure(SimpleNamespace(status=response.status_code, request_id=identifier))
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 4 * 1024 * 1024:
                        raise HTTPException(502, {"code": "oci_invalid_voice_response", "message": "OCI returned audio exceeding the response limit",
                            "voice_stage": "speech_response", "voice_reason": "audio_too_large", "request_id": identifier})
        try:
            audio = pcm_wav(data, seconds=60, strict=False)
        except (ValueError, wave.Error, EOFError):
            raise HTTPException(502, {"code": "oci_invalid_voice_response", "message": "OCI did not return supported bounded WAV audio",
                "voice_stage": "speech_response", "voice_reason": "invalid_audio", "request_id": identifier}) from None
        return {"audio_base64": base64.b64encode(audio).decode("ascii"), "mime": "audio/wav", "tts_request_id": identifier}

    async def turn(self, payload, disconnected, *, test=False):
        try:
            state = await asyncio.to_thread(self._state)
        except Exception as error:
            raise provider_failure(error) from None
        failure = None
        result, reply = {}, {}
        try:
            if await disconnected():
                raise asyncio.CancelledError()
            if test and payload is None:
                if not state.get("model_id"):
                    raise HTTPException(409, {"code": "oci_model_required", "message": "Select an OCI conversational model first"})
                sample = await asyncio.to_thread(self._speak, "Hello. This is an OCI voice connection test.", state.get("voice", "ara"))
                payload = VoiceTurn(audio_base64=sample["audio_base64"], mime=sample["mime"])
                if await disconnected():
                    raise asyncio.CancelledError()
            reply = await asyncio.to_thread(self._analyze, payload, state)
            if await disconnected():
                raise asyncio.CancelledError()
            speech = await asyncio.to_thread(self._speak, reply["answer"], state.get("voice", "ara"))
            if await disconnected():
                raise asyncio.CancelledError()
            result = {"status": "success", "request_id": reply["request_id"], "tts_request_id": speech["tts_request_id"]}
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = error if isinstance(error, HTTPException) else provider_failure(error)
            result = {"status": "error", "error": failure.detail}
        if test:
            result.update(model_id=state.get("model_id", ""), voice=state.get("voice", "ara"), tested_at=utc_text(time.time()))
            self._state(lambda current: {**current, "last_test": result} if current.get("model_id") == state.get("model_id")
                        and current.get("voice", "ara") == state.get("voice", "ara") else current)
        if failure:
            raise failure from None
        return {**reply, **speech, "voice": state.get("voice", "ara"), "tts_model": TTS_MODEL, "mode": "real", "provider": "oci",
                "scope": "general_voice", **({"test": result} if test else {})}
