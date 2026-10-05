"""Bridge contract checks use fake publications and clients; no cloud or database."""
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.territorial import agent_gateway


sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
spec = importlib.util.spec_from_file_location("territorial_viewer_bridge", Path(__file__).parents[2] / "territorial-viewer" / "server.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)
HEADERS = {"x-prisma-user": "operator", "cookie": "admin-session=fixture-value"}
PUBLIC_ID = "855a0379-97d5-4c8a-8137-76a5d24912b2"


@pytest.mark.parametrize("requested", ["Synthetic", "simulation"])
@pytest.mark.parametrize("stored", ["Synthetic", "simulation"])
def test_mode_filters_match_both_provenance_spellings_without_including_real(requested, stored):
    events = [{"id": "match", "mode": stored, "locality": "Kennedy"},
              {"id": "real", "mode": "real", "locality": "Kennedy"},
              {"id": "elsewhere", "mode": stored, "locality": "Suba"}]
    payload = bridge.ChatRequest(question="Resumen", version="v1", filters={"mode": requested, "locality": "Kennedy"})
    assert bridge.selected_incidents(payload, {"incidents": events, "evidence": []}) == [events[0]]


SNAPSHOT = {"version": "v1", "published_at": "2026-10-05T14:00:00Z", "incidents": [
    {"id": "incident-1", "mode": "simulation", "category": "inundacion", "locality": "Kennedy", "severity": "high", "created_at": "2026-10-05T14:00:00Z", "evidence_ids": ["x:1"]},
    {"id": "incident-2", "mode": "real", "category": "inundacion", "locality": "Suba", "severity": "medium", "created_at": "2026-10-05T14:05:00Z", "evidence_ids": ["sensor:2"]},
], "evidence": [{"id": "x:1", "mode": "simulation", "platform": "x", "created_at": "2026-10-05T14:00:00Z"},
                 {"id": "sensor:2", "mode": "real", "platform": "sensor", "created_at": "2026-10-05T14:05:00Z"}]}
ANSWER = {"answer": "Alerta simulada respaldada por x:1", "version": "v1", "evidence_ids": ["x:1"], "actions": [{"type": "focus_incident", "incident_id": "incident-1"}]}
SENSOR_READING = {"id": "reading-1", "sensor_id": "station-1", "sensor_type": "rainfall", "observed_at": "2026-10-05T14:00:00Z",
                  "lat": 4.62, "lon": -74.15, "locality": "Kennedy", "value": 32.5, "unit": "mm/h",
                  "status": "warning", "mode": "Synthetic", "is_simulated": True}


@pytest.mark.parametrize("route,payload", [("/api/territorial/oci-chat", {"question": "hello"}),
                                         ("/api/territorial/oci-voice/turn", {"audio_base64": "fixture"})])
def test_oci_bridge_preserves_safe_provider_failure_without_forwarding_raw_fields(monkeypatch, route, payload):
    def respond(request):
        assert request.headers["cookie"] == HEADERS["cookie"]
        return httpx.Response(429, headers={"Retry-After": "60"}, json={"detail": {
            "code": "oci_rate_limited", "message": "PRIVATE CONFIG /server/key.pem",
            "provider_status": 429, "request_id": "safe/request-id", "private_key": "NEVER FORWARD"}})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    with TestClient(bridge.app) as client:
        response = client.post(route, headers={**HEADERS, "x-gev-origin": "http://testserver", "origin": "http://testserver"}, json=payload)
    assert response.status_code == 429 and response.headers["Retry-After"] == "60"
    assert response.json()["detail"] == {"code": "oci_rate_limited", "message": "OCI quota or rate limit reached; try again later",
                                          "provider_status": 429, "request_id": "safe/request-id"}
    assert "PRIVATE" not in response.text and "key.pem" not in response.text and "NEVER" not in response.text


def test_voice_bridge_requires_session_and_same_origin_before_forwarding(monkeypatch):
    seen = []
    async def forward(_request, method, path, payload=None):
        seen.append((method, path, payload))
        return {"configured": True}
    monkeypatch.setattr(bridge, "admin_request", forward)
    with TestClient(bridge.app) as client:
        assert client.get("/api/territorial/oci-voice").status_code == 401
        for headers, status in [({}, 401), (HEADERS, 403),
                ({**HEADERS, "x-gev-origin": "http://testserver", "origin": "https://foreign.example"}, 403)]:
            assert client.post("/api/territorial/oci-voice/turn", headers=headers, json={}).status_code == status
        assert not seen
        assert client.get("/api/territorial/oci-voice", headers=HEADERS).json() == {"configured": True}
        payload = {"audio_base64": "fixture", "mime_type": "audio/wav"}
        assert client.post("/api/territorial/oci-voice/turn", headers={**HEADERS,
            "x-gev-origin": "http://testserver", "origin": "http://testserver"}, json=payload).status_code == 200
    assert seen[-1] == ("POST", "/api/territorial/oci-voice/turn", payload)


def test_sensor_coordinate_bridge_checks_admin_origin_identifier_and_body(monkeypatch):
    seen = []
    async def forward(_request, method, path, payload=None):
        seen.append((method, path, payload))
        return {"sensor_id": "station-1", "lat": payload["lat"], "lon": payload["lon"],
                "location_saved": True, "location_pending_publication": True}
    monkeypatch.setattr(bridge, "admin_request", forward)
    payload = {"lat": 4.6, "lon": -74.1, "expected_lat": 4.5, "expected_lon": -74.0}
    headers = {**HEADERS, "x-gev-origin": "http://testserver", "origin": "http://testserver"}
    url = "/api/territorial/sensors/station-1/location"
    with TestClient(bridge.app) as client:
        assert client.post(url, json=payload).status_code == 401
        assert client.post(url, headers=HEADERS, json=payload).status_code == 403
        assert client.post(url, headers={**headers, "origin": "https://foreign.example"}, json=payload).status_code == 403
        assert client.post(url, headers={**headers, "x-prisma-user": "local-prisma:viewer"}, json=payload).status_code == 403
        assert client.post(url.replace("station-1", "bad.id"), headers=headers, json=payload).status_code == 422
        assert client.post(url, headers=headers, json={**payload, "lat": True}).status_code == 422
        assert client.post(url, headers=headers, json={"lat": 4.6, "lon": -74.1}).status_code == 422
        assert not seen
        assert client.post(url, headers=headers, json=payload).json()["location_pending_publication"]
    assert seen == [("POST", url, payload)]


@pytest.mark.parametrize("supplied", [{"code": "unexpected", "message": "secret"}, ["secret"], "secret", None])
def test_oci_bridge_unknown_errors_remain_generic(supplied):
    failure = bridge.admin_failure(httpx.Response(503, json={"detail": supplied}), "/api/territorial/oci-provider")
    assert failure.status_code == 503 and failure.detail == "The administration service could not complete the request"


def test_oci_bridge_rejects_secret_shaped_metadata_and_preserves_other_error_contracts():
    response = httpx.Response(503, headers={"Retry-After": "private-config"}, json={"detail": {
        "code": "oci_not_configured", "message": "secret", "provider_status": "PRIVATE", "request_id": "-----BEGIN PRIVATE KEY-----\nsecret"}})
    failure = bridge.admin_failure(response, "/api/territorial/oci-provider")
    assert failure.detail == {"code": "oci_not_configured", "message": "OCI server credentials and compartment are not configured"}
    assert not failure.headers
    assert bridge.admin_failure(response, "/api/territorial/snapshot").detail == "The administration service could not complete the request"


@pytest.mark.parametrize("code", ["oci_invalid_response", "oci_invalid_voice_response"])
@pytest.mark.parametrize("blocked", [True, "PRIVATE reason"])
def test_oci_bridge_preserves_only_boolean_blocked_status_with_a_fixed_message(code, blocked):
    response = httpx.Response(502, json={"detail": {"code": code, "message": "PRIVATE model output",
        "response_blocked": blocked, "request_id": "safe/recitation"}})
    failure = bridge.admin_failure(response, "/api/territorial/oci-voice/turn")
    assert failure.status_code == 502 and failure.detail["request_id"] == "safe/recitation"
    assert "PRIVATE" not in json.dumps(failure.detail)
    if blocked is True:
        assert failure.detail["response_blocked"] is True
        assert failure.detail["message"] == "OCI blocked the model response; no answer was returned"
    else:
        assert "response_blocked" not in failure.detail


@pytest.mark.parametrize("stage,reason", [
    ("analysis_response", "empty_or_oversized"), ("analysis_response", "blocked"),
    ("analysis_schema", "invalid_json"), ("analysis_schema", "invalid_schema"),
    ("speech_response", "audio_too_large"), ("speech_response", "invalid_audio"),
])
def test_voice_bridge_preserves_closed_diagnostics_without_response_content(stage, reason):
    supplied = {"code": "oci_invalid_voice_response", "voice_stage": stage, "voice_reason": reason,
                "request_id": "safe/voice", "message": "PRIVATE audio", "transcript": "PRIVATE transcript"}
    detail = bridge.admin_failure(httpx.Response(502, json={"detail": supplied}), "/api/territorial/oci-voice/turn").detail
    assert (detail["voice_stage"], detail["voice_reason"]) == (stage, reason)
    assert detail["request_id"] == "safe/voice" and detail["message"] != "OCI did not return a valid voice response"
    assert "PRIVATE" not in json.dumps(detail) and "transcript" not in detail


@pytest.mark.parametrize("stage,reason", [("PRIVATE", "invalid_audio"), ("analysis_response", "PRIVATE"),
                                        ("analysis_schema", "invalid_audio"), (["PRIVATE"], {})])
def test_voice_bridge_rejects_unknown_or_mismatched_diagnostics(stage, reason):
    assert bridge.voice_failure_detail({"voice_stage": stage, "voice_reason": reason}) == {}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(bridge, "MODE", "local")
    monkeypatch.delenv("PRISMA_ADMIN_URL", raising=False)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 5, 14, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(bridge, "datetime", Clock)
    async def admin_request(*_args, **_kwargs):
        return SNAPSHOT
    monkeypatch.setattr(bridge, "admin_request", admin_request)
    with TestClient(bridge.app) as test_client:
        yield test_client


@pytest.mark.parametrize("headers", [{}, {"x-prisma-user": "operator"}, {"cookie": "admin-session=fixture-value"}])
def test_bridge_rejects_absent_authenticated_proxy_context(client, headers):
    assert client.get("/api/territorial/snapshot", headers=headers).status_code == 401
    assert client.post("/api/territorial/chat", headers=headers, json={"question": "Hola", "version": "v1"}).status_code == 401


def test_chat_is_grounded_versioned_and_preserves_public_session(client):
    response = client.post("/api/territorial/chat", headers=HEADERS, json={"question": "¿Qué ocurrió?", "version": "v1", "session_id": PUBLIC_ID, "filters": {"platform": "x", "locality": ""}})
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == PUBLIC_ID
    assert body["evidence_ids"] == ["x:1"]
    assert "DEMOSTRACIÓN LOCAL" in body["answer"] and "no interviene un agente AIDP" in body["answer"]
    assert "inundacion en Kennedy: high (incident-1)" in body["answer"]
    assert "SIMULADO ·" not in body["answer"] and "REAL ·" not in body["answer"]
    evidence = client.get("/api/territorial/snapshot", headers=HEADERS).json()["evidence"]
    assert next(item for item in evidence if item["id"] == "x:1")["mode"] == "simulation"
    assert body["runtime"] == "local_fixture"
    assert UUID(client.post("/api/territorial/chat", headers=HEADERS, json={"question": "Resumen", "version": "v1"}).json()["session_id"])


def test_sensor_fixture_has_explicit_provenance_and_scoped_publication_references(client, monkeypatch):
    publication = {**SNAPSHOT, "sensors": [SENSOR_READING]}
    async def snapshot(*_args):
        return publication
    monkeypatch.setattr(bridge, "snapshot_for", snapshot)
    body = {"question": "Compare the sensor with social reports", "version": "v1", "sensor_id": "station-1", "filters": {"locality": "Kennedy"}}
    response = client.post("/api/territorial/chat", headers=HEADERS, json=body)
    assert response.status_code == 200
    answer = response.json()
    assert answer["sensor_evidence_ids"] == ["reading-1"] and answer["evidence_ids"] == ["x:1"]
    assert "32.5 mm/h" in answer["answer"] and "Synthetic" in answer["answer"] and "no son observaciones reales" in answer["answer"]
    assert publication["sensors"] == [SENSOR_READING] and "review_status" not in publication["incidents"][0]
    assert client.post("/api/territorial/chat", headers=HEADERS, json={**body, "sensor_id": "unknown"}).status_code == 422


@pytest.mark.parametrize("filters", [{"locality": "Suba"}, {"mode": "real"}, {"bbox": "-74.1,4.7,-74.0,4.8"}, {"date_from": "2026-10-05T14:01:00Z"}])
def test_sensor_citations_outside_selected_context_are_rejected(filters):
    publication = {**SNAPSHOT, "sensors": [SENSOR_READING]}
    payload = bridge.ChatRequest(question="Sensor status?", version="v1", filters=filters)
    with pytest.raises(HTTPException) as failure:
        bridge.grounded_reply({"answer": "Synthetic reading", "version": "v1", "evidence_ids": [],
                               "sensor_evidence_ids": ["reading-1"], "actions": []}, publication, payload)
    assert failure.value.status_code == 502


@pytest.mark.parametrize("refs,valid", [(["reading-1"], True), (["unknown-reading"], False), ("reading-1", False), ([True], False)])
def test_agent_gateway_requires_published_sensor_evidence(refs, valid):
    answer = {**ANSWER, "sensor_evidence_ids": refs}
    response = SimpleNamespace(raise_for_status=lambda: None,
        json=lambda: {"status": "completed", "output": [{"role": "assistant", "text": json.dumps(answer)}]})
    runtime = SimpleNamespace(settings=SimpleNamespace(aidp_region="us-ashburn-1"), signer=object(),
        session=SimpleNamespace(post=lambda *_args, **_kwargs: response))
    args = (runtime, "https://gateway.aidp.us-ashburn-1.oci.oraclecloud.com/agentendpoint/prisma/chat",
            {"question": "Sensor status?", "version": "v1", "session_id": PUBLIC_ID}, "cookie", b"fixture-key", {**SNAPSHOT, "sensors": [SENSOR_READING]})
    if valid:
        assert agent_gateway.invoke(*args) == answer
    else:
        with pytest.raises(HTTPException) as error:
            agent_gateway.invoke(*args)
        assert error.value.status_code == 502


def test_snapshot_defaults_to_24_hours_and_keeps_full_review_membership(client, monkeypatch):
    records = [{"id": name, "created_at": date} for name, date in [
        ("old", "2026-10-04T14:09:59Z"), ("start", "2026-10-04T14:10:00Z"),
        ("recent", "2026-10-05T14:00:00Z"), ("future", "2026-10-05T14:11:00Z")]]
    incident = {**SNAPSHOT["incidents"][0], "created_at": records[0]["created_at"],
                "evidence_ids": [item["id"] for item in records], "reviewed_evidence_ids": ["old"]}
    original = {**SNAPSHOT, "incidents": [incident], "evidence": records,
                "event_posts": [{"event_id": incident["id"], "post_key": item["id"]} for item in records]}
    async def publication(*_args, **_kwargs):
        return original
    monkeypatch.setattr(bridge, "snapshot_for", publication)
    result = client.get("/api/territorial/snapshot", headers=HEADERS).json()
    assert [item["id"] for item in result["evidence"]] == ["start", "recent"]
    assert result["incidents"] == [incident]  # all IDs retained for the review conflict check
    assert [item["post_key"] for item in result["event_posts"]] == ["start", "recent"]
    assert result["window"] == {"date_from": "2026-10-04T14:10:00+00:00", "date_to": "2026-10-05T14:10:00+00:00"}
    assert len(original["evidence"]) == 4
    exact = {"date_from": records[0]["created_at"], "date_to": records[0]["created_at"]}
    assert [item["id"] for item in client.get("/api/territorial/snapshot", params=exact, headers=HEADERS).json()["evidence"]] == ["old"]
    for invalid in [{"date_from": "yesterday"}, {"date_to": "2026-10-05T14:00:00"}, {"date_from": "2026-10-06T00:00:00Z"}]:
        assert client.get("/api/territorial/snapshot", params=invalid, headers=HEADERS).status_code == 422
    payload = bridge.ChatRequest(question="Resumen", version="v1", filters=result["window"])
    assert bridge.fixture_reply(payload, result)["evidence_ids"] == ["start", "recent"]
    assert bridge.selected_incidents(payload, result)[0]["id"] == incident["id"]


@pytest.mark.parametrize("override,status", [({"version": "old"}, 409), ({"incident_id": "invented"}, 422), ({"filters": {"script": "bad"}}, 422), ({"session_id": "not-a-uuid"}, 422), ({"question": "   "}, 422), ({"script": "bad"}, 422)])
def test_chat_rejects_stale_or_invalid_context(client, override, status):
    response = client.post("/api/territorial/chat", headers=HEADERS, json={"question": "Resumen", "version": "v1", **override})
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
    response = client.post("/api/territorial/chat", headers=HEADERS, json={"question": "Resumen", "version": "v1"})
    assert response.status_code == 503


def test_period_is_inclusive_and_the_local_answer_uses_the_same_window(client):
    response = client.post("/api/territorial/chat", headers=HEADERS, json={"question": "Resumen", "version": "v1", "filters": {"date_from": "2026-10-05T09:00:00-05:00", "date_to": "2026-10-05T14:00:00Z"}})
    assert response.status_code == 200
    assert response.json()["evidence_ids"] == ["x:1"]
    for filters in [{"date_from": "yesterday"}, {"date_from": "2026-10-05T09:00:00"}, {"date_from": "2026-10-06T00:00:00Z", "date_to": "2026-10-05T00:00:00Z"}]:
        assert client.post("/api/territorial/chat", headers=HEADERS, json={"question": "Resumen", "version": "v1", "filters": filters}).status_code == 422
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
    body = client.post("/api/territorial/chat", headers=HEADERS, json={"question": "Resumen", "version": "v1", "session_id": PUBLIC_ID}).json()
    assert body["runtime"] == "aidp"
    assert calls[0][:3] == (HEADERS["cookie"], "POST", "/api/admin/territorial/chat")
    assert calls[0][3]["session_id"] == PUBLIC_ID


@pytest.mark.parametrize("runtime,status", [("aidp", 200), ("local_fixture", 503), (None, 503)])
def test_explicit_cloud_backend_keeps_snapshot_and_agent_together(client, monkeypatch, runtime, status):
    monkeypatch.setattr(bridge, "MODE", "oci")
    monkeypatch.setenv("PRISMA_ADMIN_URL", "http://aidp-lab:8000")
    monkeypatch.setattr(bridge, "published_snapshot", lambda: pytest.fail("Backend mode used instance credentials"))
    monkeypatch.setattr(bridge, "fixture_reply", lambda *_: pytest.fail("Cloud mode used fixture"))
    reading = {**SENSOR_READING, "observed_at": "2026-10-01T14:00:00Z"}
    calls = []
    async def backend(request, method, path, payload=None):
        calls.append((request.headers["cookie"], method, path, payload))
        if method == "GET":
            return {**SNAPSHOT, "sensors": [reading], "runtime": runtime}
        assert payload["filters"] == {}  # Last-known sensors do not inherit the social feed's 24-hour window.
        return {"version": "v1", "answer": "Synthetic last-known reading", "evidence_ids": [],
                "sensor_evidence_ids": [reading["id"]], "actions": []}
    monkeypatch.setattr(bridge, "admin_request", backend)
    response = client.post("/api/territorial/chat", headers=HEADERS,
        json={"question": "Latest sensor reading?", "version": "v1", "sensor_id": reading["sensor_id"]})
    assert response.status_code == status
    assert all(call[0] == HEADERS["cookie"] for call in calls)
    if status == 200:
        assert response.json()["runtime"] == "aidp" and response.json()["sensor_evidence_ids"] == [reading["id"]]
        assert calls[1][2] == "/api/admin/territorial/chat"
    else:
        assert len(calls) == 1


def test_cloud_backend_readiness_defers_publication_to_authenticated_request(client, monkeypatch):
    monkeypatch.setattr(bridge, "MODE", "oci")
    monkeypatch.setenv("PRISMA_ADMIN_URL", "http://aidp-lab:8000")
    monkeypatch.setattr(bridge.native_proxy, "health", lambda: None)
    monkeypatch.setattr(bridge, "published_snapshot", lambda: pytest.fail("Proxy readiness used instance credentials"))
    assert client.get("/ready").json()["publication_check"] == "authenticated_backend_request"
    monkeypatch.setattr(bridge, "MODE", "invalid")
    assert client.get("/ready").status_code == 503


@pytest.mark.parametrize("status", ["pending", "validated", "rejected"])
def test_review_uses_backend_status_contract(client, monkeypatch, status):
    calls = []
    payload = {"status": status, "note": "Contraste humano", "expected_evidence_ids": ["x:1"]}
    saved = {"review_status": status, "review_note": payload["note"], "reviewed_evidence_ids": ["x:1"],
             "review_saved": True, "review_pending_publication": True}
    async def admin_request(_request, method, path, payload):
        calls.append((method, path, payload))
        return saved
    monkeypatch.setattr(bridge, "admin_request", admin_request)
    response = client.post("/api/territorial/incidents/incident-1/review", headers=HEADERS, json=payload)
    assert response.status_code == 200
    assert response.json() == saved
    assert calls == [("POST", "/api/territorial/incidents/incident-1/review", payload)]


def test_review_forwards_coordinates_without_modifying_evidence_guard(client, monkeypatch):
    payload = {"status": "pending", "note": "Position correction", "expected_evidence_ids": ["x:1", "x:2"],
               "lat": 4.63, "lon": -74.15}
    async def forward(_request, method, path, body):
        assert method == "POST" and path.endswith("incident-1/review") and body == payload
        return {**payload, "review_saved": True, "review_pending_publication": True}
    monkeypatch.setattr(bridge, "admin_request", forward)
    result = client.post("/api/territorial/incidents/incident-1/review", headers=HEADERS, json=payload)
    assert result.status_code == 200 and result.json()["lat"] == 4.63


@pytest.mark.parametrize("position", [{"lat": 4}, {"lon": -74}, {"lat": None, "lon": None},
    {"lat": None, "lon": -74}, {"lat": True, "lon": -74}, {"lat": "4", "lon": -74},
    {"lat": 91, "lon": 0}, {"lat": 0, "lon": -181}, {"lat": float("nan"), "lon": -74},
    {"lat": 4, "lon": float("inf")}])
def test_review_models_reject_invalid_or_partial_coordinates(position):
    from app.territorial.api import ReviewRequest
    for model in (ReviewRequest, bridge.ReviewRequest):
        with pytest.raises(ValueError):
            model.model_validate({"status": "pending", **position})
        assert model.model_validate({"status": "pending", "lat": 90, "lon": -180}).lat == 90
        assert model.model_validate({"status": "pending"}).lat is None


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


@pytest.mark.parametrize("kind", ["social", "sensors"])
def test_capture_status_bridge_requires_session_and_forwards_only_valid_kind(monkeypatch, kind):
    calls = []
    async def forward(request, method, path, payload=None):
        calls.append((method, path, request.headers.get('cookie')))
        return {"server_now": "2026-10-05T12:00:00Z", "publication_revision": "one", "schedule": {"interval_minutes": 5}}
    monkeypatch.setattr(bridge, 'admin_request', forward)
    with TestClient(bridge.app) as client:
        assert client.get('/api/territorial/capture-status').status_code == 401
        assert client.get('/api/territorial/capture-status?kind=arbitrary', headers=HEADERS).status_code == 422
        result = client.get('/api/territorial/capture-status?kind='+kind, headers=HEADERS)
    assert result.status_code == 200 and result.json()['publication_revision'] == 'one'
    assert calls == [('GET', '/api/territorial/capture-status?kind='+kind, HEADERS['cookie'])]
