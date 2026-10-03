import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import LOCAL_COOKIE_NAME, create_app
from app.prisma.core import build_snapshot, normalize_event, simulation_events
from app.prisma.local import LocalPrismaRuntime
from app.prisma.store import PrismaStore
from app.prisma.x import XFailure, fetch_page
from app.security import issue_session


NOW = 1791209100.0


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.status_code, self.headers, self.payload = status, headers or {}, payload or {}

    def json(self):
        return self.payload


class Client:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def post(identifier):
    return {"id": identifier, "text": "Inundación en Kennedy", "created_at": "2026-10-05T14:00:00Z"}


def test_x_failed_second_page_resumes_after_restart_without_losing_watermark(tmp_path):
    clock = Clock()
    first = Client([
        Response({"data": [post("30"), post("25")], "meta": {"newest_id": "30", "next_token": "page2"}}),
        Response(status=429, headers={"x-rate-limit-reset": str(NOW + 90)}),
    ])
    runtime = LocalPrismaRuntime(tmp_path, clock=clock, client_factory=lambda: first)
    asyncio.run(runtime.update_source("x", {"mode": "real", "query": "#Bogota", "bearer_token": "private-value"}))
    result = asyncio.run(runtime.run_source("x"))
    assert result["status"] == "rate_limited"
    assert result["source"]["last_received_count"] is None
    assert next(iter(runtime.store.checkpoint("x")["queries"].values()))["cursor"]["pending_newest_id"] == "30"
    assert len(runtime.store.snapshot()["evidence"]) == 2
    asyncio.run(runtime.run_source("x"))
    assert len(first.calls) == 2  # Even manual Run respects the rate-limit window.
    clock.now += 91
    second = Client([Response({"data": [post("25"), post("20")], "meta": {"newest_id": "25"}})])
    restarted = LocalPrismaRuntime(tmp_path, clock=clock, client_factory=lambda: second)
    captured = asyncio.run(restarted.run_source("x"))
    assert captured["status"] == "ready" and captured["source"]["last_received_count"] == 2
    assert second.calls[0][1]["params"]["next_token"] == "page2"
    assert next(iter(restarted.store.checkpoint("x")["queries"].values()))["cursor"]["since_id"] == "30"
    assert len(restarted.store.snapshot()["evidence"]) == 3
    assert b"private-value" not in (tmp_path / "prisma.sqlite3").read_bytes()
    assert "private-value" not in json.dumps(asyncio.run(restarted.sources()))
    second.responses = [Response(status=500)]
    assert asyncio.run(restarted.run_source("x"))["source"]["last_received_count"] == 2


def test_page_cursor_uses_first_page_newest_id_and_no_start_time_with_since_id():
    client = Client([Response({"data": [post("30")], "meta": {"newest_id": "30"}})])
    _, cursor = fetch_page(client, "token", "#Bogota", {"since_id": "10"}, NOW)
    assert cursor["since_id"] == "30"
    params = client.calls[0][1]["params"]
    assert params["since_id"] == "10" and "start_time" not in params
    assert client.calls[0][1]["timeout"] == 20


@pytest.mark.parametrize("status,code", [(401, "invalid_credential"), (403, "access_denied"), (500, "upstream_error")])
def test_failed_x_reads_never_look_like_empty_success(status, code):
    with pytest.raises(XFailure) as error:
        fetch_page(Client([Response(status=status)]), "token", "#Bogota", {}, NOW)
    assert error.value.code == code


def test_history_gap_is_explicit_and_does_not_query():
    client = Client([])
    with pytest.raises(XFailure, match="history_gap"):
        fetch_page(client, "token", "#Bogota", {"since_id": "10", "committed_at": NOW - 8 * 86400}, NOW)
    assert client.calls == []


