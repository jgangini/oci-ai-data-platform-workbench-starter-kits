"""Administration of the deployed AIDP workflow, using the existing operator bootstrap."""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import quote
from uuid import uuid4

from fastapi import HTTPException

from ..autonomous import AutonomousGovernanceClient
from .core import PLATFORMS, utc_text, default_source
from .database import read_document, mutate_document
from .scheduling import needs_schedule, set_schedule, submit_run


class CloudRuntime:
    def __init__(self, settings, aidp_factory):
        self.settings, self.aidp_factory = settings, aidp_factory
        self.database = AutonomousGovernanceClient(settings.autonomous_runtime_file)

    def _connect(self):
        return self.database._connect(self.database._runtime())

    async def _io(self, function, *args):
        try:
            return await asyncio.to_thread(function, *args)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "El runtime PRISMA AIDP/Autonomous no está listo; revise el despliegue") from exc

    def _doc(self, name):
        with self._connect() as connection:
            return read_document(connection, name)

    def _change(self, name, change):
        with self._connect() as connection:
            return mutate_document(connection, name, change)

    def _sources(self):
        configuration = self._doc("configuration")
        sources = []
        for platform in PLATFORMS:
            source = {**default_source(platform), **configuration.get("sources", {}).get(platform, {})}
            status = self._doc("status_" + platform)
            source.update({key: value for key, value in status.items() if key in
                {"status", "last_run_at", "next_due", "last_error", "requested_action", "request_id", "last_received_count"}})
            if not source["enabled"]:
                source.update(status="disabled", next_due=None)
            elif source["mode"] == "simulation" and not status.get("requested_action"):
                source.update(status="simulation", last_error=None, next_due=None)
            elif status.get("configuration_revision") != configuration.get("revision", 0) and not status.get("requested_action"):
                source.update(status="ready", last_error=None, next_due=None)
            sources.append(source)
        return {"sources": sources, "simulation": self._simulation_state(), "runtime": "aidp"}

    async def sources(self):
        return await self._io(self._sources)

    def _credential(self, platform, token):
        client = self.aidp_factory()
        name = "PrismaSource_" + platform
        existing = [item for item in client._list("/credentials", params={"displayName": name}, phase="control")
                    if (item.get("displayName") or item.get("name")) == name]
        if len(existing) > 1:
            raise HTTPException(409, "Existen credenciales PRISMA duplicadas")
        payload = {"displayName": name, "type": "SECRET_TOKEN", "credentialDescription": "PRISMA API source credential",
                   "credentialDetails": {"credentialType": "SECRET_TOKEN", "secretTokenPair": [{"secretKey": "bearer_token", "secretValue": token}]}}
        if existing:
            key = str(existing[0].get("key") or existing[0].get("id"))
            client._request("PUT", "/credentials/" + quote(key, safe=""), payload=payload, phase="control")
        else:
            client._request("POST", "/credentials", payload=payload, phase="control")
        return name

    def _update(self, platform, payload):
        if platform not in PLATFORMS:
            raise HTTPException(404, "Plataforma desconocida")
        fields = {name: value for name, value in payload.items() if name in {"enabled", "mode", "query", "interval_minutes"}}
        old = {**default_source(platform), **self._doc("configuration").get("sources", {}).get(platform, {})}
        candidate = {**old, **fields}
        if candidate["mode"] == "real" and platform != "x":
            raise HTTPException(422, "Solo X tiene conector real; las demás plataformas admiten simulación")
        if candidate["mode"] == "real" and not candidate["query"].strip():
            raise HTTPException(422, "La captura real de X requiere una consulta")
        if payload.get("secret_ref", old["secret_ref"]) not in {old["secret_ref"], "PrismaSource_" + platform}:
            raise HTTPException(422, "AIDP administra la referencia de credencial de esta fuente")
        token = payload.get("bearer_token")
        if token:
            if len(token) > 8192 or any(character.isspace() for character in token):
                raise HTTPException(422, "Valor de credencial inválido")
            fields.update(secret_ref=self._credential(platform, token), credential_configured=True)
        def change(document):
            sources = dict(document.get("sources", {}))
            old = {**default_source(platform), **sources.get(platform, {})}
            if fields.get("query", old["query"]) != old["query"] or fields.get("mode", old["mode"]) != old["mode"]:
                fields["query_version"] = str(uuid4())
            sources[platform] = {**old, **fields}
            return {**document, "sources": sources}
        document = self._change("configuration", change)
        return document["sources"][platform]

    async def update_source(self, platform, payload):
        result = await self._io(self._update, platform, payload)
        await self._io(self._wake, str(uuid4()))
        return result

    def _wake(self, request_id):
        runtime = self._doc("runtime")
        client = self.aidp_factory()
        # Persisted requests survive a busy job; queue this finite run before changing the recurring schedule.
        submit_run(client._request, runtime, request_id)
        set_schedule(client._request, runtime, needs_schedule(self._doc("configuration"), self._doc("simulation")))

    def _request_source(self, platform, action):
        if platform not in PLATFORMS:
            raise HTTPException(404, "Plataforma desconocida")
        source = next(item for item in self._sources()["sources"] if item["platform"] == platform)
        if not source["enabled"]:
            raise HTTPException(409, "Habilite la fuente antes de ejecutarla")
        if source["mode"] == "real" and (platform != "x" or not source["credential_configured"]):
            raise HTTPException(409, "La fuente real requiere un conector y credencial habilitados")
        request_id = str(uuid4())
        self._change("status_" + platform, lambda doc: {**doc, "status": "queued", "requested_action": action,
            "request_id": request_id, "next_due": utc_text(time.time()), "last_error": None})
        self._wake(request_id)
        return {"status": "queued", "message": "Solicitud registrada como ejecución finita AIDP", "source": {**source, "status": "queued"}}

    async def test_source(self, platform):
        return await self._io(self._request_source, platform, "test")

    async def run_source(self, platform):
        return await self._io(self._request_source, platform, "run")

    def _simulation_state(self):
        state = self._doc("simulation")
        elapsed = float(state.get("elapsed_seconds", 0))
        if state.get("status") == "running":
            elapsed = min(600, elapsed + max(0, time.time() - state.get("started_at", time.time())))
        return {"status": "completed" if elapsed >= 600 else state.get("status", "idle"),
                "elapsed_seconds": elapsed, "duration_seconds": 600, "run_id": state.get("run_id"), "anchor_at": state.get("anchor_at")}

    def _simulation(self, action):
        if action not in {"start", "pause", "resume", "reset", "replay"}:
            raise HTTPException(422, "Acción de simulación inválida")
        state = self._simulation_state()
        if action in {"start", "reset", "replay"}:
            state = {"elapsed_seconds": 0, "run_id": str(uuid4()), "anchor_at": time.time()}
        elif state.get("anchor_at") is None:
            state["anchor_at"] = time.time() - state["elapsed_seconds"]
        state.update(status="paused" if action in {"pause", "reset"} else "running", started_at=time.time())
        self._change("simulation", lambda current: {**current, **state})
        self._wake(str(uuid4()))
        return self._simulation_state()

    async def simulation(self, action):
        return await self._io(self._simulation, action)

    def _snapshot(self):
        client = self.aidp_factory()
        bucket = self._doc("runtime").get("bucket") or self.settings.bucket_name
        def fetch(key):
            response = client.object_storage.get_object(self.settings.objectstorage_namespace, bucket, key)
            return json.loads(response.data.content)
        pointer = fetch("04_gold/prisma/current.json")
        key = str(pointer.get("snapshot_key", ""))
        if not key.startswith("04_gold/prisma/snapshots/") or ".." in key:
            raise HTTPException(503, "Publicación PRISMA inválida")
        snapshot = fetch(key)
        if snapshot.get("version") != pointer.get("version"):
            raise HTTPException(503, "Publicación PRISMA incompleta")
        return {**snapshot, "runtime": "aidp"}

    async def snapshot(self):
        return await self._io(self._snapshot)

    def _review(self, incident_id, status, note):
        snapshot = self._snapshot()
        incident = next((item for item in snapshot.get("incidents", []) if item["id"] == incident_id), None)
        if incident is None:
            raise HTTPException(404, "Incidente desconocido")
        self._change("reviews", lambda doc: {**doc, "items": {**doc.get("items", {}),
            incident_id: {"status": status, "note": note, "updated_at": utc_text(time.time())}}})
        self._wake(str(uuid4()))
        return {**incident, "review_status": status, "review_pending_publication": True}

    async def review(self, incident_id, status, note):
        return await self._io(self._review, incident_id, status, note)

    def _chat(self, payload, cookie, key):
        from .agent_gateway import invoke
        client = self.aidp_factory()
        bucket = self._doc("runtime").get("bucket") or self.settings.bucket_name
        response = client.object_storage.get_object(self.settings.objectstorage_namespace,
            bucket, ".control/prisma/agent.json")
        metadata = json.loads(response.data.content)
        if metadata.get("state") != "ACTIVE":
            raise HTTPException(503, "El agente PRISMA no está activo")
        return invoke(client, metadata["endpoint"], payload, cookie, key, self._snapshot())

    async def chat(self, payload, cookie, key):
        return await self._io(self._chat, payload, cookie, key)
