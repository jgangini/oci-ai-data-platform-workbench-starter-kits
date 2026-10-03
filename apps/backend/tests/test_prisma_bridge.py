"""Bridge contract checks use fake publications and clients; no cloud or database."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.prisma import agent_gateway


sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
spec = importlib.util.spec_from_file_location("prisma_viewer_bridge", Path(__file__).parents[2] / "prisma-viewer" / "server.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)
HEADERS = {"x-prisma-user": "operator", "cookie": "admin-session=fixture-value"}
PUBLIC_ID = "855a0379-97d5-4c8a-8137-76a5d24912b2"
SNAPSHOT = {"version": "v1", "published_at": "2026-10-05T14:00:00Z", "incidents": [
    {"id": "incident-1", "mode": "simulation", "category": "inundacion", "locality": "Kennedy", "severity": "high", "created_at": "2026-10-05T14:00:00Z", "evidence_ids": ["x:1"]},
    {"id": "incident-2", "mode": "real", "category": "inundacion", "locality": "Suba", "severity": "medium", "created_at": "2026-10-05T14:05:00Z", "evidence_ids": ["sensor:2"]},
], "evidence": [{"id": "x:1", "mode": "simulation", "platform": "x"}, {"id": "sensor:2", "mode": "real", "platform": "sensor"}]}
ANSWER = {"answer": "Alerta simulada respaldada por x:1", "version": "v1", "evidence_ids": ["x:1"], "actions": [{"type": "focus_incident", "incident_id": "incident-1"}]}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(bridge, "MODE", "local")
    async def admin_request(*_args, **_kwargs):
        return SNAPSHOT
    monkeypatch.setattr(bridge, "admin_request", admin_request)
    with TestClient(bridge.app) as test_client:
        yield test_client


@pytest.mark.parametrize("headers", [{}, {"x-prisma-user": "operator"}, {"cookie": "admin-session=fixture-value"}])
def test_bridge_rejects_absent_authenticated_proxy_context(client, headers):
    assert client.get("/api/prisma/snapshot", headers=headers).status_code == 401
    assert client.post("/api/prisma/chat", headers=headers, json={"question": "Hola", "version": "v1"}).status_code == 401


def test_chat_is_grounded_versioned_and_preserves_public_session(client):
    response = client.post("/api/prisma/chat", headers=HEADERS, json={"question": "¿Qué ocurrió?", "version": "v1", "session_id": PUBLIC_ID, "filters": {"platform": "x", "locality": ""}})
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == PUBLIC_ID
    assert body["evidence_ids"] == ["x:1"]
    assert "SIMULADO" in body["answer"] and "DEMOSTRACIÓN LOCAL" in body["answer"]
    assert body["runtime"] == "local_fixture"
    assert UUID(client.post("/api/prisma/chat", headers=HEADERS, json={"question": "Resumen", "version": "v1"}).json()["session_id"])


@pytest.mark.parametrize("override,status", [({"version": "old"}, 409), ({"incident_id": "invented"}, 422), ({"filters": {"script": "bad"}}, 422), ({"session_id": "not-a-uuid"}, 422), ({"question": "   "}, 422), ({"script": "bad"}, 422)])
def test_chat_rejects_stale_or_invalid_context(client, override, status):
    response = client.post("/api/prisma/chat", headers=HEADERS, json={"question": "Resumen", "version": "v1", **override})
    assert response.status_code == status


@pytest.mark.parametrize("changes", [{"evidence_ids": ["fake"]}, {"version": "v0"}, {"answer": ""}, {"actions": [{"type": "execute", "code": "bad"}]}, {"actions": [{"type": "focus_incident", "incident_id": "fake"}]}, {"actions": [{"type": "focus_incident", "incident_id": ["bad"]}]}, {"actions": [{"type": "filter_incidents", "filters": {"url": "https://example.com"}}]}])
def test_cloud_reply_cannot_invent_references_or_actions(changes):
    with pytest.raises(HTTPException) as error:
        bridge.grounded_reply({**ANSWER, **changes}, SNAPSHOT)
    assert error.value.status_code == 502


def test_cloud_failure_never_calls_fixture_answer(client, monkeypatch):
    monkeypatch.setattr(bridge, "MODE", "oci")
    def unavailable():
        raise HTTPException(503, "Publicación no disponible")
    monkeypatch.setattr(bridge, "published_snapshot", unavailable)
    monkeypatch.setattr(bridge, "fixture_reply", lambda *_: pytest.fail("Cloud failure used local fixtures"))
    response = client.post("/api/prisma/chat", headers=HEADERS, json={"question": "Resumen", "version": "v1"})
    assert response.status_code == 503


def test_period_is_inclusive_and_the_local_answer_uses_the_same_window(client):
    response = client.post("/api/prisma/chat", headers=HEADERS, json={"question": "Resumen", "version": "v1", "filters": {"date_from": "2026-10-05T09:00:00-05:00", "date_to": "2026-10-05T14:00:00Z"}})
    assert response.status_code == 200
    assert response.json()["evidence_ids"] == ["x:1"]
    for filters in [{"date_from": "yesterday"}, {"date_from": "2026-10-05T09:00:00"}, {"date_from": "2026-10-06T00:00:00Z", "date_to": "2026-10-05T00:00:00Z"}]:
        assert client.post("/api/prisma/chat", headers=HEADERS, json={"question": "Resumen", "version": "v1", "filters": filters}).status_code == 422
    with pytest.raises(HTTPException) as error:
        bridge.grounded_reply({**ANSWER, "actions": [{"type": "filter_incidents", "filters": {"date_from": "not-a-date"}}]}, SNAPSHOT)
    assert error.value.status_code == 502


def test_area_preserves_evidence_and_focus_scope():
    data = {**SNAPSHOT, "incidents": [
        {**SNAPSHOT["incidents"][0], "lat": 4.627, "lon": -74.155},
        {**SNAPSHOT["incidents"][1], "lat": 4.741, "lon": -74.084},
    ]}
    payload = bridge.ChatRequest(question="Resumen", version="v1", filters={"bbox": "-74.2,4.6,-74.1,4.7"})
    bridge.validate_context(payload, data)
    assert bridge.fixture_reply(payload, data)["evidence_ids"] == ["x:1"]
    assert bridge.grounded_reply(ANSWER, data, payload)["evidence_ids"] == ["x:1"]
    for changes in [{"evidence_ids": ["sensor:2"]}, {"actions": [{"type": "focus_incident", "incident_id": "incident-2"}]}]:
        with pytest.raises(HTTPException) as error:
            bridge.grounded_reply({**ANSWER, **changes}, data, payload)
        assert error.value.status_code == 502
    payload.filters["bbox"] = "bad"
    with pytest.raises(HTTPException) as error:
        bridge.validate_context(payload, data)
    assert error.value.status_code == 422


@pytest.mark.parametrize("context", [{"incident_id": "incident-1"}, {"filters": {"platform": "x"}}, {"filters": {"locality": "Kennedy"}}, {"filters": {"date_to": "2026-10-05T14:00:00Z"}}])
def test_existing_reference_or_focus_outside_explicit_context_is_rejected(context):
    payload = bridge.ChatRequest(question="Resumen", version="v1", **context)
    for changes in [{"evidence_ids": ["sensor:2"]}, {"actions": [{"type": "focus_incident", "incident_id": "incident-2"}]}]:
        with pytest.raises(HTTPException) as error:
            bridge.grounded_reply({**ANSWER, **changes}, SNAPSHOT, payload)
        assert error.value.status_code == 502


def test_cloud_chat_forwards_cookie_and_context_to_operator_boundary(client, monkeypatch):
    monkeypatch.setattr(bridge, "MODE", "oci")
    monkeypatch.setattr(bridge, "published_snapshot", lambda: SNAPSHOT)
    calls = []
    async def admin_request(request, method, path, payload):
        calls.append((request.headers["cookie"], method, path, payload))
        return ANSWER
    monkeypatch.setattr(bridge, "admin_request", admin_request)
    body = client.post("/api/prisma/chat", headers=HEADERS, json={"question": "Resumen", "version": "v1", "session_id": PUBLIC_ID}).json()
    assert body["runtime"] == "aidp"
    assert calls[0][:3] == (HEADERS["cookie"], "POST", "/api/admin/prisma/chat")
    assert calls[0][3]["session_id"] == PUBLIC_ID


@pytest.mark.parametrize("status", ["pending", "validated", "rejected"])
def test_review_uses_backend_status_contract(client, monkeypatch, status):
    calls = []
    async def admin_request(_request, method, path, payload):
        calls.append((method, path, payload))
        return {"review_status": payload["status"]}
    monkeypatch.setattr(bridge, "admin_request", admin_request)
    response = client.post("/api/prisma/incidents/incident-1/review", headers=HEADERS, json={"status": status, "note": "Contraste humano"})
    assert response.status_code == 200
    assert calls == [("POST", "/api/prisma/incidents/incident-1/review", {"status": status, "note": "Contraste humano"})]


def test_endpoint_validation_is_region_exact_and_active():
    endpoint = "https://gateway.aidp.us-ashburn-1.oci.oraclecloud.com/agentendpoint/prisma/chat"
    assert bridge.endpoint_from({"endpoint": endpoint, "state": "ACTIVE"}, "us-ashburn-1") == endpoint
    for value in [endpoint + "?token=x", endpoint.replace("https", "http"), endpoint.replace("us-ashburn-1", "eu-frankfurt-1")]:
        with pytest.raises(HTTPException):
            bridge.endpoint_from({"endpoint": value, "state": "ACTIVE"}, "us-ashburn-1")


def test_gateway_hides_tools_and_scopes_sessions_to_authenticated_cookie():
    requests = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"status": "completed", "output": [{"role": "tool", "text": "secret"}, {"role": "assistant", "content": [{"type": "OUTPUT_TEXT", "text": json.dumps(ANSWER)}, {"type": "reasoning", "text": "private reasoning"}]}]}
    def post(endpoint, **kwargs):
        requests.append((endpoint, kwargs))
        return Response()
    client = SimpleNamespace(settings=SimpleNamespace(aidp_region="us-ashburn-1"), session=SimpleNamespace(post=post), signer=object())
    endpoint = "https://gateway.aidp.us-ashburn-1.oci.oraclecloud.com/agentendpoint/prisma/chat"
    payload = {"question": "Resumen", "version": "v1", "session_id": PUBLIC_ID}
    for cookie in ["admin-a", "admin-a", "admin-b"]:
        assert agent_gateway.invoke(client, endpoint, payload, cookie, b"fixture-key", SNAPSHOT) == ANSWER
    keys = [request[1]["json"]["sessionKey"] for request in requests]
    assert keys[0] == keys[1] and keys[0] != keys[2] and keys[0] != PUBLIC_ID
    assert requests[0][1]["json"]["isStreamEnabled"] is False
    assert requests[0][1]["json"]["trace"] is False
    assert agent_gateway.assistant_texts({"role": "tool", "text": "secret"}) == []
