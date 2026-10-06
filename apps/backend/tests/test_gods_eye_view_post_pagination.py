"""Bounded native pages and the same signed-cursor contract as the search fallback."""
import asyncio
from contextlib import nullcontext
import json
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.gods_eye_view import database
from app.gods_eye_view.cloud import CloudRuntime
from app.gods_eye_view.core import utc_text
from app.gods_eye_view.local import LocalGodsEyeViewRuntime
from app.gods_eye_view.posts import cursor_values, page_result, search_page
from test_gods_eye_view_post_search import NOW, post


def oracle_ordered_runtime(store):
    """Execute the production Oracle page SELECT in SQLite, translating only syntax."""
    body = database.PACKAGE_BODY.split("  FUNCTION LIST_ORDERED_POSTS", 1)[1]
    cte = body[body.index("WITH candidates AS"):body.index("    SELECT JSON_OBJECT")]
    select = body[body.index("SELECT candidates.*"):body.index(") page_rows")]
    sql = (cte + select).replace("ADMIN.PRISMA_SOCIAL_POSTS", "social_posts")
    sql = sql.replace("FETCH FIRST v_fetch ROWS ONLY", "LIMIT v_fetch")
    sql = re.sub(r"\b(p_platform|p_sort|p_order|p_before|p_id|v_max|v_fetch)\b", r":\1", sql)
    calls = []

    def callfunc(name, _kind, values):
        assert name == "ADMIN.PRISMA_CONTROL.LIST_ORDERED_POSTS"
        platform, limit, before, identity, maximum, sort, order = values
        with store.connection() as db:
            db.create_function("LPAD", 3, lambda value, size, pad: str(value).rjust(size, pad))
            db.create_function("TO_CHAR", 2, lambda value, _format: str(value))
            db.create_function("NLSSORT", 2, lambda value, _sort: value)
            scope = "platform IN ('x','facebook','instagram','tiktok') AND (? IS NULL OR platform=?)"
            if maximum is None:
                maximum = db.execute(f"SELECT COALESCE(MAX(capture_seq),0) FROM social_posts WHERE {scope}", (platform, platform)).fetchone()[0]
            total, revision, pending = db.execute(f"SELECT COUNT(*),COALESCE(SUM(listing_revision),0),COUNT(CASE WHEN listing_revision=0 OR listing_published_at='?' THEN 1 END) FROM social_posts WHERE {scope} AND capture_seq<=?", (platform, platform, maximum)).fetchone()
            cursor = db.execute(sql, {"p_platform": platform, "p_sort": sort, "p_order": order,
                "p_before": before, "p_id": identity, "v_max": maximum, "v_fetch": limit + 1})
            rows = [dict(zip([item[0] for item in cursor.description], row)) for row in cursor.fetchall()]
        calls.append({"values": values, "rows": len(rows)})
        result = json.dumps({"max_seq": maximum, "total": total, "listing_revision": revision, "projection_pending": pending,
            "items": [{"payload": json.loads(row["payload"]), "capture_seq": row["capture_seq"],
                       "analysis_status": row["analysis_status"], "captured_at": row["ingested_at"]} for row in rows]})
        return SimpleNamespace(read=lambda: result)

    runtime = CloudRuntime.__new__(CloudRuntime)
    runtime._connect = lambda: nullcontext(SimpleNamespace(cursor=lambda: SimpleNamespace(callfunc=callfunc)))
    return runtime, calls


def read(runtime, *, cursor=None, platform=None, limit=20, sort="published_at", order="desc", q=""):
    options = {"sort": sort, "order": order, "q": q}
    before, maximum = cursor_values(cursor, platform, b"key", now=NOW, **options)
    page = asyncio.run(search_page(lambda size, start, cutoff: runtime.posts(platform, size, start, cutoff),
        limit, before, maximum, **options,
        read_ordered=lambda size, start, cutoff, sort, order: runtime.ordered_posts(platform, size, start, cutoff, sort, order)))
    return page_result(page, platform, b"key", now=NOW, **options)


