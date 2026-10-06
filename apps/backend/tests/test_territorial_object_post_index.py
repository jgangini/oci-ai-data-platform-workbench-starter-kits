"""Object CAS failures, disposable SQLite recovery and the real paging contract; no network."""
import asyncio
import copy
import sqlite3

import pytest
from fastapi import HTTPException

from app.territorial import post_index as index
from app.territorial.posts import search_page


class Conflict(RuntimeError):
    status = 409


class Objects:
    def __init__(self):
        self.values, self.gets, self.puts = {}, [], []
        self.before_put = self.after_put = self.before_get = None


class Store:
    def __init__(self, path, objects=None):
        self.index_path, self.objects = path, objects or Objects()
        self.active = "reset-1"
        self.guards = 0

    def get_json(self, key):
        self.objects.gets.append(key)
        if self.objects.before_get:
            self.objects.before_get(key)
        return copy.deepcopy(self.objects.values.get(key, (None, None)))

    def put_json(self, key, value, expected_etag=None, create=False):
        self.objects.puts.append(key)
        if self.objects.before_put:
            self.objects.before_put(key, value)
        current, etag = self.objects.values.get(key, (None, None))
        if not (create and current is None or not create and expected_etag and expected_etag == etag):
            raise Conflict()
        following = str(int(etag or 0) + 1)
        self.objects.values[key] = copy.deepcopy(value), following
        if self.objects.after_put:
            self.objects.after_put(key, value)
        return following

    def assert_reset(self, operation_id):
        self.guards += 1
        if operation_id != self.active:
            raise ValueError("Reset is no longer active")


def post(name, *, platform="x", mode="Synthetic", date="2026-10-05T10:00:00Z", **kwargs):
    return {"source_id": name, "platform": platform, "created_at": date, "mode": mode, "text": name, **kwargs}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "posts.sqlite")


def rows(store):
    return index.query_posts(store, None, 100)["items"]


def test_status_monotonic_late_capture_and_incremental_rebuild(store, tmp_path):
    original = post("a")
    index.upsert_posts(store, [original], "ingested", "2026-10-05T10:01:00Z", "batch")
    index.upsert_posts(store, [post("a", text="classified", category="incendio")], "processed")
    before = index.query_ordered_posts(store, None, 10)
    index.upsert_posts(store, [post("a", text="stale capture")], "captured", "2026-10-05T10:00:05Z")
    after = index.query_ordered_posts(store, None, 10)
    assert original == post("a")
    assert after["items"][0]["text"] == "classified"
    assert after["items"][0]["analysis_status"] == "processed"
    assert after["items"][0]["capture_seq"] == 1
    assert after["items"][0]["captured_at"] == "2026-10-05T10:00:05Z"
    assert before["listing_revision"] == after["listing_revision"] == 2
    rebuilt = Store(tmp_path / "second.sqlite", store.objects)
    assert index.query_ordered_posts(rebuilt, None, 10) == after
    store.objects.gets.clear()
    assert index.query_ordered_posts(store, None, 10) == after
    assert store.objects.gets == [index.POST_HEAD]  # No historical scans on a warm scroll.


def test_same_rank_replay_does_not_replace_payload(store):
    index.upsert_posts(store, [post("a", text="first")], "processed")
    index.upsert_posts(store, [post("a", text="different")], "processed")
    assert rows(store)[0]["text"] == "first"
    assert index.query_ordered_posts(store, None, 10)["listing_revision"] == 1


def test_cas_rebases_over_competing_writer_and_disposable_caches_match(store, tmp_path):
    competitor = Store(None, store.objects)

    def race(key, _value):
        if key == index.POST_HEAD:
            store.objects.before_put = None
            index.upsert_posts(competitor, [post("other", platform="facebook")], "processed")

    store.objects.before_put = race
    index.upsert_posts(store, [post("mine")], "captured")
    assert [(row["id"], row["capture_seq"]) for row in rows(store)] == [("x:mine", 2), ("facebook:other", 1)]
    assert rows(Store(tmp_path / "rebuilt.sqlite", store.objects)) == rows(store)


def test_ambiguous_head_commit_can_be_retried_without_duplicate(store):
    def fail(key, _value):
        if key == index.POST_HEAD:
            store.objects.after_put = None
            raise TimeoutError("ambiguous response")

    store.objects.after_put = fail
    with pytest.raises(TimeoutError):
        index.upsert_posts(store, [post("a")], "captured")
    head = copy.deepcopy(store.objects.values[index.POST_HEAD])
    index.upsert_posts(store, [post("a")], "captured")
    assert store.objects.values[index.POST_HEAD] == head
    assert len(rows(store)) == 1


def test_failed_head_write_leaves_only_ignored_orphan_and_retry_succeeds(store):
    def fail(key, _value):
        if key == index.POST_HEAD:
            raise TimeoutError()

    store.objects.before_put = fail
    with pytest.raises(TimeoutError):
        index.upsert_posts(store, [post("a")], "captured")
    store.objects.before_put = None
    assert rows(store) == []
    index.upsert_posts(store, [post("a")], "captured")
    assert len(rows(store)) == 1


