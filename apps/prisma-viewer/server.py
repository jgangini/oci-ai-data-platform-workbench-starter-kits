"""Authenticated PRISMA viewer bridge; cloud failures never fall back to fixtures."""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from prisma.area import parse_bbox, within_bbox
from prisma import native_proxy

app = FastAPI(title="Territorial Control · Bogotá", docs_url=None, redoc_url=None)
MODE = os.getenv("PRISMA_MODE", "oci")
STATIC = Path(os.getenv("PRISMA_STATIC_DIR", "/app/static"))
FILTERS = {"locality", "platform", "category", "severity", "mode", "date_from", "date_to", "bbox"}


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=2000)
    session_id: UUID | None = None
    version: str = Field(min_length=1, max_length=100)
    incident_id: str | None = Field(default=None, max_length=200)
    sensor_id: str | None = Field(default=None, max_length=200)
    filters: dict[str, str] = Field(default_factory=dict)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "validated", "rejected"]
    note: str = Field(default="", max_length=1000)
    expected_evidence_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(default=None, max_length=10000)
    lat: float | None = Field(default=None, strict=True, allow_inf_nan=False, ge=-90, le=90)
    lon: float | None = Field(default=None, strict=True, allow_inf_nan=False, ge=-180, le=180)

    @model_validator(mode="after")
    def coordinates_together(self):
        if self.model_fields_set & {"lat", "lon"} and (self.lat is None or self.lon is None):
            raise ValueError("Latitude and longitude must be provided together as numbers")
        return self


class SensorLocationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float = Field(strict=True, allow_inf_nan=False, ge=-4.3, le=13.6)
    lon: float = Field(strict=True, allow_inf_nan=False, ge=-81.8, le=-66.7)
    expected_lat: float = Field(strict=True, allow_inf_nan=False, ge=-4.3, le=13.6)
    expected_lon: float = Field(strict=True, allow_inf_nan=False, ge=-81.8, le=-66.7)


def principal(request: Request) -> str:
    # The private listener accepts only VM1; nginx overwrites this header after auth_request.
    value = request.headers.get("x-prisma-user", "")
    if not value or len(value) > 200 or not request.headers.get("cookie"):
        raise HTTPException(401, "An authenticated session is required")
    return value


def voice_failure_detail(supplied):
    stage, reason = supplied.get("voice_stage"), supplied.get("voice_reason")
    messages = {
        ("analysis_response", "empty_or_oversized"): "OCI did not return a complete voice reply; please try again",
        ("analysis_response", "blocked"): "OCI blocked the model response; no answer was returned",
        ("analysis_schema", "invalid_json"): "OCI returned an invalid voice reply; no map action was run",
        ("analysis_schema", "invalid_schema"): "OCI returned an unsupported voice reply; no map action was run",
        ("speech_response", "audio_too_large"): "OCI generated a spoken reply exceeding the playback limit",
        ("speech_response", "invalid_audio"): "OCI did not return playable speech; please try again",
    }
    message = messages.get((stage, reason)) if isinstance(stage, str) and isinstance(reason, str) else None
    return {"voice_stage": stage, "voice_reason": reason, "message": message} if message else {}


