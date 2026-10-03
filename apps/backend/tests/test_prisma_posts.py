import asyncio
import copy

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import LOCAL_COOKIE_NAME, create_app
from app.prisma.core import build_snapshot, default_source, normalize_event, utc_text
from app.prisma.local import LocalPrismaRuntime
from app.prisma.posts import cursor_values, page_result, post_view
from app.security import issue_session


NOW = 1791209100.0


def event(index, offset=0, **extra):
    return {"platform": "x", "source_id": str(index), "mode": "simulation", "is_simulated": True,
        "text": f"Inundación en Kennedy calle {index}", "created_at": utc_text(NOW + offset),
        "raw_metadata": {"author_id": str(index)}, **extra}


def test_cursor_pages_hold_insertion_ceiling_and_retries_do_not_add_rows(tmp_path):
    runtime = LocalPrismaRuntime(tmp_path, clock=lambda: NOW)
    runtime.store.persist_page("x", [event(i) for i in range(5)], {})
    first = page_result(runtime.store.posts("x", 2), "x", b"key", now=NOW)
    before, maximum = cursor_values(first["next_cursor"], "x", b"key", now=NOW + 1)
    runtime.store.persist_page("x", [event(4), event(6, -3600)], {})
    second = runtime.store.posts("x", 2, before, maximum)
    assert second["total"] == 5
    assert {item["payload"]["id"] for item in second["items"]} == {"x:1", "x:2"}
    assert runtime.store.posts("x", 20)["total"] == 6
    for cursor, platform, key, now in [(first["next_cursor"], "facebook", b"key", NOW),
            (first["next_cursor"] + "0", "x", b"key", NOW), (first["next_cursor"], "x", b"key", NOW + 86401)]:
        with pytest.raises(HTTPException) as error:
            cursor_values(cursor, platform, key, now)
        assert error.value.status_code == 409


