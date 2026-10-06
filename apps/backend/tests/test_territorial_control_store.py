import copy
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.territorial import database
from app.territorial.control_store import ControlConflict, ObjectControlStore, validate_replacement
from app.territorial.synthetic_reset import HISTORY_PREFIX, prune_publication


class StorageError(RuntimeError):
    def __init__(self, status):
        self.status = status


class Objects:
    def __init__(self):
        self.values, self.serial, self.lock = {}, 0, Lock()
        self.barrier = None
        self.calls = []

    def get_object(self, namespace, bucket, key):
        with self.lock:
            value = self.values.get(key)
        if self.barrier and key.endswith("configuration.json"):
            self.barrier.wait(timeout=5)
        if value is None:
            raise StorageError(404)
        body, etag = value
        return SimpleNamespace(data=SimpleNamespace(content=body), headers={"etag": etag})

    def put_object(self, namespace, bucket, key, body, **options):
        with self.lock:
            self.calls.append((key, options))
            current = self.values.get(key)
            if options.get("if_none_match") == "*" and current is not None:
                raise StorageError(412)
            if "if_match" in options and (current is None or current[1] != options["if_match"]):
                raise StorageError(412)
            self.serial += 1
            etag = str(self.serial)
            self.values[key] = (body, etag)
            return SimpleNamespace(headers={"etag": etag})


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setitem(sys.modules, "oracledb", None)
    return ObjectControlStore(Objects(), "namespace", "bucket")


def ready(store, **extra):
    return database.write_document(store, "runtime", {
        "analytics_store": "gold", "control_migration_complete": True, **extra}, 0)


def test_documents_keep_revision_contract_and_have_no_sql_transaction(store):
    with store as connection:
        assert database.read_document(connection, "configuration") == {"revision": 0}
        first = database.write_document(connection, "configuration", {"sources": {"x": {"enabled": True}}}, 0)
        second = database.mutate_document(connection, "configuration", lambda value: {**value, "schedule": "kept"})
        connection.rollback()
        connection.commit()
    assert first["revision"] == 1 and second["revision"] == 2
    assert database.read_documents(store, ("configuration", "status_pipeline")) == {
        "configuration": second, "status_pipeline": {"revision": 0}}
    assert store.objects.calls[0][1]["if_none_match"] == "*"
    assert store.objects.calls[1][1]["if_match"] == "1"


def test_document_batch_reads_overlap_with_at_most_eight_workers(store):
    names = ("configuration", "simulation", "reviews", "runtime", "event_registry", "status_x",
             "status_facebook", "checkpoint_reset", "checkpoint_sensors")
    expected = {name: {"revision": i + 1, "value": name} for i, name in enumerate(names[:-1])}
    for name, value in expected.items():
        store.put_json("docs/" + name + ".json", value, create=True)
    expected[names[-1]] = {"revision": 0}
    get_object = store.objects.get_object
    barrier, lock = Barrier(8), Lock()
    active = peak = calls = 0
    def concurrent_get(namespace, bucket, key):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            call = calls
            peak = max(peak, active)
        try:
            if call <= 8:
                barrier.wait(timeout=5)
            return get_object(namespace, bucket, key)
        finally:
            with lock:
                active -= 1
    store.objects.get_object = concurrent_get
    assert database.read_documents(store, names) == expected
    assert calls == 9 and peak == 8 and active == 0


def test_document_batch_preserves_missing_documents_and_propagates_other_errors(store):
    get_object = store.objects.get_object
    def failing_get(namespace, bucket, key):
        if key.endswith("runtime.json"):
            raise StorageError(403)
        return get_object(namespace, bucket, key)
    store.objects.get_object = failing_get
    assert database.read_documents(store, ("configuration", "status_x")) == {
        "configuration": {"revision": 0}, "status_x": {"revision": 0}}
    with pytest.raises(StorageError) as error:
        database.read_documents(store, ("configuration", "runtime"))
    assert error.value.status == 403