def admin_failure(response, path):
    detail, headers = "The administration service could not complete the request", {}
    if path in {"/api/prisma/oci-provider", "/api/prisma/oci-chat", "/api/prisma/oci-voice", "/api/prisma/oci-voice/turn"}:
        try:
            supplied = response.json().get("detail", {})
        except (ValueError, AttributeError):
            supplied = {}
        messages = {
            "oci_authentication_failed": "OCI did not accept the server operator credentials",
            "oci_access_denied": "The server operator cannot access this OCI resource",
            "oci_model_unavailable": "The selected OCI model is unavailable in this deployment",
            "oci_rate_limited": "OCI quota or rate limit reached; try again later",
            "oci_unavailable": "The OCI provider is unavailable; check the server configuration",
            "oci_not_configured": "OCI server credentials and compartment are not configured",
            "oci_model_required": "Select an OCI conversational model first",
            "oci_model_not_selectable": "Choose an active on-demand GENERIC chat model from the current OCI catalog",
            "oci_invalid_response": "OCI did not return a bounded text response",
            "oci_request_limit": "Too many OCI assistant requests; try again shortly",
            "oci_invalid_audio": "Use a valid bounded PCM WAV recording",
            "oci_invalid_voice_response": "OCI did not return a valid voice response",
            "oci_voice_model_not_selectable": "Choose an available OCI audio conversational model",
        }
        code = supplied.get("code") if isinstance(supplied, dict) else None
        if isinstance(code, str) and code in messages:
            # Only known local messages cross the bridge, never a provider's raw error text.
            detail = {"code": code, "message": messages[code]}
            if code == "oci_invalid_voice_response":
                detail.update(voice_failure_detail(supplied))
            if code in {"oci_invalid_response", "oci_invalid_voice_response"} and supplied.get("response_blocked") is True:
                detail.update(message="OCI blocked the model response; no answer was returned", response_blocked=True)
            status, identifier = supplied.get("provider_status"), supplied.get("request_id")
            if type(status) is int and 100 <= status <= 599:
                detail["provider_status"] = status
            if isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9/_.:-]{1,256}", identifier):
                detail["request_id"] = identifier
            retry = response.headers.get("retry-after", "")
            if re.fullmatch(r"[0-9]{1,6}", retry):
                headers["Retry-After"] = retry
    return HTTPException(response.status_code, detail, headers=headers)


async def admin_request(request: Request, method: str, path: str, payload=None):
    base = os.environ.get("PRISMA_ADMIN_URL", "http://aidp-lab:8000")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(100, connect=10)) as client:
            response = await client.request(method, base + path,
                headers={"Cookie": request.headers.get("cookie", "")}, json=payload)
        if response.status_code >= 400:
            raise admin_failure(response, path)
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "Territorial Control administration is unavailable") from exc


def cloud_object(key: str) -> dict:
    import oci
    try:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        client = oci.object_storage.ObjectStorageClient({"region": os.environ["OCI_REGION"]}, signer=signer)
        response = client.get_object(os.environ["PRISMA_NAMESPACE"], os.environ["PRISMA_BUCKET"], key)
        body = response.data.content
        if len(body) > 8_000_000:
            raise ValueError("Snapshot exceeds the bounded demo publication")
        return json.loads(body)
    except Exception as exc:
        raise HTTPException(503, "The AIDP publication is unavailable") from exc


def published_snapshot() -> dict:
    pointer = cloud_object(os.getenv("PRISMA_SNAPSHOT_KEY", "04_gold/prisma/current.json"))
    key = str(pointer.get("snapshot_key", ""))
    if not key.startswith("04_gold/prisma/snapshots/") or ".." in key:
        raise HTTPException(503, "The Territorial Control publication is invalid")
    snapshot = cloud_object(key)
    if snapshot.get("version") != pointer.get("version"):
        raise HTTPException(503, "The Territorial Control publication is incomplete")
    return {**snapshot, "runtime": "aidp"}


async def snapshot_for(request: Request):
    principal(request)
    if MODE not in {"local", "oci"}:
        raise HTTPException(503, "Invalid Territorial Control mode")
    if MODE == "local" or os.environ.get("PRISMA_ADMIN_URL"):
        snapshot = await admin_request(request, "GET", "/api/prisma/snapshot")
        if MODE == "oci" and snapshot.get("runtime") != "aidp":
            raise HTTPException(503, "The administration service is not using the AIDP publication")
        return snapshot
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(published_snapshot)


def period_bounds(filters: dict, status: int = 422):
    bounds = []
    try:
        for key in ("date_from", "date_to"):
            value = filters.get(key)
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
            if parsed and parsed.tzinfo is None:
                raise ValueError("UTC or an explicit offset is required")
            bounds.append(parsed.astimezone(timezone.utc) if parsed else None)
        if all(bounds) and bounds[0] > bounds[1]:
            raise ValueError("Reversed period")
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status, "Invalid period: use ISO dates with a timezone and a start no later than the end") from exc
    return bounds


def validate_filters(filters, status=422):
    if not isinstance(filters, dict) or set(filters) - FILTERS:
        raise HTTPException(status, "This filter is not allowed")
    if any(not isinstance(value, str) or len(value) > 100 for value in filters.values()):
        raise HTTPException(status, "Invalid filter value")
    period_bounds(filters, status)
    try:
        parse_bbox(filters.get("bbox"))
    except ValueError as exc:
        raise HTTPException(status, str(exc)) from exc