def test_cas_contention_is_bounded(store):
    def conflict(key, _value):
        if key == index.POST_HEAD:
            raise Conflict()

    store.objects.before_put = conflict
    with pytest.raises(Conflict):
        index.upsert_posts(store, [post("a")], "captured")
    assert store.objects.puts.count(index.POST_HEAD) == 5


def test_seed_preserves_gaps_status_payload_and_both_sequence_high_waters(store):
    payload = {**post("old"), "id": "x:old", "legacy": "preserved"}
    index.seed_posts(store, [{"payload": payload, "capture_seq": 4, "listing_revision": 17,
                            "analysis_status": "processed", "captured_at": None}], 31, 40)
    index.upsert_posts(store, [post("new")], "captured")
    page = index.query_ordered_posts(store, None, 10, sort="captured_at")
    assert [row["capture_seq"] for row in page["items"]] == [41, 4]
    assert page["listing_revision"] == 58
    assert page["items"][1]["legacy"] == "preserved"
    with pytest.raises(ValueError, match="empty journal"):
        index.seed_posts(store, [], 0, 0)


def test_invalid_seed_has_no_remote_writes(store):
    entry = {"payload": {**post("a"), "id": "x:a"}, "capture_seq": 8, "listing_revision": 14,
             "analysis_status": "processed", "captured_at": None}
    with pytest.raises(ValueError, match="high-water"):
        index.seed_posts(store, [entry], 7, 14)
    assert store.objects.puts == []


def test_purge_is_guarded_preserves_real_and_is_not_reapplied_to_new_posts(store):
    index.upsert_posts(store, [post("old"), post("real", mode="real"), post("unknown", mode="unrecognized")], "processed")
    assert index.purge_synthetic_posts(store, "reset-1") == 1
    index.upsert_posts(store, [post("new")], "captured")
    assert index.purge_synthetic_posts(store, "reset-1") == 1
    assert {row["id"] for row in rows(store)} == {"x:new", "x:real", "x:unknown"}
    store.active = None
    writes = len(store.objects.puts)
    with pytest.raises(ValueError, match="no longer active"):
        index.purge_synthetic_posts(store, "reset-1")
    assert len(store.objects.puts) == writes


def test_purge_guard_changes_before_cas_without_committing(store):
    index.upsert_posts(store, [post("a")], "captured")
    head = copy.deepcopy(store.objects.values[index.POST_HEAD])

    def cancel(key, _value):
        if key.startswith("posts/nodes/"):
            store.active = None

    store.objects.after_put = cancel
    with pytest.raises(ValueError, match="no longer active"):
        index.purge_synthetic_posts(store, "reset-1")
    assert store.objects.values[index.POST_HEAD] == head


def test_spark_append_requires_no_sqlite_cache_or_reads(store, monkeypatch):
    store.index_path = None
    monkeypatch.setattr(index, "_post_sync", lambda *_: pytest.fail("Spark must not build a VM index"))
    index.upsert_posts(store, [post("a")], "captured")
    assert index.purge_synthetic_posts(store, "reset-1") is None


def test_capture_cutoff_and_platform_scope(store):
    index.upsert_posts(store, [post("a"), post("b", platform="facebook"), post("c"), post("internal", platform="linea123")], "captured")
    page = index.query_posts(store, None, 1)
    assert (page["max_seq"], page["total"], page["next_seq"]) == (3, 3, 3)
    index.upsert_posts(store, [post("later")], "captured")
    later = index.query_posts(store, None, 100, page["next_seq"], page["max_seq"])
    assert [row["id"] for row in later["items"]] == ["facebook:b", "x:a"]
    assert later["total"] == 3
    assert index.query_posts(store, "facebook", 100)["max_seq"] == 2


@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("sort", ["published_at", "captured_at"])
def test_ordered_keyset_matches_full_order_with_unicode_ties_and_missing_dates(store, order, sort):
    index.upsert_posts(store, [post("Árbol"), post("quote' OR 1=1 --"), post("late", date="2026-10-06T01:00:00+02:00"),
                             post("missing", date="bad"), post("old", date="2025-01-01T00:00:00Z")], "captured")
    expected = index.query_ordered_posts(store, None, 100, sort=sort, order=order)
    seen, position = [], None
    for _ in range(5):
        page = index.query_ordered_posts(store, None, 1, position, expected["max_seq"], sort, order)
        row = page["items"][0]
        seen.append(row["id"])
        anchor = row["capture_seq"] if sort == "captured_at" else row["listing_published_at"] or ("" if order == "desc" else "\uffff")
        position = {"key": [anchor, row["id"]]}
    assert seen == [row["id"] for row in expected["items"]]
    assert index.query_ordered_posts(store, None, 1, position, expected["max_seq"], sort, order)["items"] == []


