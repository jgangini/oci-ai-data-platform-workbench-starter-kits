import asyncio
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import LOCAL_COOKIE_NAME, create_app
from app.gods_eye_view import corpus
from app.gods_eye_view.cloud import CloudRuntime
from app.gods_eye_view.core import utc_text
from app.gods_eye_view.local import LocalGodsEyeViewRuntime
from app.gods_eye_view.posts import cursor_values, page_result, search_page
from app.security import issue_session


NOW = 1791209100.0


def post(index, **values):
    return {"platform": "x", "source_id": str(index), "mode": "real", "text": "ordinary report",
            "created_at": "2026-10-03T12:00:00Z", **values}


def oracle_runtime(store):
    """Exercise the actual Oracle adapter with its package's CLOB page contract, offline."""
    calls = []

    def callfunc(name, _kind, values):
        assert name == "ADMIN.PRISMA_CONTROL.LIST_POSTS"
        calls.append(values)
        platform, limit, before, maximum = values
        with store.connection() as db:
            rows = [{"capture_seq": row[0], "payload": json.loads(row[1]), "analysis_status": row[2], "captured_at": row[3]}
                    for row in db.execute("SELECT capture_seq,payload,analysis_status,ingested_at FROM social_posts ORDER BY capture_seq DESC")]
        rows = [item for item in rows if platform is None or item["payload"]["platform"] == platform]
        maximum = max((item["capture_seq"] for item in rows), default=0) if maximum is None else maximum
        snapshot = [item for item in rows if item["capture_seq"] <= maximum]
        selected = [item for item in snapshot if before is None or item["capture_seq"] < before]
        return json.dumps({"items": selected[:limit + 1], "total": len(snapshot), "max_seq": maximum})

    connection = SimpleNamespace(cursor=lambda: SimpleNamespace(callfunc=callfunc))
    runtime = CloudRuntime.__new__(CloudRuntime)
    runtime._connect = lambda: nullcontext(connection)
    return runtime, calls


@pytest.mark.parametrize("backend", ["local", "oracle"])
def test_search_spans_native_pages_and_freezes_filtered_total_and_capture_cutoff(tmp_path, backend):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    local.store.persist_page("x", [post(index, text="BOGOTÁ 50%_ O'Brien" if index in (5, 105, 205) else "ordinary report")
                                    for index in range(230)], {})
    runtime, calls = oracle_runtime(local.store) if backend == "oracle" else (local, [])

    def search(cursor=None, q="  bogotá 50%_ o'brien  "):
        before, maximum = cursor_values(cursor, None, b"key", now=NOW, q=q)
        page = asyncio.run(search_page(lambda limit, start, cutoff: runtime.posts(None, limit, start, cutoff),
                                       1, before, maximum, q=q))
        return page_result(page, None, b"key", now=NOW, q=q)

    first = search()
    assert [item["id"] for item in first["items"]] == ["x:205"] and first["total"] == 3
    local.store.persist_page("x", [post(999, text="Bogotá 50%_ O'Brien")], {})
    second = search(first["next_cursor"], q="BOGOTÁ 50%_ O'BRIEN")
    third = search(second["next_cursor"])
    assert [item["id"] for item in second["items"] + third["items"]] == ["x:105", "x:5"]
    assert second["total"] == third["total"] == 3 and third["next_cursor"] is None
    assert first["version"] == second["version"] == third["version"]
    assert search()["total"] == 4
    assert search(q="missing")["total"] == 0
    if backend == "oracle":
        assert calls[:3] == [[None, 100, None, None], [None, 100, 131, 230], [None, 100, 31, 230]]
        assert calls[3][3] == 230  # The next filtered page keeps the original native ceiling.


def test_search_uses_hydrated_author_and_visible_fields_without_changing_capture(tmp_path):
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    captured = corpus.events("x", "run", 0, NOW, -1, 0)[0]
    captured.update(username="sim_bogota_0001", display_name="Fictional Bogotá Reporter 001")
    runtime.store.persist_page("x", [captured], {})
    runtime.store.persist_page("facebook", [post(2, platform="facebook", country="Deutschland", display_name="Straße Reporter")], {})

    def search(q, platform=None):
        return asyncio.run(search_page(lambda limit, before, maximum: runtime.posts(platform, limit, before, maximum), 20, q=q))

    for query in ("MARIANA ROJAS", "mar_rojas", "Colombia", "Kennedy"):
        assert search(query)["total"] == 1
    for query in ("STRASSE REPORTER", "facebook", "deutschland"):
        assert search(query)["total"] == 1
    assert search("facebook", platform="x")["total"] == 0
    assert search("sim_bogota_0001")["total"] == 0
    assert runtime.store.posts("x", 1)["items"][0]["payload"]["username"] == "sim_bogota_0001"