def test_simulation_restart_pause_resume_replay_and_real_evidence_survives(tmp_path):
    clock = Clock()
    store = PrismaStore(tmp_path / "state.db", clock)
    store.control_simulation("start")
    clock.now += 200
    store.control_simulation("pause")
    paused = store.snapshot()
    clock.now += 120
    restarted = PrismaStore(tmp_path / "state.db", clock)
    assert restarted.snapshot()["evidence"] == paused["evidence"]
    restarted.control_simulation("resume")
    clock.now += 400
    finished = restarted.snapshot()
    assert finished["simulation"]["status"] == "completed"
    expected_ids = {event["id"] for event in finished["evidence"]}
    assert len(expected_ids) == 12  # The configured X risk query excludes the cultural post.
    assert "Kennedy" in {item["locality"] for item in finished["incidents"]}
    assert len(finished["incidents"]) == 5  # Four located risks plus one risk pending location.
    assert finished["simulation"]["anchor_at"] == NOW
    assert finished["simulation"]["run_id"] == paused["simulation"]["run_id"]
    assert all(item["raw_metadata"]["scenario_run_id"] == finished["simulation"]["run_id"] for item in finished["evidence"])
    assert min(item["created_at"] for item in finished["evidence"]) == "2026-10-05T14:05:00Z"
    real = {**simulation_events(0)[0], "source_id": "99", "mode": "real", "is_simulated": False}
    restarted.persist_page("x", [real], {"since_id": "99"})
    restarted.control_simulation("replay")
    assert len(restarted.snapshot()["evidence"]) == 2
    clock.now += 600
    replayed = restarted.snapshot()
    replay_ids = {item["id"] for item in replayed["evidence"] if item["mode"] == "simulation"}
    assert {item.rsplit(":", 1)[-1] for item in replay_ids} == {item.rsplit(":", 1)[-1] for item in expected_ids}
    assert not replay_ids.intersection(expected_ids)
    assert len(replayed["evidence"]) == 13
    assert replayed["simulation"]["anchor_at"] == NOW + 720
    assert replayed["simulation"]["run_id"] != finished["simulation"]["run_id"]
    restarted.control_simulation("reset")
    assert [item["mode"] for item in restarted.snapshot()["evidence"]] == ["real"]


def test_forwarded_content_does_not_increase_independent_corroboration():
    event = simulation_events(0)[0]
    events = [normalize_event(event), normalize_event({**event, "platform": "facebook"})]
    snapshot = build_snapshot(events, {}, "v1", "2026-10-05T14:00:00Z")
    assert snapshot["incidents"][0]["independent_source_count"] == 1
    assert len(snapshot["evidence"]) == 2


def test_admin_contract_and_review_persistence(tmp_path):
    settings = Settings(local_development_mode=True, cookie_secure=False,
                        aidp_settings_file=str(tmp_path / "settings.json"), session_secret_file=str(tmp_path / "session.key"))
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/admin/prisma/sources").status_code == 401
        client.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
        assert client.get("/api/admin/session").headers["x-prisma-user"] == "admin"
        response = client.put("/api/admin/prisma/sources/x", json={"interval_minutes": 0})
        assert response.status_code == 422
        assert client.put("/api/admin/prisma/sources/tiktok", json={"mode": "real"}).status_code == 422
        client.post("/api/admin/prisma/simulation", json={"action": "start"}).raise_for_status()
        snapshot = client.get("/api/prisma/snapshot").json()
        incident = snapshot["incidents"][0]
        review = client.post(f"/api/prisma/incidents/{incident['id']}/review", json={"status": "validated", "note": "Revisado"})
        assert review.status_code == 200
        assert review.json()["review_status"] == "validated"
    new_store = PrismaStore(tmp_path / "prisma.sqlite3")
    assert new_store.snapshot()["incidents"][0]["review_status"] == "validated"


def test_local_correlation_upgrade_invalidates_old_version_and_retains_old_review(tmp_path):
    clock = Clock()
    raw = simulation_events(0, NOW)[0]
    raw["raw_metadata"].update(capture_run_id="capture-x", scenario_run_id="capture-x:0")
    event = normalize_event(raw)
    previous = {**event, "raw_metadata": {key: value for key, value in event["raw_metadata"].items() if key != "capture_run_id"}}
    old_id = build_snapshot([previous], {}, "", "")["incidents"][0]["id"]
    review = {"status": "validated", "note": "Existing decision"}
    path = tmp_path / "state.db"
    store = PrismaStore(path, clock)
    store.persist_page("x", [raw], {})
    with store.connection() as db:
        publication = store._get(db, "publication", {})
        db.execute("INSERT INTO reviews VALUES (?,?)", (old_id, json.dumps(review)))
    restarted = PrismaStore(path, clock)
    snapshot = restarted.snapshot()
    assert snapshot["version"] == f"local-v2-{publication['revision']}"
    assert snapshot["version"] != f"local-{publication['revision']}"
    assert snapshot["incidents"][0]["id"] != old_id
    assert snapshot["incidents"][0]["review_status"] == "pending"
    assert restarted.snapshot() == snapshot
    with restarted.connection() as db:
        assert restarted._get(db, "publication", {}) == publication
        assert json.loads(db.execute("SELECT payload FROM reviews WHERE id=?", (old_id,)).fetchone()[0]) == review


def test_cloud_mode_delegates_without_local_processing(tmp_path):
    class Cloud:
        async def snapshot(self):
            return {"runtime": "aidp", "version": "gold-123"}
    settings = Settings(cookie_secure=False, aidp_settings_file=str(tmp_path / "settings.json"),
                        session_secret_file=str(tmp_path / "session.key"))
    app = create_app(settings)
    app.state.prisma_cloud_runtime = Cloud()
    with TestClient(app) as client:
        client.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
        assert client.get("/api/prisma/snapshot").json()["runtime"] == "aidp"
    assert not (tmp_path / "prisma.sqlite3").exists()