def validate_context(payload: ChatRequest, snapshot: dict) -> dict:
    if payload.version != snapshot.get("version"):
        raise HTTPException(409, "The data changed; refresh the map and ask again")
    validate_filters(payload.filters)
    ids = {str(item["id"]) for item in snapshot.get("incidents", [])}
    if payload.incident_id and payload.incident_id not in ids:
        raise HTTPException(422, "Unknown incident in this publication")
    if payload.sensor_id and payload.sensor_id not in {item["sensor_id"] for item in snapshot.get("sensors", [])}:
        raise HTTPException(422, "Unknown sensor in this publication")
    return {"version": payload.version, "incident_id": payload.incident_id,
            "sensor_id": payload.sensor_id, "filters": payload.filters, "published_at": snapshot.get("published_at")}


def endpoint_from(metadata: dict, region: str) -> str:
    endpoint = str(metadata.get("endpoint", ""))
    url = urlsplit(endpoint)
    if (url.scheme != "https" or url.netloc != f"gateway.aidp.{region}.oci.oraclecloud.com"
        or not re.fullmatch(r"/agentendpoint/[A-Za-z0-9_.-]+/chat", url.path)
        or url.query or url.fragment or metadata.get("state") != "ACTIVE"):
        raise HTTPException(503, "The agent endpoint has not been validated")
    return endpoint


def validate_reply_text(reply, version):
    if not isinstance(reply, dict):
        raise HTTPException(502, "The agent response is invalid")
    answer = reply.get("answer")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 20000:
        raise HTTPException(502, "The agent did not return valid text")
    if reply.get("version") != version:
        raise HTTPException(502, "The response does not match the publication")


def validate_references(refs, evidence):
    if not isinstance(refs, list):
        raise HTTPException(502, "Invalid agent references")
    if any(not isinstance(ref, str) or ref not in evidence for ref in refs):
        raise HTTPException(502, "The response does not match the published evidence")


def validate_actions(actions, incidents):
    if not isinstance(actions, list) or len(actions) > 10:
        raise HTTPException(502, "Invalid agent actions")
    for action in actions:
        if not isinstance(action, dict):
            raise HTTPException(502, "Invalid agent action")
        if action.get("type") == "focus_incident":
            if not isinstance(action.get("incident_id"), str) or action["incident_id"] not in incidents:
                raise HTTPException(502, "The agent incident is outside the current context")
        elif action.get("type") == "filter_incidents":
            validate_filters(action.get("filters"), 502)
        else:
            raise HTTPException(502, "This agent action is not allowed")


def grounded_reply(reply: dict, snapshot: dict, payload: ChatRequest | None = None) -> dict:
    validate_reply_text(reply, snapshot.get("version"))
    selected = selected_incidents(payload, snapshot) if payload else snapshot.get("incidents", [])
    evidence = {str(ref) for item in selected for ref in item.get("evidence_ids", [])}
    evidence.intersection_update(str(item["id"]) for item in snapshot.get("evidence", []))
    refs, actions = reply.get("evidence_ids", []), reply.get("actions", [])
    validate_references(refs, evidence)
    validate_actions(actions, {str(item["id"]) for item in selected})
    sensors = selected_sensors(payload, snapshot) if payload else snapshot.get("sensors", [])
    sensor_refs = reply.get("sensor_evidence_ids", [])
    validate_references(sensor_refs, {item["id"] for item in sensors})
    return {"answer": reply["answer"], "evidence_ids": refs, "sensor_evidence_ids": sensor_refs, "actions": actions}


def within_period(item, date_from, date_to, field="created_at"):
    if date_from is None and date_to is None:
        return True
    if not item.get(field):
        return False
    try:
        created = datetime.fromisoformat(item[field].replace("Z", "+00:00"))
        if created.tzinfo is None:
            return False
    except (ValueError, AttributeError):
        return False
    if date_from and created < date_from:
        return False
    return date_to is None or created <= date_to