@pytest.mark.parametrize("initial", [None, {"revision": 7, "value": "preserved"}])
def test_concurrent_document_creates_and_updates_have_one_winner(store, initial):
    if initial:
        store.put_json("docs/configuration.json", initial, create=True)
    store.objects.barrier = Barrier(2)
    def write(value):
        try:
            return database.write_document(store, "configuration", {"value": value}, 7 if initial else 0)
        except ControlConflict as exc:
            assert exc.status == 409
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ("first", "second")))
    store.objects.barrier = None
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert database.read_document(store, "configuration") == winners[0]


def test_stale_admin_change_is_not_retried_or_overwritten(store):
    database.write_document(store, "configuration", {"saved": True}, 0)
    change = Mock(side_effect=lambda doc: (
        database.write_document(store, "configuration", {"concurrent": True}, doc["revision"]),
        {**doc, "stale": True})[1])
    with pytest.raises(ControlConflict):
        database.mutate_document(store, "configuration", change)
    change.assert_called_once()
    assert database.read_document(store, "configuration") == {"concurrent": True, "revision": 2}


@pytest.mark.parametrize("key", ["", "../outside", "docs/../runtime.json", "/docs/runtime.json", "docs//x.json",
                                    "docs\\x.json", "docs/%2e%2e/x.json", "docs/./x.json"])
def test_object_keys_cannot_escape_control_scope(store, key):
    with pytest.raises(ValueError):
        store.get_json(key)
    with pytest.raises(ValueError):
        store.put_json(key, {}, create=True)
    assert store.objects.calls == []


@pytest.mark.parametrize("prefix", ["", "unrelated/", ".control/gods_eye_view/../other/", ".control/gods_eye_view//",
                                       ".control/gods_eye_view", ".control/gods_eye_view/./"])
def test_store_scope_is_restricted(prefix):
    with pytest.raises(ValueError):
        ObjectControlStore(Objects(), "namespace", "bucket", prefix)


def test_generic_put_cannot_blindly_overwrite_or_create_twice(store):
    with pytest.raises(ValueError):
        store.put_json("posts/head.json", {})
    etag = store.put_json("posts/head.json", {"revision": 1}, create=True)
    with pytest.raises(ControlConflict):
        store.put_json("posts/head.json", {"revision": 2}, create=True)
    with pytest.raises(ControlConflict):
        store.put_json("posts/head.json", {"revision": 2}, expected_etag="stale")
    with pytest.raises(ValueError):
        store.put_json("posts/head.json", {}, expected_etag=etag, create=True)
    assert store.get_json("posts/head.json") == ({"revision": 1}, etag)


@pytest.mark.parametrize("name,document,expected", [("../runtime", {}, 0), ("configuration", [], 0),
                                                     ("configuration", {}, True), ("configuration", {}, -1)])
def test_document_validation_precedes_storage_writes(store, name, document, expected):
    with pytest.raises(ValueError):
        database.write_document(store, name, document, expected)
    assert store.objects.calls == []


def test_object_errors_and_missing_etag_are_not_treated_as_missing_documents(store):
    store.objects.get_object = Mock(side_effect=StorageError(403))
    with pytest.raises(StorageError):
        database.read_document(store, "configuration")
    store.objects.get_object = Mock(return_value=SimpleNamespace(data=SimpleNamespace(content=b'{"revision":1}'), headers={}))
    with pytest.raises(ValueError, match="ETag"):
        database.read_document(store, "configuration")


@pytest.mark.parametrize("runtime", [{}, {"analytics_store": "gold"},
    {"analytics_store": "gold", "control_migration_complete": "true"},
    {"analytics_store": "autonomous", "control_migration_complete": True}])