def test_cursor_binds_normalized_query_without_growing_with_unicode_input():
    raw = {"items": [], "next_seq": 1, "max_seq": 2, "total": 2}
    q = "災" * 200
    cursor = page_result(raw, None, b"key", now=NOW, q=q)["next_cursor"]
    assert len(cursor) < 1024
    assert cursor_values(cursor, None, b"key", now=NOW, q=q) == (1, 2)
    for query in ("different", ""):
        with pytest.raises(HTTPException) as error:
            cursor_values(cursor, None, b"key", now=NOW, q=query)
        assert error.value.status_code == 409
    unfiltered = page_result(raw, None, b"key", now=NOW)["next_cursor"]
    with pytest.raises(HTTPException):
        cursor_values(unfiltered, None, b"key", now=NOW, q="new search")


def test_admin_search_enforces_query_bound_and_rejects_cursor_from_another_search(tmp_path):
    settings = Settings(local_development_mode=True, cookie_secure=False,
        aidp_settings_file=str(tmp_path / "settings.json"), session_secret_file=str(tmp_path / "session.key"))
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/admin/gods-eye-view/posts", params={"q": "private"}).status_code == 401
        client.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
        assert client.get("/api/admin/gods-eye-view/posts").status_code == 200
        runtime = app.state.gods_eye_view_runtime
        runtime.store.persist_page("x", [post(index, text="Match_%") for index in range(3)], {})
        runtime.store.persist_page("x", [post(9, text="MatchOTHER")], {})
        first = client.get("/api/admin/gods-eye-view/posts", params={"q": "  MATCH_%  ", "limit": 2}).json()
        assert first["total"] == 3 and len(first["items"]) == 2
        cursor = first["next_cursor"]
        final = client.get("/api/admin/gods-eye-view/posts", params={"q": "match_%", "cursor": cursor}).json()
        assert final["total"] == 3 and len(final["items"]) == 1 and final["next_cursor"] is None
        assert client.get("/api/admin/gods-eye-view/posts", params={"q": "other", "cursor": cursor}).status_code == 409
        assert client.get("/api/admin/gods-eye-view/posts", params={"cursor": cursor}).status_code == 409
        assert client.get("/api/admin/gods-eye-view/posts", params={"q": " "}).json()["total"] == 4
        assert client.get("/api/admin/gods-eye-view/posts", params={"q": "災" * 201}).status_code == 422


@pytest.mark.parametrize("backend", ["local", "oracle"])
@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("platform", [None, "facebook"])
def test_publication_sort_spans_all_pages_with_stable_id_ties_and_capture_cutoff(tmp_path, backend, order, platform):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    records = [post(index, platform="facebook" if index % 2 else "x", text="match" if index % 3 else "different",
                    created_at=utc_text(NOW + (index * 37 % 97))) for index in range(235)]
    local.store.persist_page("x", records[::2], {})
    local.store.persist_page("facebook", records[1::2], {})
    runtime, calls = oracle_runtime(local.store) if backend == "oracle" else (local, [])
    options = {"q": "MATCH", "sort": "published_at", "order": order}
    def read(cursor=None):
        before, maximum = cursor_values(cursor, platform, b"key", now=NOW, **options)
        raw = asyncio.run(search_page(lambda limit, start, cutoff: runtime.posts(platform, limit, start, cutoff),
                                     17, before, maximum, **options))
        return page_result(raw, platform, b"key", now=NOW, **options)
    first = read()
    candidates = records if platform is None else records[1::2]
    expected = sorted([item for item in candidates if item["text"] == "match"],
                      key=lambda item: (item["created_at"], f"{item['platform']}:{item['source_id']}"), reverse=order == "desc")
    local.store.persist_page("facebook", [post(999, platform="facebook", text="match", created_at=utc_text(NOW - 100)),
                                          post(1000, platform="facebook", text="match", created_at=utc_text(NOW + 1000))], {})
    page, seen = first, list(first["items"])
    while page["next_cursor"]:
        page = read(page["next_cursor"])
        assert page["version"] == first["version"] and page["total"] == len(expected)
        seen.extend(page["items"])
    assert [item["id"] for item in seen] == [f"{item['platform']}:{item['source_id']}" for item in expected]
    assert read()["total"] == len(expected) + 2
    if backend == "oracle":
        assert any(call[2] is not None for call in calls)  # Sort scans beyond the Oracle package's first native page.


@pytest.mark.parametrize("backend", ["local", "oracle"])
def test_changed_publication_sort_key_invalidates_cursor_instead_of_skipping_records(tmp_path, backend):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    local.store.persist_page("x", [post(index, created_at=utc_text(NOW + index)) for index in range(3)], {})
    runtime, _ = oracle_runtime(local.store) if backend == "oracle" else (local, [])
    read = lambda limit, start, cutoff: runtime.posts(None, limit, start, cutoff)
    raw = asyncio.run(search_page(read, 1, sort="published_at"))
    first = page_result(raw, None, b"key", now=NOW, sort="published_at")
    before, maximum = cursor_values(first["next_cursor"], None, b"key", now=NOW, sort="published_at")
    local.store.persist_page("x", [post(0, created_at=utc_text(NOW + 300))], {})
    with pytest.raises(HTTPException) as error:
        asyncio.run(search_page(read, 1, before, maximum, sort="published_at"))
    assert error.value.status_code == 409