def test_pause_resume_keeps_namespace_and_stale_save_cannot_replace_config(tmp_path):
    now = [NOW]
    runtime = LocalPrismaRuntime(tmp_path, clock=lambda: now[0])
    asyncio.run(runtime.update_source("x", {"expected_revision": 1, "interval_minutes": 1}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(runtime.update_source("x", {"expected_revision": 1, "interval_minutes": 9}))
    assert error.value.status_code == 409
    asyncio.run(runtime.test_source("x"))
    assert runtime.store.posts("x", 20)["total"] == 0
    asyncio.run(runtime.run_source("x"))
    with runtime.store.connection() as db:
        control = copy.deepcopy(runtime.store._get(db, "capture_controls", {}))
    count = runtime.store.posts("x", 100)["total"]
    assert asyncio.run(runtime.pause_source("x"))["source"]["capture_state"] == "paused"
    now[0] += 300
    asyncio.run(runtime.tick())
    assert runtime.store.posts("x", 100)["total"] == count
    restarted = LocalPrismaRuntime(tmp_path, clock=lambda: now[0])
    asyncio.run(restarted.run_source("x"))
    with restarted.store.connection() as db:
        assert restarted.store._get(db, "capture_controls", {})["x"] == control["x"]
    assert restarted.store.posts("x", 100)["total"] > count


def test_scheduled_capture_waiting_behind_pause_cannot_restart_it(tmp_path, monkeypatch):
    runtime = LocalPrismaRuntime(tmp_path, clock=lambda: NOW)
    runtime.store.update_source("x", {"mode": "real", "enabled": True, "capture_running": True})
    captured = []

    def poll(source, test):
        captured.append(source["platform"])
        return runtime.store.record_source("x", {"status": "ready", "last_error": None})

    monkeypatch.setattr(runtime, "_poll", poll)

    async def overlap():
        await runtime.lock.acquire()
        pause = asyncio.create_task(runtime.pause_source("x"))
        await asyncio.sleep(0)
        scheduled = asyncio.create_task(runtime.tick())
        await asyncio.sleep(0)
        runtime.lock.release()
        await asyncio.gather(pause, scheduled)
        assert runtime.store.source("x")["capture_running"] is False
        assert captured == []
        await runtime.run_source("x")
        assert runtime.store.source("x")["capture_running"] is True
        assert captured == ["x"]

    asyncio.run(overlap())


def test_event_ids_reviews_and_activity_remain_distinct_across_hour_boundary():
    rules = {"x": default_source("x")}
    records = [normalize_event(event(i, i * 60)) for i in range(20)]
    initial = build_snapshot(records[:5], {}, "v1", "", rules=rules, now=NOW + 300)
    incident = initial["incidents"][0]
    later = build_snapshot(records, {incident["id"]: {"status": "validated", "note": "Human decision"}},
        "v2", "", rules=rules, previous=initial["incidents"], now=NOW + 1200)
    assert len(later["incidents"]) == 1
    assert later["incidents"][0]["id"] == incident["id"]
    assert later["incidents"][0]["report_activity"] == "high"
    assert later["incidents"][0]["severity"] == "medium"
    assert later["incidents"][0]["review_note"] == "Human decision"
    assert {item["relation"] for item in later["event_posts"]} == {"unclassified"}
    changed = build_snapshot(records, {}, "v3", "", rules=rules, previous=later["incidents"], now=NOW + 4000)
    assert changed["incidents"][0]["id"] == incident["id"]
    assert changed["incidents"][0]["report_activity"] == "below_threshold"


def test_admin_posts_and_media_require_authentication_and_thresholds_are_strict(tmp_path):
    settings = Settings(local_development_mode=True, cookie_secure=False,
        aidp_settings_file=str(tmp_path / "settings.json"), session_secret_file=str(tmp_path / "session.key"))
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/admin/prisma/posts?platform=x").status_code == 401
        assert client.get("/api/admin/prisma/media/post-0001/image-01.svg").status_code == 401
        client.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
        for values in ({"low": True, "medium": 10, "high": 20}, {"low": 10, "medium": 5, "high": 20}):
            assert client.put("/api/admin/prisma/sources/x", json={"report_thresholds": values}).status_code == 422
        assert client.post("/api/admin/prisma/sources/x/run").status_code == 200
        result = client.get("/api/admin/prisma/posts?platform=x").json()
        assert result["total"] == 1 and result["items"][0]["url"] == ""
        assert client.post("/api/admin/prisma/sources/x/pause").json()["source"]["capture_state"] == "paused"


def test_preview_never_invents_a_real_link_or_accepts_untrusted_media():
    value = post_view(event(1, source_uri="https://x.com/i/web/status/1", attachments=[{"dataset_path": "../../secret.pem"}]))
    assert value["url"] == "" and value["attachments"] == []
    value = post_view(event(1, mode="real", source_uri="javascript:alert(1)", country=""))
    assert value["url"] == "" and value["country"] == "Unknown"


def test_two_claims_share_one_post_without_copying_its_location_or_losing_stance():
    raw = event(1, text="Inundación en Kennedy. No hay incendio en Bosa.", claims=[
        {"category": "inundacion", "locality": "Kennedy", "severity": "medium", "confidence": .7,
         "relation": "supports", "evidence_text": "Inundación en Kennedy", "summary_en": "Flooding reported in Kennedy."},
        {"category": "incendio", "locality": "Bosa", "severity": "low", "confidence": .6,
         "relation": "contradicts", "evidence_text": "No hay incendio en Bosa", "summary_en": "A report disputes a fire in Bosa."}])
    snapshot = build_snapshot([normalize_event(raw)], {}, "v1", "", rules={"x": default_source("x")}, now=NOW)
    assert len(snapshot["evidence"]) == 1 and len(snapshot["incidents"]) == 2
    assert {item["relation"] for item in snapshot["event_posts"]} == {"supports", "contradicts"}
    disputed = next(item for item in snapshot["incidents"] if item["category"] == "incendio")
    assert disputed["contradicting_report_count"] == 1 and disputed["independent_source_count"] == 0
    assert disputed["corroboration_status"] == "no_supporting_sources"
    assert len({item["lon"] for item in snapshot["incidents"]}) == 2
    assert all(item["evidence_ids"] == ["x:1"] for item in snapshot["incidents"])
    revised = build_snapshot(snapshot["evidence"], {}, "v2", "", rules={"x": default_source("x")}, previous=snapshot["incidents"], now=NOW)
    assert revised["incidents"] == snapshot["incidents"]
