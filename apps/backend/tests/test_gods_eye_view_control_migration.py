import copy
import json
from types import SimpleNamespace

import pytest

from app.gods_eye_view import control_migration as migration
from app.gods_eye_view.control_store import ControlConflict, ObjectControlStore
from app.gods_eye_view.post_index import query_posts


class Missing(RuntimeError):
    status = 404


class Objects:
    def __init__(self):
        self.values, self.writes, self.fail_key = {}, 0, None

    def get_object(self, namespace, bucket, key):
        if key not in self.values:
            raise Missing()
        value, etag = self.values[key]
        return SimpleNamespace(data=SimpleNamespace(content=json.dumps(value).encode()), headers={"etag": etag})

    def put_object(self, namespace, bucket, key, body, **conditions):
        if self.fail_key and key.endswith(self.fail_key):
            raise OSError("interrupted staging")
        existing = self.values.get(key)
        if conditions.get("if_none_match") == "*" and existing or "if_match" in conditions and (not existing or existing[1] != conditions["if_match"]):
            raise ControlConflict("changed")
        self.writes += 1
        self.values[key] = (json.loads(body), str(self.writes))
        return SimpleNamespace(headers={"etag": str(self.writes)})


class Oracle:
    transaction_in_progress = False
    autocommit = False

    def __init__(self):
        self.statements, self.rollbacks, self.closed = [], 0, False
        self.documents = {"runtime": {"revision": 12, "analytics_store": "autonomous", "namespace": "namespace", "bucket": "gold",
                         "writer_credential_name": "LegacyWriter", "reader_credential_name": "LegacyReader"},
            "configuration": {"revision": 41, "sources": {"x": {"enabled": False, "mode": "real"}}, "capture_interval_minutes": 7},
            "checkpoint_reset": {"revision": 88, "status": "cancelled", "operation_id": "cancelled", "sensor_type": "river_level",
                "counts": {"history_rewritten": 94}, "replacements": {"old": "new"}, "cancelled_ids": ["cancelled"],
                "cancelled_operations": {"earlier": {"status": "cancelled"}}, "operation_scopes": {"earlier": "all"}}}
        self.posts = [{"payload": {"id": "x:real", "platform": "x", "source_id": "real", "mode": "real", "created_at": "2026-01-01T00:00:00Z"},
                       "capture_seq": 9, "listing_revision": 14, "captured_at": "2026-01-01T01:00:00Z", "analysis_status": "processed"}]
        version = "gold-" + "a" * 32
        self.publications = {version: {"version": version, "incidents": [], "evidence": [], "sensors": []}}

    def cursor(self):
        return self

    def execute(self, sql, **args):
        self.statements.append(sql)
        if sql.startswith("SET TRANSACTION"):
            self.rows = []
        elif "FROM ADMIN.PRISMA_CONTROL_DOCS" in sql:
            self.rows = [(name, json.dumps(value)) for name, value in sorted(self.documents.items())]
        elif "FROM ADMIN.PRISMA_SOCIAL_POSTS" in sql:
            self.rows = [(json.dumps(row["payload"]), row["capture_seq"], row["listing_revision"], row["captured_at"], row["analysis_status"]) for row in self.posts]
        elif "ALL_TAB_IDENTITY_COLS" in sql:
            self.rows = [("ISEQ$$42",)]
        elif "ALL_SEQUENCES" in sql:
            assert args == {"capture": "ISEQ$$42", "listing": "PRISMA_POST_LIST_REVISION"}
            self.rows = [("ISEQ$$42", 101, 1, "N"), ("PRISMA_POST_LIST_REVISION", 201, 1, "N")]
        elif "FROM ADMIN.PRISMA_PUBLICATIONS" in sql:
            self.rows = [(name, json.dumps(value)) for name, value in self.publications.items()]
        else:
            raise AssertionError(sql)

    def __iter__(self):
        return iter(self.rows)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def export(oracle, **kwargs):
    return migration.export_snapshot(oracle, publication_sink=lambda *_: None, **kwargs)