def test_sorted_cursor_binds_order_filter_query_and_bounded_unicode_tie_key():
    position = {"key": ["2026-10-03T12:00:00.000000+00:00", "災" * 200], "digest": "a" * 64}
    raw = {"items": [], "next_seq": position, "max_seq": 2, "total": 2, "sort_digest": position["digest"]}
    cursor = page_result(raw, "facebook", b"key", now=NOW, sort="published_at", order="asc", q="Match")["next_cursor"]
    assert len(cursor) < 2048
    assert cursor_values(cursor, "facebook", b"key", now=NOW, sort="published_at", order="asc", q="match") == (position, 2)
    for values in ({"platform": None}, {"sort": "captured_at"}, {"order": "desc"}, {"q": "other"}):
        options = {"platform": "facebook", "sort": "published_at", "order": "asc", "q": "match", **values}
        with pytest.raises(HTTPException) as error:
            cursor_values(cursor, key=b"key", now=NOW, **options)
        assert error.value.status_code == 409


@pytest.mark.parametrize("backend", ["local", "oracle"])
@pytest.mark.parametrize("order,expected", [
    ("asc", ["x:1", "x:3", "x:2", "x:4", "x:5", "x:6"]),
    ("desc", ["x:2", "x:3", "x:1", "x:6", "x:5", "x:4"]),
])
def test_publication_sort_compares_instants_and_keeps_unknown_dates_last_across_pages(tmp_path, backend, order, expected):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    dates = ["2026-10-03T12:30:00+01:00", "2026-10-03T11:45:00Z", "2026-10-03T06:30:00-05:00", None, "invalid", "2026-10-03T12:00:00"]
    # Seed historical projections directly: normal ingestion already validates and normalizes dates.
    with local.store.connection() as db:
        for index, value in enumerate(dates, 1):
            record = {**post(index, created_at=value), "id": f"x:{index}"}
            local.store._post(db, record)
    runtime, _ = oracle_runtime(local.store) if backend == "oracle" else (local, [])
    cursor, seen = None, []
    while True:
        position, maximum = cursor_values(cursor, None, b"key", now=NOW, sort="published_at", order=order)
        raw = asyncio.run(search_page(lambda size, before, cutoff: runtime.posts(None, size, before, cutoff),
                                     1, position, maximum, sort="published_at", order=order))
        page = page_result(raw, None, b"key", now=NOW, sort="published_at", order=order)
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert [item["id"] for item in seen] == expected
    expected_dates = dict(zip([f"x:{index}" for index in range(1, 7)], dates[:3] + [None] * 3))
    assert {item["id"]: item["published_at"] for item in seen} == expected_dates
    captured = local.store.posts(None, 100)["items"]
    assert [item["payload"]["created_at"] for item in reversed(captured)] == dates


def test_admin_publication_date_sort_contract_and_invalid_options(tmp_path):
    settings = Settings(local_development_mode=True, cookie_secure=False,
        aidp_settings_file=str(tmp_path / "settings.json"), session_secret_file=str(tmp_path / "session.key"))
    app = create_app(settings)
    with TestClient(app) as client:
        client.cookies.set(LOCAL_COOKIE_NAME, issue_session(app.state.session_key, "admin"))
        client.get("/api/admin/gods-eye-view/posts").raise_for_status()
        runtime = app.state.gods_eye_view_runtime
        runtime.store.persist_page("facebook", [post(1, platform="facebook", created_at=utc_text(NOW + 5)),
                                                post(2, platform="facebook", created_at=utc_text(NOW - 5))], {})
        runtime.store.persist_page("x", [post(3)], {})
        options = {"platform": "facebook", "sort": "published_at", "order": "asc", "limit": 1}
        first = client.get("/api/admin/gods-eye-view/posts", params=options).json()
        assert first["items"][0]["id"] == "facebook:2" and first["total"] == 2
        second = client.get("/api/admin/gods-eye-view/posts", params={**options, "cursor": first["next_cursor"]}).json()
        assert second["items"][0]["id"] == "facebook:1" and second["next_cursor"] is None
        assert client.get("/api/admin/gods-eye-view/posts", params={**options, "order": "desc", "cursor": first["next_cursor"]}).status_code == 409
        for invalid in ({"sort": "text"}, {"order": "DROP TABLE"}, {"platform": "unknown"}):
            assert client.get("/api/admin/gods-eye-view/posts", params={**options, **invalid}).status_code == 422