@pytest.mark.parametrize("backend", ["local", "oracle"])
@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("platform", [None, "facebook"])
def test_sql_pages_bound_payloads_preserve_utc_order_and_ignore_new_captures(tmp_path, backend, order, platform):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    records = [post(index, platform="facebook" if index % 2 else "x",
                    created_at=utc_text(NOW + (index * 37 % 97))) for index in range(235)]
    local.store.persist_page("x", records[::2], {})
    local.store.persist_page("facebook", records[1::2], {})
    runtime, calls = oracle_ordered_runtime(local.store) if backend == "oracle" else (local, [])
    options = {"order": order, "platform": platform}
    first = read(runtime, **options)
    expected = sorted([item for item in records if platform is None or item["platform"] == platform],
        key=lambda item: (item["created_at"], f"{item['platform']}:{item['source_id']}"), reverse=order == "desc")
    local.store.persist_page("facebook", [post(999, platform="facebook", created_at=utc_text(NOW - 100))], {})
    page, seen = first, list(first["items"])
    while page["next_cursor"]:
        page = read(runtime, cursor=page["next_cursor"], **options)
        assert page["version"] == first["version"] and page["total"] == len(expected)
        seen.extend(page["items"])
    assert [item["id"] for item in seen] == [f"{item['platform']}:{item['source_id']}" for item in expected]
    assert read(runtime, **options)["total"] == len(expected) + 1
    if backend == "oracle":
        assert all(item["rows"] <= 21 for item in calls)
        assert len(calls) == (len(expected) + 19) // 20 + 1


@pytest.mark.parametrize("backend", ["local", "oracle"])
@pytest.mark.parametrize("order,expected", [("asc", ["x:1", "x:3", "x:2", "x:4", "x:5", "x:6"]),
                                          ("desc", ["x:2", "x:3", "x:1", "x:6", "x:5", "x:4"])])
def test_sql_dates_and_nulls_are_identical_to_the_publication_view(tmp_path, backend, order, expected):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    dates = ["2026-10-03T12:30:00+01:00", "2026-10-03T11:45:00Z", "2026-10-03T06:30:00-05:00", None, "invalid", "2026-10-03T12:00:00"]
    with local.store.connection() as db:
        for index, value in enumerate(dates, 1):
            local.store._post(db, {**post(index, created_at=value), "id": f"x:{index}"})
    runtime, _ = oracle_ordered_runtime(local.store) if backend == "oracle" else (local, [])
    page, seen = read(runtime, limit=1, order=order), []
    while True:
        seen.extend(page["items"])
        if not page["next_cursor"]:
            break
        page = read(runtime, cursor=page["next_cursor"], limit=1, order=order)
    assert [item["id"] for item in seen] == expected
    assert {item["id"]: item["published_at"] for item in seen} == dict(zip([f"x:{i}" for i in range(1, 7)], dates[:3] + [None] * 3))


@pytest.mark.parametrize("backend", ["local", "oracle"])
@pytest.mark.parametrize("change", ["date", "enrichment", "delete"])
def test_changes_under_the_cutoff_invalidate_sql_cursor(tmp_path, backend, change):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    local.store.persist_page("x", [post(i, created_at=utc_text(NOW + i)) for i in range(3)], {})
    runtime, _ = oracle_ordered_runtime(local.store) if backend == "oracle" else (local, [])
    first = read(runtime, limit=1)
    if change == "delete":
        with local.store.connection() as db:
            db.execute("DELETE FROM social_posts WHERE post_key='x:0'")
    else:
        local.store.persist_page("x", [post(0, created_at=utc_text(NOW + (300 if change == "date" else 0)), text="Enriched Bogotá")], {})
    with pytest.raises(HTTPException) as error:
        read(runtime, cursor=first["next_cursor"], limit=1)
    assert error.value.status_code == 409


@pytest.mark.parametrize("backend", ["local", "oracle"])
def test_capture_ascending_sql_pages_and_unicode_search_fallback(tmp_path, backend):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    local.store.persist_page("x", [post(i, text="Straße Bogotá") for i in range(3)], {})
    runtime, _ = oracle_ordered_runtime(local.store) if backend == "oracle" else (local, [])
    first = read(runtime, limit=1, sort="captured_at", order="asc")
    second = read(runtime, cursor=first["next_cursor"], limit=1, sort="captured_at", order="asc")
    assert [first["items"][0]["id"], second["items"][0]["id"]] == ["x:0", "x:1"]
    async def forbidden(*_args):
        raise AssertionError("Unicode search must use hydrated fields")
    local.ordered_posts = forbidden
    assert read(local, q="STRASSE BOGOTÁ")["total"] == 3