def stage(tmp_path):
    oracle = Oracle()
    publications = {}
    snapshot = migration.export_snapshot(oracle, publication_sink=publications.__setitem__, writers_frozen=True)
    store = ObjectControlStore(Objects(), "namespace", "gold", index_path=tmp_path / "posts.sqlite")
    return oracle, snapshot, store, publications


def history_objects(store, publications):
    for version, publication in publications.items():
        store.objects.put_object(store.namespace, store.bucket, migration.HISTORY_PREFIX + version + ".json", json.dumps(publication).encode(), if_none_match="*")
    version = next(iter(publications))
    store.objects.put_object(store.namespace, store.bucket, "04_gold/prisma/current.json",
        json.dumps({"version": version, "snapshot_key": migration.HISTORY_PREFIX + version + ".json"}).encode(), if_none_match="*")


def test_readonly_export_preserves_revisions_gaps_cancelled_history_and_reserved_bounds(tmp_path):
    oracle, snapshot, store, publications = stage(tmp_path)
    assert oracle.statements[0] == "SET TRANSACTION READ ONLY"
    assert all(sql.startswith(("SELECT", "SET TRANSACTION READ ONLY")) and "NEXTVAL" not in sql for sql in oracle.statements)
    assert oracle.rollbacks == 1 and oracle.closed
    assert snapshot["documents"]["configuration"] == oracle.documents["configuration"]
    assert snapshot["documents"]["checkpoint_reset"] == oracle.documents["checkpoint_reset"]
    assert snapshot["posts"] == oracle.posts and publications == oracle.publications
    assert "publications" not in snapshot
    assert snapshot["publication_hashes"] == {key: migration._migration_hash(value) for key, value in publications.items()}
    assert snapshot["sequence_bounds"] == {"capture_sequence": {"row_max": 9, "reserved_upper_bound": 100},
                                           "listing_sequence": {"row_max": 14, "reserved_upper_bound": 200}}
    assert snapshot["removed_runtime_fields"] == ["reader_credential_name", "writer_credential_name"]
    assert "writer_credential_name" not in snapshot["documents"]["runtime"]
    assert store.objects.writes == 0


def test_publications_reach_sink_before_next_row_and_snapshot_keeps_only_hashes():
    seen = {}
    class StreamingOracle(Oracle):
        def __iter__(self):
            if "FROM ADMIN.PRISMA_PUBLICATIONS" not in self.statements[-1]:
                yield from self.rows
                return
            assert self.arraysize == self.prefetchrows == 1
            for index, (version, payload) in enumerate(self.rows):
                assert len(seen) == index
                yield version, payload
                assert seen[version] == json.loads(payload)
    oracle = StreamingOracle()
    second = "gold-" + "b" * 32
    oracle.publications[second] = {"version": second, "incidents": [], "evidence": []}
    snapshot = migration.export_snapshot(oracle, publication_sink=seen.__setitem__, writers_frozen=True)
    assert seen == oracle.publications
    assert "publications" not in snapshot
    assert snapshot["publication_hashes"] == {key: migration._migration_hash(value) for key, value in seen.items()}
    assert oracle.rollbacks == 1 and oracle.closed


def test_publication_sink_failure_aborts_export_and_closes_readonly_transaction():
    oracle = Oracle()
    def failed_sink(version, payload):
        assert version in oracle.publications and payload == oracle.publications[version]
        raise OSError("private export failed")
    with pytest.raises(OSError, match="private export failed"):
        migration.export_snapshot(oracle, publication_sink=failed_sink, writers_frozen=True)
    assert oracle.rollbacks == 1 and oracle.closed


def test_publication_sink_must_be_callable_before_database_access():
    oracle = Oracle()
    with pytest.raises(ValueError, match="publication sink"):
        migration.export_snapshot(oracle, publication_sink=None, writers_frozen=True)
    assert oracle.statements == []