def window_publication(data, filters, now=None):
    start, end = period_bounds(filters)
    end = end or now or datetime.now(timezone.utc)
    start = start or end - timedelta(hours=24)
    if start > end:
        raise HTTPException(422, "From must precede To")
    evidence = list({item["id"]: item for item in data.get("evidence", []) if within_period(item, start, end)}.values())
    ids = {item["id"] for item in evidence}
    incidents = [item for item in data.get("incidents", []) if ids.intersection(item.get("evidence_ids", []))]
    incident_ids = {item["id"] for item in incidents}
    # ponytail: filter the bounded Gold artifact before browser transfer; a larger publication needs partitioned/indexed serving.
    # Keep full incident membership for review concurrency; the carousel uses only the scoped evidence payloads.
    return {**data, "incidents": incidents, "evidence": evidence,
            "event_posts": [item for item in data.get("event_posts", []) if item.get("event_id") in incident_ids and item.get("post_key") in ids],
            "window": {"date_from": start.isoformat(), "date_to": end.isoformat()}}


def selected_incidents(payload: ChatRequest, snapshot: dict) -> list:
    selected = snapshot.get("incidents", [])
    if payload.incident_id:
        selected = [item for item in selected if item["id"] == payload.incident_id]
    attributes = {key: value for key, value in payload.filters.items() if value and key not in {"platform", "date_from", "date_to", "bbox"}}
    for key, value in attributes.items():
        accepted = {"Synthetic", "simulation"} if key == "mode" and value in {"Synthetic", "simulation"} else {value}
        selected = [item for item in selected if str(item.get(key, "")) in accepted]
    date_from, date_to = period_bounds(payload.filters)
    area = parse_bbox(payload.filters.get("bbox"))
    selected = [item for item in selected if within_bbox(item, area)]
    platform = payload.filters.get("platform")
    if date_from is None and date_to is None and not platform:
        return selected
    ids = {item["id"] for item in snapshot.get("evidence", []) if within_period(item, date_from, date_to)
           and (not platform or item.get("platform") == platform)}
    return [{**item, "evidence_ids": [ref for ref in item.get("evidence_ids", []) if ref in ids]}
            for item in selected if ids.intersection(item.get("evidence_ids", []))]


def selected_sensors(payload: ChatRequest, snapshot: dict) -> list:
    selected = snapshot.get("sensors", [])
    if payload.sensor_id:
        selected = [item for item in selected if item["sensor_id"] == payload.sensor_id]
    for key in ("locality", "mode"):
        value = payload.filters.get(key)
        if value:
            accepted = {"Synthetic", "simulation"} if value in {"Synthetic", "simulation"} else {value}
            selected = [item for item in selected if item.get(key) in accepted]
    bounds, area = period_bounds(payload.filters), parse_bbox(payload.filters.get("bbox"))
    return [item for item in selected if within_bbox(item, area) and within_period(item, *bounds, field="observed_at")]


def fixture_reply(payload: ChatRequest, snapshot: dict) -> dict:
    import unicodedata
    normalize = lambda text: "".join(c for c in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(c))
    question = normalize(payload.question)
    items = snapshot.get("incidents", [])
    selected = selected_incidents(payload, snapshot)
    localities = {normalize(str(item.get("locality", ""))) for item in items} - {""}
    matches = [name for name in localities if name in question]
    if matches:
        selected = [item for item in selected if normalize(item.get("locality", "")) in matches]
    refs = list(dict.fromkeys(ref for item in selected for ref in item.get("evidence_ids", [])))
    descriptions = [f"{item.get('category')} en {item.get('locality')}: {item.get('severity')} ({item['id']})" for item in selected[:10]]
    answer = "DEMOSTRACIÓN LOCAL · Respuesta de prueba; no interviene un agente AIDP.\n"
    answer += "\n".join(descriptions) if descriptions else "No hay evidencia que coincida en esta publicación."
    sensors = selected_sensors(payload, snapshot)[:10]
    if sensors:
        answer += "\nLecturas Synthetic de demostración; no son observaciones reales ni validan incidentes:\n"
        answer += "\n".join(f"{item['sensor_id']}: {item['value']} {item['unit']} · {item['status']} · {item['observed_at']} ({item['id']})" for item in sensors)
    return {"answer": answer, "version": snapshot["version"], "evidence_ids": refs,
            "sensor_evidence_ids": [item["id"] for item in sensors],
            "actions": [{"type": "focus_incident", "incident_id": item["id"]} for item in selected[:3]]}


@app.get("/health")
def health():
    native_proxy.health()
    return {"status": "ok", "mode": MODE}


