"""Authenticated PRISMA viewer bridge; cloud failures never fall back to fixtures."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from prisma.area import parse_bbox, within_bbox

app = FastAPI(title="PRISMA Bogotá", docs_url=None, redoc_url=None)
MODE = os.getenv("PRISMA_MODE", "oci")
STATIC = Path(os.getenv("PRISMA_STATIC_DIR", "/app/static"))
FILTERS = {"locality", "platform", "category", "severity", "mode", "date_from", "date_to", "bbox"}


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=2000)
    session_id: UUID | None = None
    version: str = Field(min_length=1, max_length=100)
    incident_id: str | None = Field(default=None, max_length=200)
    filters: dict[str, str] = Field(default_factory=dict)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "validated", "rejected"]
    note: str = Field(default="", max_length=1000)


def principal(request: Request) -> str:
    # The private listener accepts only VM1; nginx overwrites this header after auth_request.
    value = request.headers.get("x-prisma-user", "")
    if not value or len(value) > 200 or not request.headers.get("cookie"):
        raise HTTPException(401, "Se requiere una sesión administrativa")
    return value


async def admin_request(request: Request, method: str, path: str, payload=None):
    base = os.environ.get("PRISMA_ADMIN_URL", "http://aidp-lab:8000")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(100, connect=10)) as client:
            response = await client.request(method, base + path,
                headers={"Cookie": request.headers.get("cookie", "")}, json=payload)
        if response.status_code >= 400:
            raise HTTPException(response.status_code, "La administración no pudo completar la solicitud")
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "Administración PRISMA no disponible") from exc


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
        raise HTTPException(503, "La publicación AIDP no está disponible") from exc


def published_snapshot() -> dict:
    pointer = cloud_object(os.getenv("PRISMA_SNAPSHOT_KEY", "04_gold/prisma/current.json"))
    key = str(pointer.get("snapshot_key", ""))
    if not key.startswith("04_gold/prisma/snapshots/") or ".." in key:
        raise HTTPException(503, "La publicación PRISMA es inválida")
    snapshot = cloud_object(key)
    if snapshot.get("version") != pointer.get("version"):
        raise HTTPException(503, "La publicación PRISMA está incompleta")
    return {**snapshot, "runtime": "aidp"}


async def snapshot_for(request: Request):
    principal(request)
    if MODE == "local":
        return await admin_request(request, "GET", "/api/prisma/snapshot")
    if MODE != "oci":
        raise HTTPException(503, "Modo PRISMA inválido")
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
        raise HTTPException(status, "Período inválido: use fechas ISO con zona horaria y Desde no posterior a Hasta") from exc
    return bounds


def validate_filters(filters, status=422):
    if not isinstance(filters, dict) or set(filters) - FILTERS:
        raise HTTPException(status, "Filtro no permitido")
    if any(not isinstance(value, str) or len(value) > 100 for value in filters.values()):
        raise HTTPException(status, "Valor de filtro inválido")
    period_bounds(filters, status)
    try:
        parse_bbox(filters.get("bbox"))
    except ValueError as exc:
        raise HTTPException(status, str(exc)) from exc


def validate_context(payload: ChatRequest, snapshot: dict) -> dict:
    if payload.version != snapshot.get("version"):
        raise HTTPException(409, "Los datos cambiaron; actualice el mapa y repita la pregunta")
    validate_filters(payload.filters)
    ids = {str(item["id"]) for item in snapshot.get("incidents", [])}
    if payload.incident_id and payload.incident_id not in ids:
        raise HTTPException(422, "Incidente desconocido en esta publicación")
    return {"version": payload.version, "incident_id": payload.incident_id,
            "filters": payload.filters, "published_at": snapshot.get("published_at")}


def endpoint_from(metadata: dict, region: str) -> str:
    endpoint = str(metadata.get("endpoint", ""))
    url = urlsplit(endpoint)
    if (url.scheme != "https" or url.netloc != f"gateway.aidp.{region}.oci.oraclecloud.com"
        or not re.fullmatch(r"/agentendpoint/[A-Za-z0-9_.-]+/chat", url.path)
        or url.query or url.fragment or metadata.get("state") != "ACTIVE"):
        raise HTTPException(503, "El endpoint del agente no está validado")
    return endpoint


def validate_reply_text(reply, version):
    if not isinstance(reply, dict):
        raise HTTPException(502, "La respuesta del agente es inválida")
    answer = reply.get("answer")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 20000:
        raise HTTPException(502, "El agente no devolvió un texto válido")
    if reply.get("version") != version:
        raise HTTPException(502, "La respuesta no coincide con la publicación")


def validate_references(refs, evidence):
    if not isinstance(refs, list):
        raise HTTPException(502, "Referencias del agente inválidas")
    if any(not isinstance(ref, str) or ref not in evidence for ref in refs):
        raise HTTPException(502, "La respuesta no coincide con las evidencias publicadas")


def validate_actions(actions, incidents):
    if not isinstance(actions, list) or len(actions) > 10:
        raise HTTPException(502, "Acciones del agente inválidas")
    for action in actions:
        if not isinstance(action, dict):
            raise HTTPException(502, "Acción del agente inválida")
        if action.get("type") == "focus_incident":
            if not isinstance(action.get("incident_id"), str) or action["incident_id"] not in incidents:
                raise HTTPException(502, "Incidente del agente fuera de contexto")
        elif action.get("type") == "filter_incidents":
            validate_filters(action.get("filters"), 502)
        else:
            raise HTTPException(502, "Acción del agente no permitida")


def grounded_reply(reply: dict, snapshot: dict, payload: ChatRequest | None = None) -> dict:
    validate_reply_text(reply, snapshot.get("version"))
    selected = selected_incidents(payload, snapshot) if payload else snapshot.get("incidents", [])
    evidence = {str(ref) for item in selected for ref in item.get("evidence_ids", [])}
    evidence.intersection_update(str(item["id"]) for item in snapshot.get("evidence", []))
    refs, actions = reply.get("evidence_ids", []), reply.get("actions", [])
    validate_references(refs, evidence)
    validate_actions(actions, {str(item["id"]) for item in selected})
    return {"answer": reply["answer"], "evidence_ids": refs, "actions": actions}


def filter_platform(items, evidence, platform):
    if not platform:
        return items
    evidence_ids = {item["id"] for item in evidence if item.get("platform") == platform}
    return [item for item in items if evidence_ids.intersection(item.get("evidence_ids", []))]


def within_period(item, date_from, date_to):
    if date_from is None and date_to is None:
        return True
    if not item.get("created_at"):
        return False
    created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
    if date_from and created < date_from:
        return False
    return date_to is None or created <= date_to


def selected_incidents(payload: ChatRequest, snapshot: dict) -> list:
    selected = snapshot.get("incidents", [])
    if payload.incident_id:
        selected = [item for item in selected if item["id"] == payload.incident_id]
    attributes = {key: value for key, value in payload.filters.items() if value and key not in {"platform", "date_from", "date_to", "bbox"}}
    selected = [item for item in selected if all(str(item.get(key, "")) == value for key, value in attributes.items())]
    selected = filter_platform(selected, snapshot.get("evidence", []), payload.filters.get("platform"))
    date_from, date_to = period_bounds(payload.filters)
    area = parse_bbox(payload.filters.get("bbox"))
    selected = [item for item in selected if within_bbox(item, area)]
    return [item for item in selected if within_period(item, date_from, date_to)]


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
    descriptions = [f"{'REAL' if item.get('mode') == 'real' else 'SIMULADO'} · {item.get('category')} en {item.get('locality')}: {item.get('severity')} ({item['id']})" for item in selected[:10]]
    answer = "DEMOSTRACIÓN LOCAL · Respuesta de prueba; no interviene un agente AIDP.\n"
    answer += "\n".join(descriptions) if descriptions else "No hay evidencia que coincida en esta publicación."
    return {"answer": answer, "version": snapshot["version"], "evidence_ids": refs,
            "actions": [{"type": "focus_incident", "incident_id": item["id"]} for item in selected[:3]]}


@app.get("/health")
def health():
    return {"status": "ok", "mode": MODE}


@app.get("/ready")
def ready():
    if MODE == "oci":
        published_snapshot()
        endpoint_from(cloud_object(os.getenv("PRISMA_AGENT_ENDPOINT_KEY", ".control/prisma/agent.json")), os.environ["OCI_REGION"])
    return {"status": "ready", "mode": MODE}


@app.get("/api/prisma/snapshot")
async def snapshot(request: Request):
    data = await snapshot_for(request)
    can_admin = not principal(request).startswith("local-prisma:")
    return {**data, "can_review": can_admin, "can_admin": can_admin}


@app.post("/api/prisma/chat")
async def chat(payload: ChatRequest, request: Request):
    data = await snapshot_for(request)
    validate_context(payload, data)
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
        raise HTTPException(422, "Identificador inválido")
    return await admin_request(request, "POST", f"/api/prisma/incidents/{incident_id}/review", payload.model_dump())


@app.get("/api/prisma/context")
async def context(request: Request):
    principal(request)
    from context_layers import context_layers
    return await context_layers()


@app.get("/{path:path}")
def static(path: str):
    target = (STATIC / path).resolve()
    if not target.is_relative_to(STATIC.resolve()):
        raise HTTPException(404)
    if not target.is_file():
        target = STATIC / "index.html"
    if not target.is_file():
        raise HTTPException(503, "El visor todavía no está construido")
    return FileResponse(target)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081, access_log=False)