@pytest.mark.parametrize("changed", ["version", "incidents", "evidence"])
def test_export_rejects_invalid_publication_before_calling_sink(changed):
    oracle, seen = Oracle(), []
    next(iter(oracle.publications.values()))[changed] = None
    with pytest.raises(ValueError, match="publication"):
        migration.export_snapshot(oracle, publication_sink=lambda *row: seen.append(row), writers_frozen=True)
    assert seen == [] and oracle.rollbacks == 1 and oracle.closed


def test_partial_stage_resumes_exactly_without_activating_or_overwriting(tmp_path):
    _, snapshot, store, publications = stage(tmp_path)
    store.objects.fail_key = "runtime.json"
    with pytest.raises(OSError):
        migration.stage_snapshot(store, snapshot)
    preserved = copy.deepcopy(store.objects.values)
    store.objects.fail_key = None
    receipt = migration.stage_snapshot(store, snapshot)
    for key, value in preserved.items():
        assert store.objects.values[key] == value
    writes = store.objects.writes
    assert migration.stage_snapshot(store, snapshot) == receipt
    assert store.objects.writes == writes
    with pytest.raises(RuntimeError, match="migration"):
        store.require_ready()
    page = query_posts(store, None, 20)
    assert page["items"][0]["capture_seq"] == 9 and page["items"][0]["captured_at"] == "2026-01-01T01:00:00Z"
    assert store.get_json("posts/head.json")[0]["sequence"] == 200
    changed = copy.deepcopy(snapshot)
    changed["documents"]["configuration"]["revision"] += 1
    with pytest.raises(ControlConflict):
        migration.stage_snapshot(store, changed)


def test_native_oracle_json_dicts_keep_export_validation_and_runtime_allowlist():
    class NativeJsonOracle(Oracle):
        def execute(self, sql, **args):
            super().execute(sql, **args)
            self.rows = [tuple(json.loads(value) if isinstance(value, str) and value.startswith("{") else value
                               for value in row) for row in self.rows]

    oracle = NativeJsonOracle()
    snapshot = export(oracle, writers_frozen=True)
    assert snapshot == export(Oracle(), writers_frozen=True)
    assert oracle.closed and oracle.rollbacks == 1
    oracle = NativeJsonOracle()
    oracle.documents["runtime"]["private_key"] = "never-copy-or-display"
    with pytest.raises(ValueError, match="unreviewed") as error:
        export(oracle, writers_frozen=True)
    assert "never-copy-or-display" not in str(error.value)
    assert oracle.closed and oracle.rollbacks == 1


def test_cancelled_cleanup_audit_metadata_survives_staging_and_activation(tmp_path):
    oracle = Oracle()
    audit = {"cleanup_cancel_operation_id": oracle.documents["checkpoint_reset"]["operation_id"],
             "cleanup_cancel_requested_at": "2026-01-01T03:00:00Z", "cleanup_previous_streaming_mode": "persistent"}
    oracle.documents["runtime"].update(audit)
    publications = {}
    snapshot = migration.export_snapshot(oracle, publication_sink=publications.__setitem__, writers_frozen=True)
    store = ObjectControlStore(Objects(), "namespace", "gold", index_path=tmp_path / "posts.sqlite")
    migration.stage_snapshot(store, snapshot)
    history_objects(store, publications)
    migration.activate_snapshot(store, snapshot, gold_publications=snapshot["publication_hashes"], writers_frozen=True)
    runtime = store.get_json("docs/runtime.json")[0]
    assert {key: runtime[key] for key in audit} == audit
    assert store.get_json("docs/checkpoint_reset.json")[0] == oracle.documents["checkpoint_reset"]
    assert store.get_json("docs/configuration.json")[0] == oracle.documents["configuration"]