@app.get("/ready")
def ready():
    native_proxy.health()
    if MODE not in {"local", "oci"}:
        raise HTTPException(503, "Invalid Territorial Control mode")
    if MODE == "oci" and os.environ.get("PRISMA_ADMIN_URL"):
        # Backend module activation checks its deployment and snapshot; private viewer has no user cookie here.
        return {"status": "ready", "mode": MODE, "publication_check": "authenticated_backend_request"}
    if MODE == "oci":
        published_snapshot()
        endpoint_from(cloud_object(os.getenv("PRISMA_AGENT_ENDPOINT_KEY", ".control/prisma/agent.json")), os.environ["OCI_REGION"])
    return {"status": "ready", "mode": MODE}


@app.get("/api/prisma/capture-status")
async def capture_status(request: Request, kind: Literal["social", "sensors"] = "social"):
    principal(request)
    return await admin_request(request, "GET", f"/api/prisma/capture-status?kind={kind}")


@app.get("/api/prisma/snapshot")
async def snapshot(request: Request, date_from: str = "", date_to: str = ""):
    period_bounds({"date_from": date_from, "date_to": date_to})
    data = window_publication(await snapshot_for(request), {"date_from": date_from, "date_to": date_to})
    can_admin = not principal(request).startswith("local-prisma:")
    return {**data, "can_review": can_admin, "can_admin": can_admin}


@app.post("/api/prisma/chat")
async def chat(payload: ChatRequest, request: Request):
    data = await snapshot_for(request)
    validate_context(payload, data)
    data = window_publication(data, payload.filters)
    public_id = payload.session_id or uuid4()
    if MODE == "local":
        reply = fixture_reply(payload, data)
    else:
        reply = await admin_request(request, "POST", "/api/admin/prisma/chat", {
            **payload.model_dump(mode="json"), "session_id": str(public_id)})
    return {**grounded_reply(reply, data, payload), "session_id": str(public_id), "version": data["version"],
            "published_at": data.get("published_at"), "runtime": "local_fixture" if MODE == "local" else "aidp"}


@app.post("/api/prisma/incidents/{incident_id}/review")
async def review(incident_id: str, payload: ReviewRequest, request: Request):
    principal(request)
    if not re.fullmatch(r"[A-Za-z0-9:_-]{1,200}", incident_id):
        raise HTTPException(422, "Invalid identifier")
    return await admin_request(request, "POST", f"/api/prisma/incidents/{incident_id}/review", payload.model_dump(exclude_none=True))


@app.post("/api/prisma/sensors/{sensor_id}/location")
async def sensor_location(sensor_id: str, payload: SensorLocationUpdate, request: Request):
    if principal(request).startswith("local-prisma:"):
        raise HTTPException(403, "Administrator access is required")
    native_proxy.check_origin(request)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", sensor_id):
        raise HTTPException(422, "Invalid sensor identifier")
    return await admin_request(request, "POST", f"/api/prisma/sensors/{sensor_id}/location", payload.model_dump())


@app.get("/api/prisma/context")
async def context(request: Request):
    principal(request)
    from context_layers import context_layers
    return await context_layers()


@app.get("/api/prisma/oci-provider")
async def oci_provider(request: Request):
    principal(request)
    return await admin_request(request, "GET", "/api/prisma/oci-provider")


@app.post("/api/prisma/oci-chat")
async def oci_chat(request: Request, payload: dict):
    principal(request)
    native_proxy.check_origin(request)
    return await admin_request(request, "POST", "/api/prisma/oci-chat", payload)


@app.get("/api/prisma/oci-voice")
async def oci_voice_status(request: Request):
    principal(request)
    return await admin_request(request, "GET", "/api/prisma/oci-voice")


@app.post("/api/prisma/oci-voice/turn")
async def oci_voice_turn(request: Request, payload: dict):
    principal(request)
    native_proxy.check_origin(request)
    return await admin_request(request, "POST", "/api/prisma/oci-voice/turn", payload)


@app.get("/api/setup/status")
def provider_status(request: Request):
    principal(request)
    native_proxy.check_origin(request)
    status = native_proxy.native_json("/__native_setup")
    # Original provider secrets are supplied in the server environment. The OCI
    # extension uses authenticated VM1 controls instead of the dev-only .env writer.
    return {**status, "keys": [{**key, "managed": "external"} for key in status.get("keys", [])],
            "store": "server environment", "server_managed": True}


