"""Runtime/gateway contract tests; OCI responses are doubles, never live acceptance."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main
from app.aidp import LocalAidpClient
from app.config import Settings
from app.identity import LocalIdentityClient
from app.prisma.api import runtime_for
from app.prisma.cloud import CloudRuntime
from app.prisma.local import LocalPrismaRuntime
from app.prisma.module import TerritorialModule
from app.prisma.oci_provider import OciProvider
from app.security import issue_session


def settings_at(tmp_path, **values):
    return Settings(cookie_secure=False, aidp_settings_file=str(tmp_path / "settings.json"),
        session_secret_file=str(tmp_path / "session.key"), autonomous_runtime_file=str(tmp_path / "missing-runtime.json"),
        **values)


@pytest.mark.parametrize("local,mode,expected", [(True, None, True), (False, None, False),
    (True, "oci", False), (False, "local", True), (True, "local", True), (False, "oci", False)])
def test_prisma_mode_defaults_remain_independent_of_explicit_identity_override(tmp_path, local, mode, expected):
    settings = settings_at(tmp_path, local_development_mode=local, prisma_mode=mode)
    app = main.create_app(settings)
    assert settings.prisma_local_mode is expected
    assert isinstance(runtime_for(app), LocalPrismaRuntime if expected else CloudRuntime)
    assert runtime_for(app) is runtime_for(app)
    assert Settings(local_development_mode=local).prisma_local_mode is local
    assert replace(Settings(), local_development_mode=True).prisma_local_mode is True


def test_prisma_mode_environment_is_explicit_and_rejects_unknown_values(monkeypatch):
    monkeypatch.delenv("PRISMA_MODE", raising=False)
    monkeypatch.setenv("LOCAL_DEVELOPMENT_MODE", "true")
    assert Settings.from_env().prisma_local_mode is True
    monkeypatch.setenv("PRISMA_MODE", "oci")
    settings = Settings.from_env()
    assert settings.local_development_mode is True and settings.prisma_local_mode is False
    for invalid in ("", "fixture", "OCI"):
        monkeypatch.setenv("PRISMA_MODE", invalid)
        with pytest.raises(ValueError, match="PRISMA_MODE"):
            Settings.from_env()


@pytest.mark.parametrize("question,social,sensors", [
    ("¿Cuál es la lectura del sensor de lluvia en Kennedy?", False, True),
    ("¿Qué publicaciones de X reportan inundación en Kennedy?", True, False),
    ("Compara los sensores de Kennedy con los reportes sociales", True, True),
    ("¿Hay evidencia en esta publicación vacía?", False, False),
])
def test_local_identity_oci_uses_same_cloud_publication_and_signed_agent_without_fixture(
        monkeypatch, tmp_path, question, social, sensors):
    settings = settings_at(tmp_path, local_development_mode=True, prisma_mode="oci", prisma_enabled=True,
        aidp_region="us-chicago-1", objectstorage_namespace="test-namespace", bucket_name="test-bucket")
    snapshot = {"version": "cloud-42", "published_at": "2026-10-04T15:00:00Z",
        "incidents": [{"id": "incident-42", "locality": "Kennedy", "evidence_ids": ["x:42"]}] if social else [],
        "evidence": [{"id": "x:42", "platform": "x", "text": "Reporte de inundación"}] if social else [],
        "sensors": [{"id": "reading-42", "sensor_id": "station-42", "value": 32.5, "unit": "mm/h"}] if sensors else []}
    reply = {"answer": "Lectura 32.5 mm/h" if sensors else "Reporte social x:42" if social else "No hay evidencia suficiente en esta publicación.",
        "version": "cloud-42", "evidence_ids": ["x:42"] if social else [], "sensor_evidence_ids": ["reading-42"] if sensors else [], "actions": []}
    endpoint = "https://gateway.aidp.us-chicago-1.oci.oraclecloud.com/agentendpoint/prisma/chat"
    objects = {"04_gold/prisma/current.json": {"version": "cloud-42", "snapshot_key": "04_gold/prisma/snapshots/cloud-42.json"},
        "04_gold/prisma/snapshots/cloud-42.json": snapshot, ".control/prisma/agent.json": {"state": "ACTIVE", "endpoint": endpoint}}
    reads, requests, created = [], [], []
    signer = object()

    def read(namespace, bucket, key):
        reads.append((namespace, bucket, key))
        return SimpleNamespace(data=SimpleNamespace(content=json.dumps(objects[key]).encode()))

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"status": "completed", "result": {"messages": [
            {"type": "human", "content": "PRIVATE question"}, {"type": "tool", "content": "PRIVATE tool trace"},
            {"type": "ai", "content": json.dumps(reply)}]}})

    cloud = SimpleNamespace(settings=settings, signer=signer, object_storage=SimpleNamespace(get_object=read),
        session=SimpleNamespace(post=post), close=AsyncMock())
    monkeypatch.setattr(main, "AidpClient", lambda configured: created.append(configured) or cloud)
    producer = AsyncMock()
    monkeypatch.setattr(main, "run_local_prisma", producer)
    app = main.create_app(settings)
    runtime = runtime_for(app)
    monkeypatch.setattr(runtime, "_doc", lambda _: {"bucket": "test-bucket"})
    with TestClient(app) as client:
        assert isinstance(app.state.identity_factory(), LocalIdentityClient)
        assert isinstance(app.state.aidp_factory(), LocalAidpClient)
        assert client.get("/api/prisma/snapshot").status_code == 401
        client.cookies.set(main.LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
        published = client.get("/api/prisma/snapshot")
        assert published.status_code == 200 and published.json() == {**snapshot, "runtime": "aidp"}
        payload = {"question": question, "version": "cloud-42", "session_id": "855a0379-97d5-4c8a-8137-76a5d24912b2",
            "filters": {"locality": "Kennedy"}, **({"sensor_id": "station-42"} if sensors else {})}
        answer = client.post("/api/admin/prisma/chat", json=payload)
        assert answer.status_code == 200 and answer.json() == reply
        assert "DEMOSTRACIÓN LOCAL" not in answer.text and "local_fixture" not in answer.text
        assert client.post("/api/admin/prisma/chat", json={**payload, "version": "local-version"}).status_code == 409
        assert len(requests) == 1 and created == [settings]
        url, sent = requests[0]
        assert url == endpoint and sent["auth"] is signer
        assert sent["json"]["trace"] is False and sent["json"]["isStreamEnabled"] is False
        content = json.loads(sent["json"]["input"][0]["content"][0]["text"])
        assert content == {"question": question, "context": {"version": "cloud-42", "published_at": snapshot["published_at"],
            "incident_id": None, "sensor_id": payload.get("sensor_id"), "filters": payload["filters"]}}
        assert all(namespace == "test-namespace" and bucket == "test-bucket" for namespace, bucket, _ in reads)
        assert not hasattr(app.state, "prisma_runtime")
    producer.assert_not_awaited()
    cloud.close.assert_awaited_once()


def test_explicit_cloud_mode_fails_closed_when_operator_configuration_is_missing(monkeypatch, tmp_path):
    settings = settings_at(tmp_path, local_development_mode=True, prisma_mode="oci")
    monkeypatch.setattr(main, "AidpClient", lambda _: (_ for _ in ()).throw(RuntimeError("PRIVATE missing credentials")))
    app = main.create_app(settings)
    with TestClient(app) as client:
        client.cookies.set(main.LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
        for response in (client.get("/api/prisma/snapshot"), client.post("/api/admin/prisma/chat", json={
                "question": "Sensores", "version": "v1", "session_id": "855a0379-97d5-4c8a-8137-76a5d24912b2"})):
            assert response.status_code == 503
            assert "PRIVATE" not in response.text and "DEMOSTRACIÓN" not in response.text
        assert not hasattr(app.state, "prisma_runtime")


def test_module_and_provider_state_follow_cloud_mode_even_with_local_identity(tmp_path, monkeypatch):
    settings = settings_at(tmp_path, local_development_mode=True, prisma_mode="oci", prisma_enabled=True)
    documents = {"status_module": {}, "configuration": {"oci_provider": {"model_id": "stored-cloud-model"}}}
    def change(name, update):
        documents[name] = update(documents[name])
        return documents[name]
    runtime = SimpleNamespace(_doc=lambda name: documents[name], _change=change)
    module = TerritorialModule(settings, runtime)
    monkeypatch.setattr(module, "_prerequisites", lambda **_: (_ for _ in ()).throw(RuntimeError("Cloud deployment missing")))
    with pytest.raises(HTTPException) as failure:
        asyncio.run(module.status(True))
    assert failure.value.status_code == 503 and documents["status_module"] == {}
    assert module._response({})["runtime"] == "aidp"
    provider = OciProvider(settings, runtime, lambda: None)
    assert provider._state()["model_id"] == "stored-cloud-model"
    provider._state(lambda current: {**current, "last_test": "checked"})
    assert documents["configuration"]["oci_provider"]["last_test"] == "checked"


@pytest.mark.parametrize("local,mode,enabled,starts", [(True, None, False, True), (True, "oci", True, False),
    (False, None, True, True), (False, None, False, False)])
def test_existing_vm_remains_producer_owner_but_local_cloud_viewer_never_starts_one(monkeypatch, tmp_path, local, mode, enabled, starts):
    producer = AsyncMock()
    monkeypatch.setattr(main, "run_local_prisma", producer)
    app = main.create_app(settings_at(tmp_path, local_development_mode=local, prisma_mode=mode, prisma_enabled=enabled))
    with TestClient(app):
        pass
    assert producer.await_count == int(starts)