def test_activation_requires_each_history_copy_gold_hash_and_current_pointer(tmp_path):
    _, snapshot, store, publications = stage(tmp_path)
    migration.stage_snapshot(store, snapshot)
    with pytest.raises(Missing):
        migration.activate_snapshot(store, snapshot, gold_publications=snapshot["publication_hashes"], writers_frozen=True)
    history_objects(store, publications)
    with pytest.raises(ValueError, match="history"):
        migration.activate_snapshot(store, snapshot, gold_publications={}, writers_frozen=True)
    assert store.get_json("docs/runtime.json")[0] == snapshot["documents"]["runtime"]
    receipt = migration.activate_snapshot(store, snapshot, gold_publications=snapshot["publication_hashes"], writers_frozen=True)
    assert receipt["runtime_revision"] == 13
    store.require_ready()
    writes = store.objects.writes
    assert migration.activate_snapshot(store, snapshot, gold_publications=snapshot["publication_hashes"], writers_frozen=True) == receipt
    assert store.objects.writes == writes
    assert store.get_json("docs/checkpoint_reset.json")[0] == snapshot["documents"]["checkpoint_reset"]


@pytest.mark.parametrize("corruption", ["history_payload", "current_pointer", "current_gold_hash"])
def test_activation_never_marks_complete_with_incomplete_or_changed_history(tmp_path, corruption):
    _, snapshot, store, publications = stage(tmp_path)
    migration.stage_snapshot(store, snapshot)
    history_objects(store, publications)
    hashes = dict(snapshot["publication_hashes"])
    version = next(iter(hashes))
    if corruption == "history_payload":
        store.objects.values[migration.HISTORY_PREFIX + version + ".json"][0]["extra"] = "changed"
    else:
        next_version = "gold-" + "b" * 32
        store.objects.values["04_gold/prisma/current.json"][0].update(version=next_version,
            snapshot_key=migration.HISTORY_PREFIX + next_version + ".json")
        if corruption == "current_gold_hash":
            store.objects.values[migration.HISTORY_PREFIX + next_version + ".json"] = ({"version": next_version}, "receipt")
    with pytest.raises((ValueError, Missing)):
        migration.activate_snapshot(store, snapshot, gold_publications=hashes, writers_frozen=True)
    assert store.get_json("docs/runtime.json")[0] == snapshot["documents"]["runtime"]


@pytest.mark.parametrize("changed", ["version", "incidents", "evidence"])
def test_activation_rejects_malformed_history_even_with_matching_hashes(tmp_path, changed):
    _, snapshot, store, publications = stage(tmp_path)
    version = next(iter(publications))
    publications[version][changed] = None
    hashes = {version: migration._migration_hash(publications[version])}
    snapshot["publication_hashes"] = hashes
    migration.stage_snapshot(store, snapshot)
    history_objects(store, publications)
    with pytest.raises(ValueError, match="publication"):
        migration.activate_snapshot(store, snapshot, gold_publications=hashes, writers_frozen=True)
    assert store.get_json("docs/runtime.json")[0] == snapshot["documents"]["runtime"]


def test_migration_cannot_write_to_a_different_scope(tmp_path):
    _, snapshot, store, publications = stage(tmp_path)
    store.bucket = "different"
    with pytest.raises(ValueError, match="scope"):
        migration.stage_snapshot(store, snapshot)
    assert store.objects.writes == 0


@pytest.mark.parametrize("field", ["private_key", "db_password", "wallet", "wallet_password", "token", "unreviewed_metadata"])
def test_runtime_unknown_or_secret_fields_abort_export_without_values_in_error(field):
    oracle = Oracle()
    oracle.documents["runtime"][field] = "never-copy-or-display"
    with pytest.raises(ValueError, match="unreviewed") as error:
        export(oracle, writers_frozen=True)
    assert "never-copy-or-display" not in str(error.value)
    assert oracle.closed and oracle.rollbacks == 1


@pytest.mark.parametrize("change", ["not_frozen", "existing_transaction", "autocommit", "pending_cleanup", "unbackfilled_posts"])
def test_export_fails_before_migration_for_unsafe_or_unsupported_source(change):
    oracle = Oracle()
    if change == "existing_transaction":
        oracle.transaction_in_progress = True
    if change == "autocommit":
        oracle.autocommit = True
    if change == "pending_cleanup":
        oracle.documents["checkpoint_reset"]["status"] = "pending"
    if change == "unbackfilled_posts":
        oracle.posts[0]["listing_revision"] = 0
    with pytest.raises(ValueError):
        export(oracle, writers_frozen=change != "not_frozen")