def test_no_empty_legacy_history_without_explicit_cutover_receipt(store, runtime):
    database.write_document(store, "runtime", runtime, 0)
    for operation in (database.reset_version, database.sensor_reset_version, lambda handle: list(database.publications(handle))):
        with pytest.raises(RuntimeError, match="migration"):
            operation(store)


@pytest.mark.parametrize("flag", ["control_migration_complete", "control_new_install"])
def test_explicit_migration_or_fresh_install_enables_gold_without_sql(store, flag):
    database.write_document(store, "runtime", {"analytics_store": "gold", flag: True}, 0)
    assert database.reset_version(store) == database.sensor_reset_version(store) == 3
    assert list(database.publications(store)) == []
    with pytest.raises(RuntimeError, match="Gold"):
        database.publish(store, {})


def replacement(store, sensor_type=None):
    ready(store)
    old = {"version": "gold-" + "a" * 32, "published_at": "2026-01-01T00:00:00Z", "incidents": [],
        "evidence": [] if sensor_type else [{"id": "synthetic", "mode": "Synthetic"}, {"id": "real", "mode": "real"}],
        "sensors": [{"id": "synthetic", "mode": "Synthetic", "is_simulated": True, "sensor_type": "river_level", "locality": "Kennedy"},
                    {"id": "real", "mode": "real", "is_simulated": False, "sensor_type": "river_level", "locality": "Kennedy"}]}
    clean = prune_publication(old, sensor_type)
    state = {"operation_id": "operation", "status": "pending", "ready": True, "sensor_type": sensor_type,
             "replacements": {old["version"]: clean["version"]}, "counts": {"history_rewritten": 94}}
    database.write_document(store, "checkpoint_reset", state, 0)
    store.objects.put_object(store.namespace, store.bucket, HISTORY_PREFIX + clean["version"] + ".json", json.dumps(clean).encode())
    return old, clean


@pytest.mark.parametrize("sensor_type", [None, "river_level", "all"])
def test_replacement_requires_a_durable_clean_scoped_snapshot_and_receipt(store, sensor_type):
    old, clean = replacement(store, sensor_type)
    before = copy.deepcopy(store.objects.values)
    if sensor_type:
        database.replace_sensor_publication(store, "operation", sensor_type, old["version"], clean["version"])
    else:
        database.replace_synthetic_publication(store, "operation", old["version"], clean["version"])
    assert store.objects.values == before
    key = HISTORY_PREFIX + clean["version"] + ".json"
    store.objects.values.pop(key)
    with pytest.raises(StorageError):
        validate_replacement(store, "operation", old["version"], clean["version"], sensor_type)


@pytest.mark.parametrize("change", [{"status": "cancelled", "cancelled_at": "2026-01-01"}, {"ready": False},
                                    {"sensor_type": "all"}, {"operation_id": "other"}, {"replacements": {}},
                                    {"cancelled_ids": ["operation"]}])
def test_terminal_or_changed_journal_cannot_authorize_replacement(store, change):
    old, clean = replacement(store)
    database.mutate_document(store, "checkpoint_reset", lambda doc: {**doc, **change})
    before = copy.deepcopy(store.objects.values)
    with pytest.raises(ControlConflict):
        database.replace_synthetic_publication(store, "operation", old["version"], clean["version"])
    assert store.objects.values == before


@pytest.mark.parametrize("change", [{"reset_of": "gold-" + "b" * 32}, {"version": "gold-" + "c" * 32},
    {"evidence": []}, {"evidence": [{"id": "synthetic", "mode": "Synthetic"}]}])
def test_replacement_rejects_corrupted_or_changed_durable_content(store, change):
    old, clean = replacement(store)
    key = HISTORY_PREFIX + clean["version"] + ".json"
    store.objects.put_object(store.namespace, store.bucket, key, json.dumps({**clean, **change}).encode())
    before = copy.deepcopy(store.objects.values)
    with pytest.raises(ValueError, match="replacement"):
        database.replace_synthetic_publication(store, "operation", old["version"], clean["version"])
    assert store.objects.values == before