def test_actual_search_cursor_detects_enrichment_revision_change(store):
    index.upsert_posts(store, [post("a"), post("b")], "captured")

    async def read(size, before, maximum):
        return index.query_posts(store, None, size, before, maximum)

    async def ordered(size, before, maximum, sort, order):
        return index.query_ordered_posts(store, None, size, before, maximum, sort, order)

    page = asyncio.run(search_page(read, 1, sort="published_at", read_ordered=ordered))
    index.upsert_posts(store, [post("a")], "processed")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(search_page(read, 1, page["next_seq"], page["max_seq"], sort="published_at", read_ordered=ordered))
    assert exc.value.status_code == 409


@pytest.mark.parametrize("bad", [post(""), post("a", platform="x';--"), post("a", date=None), post("a", value=float("nan"))])
def test_invalid_input_is_rejected_before_any_write(store, bad):
    with pytest.raises(ValueError):
        index.upsert_posts(store, [bad], "captured")
    assert store.objects.puts == []


def test_missing_node_and_unreachable_cached_head_fail_closed(store):
    index.upsert_posts(store, [post("a")], "captured")
    assert len(rows(store)) == 1
    prior = copy.deepcopy(store.objects.values[index.POST_HEAD])
    index.upsert_posts(store, [post("b")], "captured")
    head = store.objects.values[index.POST_HEAD][0]
    saved = store.objects.values.pop(f"posts/nodes/{head['node']}.json")
    with pytest.raises(ValueError, match="missing"):
        rows(store)
    store.objects.values[f"posts/nodes/{head['node']}.json"] = saved
    assert len(rows(store)) == 2
    store.objects.values[index.POST_HEAD] = prior
    with pytest.raises(ValueError, match="not reachable"):
        rows(store)


def test_no_sqlite_write_transaction_is_held_while_fetching_objects(store):
    index.upsert_posts(store, [post("a")], "captured")
    rows(store)
    index.upsert_posts(store, [post("b")], "captured")
    checked = []

    def check_lock(_key):
        with sqlite3.connect(store.index_path, timeout=0) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        checked.append(True)

    store.objects.before_get = check_lock
    assert len(rows(store)) == 2
    assert len(checked) == 2


def test_concurrent_reader_advancing_cache_is_rebased_without_double_application(store):
    index.upsert_posts(store, [post("a")], "captured")
    rows(store)
    index.upsert_posts(store, [post("b")], "captured")

    def advance(key):
        if key.startswith("posts/nodes/"):
            store.objects.before_get = None
            assert len(rows(store)) == 2

    store.objects.before_get = advance
    assert [row["capture_seq"] for row in rows(store)] == [2, 1]


def test_projection_failure_rolls_back_entire_suffix(store, monkeypatch):
    index.upsert_posts(store, [post("a")], "captured")
    index.upsert_posts(store, [post("b")], "captured")
    original = index._post_apply

    def fail(connection, node):
        original(connection, node)
        if node["sequence"] == 2:
            raise RuntimeError("disk failure")

    monkeypatch.setattr(index, "_post_apply", fail)
    with pytest.raises(RuntimeError, match="disk failure"):
        rows(store)
    with sqlite3.connect(store.index_path) as db:
        assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
        assert db.execute("SELECT node FROM post_head").fetchone()[0] is None
    monkeypatch.setattr(index, "_post_apply", original)
    assert len(rows(store)) == 2


@pytest.mark.parametrize("position", [[], {}, {"key": []}, {"key": [True, "x:a"]}, {"key": [1, ""]}])
def test_invalid_page_anchors_do_not_read_objects(store, position):
    with pytest.raises(ValueError, match="anchor"):
        index.query_ordered_posts(store, None, 10, position, sort="captured_at")
    assert store.objects.gets == []


def test_journal_payload_hash_corruption_fails_without_mutating_cache(store):
    index.upsert_posts(store, [post("a")], "captured")
    head = store.objects.values[index.POST_HEAD][0]
    node = store.objects.values[f"posts/nodes/{head['node']}.json"][0]
    node["event"]["records"][0]["text"] = "corrupted"
    with pytest.raises(ValueError, match="journal node"):
        rows(store)
    with sqlite3.connect(store.index_path) as db:
        assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0


@pytest.mark.parametrize("prefix", ["territorial", "prisma"])
def test_real_api_boundary_reports_control_cas_conflict_as_409(prefix):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.territorial.api import mount_territorial
    from app.territorial.cloud import CloudRuntime
    from app.territorial.control_store import ControlConflict

    calls = []
    settings = SimpleNamespace(territorial_local_mode=False)
    runtime = CloudRuntime(settings, None)

    def conflict(name, _change):
        calls.append(name)
        raise ControlConflict("private provider details must not be exposed")

    runtime._change = conflict
    app = FastAPI()
    app.state.settings, app.state.territorial_cloud_runtime = settings, runtime
    mount_territorial(app, lambda: "authenticated-admin")
    with TestClient(app) as client:
        response = client.put(f"/api/admin/{prefix}/social-schedule", json={
            "start_at": "2026-10-05T10:00:00Z", "interval_minutes": 5, "expected_revision": 1})
    assert response.status_code == 409
    assert response.json() == {"detail": "Control revision changed; reload before retrying"}
    assert calls == ["configuration"]
