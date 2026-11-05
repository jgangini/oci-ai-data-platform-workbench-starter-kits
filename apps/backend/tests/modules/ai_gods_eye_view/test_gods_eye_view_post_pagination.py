"""Bounded native pages and the same signed-cursor contract as the search fallback."""
import asyncio
from contextlib import nullcontext
import json
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.ai_gods_eye_view.runtime import control_store as database
from app.modules.ai_gods_eye_view.runtime.cloud import CloudRuntime
from app.modules.ai_gods_eye_view.runtime.core import utc_text
from app.modules.ai_gods_eye_view.runtime.local import LocalGodsEyeViewRuntime
from app.modules.ai_gods_eye_view.runtime.posts import cursor_values, page_result, search_page
from tests.modules.ai_gods_eye_view.test_gods_eye_view_post_search import NOW, post




def read(runtime, *, cursor=None, platform=None, limit=20, sort="published_at", order="desc", q=""):
    options = {"sort": sort, "order": order, "q": q}
    before, maximum = cursor_values(cursor, platform, b"key", now=NOW, **options)
    page = asyncio.run(search_page(lambda size, start, cutoff: runtime.posts(platform, size, start, cutoff),
        limit, before, maximum, **options,
        read_ordered=lambda size, start, cutoff, sort, order: runtime.ordered_posts(platform, size, start, cutoff, sort, order)))
    return page_result(page, platform, b"key", now=NOW, **options)


@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("platform", [None, "facebook"])
def test_sql_pages_bound_payloads_preserve_utc_order_and_ignore_new_captures(tmp_path, order, platform):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    records = [post(index, platform="facebook" if index % 2 else "x",
                    created_at=utc_text(NOW + (index * 37 % 97))) for index in range(235)]
    local.store.persist_page("x", records[::2], {})
    local.store.persist_page("facebook", records[1::2], {})
    runtime = local
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


@pytest.mark.parametrize("order,expected", [("asc", ["x:1", "x:3", "x:2", "x:4", "x:5", "x:6"]),
                                          ("desc", ["x:2", "x:3", "x:1", "x:6", "x:5", "x:4"])])
def test_sql_dates_and_nulls_are_identical_to_the_publication_view(tmp_path, order, expected):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    dates = ["2026-10-03T12:30:00+01:00", "2026-10-03T11:45:00Z", "2026-10-03T06:30:00-05:00", None, "invalid", "2026-10-03T12:00:00"]
    with local.store.connection() as db:
        for index, value in enumerate(dates, 1):
            local.store._post(db, {**post(index, created_at=value), "id": f"x:{index}"})
    runtime = local
    page, seen = read(runtime, limit=1, order=order), []
    while True:
        seen.extend(page["items"])
        if not page["next_cursor"]:
            break
        page = read(runtime, cursor=page["next_cursor"], limit=1, order=order)
    assert [item["id"] for item in seen] == expected
    assert {item["id"]: item["published_at"] for item in seen} == dict(zip([f"x:{i}" for i in range(1, 7)], dates[:3] + [None] * 3))


@pytest.mark.parametrize("change", ["date", "enrichment", "delete"])
def test_changes_under_the_cutoff_invalidate_sql_cursor(tmp_path, change):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    local.store.persist_page("x", [post(i, created_at=utc_text(NOW + i)) for i in range(3)], {})
    runtime = local
    first = read(runtime, limit=1)
    if change == "delete":
        with local.store.connection() as db:
            db.execute("DELETE FROM social_posts WHERE post_key='x:0'")
    else:
        local.store.persist_page("x", [post(0, created_at=utc_text(NOW + (300 if change == "date" else 0)), text="Enriched Bogotá")], {})
    with pytest.raises(HTTPException) as error:
        read(runtime, cursor=first["next_cursor"], limit=1)
    assert error.value.status_code == 409


def test_capture_ascending_sql_pages_and_unicode_search_fallback(tmp_path):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    local.store.persist_page("x", [post(i, text="Straße Bogotá") for i in range(3)], {})
    runtime = local
    first = read(runtime, limit=1, sort="captured_at", order="asc")
    second = read(runtime, cursor=first["next_cursor"], limit=1, sort="captured_at", order="asc")
    assert [first["items"][0]["id"], second["items"][0]["id"]] == ["x:0", "x:1"]
    async def forbidden(*_args):
        raise AssertionError("Unicode search must use hydrated fields")
    local.ordered_posts = forbidden
    assert read(local, q="STRASSE BOGOTÁ")["total"] == 3






def test_later_commit_with_revision_below_existing_max_still_invalidates_cursor(tmp_path):
    local = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    local.store.persist_page("x", [post(i) for i in range(3)], {})
    with local.store.connection() as db:
        db.execute("UPDATE social_posts SET listing_revision=capture_seq*2+4")
    runtime = local
    first = read(runtime, limit=1)
    # Transaction 10 committed before transaction 9; MAX remains 10 when 9 finishes.
    with local.store.connection() as db:
        db.execute("UPDATE social_posts SET listing_revision=9 WHERE post_key='x:0'")
        assert db.execute("SELECT MAX(listing_revision) FROM social_posts").fetchone()[0] == 10
    with pytest.raises(HTTPException) as error:
        read(runtime, cursor=first["next_cursor"], limit=1)
    assert error.value.status_code == 409