def test_cancellation_during_durable_snapshot_read_is_not_authorization(store):
    old, clean = replacement(store)
    get_object = store.objects.get_object
    def cancel_on_snapshot(namespace, bucket, key):
        result = get_object(namespace, bucket, key)
        if key.startswith(HISTORY_PREFIX):
            database.mutate_document(store, "checkpoint_reset", lambda doc: {**doc, "status": "cancelled"})
        return result
    store.objects.get_object = cancel_on_snapshot
    with pytest.raises(ControlConflict):
        database.replace_synthetic_publication(store, "operation", old["version"], clean["version"])
    assert database.read_document(store, "checkpoint_reset")["status"] == "cancelled"
    assert get_object(store.namespace, store.bucket, HISTORY_PREFIX + clean["version"] + ".json")


def test_migrated_cancelled_journal_preserves_all_history_and_revision(store):
    history = {"revision": 87, "status": "cancelled", "operation_id": "cancelled", "sensor_type": "river_level",
               "replacements": {"old": "new"}, "counts": {"history_rewritten": 94}, "cancelled_at": "2026-01-01",
               "cancelled_ids": ["earlier"], "cancelled_operations": {"earlier": {"status": "cancelled", "counts": {}}},
               "operation_scopes": {"earlier": "all"}, "completed_ids": ["completed"]}
    store.put_json("docs/checkpoint_reset.json", history, create=True)
    assert database.read_document(store, "checkpoint_reset") == history
    updated = database.mutate_document(store, "checkpoint_reset", lambda doc: {**doc, "migration_checked": True})
    assert updated == {**history, "revision": 88, "migration_checked": True}


def test_post_facade_uses_durable_journal_and_disposable_index_without_oracle(store, tmp_path):
    store.index_path = tmp_path / "posts.sqlite"
    ready(store)
    records = [{"platform": "x", "source_id": name, "mode": mode, "created_at": "2026-01-01T00:00:00Z"}
               for name, mode in (("sample", "Synthetic"), ("retained", "real"))]
    database.upsert_posts(store, records, "captured", "2026-01-01T01:00:00Z")
    page = database.query_posts(store, "x", 20)
    assert page["total"] == 2 and {item["id"] for item in page["items"]} == {"x:sample", "x:retained"}
    database.write_document(store, "checkpoint_reset", {"operation_id": "cleanup", "status": "pending", "ready": True}, 0)
    assert database.purge_synthetic_posts(store, "cleanup") == 1
    store.index_path.unlink()
    rebuilt = database.query_ordered_posts(store, "x", 20)
    assert rebuilt["total"] == 1 and rebuilt["items"][0]["id"] == "x:retained"
    database.mutate_document(store, "checkpoint_reset", lambda doc: {**doc, "status": "cancelled"})
    with pytest.raises(ControlConflict):
        database.purge_synthetic_posts(store, "cleanup")
    assert database.query_posts(store, None, 20)["total"] == 1


def test_legacy_database_connection_still_uses_original_stored_procedures(monkeypatch):
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(DB_TYPE_CLOB="clob"))
    cursor = Mock()
    connection = Mock(cursor=Mock(return_value=cursor))
    cursor.callfunc.return_value = '{"revision":4,"legacy":true}'
    assert database.read_document(connection, "runtime") == {"revision": 4, "legacy": True}
    cursor.callfunc.assert_called_with("ADMIN.PRISMA_CONTROL.READ_DOC", "clob", ["runtime"])
    assert database.write_document(connection, "runtime", {"legacy": True}, 4)["revision"] == 5
    cursor.callproc.assert_called_with("ADMIN.PRISMA_CONTROL.WRITE_DOC", ["runtime", '{"legacy": true, "revision": 5}', 4])
    cursor.callfunc.return_value = 2
    assert database.reset_version(connection) == database.sensor_reset_version(connection) == 2