def test_missing_package_falls_back_but_database_errors_propagate(tmp_path):
    class Error:
        def __init__(self, code, text):
            self.code, self.text = code, text
        def __str__(self):
            return self.text
    def fail(error):
        raise Exception(error)
    for code, text, missing in [(6550, "PLS-00302: component 'LIST_ORDERED_POSTS' must be declared", True),
                                 (6550, "PLS-00302: component 'OTHER' must be declared", False), (1031, "denied", False)]:
        connection = SimpleNamespace(cursor=lambda: SimpleNamespace(callfunc=lambda *_: fail(Error(code, text))))
        if missing:
            assert database.query_ordered_posts(connection, None, 20) is None
        else:
            with pytest.raises(Exception):
                database.query_ordered_posts(connection, None, 20)
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    runtime.store.persist_page("x", [post(1)], {})
    async def unavailable(*_args):
        return None
    runtime.ordered_posts = unavailable
    assert read(runtime)["items"][0]["id"] == "x:1"


@pytest.mark.parametrize("legacy", ["producer", "interrupted_install"])
def test_legacy_producer_requires_normalization_before_using_the_fast_path(tmp_path, legacy):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    local.store.persist_page("x", [post(1, created_at="2026-10-03T12:30:00+01:00")], {})
    with local.store.connection() as db:
        db.execute("UPDATE social_posts SET listing_published_at='?'" if legacy == "producer" else
                   "UPDATE social_posts SET listing_published_at=NULL,listing_revision=0")
    runtime, calls = oracle_ordered_runtime(local.store)
    assert asyncio.run(runtime.ordered_posts(None, 20)) is None
    assert calls[0]["rows"] == 1
    assert "listing_ready VARCHAR2(5) EXISTS PATH '$.listing_published_at'" in database.PACKAGE_BODY
    assert database.PACKAGE_BODY.count("CASE WHEN source.listing_ready='true' THEN source.listing_published_at ELSE '?' END") == 2
    assert "FROM candidates WHERE listing_revision=0 OR listing_published_at='?'" in database.PACKAGE_BODY


@pytest.mark.parametrize("backend", ["local", "oracle"])
def test_later_commit_with_revision_below_existing_max_still_invalidates_cursor(tmp_path, backend):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    local.store.persist_page("x", [post(i) for i in range(3)], {})
    with local.store.connection() as db:
        db.execute("UPDATE social_posts SET listing_revision=capture_seq*2+4")
    runtime, _ = oracle_ordered_runtime(local.store) if backend == "oracle" else (local, [])
    first = read(runtime, limit=1)
    # Transaction 10 committed before transaction 9; MAX remains 10 when 9 finishes.
    with local.store.connection() as db:
        db.execute("UPDATE social_posts SET listing_revision=9 WHERE post_key='x:0'")
        assert db.execute("SELECT MAX(listing_revision) FROM social_posts").fetchone()[0] == 10
    with pytest.raises(HTTPException) as error:
        read(runtime, cursor=first["next_cursor"], limit=1)
    assert error.value.status_code == 409
    assert "SUM(listing_revision)" in database.PACKAGE_BODY
    assert "CREATE SEQUENCE ADMIN.PRISMA_POST_LIST_REVISION ORDER" in database.TABLES


def test_schema_install_backfills_normalized_dates_without_rewriting_payload():
    statements, updates = [], []
    payload = {"id": "x:old", "created_at": "2026-10-03T12:30:00+01:00", "text": "Bogotá"}
    class Cursor:
        def execute(self, statement, **params):
            statements.append(statement)
            if params:
                updates.append(params)
        def fetchone(self):
            return [0]
        def fetchall(self):
            return [("x:old", SimpleNamespace(read=lambda: json.dumps(payload)))]
    database.install_schema(SimpleNamespace(cursor=Cursor, commit=lambda: None))
    assert updates == [{"published": "2026-10-03T11:30:00.000000+00:00", "key": "x:old"}]
    update = next(sql for sql in statements if sql.startswith("UPDATE ADMIN.PRISMA_SOCIAL_POSTS"))
    assert "payload=" not in update
    assert "listing_revision=0 OR listing_published_at='?'" in update