@app.get("/api/setup/browser")
def browser_provider_settings(request: Request):
    principal(request)
    native_proxy.check_origin(request)
    values = native_proxy.native_json("/__native_browser")
    # These two upstream providers deliberately use origin-restricted browser keys.
    return JSONResponse({key: values.get(key, "") for key in ("googleApiKey", "cesiumToken")},
                        headers={"Cache-Control": "no-store"})


async def provider_admin(request):
    # nginx overwrites this header for every browser-to-viewer path. Internal
    # calls originate at the VM1 admin facade, never at /gods-eye-view/internal/.
    if "x-prisma-user" in request.headers:
        raise HTTPException(404)
    if not request.headers.get("cookie"):
        raise HTTPException(401, "An administrator session is required")
    # The private VM1 facade must still prove its user's current admin session.
    await admin_request(request, "GET", "/api/admin/session")


def provider_metadata():
    from cryptography.exceptions import InvalidTag
    from native import provider_runtime
    try:
        result = provider_runtime.metadata()
        try:
            applied = native_proxy.native_json("/__native_health").get("provider_revision", "environment")
        except HTTPException:
            applied = None
        return {**result, "runtime_status": "applied" if applied == result["revision"] else "pending",
                "applied_revision": applied}
    except (ValueError, TypeError, InvalidTag, OSError):
        raise HTTPException(503, "Provider settings storage is unavailable") from None


@app.get("/internal/provider-parameters")
async def native_parameters(request: Request):
    await provider_admin(request)
    return JSONResponse(await asyncio.to_thread(provider_metadata), headers={"Cache-Control": "no-store"})


async def provider_payload(request):
    # Bound the encrypted request before parsing; no credential text enters validation errors.
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > 49152:
            raise HTTPException(413, "Provider envelope is too large")
    try:
        return json.loads(body)
    except ValueError:
        raise HTTPException(422, "Invalid provider envelope") from None


async def provider_change(request, identifier, *, testing=False):
    from cryptography.exceptions import InvalidTag
    from native import provider_runtime
    await provider_admin(request)
    envelope = await provider_payload(request)
    try:
        operation = provider_runtime.test_environment if testing else provider_runtime.save
        return await asyncio.to_thread(operation, identifier, envelope)
    except provider_runtime.RevisionConflict:
        raise HTTPException(409, "Provider settings changed; refresh before saving or testing") from None
    except (ValueError, TypeError, InvalidTag):
        raise HTTPException(422, "Provider credential fields or encrypted payload are invalid") from None
    except OSError:
        raise HTTPException(503, "Provider settings storage is unavailable") from None


@app.put("/internal/provider-parameters/{identifier}")
async def native_parameters_save(request: Request, identifier: str):
    revision = await provider_change(request, identifier)
    # Node alone reloads; keep the bridge available and report pending if it needs longer.
    deadline = asyncio.get_running_loop().time() + 8
    while asyncio.get_running_loop().time() < deadline:
        try:
            health = await asyncio.to_thread(native_proxy.native_json, "/__native_health")
            if health.get("provider_revision", "environment") == revision:
                break
        except HTTPException:
            pass
        await asyncio.sleep(0.2)
    return JSONResponse(await asyncio.to_thread(provider_metadata), headers={"Cache-Control": "no-store"})


@app.post("/internal/provider-parameters/{identifier}/test")
async def native_parameters_test(request: Request, identifier: str):
    from provider_probes import test_provider
    environment = await provider_change(request, identifier, testing=True)
    result = await test_provider(identifier, environment, request.headers.get("x-gev-origin"))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.api_route("/api/{path:path}", methods=["GET", "HEAD", "POST"])
async def native_api(request: Request, path: str):
    principal(request)
    return await native_proxy.proxy(request, "api/" + path)


@app.api_route("/{path:path}", methods=["GET", "HEAD"])
async def static(request: Request, path: str):
    if native_proxy.enabled():
        principal(request)
        return await native_proxy.proxy(request, path)
    target = (STATIC / path).resolve()
    if not target.is_relative_to(STATIC.resolve()):
        raise HTTPException(404)
    if not target.is_file():
        target = STATIC / "index.html"
    if not target.is_file():
        raise HTTPException(503, "God's Eye View has not been built yet")
    return FileResponse(target)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081, access_log=False)
