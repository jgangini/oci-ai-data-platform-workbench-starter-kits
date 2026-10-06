"""Reset ordering and recovery with mixed real/synthetic durable stores."""
import copy
import json
import hashlib
import threading
from unittest.mock import MagicMock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.territorial import database, landing, pipeline, sensors, synthetic_reset as reset
from app.territorial.core import normalize_event, simulation_events, build_snapshot, default_source
from test_territorial_pipeline import CONFIG, NOW, runtime, Lake


class ObjectError(RuntimeError):
    def __init__(self, status):
        self.status = status


class ResetLake(Lake):
    def synthetic_ids(self):
        return {key for layer in ("bronze", "silver") for key, value in self.data[layer].items()
                if value["mode"] in {"Synthetic", "simulation"}}

    def delete_synthetic(self):
        counts = {}
        for layer in ("bronze", "silver"):
            previous = self.data[layer]
            self.data[layer] = {key: value for key, value in previous.items() if value["mode"] not in {"Synthetic", "simulation"}}
            counts[layer] = len(previous) - len(self.data[layer])
        self.log.append("purge:delta")
        return counts

    def publications(self):
        return iter(copy.deepcopy(list(self.data["gold"].values())))

    def delete_publications(self, versions):
        for version in versions:
            self.log.append("delete:gold:" + version)
            self.data["gold"].pop(version, None)


def history_records(state, count=5):
    _, _, publications, lake, objects, *_, snapshot = state
    originals = [{**snapshot, "version": "gold-" + f"{index:032x}"} for index in range(count)]
    publications.clear()
    publications.update({item["version"]: copy.deepcopy(item) for item in originals})
    lake.data["gold"] = {item["version"]: {"id": item["version"], **copy.deepcopy(item)} for item in originals}
    objects.data = {reset.HISTORY_PREFIX + item["version"] + ".json": reset.encoded(item) for item in originals}
    return originals


@pytest.fixture
def resetting(runtime, monkeypatch):
    log, docs, publications, _lake, objects = runtime
    lake = ResetLake(log, objects)
    monkeypatch.setattr(database, "read_document", pipeline.read_document)
    monkeypatch.setattr(database, "mutate_document", pipeline.mutate_document)
    monkeypatch.setattr(database, "publish", pipeline.publish)
    monkeypatch.setattr(database, "publications", lambda _db: iter(copy.deepcopy(list(publications.values()))))
    monkeypatch.setattr(database, "purge_synthetic_posts", lambda _db, _op: log.append("purge:adb") or 1)
    def replace(_db, _op, old, new):
        assert new in publications
        assert reset.prune_publication(publications[new]) is None
        publications.pop(old, None)
        log.append("delete:adb:" + old)
    monkeypatch.setattr(database, "replace_synthetic_publication", replace)
    def get(_namespace, _bucket, key):
        if key not in objects.data:
            raise ObjectError(404)
        body = objects.data[key]
        return SimpleNamespace(data=SimpleNamespace(content=body), headers={"etag": hashlib.sha256(body).hexdigest()})
    def delete(_namespace, _bucket, key, **kwargs):
        if key not in objects.data:
            raise ObjectError(404)
        if "if_match" in kwargs:
            assert kwargs["if_match"] == hashlib.sha256(objects.data[key]).hexdigest()
        log.append("delete:object:" + key)
        del objects.data[key]
    def listing(_namespace, _bucket, **kwargs):
        return SimpleNamespace(data=SimpleNamespace(objects=[SimpleNamespace(name=key)
            for key in sorted(objects.data) if key.startswith(kwargs["prefix"])], next_start_with=None))
    objects.get_object, objects.delete_object, objects.list_objects = get, delete, listing
    synthetic = normalize_event(simulation_events(0)[0])
    real = normalize_event({**simulation_events(0)[0], "mode": "real", "is_simulated": False, "source_id": "real-preserved"})
    for layer in ("bronze", "silver"):
        lake.put(layer, [synthetic, real])
    docs["configuration"] = {"sources": {"x": {**default_source("x"), "mode": "real", "query": "#real", "capture_running": True}}}
    docs["checkpoint_x"] = {"cursor": {"since_id": "91"}}
    docs["checkpoint_controls"] = {"x": {"run_id": "real-control"}, "facebook": {"run_id": "old"}, "institutional": {"run_id": "old"}}
    docs["checkpoint_enrichment"] = {"prepared": [synthetic, real], "pending_ids": [synthetic["id"], real["id"]], "retry_at": 100}
    snapshot = pipeline.publish_snapshot(object(), objects, lake, CONFIG, [synthetic, real], {}, {}, NOW,
                                         rules={"x": default_source("x")})
    docs["reviews"] = {"items": {item["id"]: {"note": item["mode"], "evidence_ids": item["evidence_ids"]} for item in snapshot["incidents"]}}
    docs["checkpoint_reset"] = {"operation_id": str(uuid4()), "status": "pending", "ready": True}
    landing.write_objects(objects, CONFIG, [synthetic, real], {"platform": "x", "batch": "mixed"})
    landing.write_objects(objects, CONFIG, [], {"platform": "x", "batch": "empty"})
    objects.data[CONFIG["landing_prefix"] + ".keep"] = b""
    return log, docs, publications, lake, objects, synthetic, real, snapshot


def run_reset(state):
    return pipeline.process_reset(object(), state[4], state[3], CONFIG, NOW)


def test_reset_progress_tracks_cleanup_and_finishes_after_history(resetting, monkeypatch):
    _, docs, _, lake, *_ = resetting
    observed = []
    for owner, name, stage in ((lake, "consume", "draining"), (reset, "clean_landing", "landing"),
            (lake, "delete_synthetic", "delta"), (database, "purge_synthetic_posts", "database"),
            (pipeline, "publish_snapshot", "publishing"), (reset, "clean_history", "history")):
        operation = getattr(owner, name)
        def check(*args, _operation=operation, _stage=stage, **kwargs):
            assert docs["checkpoint_reset"]["stage"] == _stage
            assert docs["checkpoint_reset"]["status"] == "pending"
            observed.append(_stage)
            return _operation(*args, **kwargs)
        monkeypatch.setattr(owner, name, check)
    run_reset(resetting)
    assert observed == ["draining", "landing", "delta", "database", "publishing", "history"]
    assert docs["checkpoint_reset"]["stage"] == docs["checkpoint_reset"]["status"] == "completed"


def test_history_batches_delta_writes_and_journal_only_after_all_three_new_copies(resetting, monkeypatch):
    _, docs, publications, lake, objects, *_ = resetting
    originals = history_records(resetting)
    puts, deletes = MagicMock(wraps=lake.put), MagicMock(wraps=lake.delete_publications)
    monkeypatch.setattr(lake, "put", puts)
    monkeypatch.setattr(lake, "delete_publications", deletes)
    mutate = database.mutate_document
    commits = []
    def journal(connection, name, change):
        if name == "checkpoint_reset":
            next_state = change(copy.deepcopy(docs[name]))
            for version in next_state["replacements"].values():
                assert version in publications and version in lake.data["gold"]
                assert json.loads(objects.data[reset.HISTORY_PREFIX + version + ".json"]) == publications[version]
            commits.append(set(next_state["replacements"]))
        return mutate(connection, name, change)
    monkeypatch.setattr(database, "mutate_document", journal)
    reset.clean_history(None, objects, lake, CONFIG, docs["checkpoint_reset"]["operation_id"])
    assert [len(call.args[1]) for call in puts.call_args_list] == [4, 1]
    assert [len(call.args[0]) for call in deletes.call_args_list] == [4, 1]
    assert list(map(len, commits)) == [4, 5]
    assert docs["checkpoint_reset"]["counts"]["history_rewritten"] == 5
    assert not {item["version"] for item in originals}.intersection(publications)


@pytest.mark.parametrize("sensor_type", [None, "river_level", "all"])
def test_gold_history_reset_preserves_durable_recovery_without_recreating_adb_copies(resetting, monkeypatch, sensor_type):
    _, docs, publications, lake, objects, *_, snapshot = resetting
    readings = sensors.generate_batch(NOW, sensor_count=5)
    snapshot["sensors"] = [*readings, {**readings[0], "event_id": "real-reading", "sensor_id": "real-station",
                                      "mode": "real", "is_simulated": False}]
    old = history_records(resetting, 1)[0]
    clean = reset.prune_publication(old, sensor_type)
    config = {**CONFIG, "analytics_store": "gold"}
    monkeypatch.setattr(database, "reset_version", lambda _: 3)
    monkeypatch.setattr(database, "sensor_reset_version", lambda _: 3)
    docs["runtime"] = {"analytics_store": "gold"}
    docs["checkpoint_reset"]["sensor_type"] = sensor_type
    operation = docs["checkpoint_reset"]["operation_id"]
    replacement_key = reset.HISTORY_PREFIX + clean["version"] + ".json"
    monkeypatch.setattr(database, "publish", lambda *_: pytest.fail("Gold cleanup must not create analytical ADB copies"))
    deleted = []
    def replace(_connection, identifier, *arguments):
        old_version, new_version = arguments[-2:]
        assert identifier == operation and docs["checkpoint_reset"]["sensor_type"] == sensor_type
        assert docs["checkpoint_reset"]["replacements"][old_version] == new_version
        assert new_version not in publications
        assert lake.data["gold"][new_version] == {"id": new_version, **clean}
        assert json.loads(objects.data[replacement_key]) == clean
        publications.pop(old_version, None)
        deleted.append(old_version)
    monkeypatch.setattr(database, "replace_synthetic_publication", replace)
    monkeypatch.setattr(database, "replace_sensor_publication", replace)
    objects.fail = replacement_key
    with pytest.raises(RuntimeError, match="Object Storage"):
        reset.clean_history(None, objects, lake, config, operation, sensor_type)
    assert deleted == [] and not docs["checkpoint_reset"].get("replacements")
    assert publications == {old["version"]: old}
    assert lake.data["gold"][old["version"]] == {"id": old["version"], **old}
    assert json.loads(objects.data[reset.HISTORY_PREFIX + old["version"] + ".json"]) == old
    objects.fail = None
    reset.clean_history(None, objects, lake, config, operation, sensor_type)
    assert deleted == [old["version"]] and publications == {}
    assert lake.data["gold"] == {clean["version"]: {"id": clean["version"], **clean}}
    assert objects.data == {replacement_key: reset.encoded(clean)}
    assert docs["checkpoint_reset"]["counts"]["history_rewritten"] == 1
    assert docs["checkpoint_reset"]["replacements"] == {old["version"]: clean["version"]}
    assert docs["runtime"] == {"analytics_store": "gold"}


@pytest.mark.parametrize("sensor_type,social_version,sensor_version", [(None, 2, 3), ("all", 2, 3), ("all", 3, 2)])
def test_gold_history_reset_rejects_legacy_database_before_writing(resetting, monkeypatch, sensor_type, social_version, sensor_version):
    log, docs, publications, lake, objects, *_, snapshot = resetting
    snapshot["sensors"] = sensors.generate_batch(NOW, sensor_count=5)
    history_records(resetting, 1)
    docs["checkpoint_reset"]["sensor_type"] = sensor_type
    monkeypatch.setattr(database, "reset_version", lambda _: social_version)
    monkeypatch.setattr(database, "sensor_reset_version", lambda _: sensor_version)
    before = copy.deepcopy((docs, publications, lake.data, objects.data))
    log.clear()
    with pytest.raises(RuntimeError, match="Gold reset database contract"):
        reset.clean_history(None, objects, lake, {**CONFIG, "analytics_store": "gold"},
                            docs["checkpoint_reset"]["operation_id"], sensor_type)
    assert (docs, publications, lake.data, objects.data) == before
    assert log == []


@pytest.mark.parametrize("phase", ["gold", "adb", "object", "journal", "delta_delete", "adb_delete", "object_delete"])
def test_history_batch_failure_recovers_every_store_without_losing_retained_data(resetting, monkeypatch, phase):
    _, docs, publications, lake, objects, *_ = resetting
    originals = history_records(resetting, 4)
    targets = {"gold": (lake, "put"), "adb": (database, "publish"), "object": (objects, "put_object"),
               "journal": (database, "mutate_document"), "delta_delete": (lake, "delete_publications"),
               "adb_delete": (database, "replace_synthetic_publication"), "object_delete": (objects, "delete_object")}
    owner, name = targets[phase]
    original = getattr(owner, name)
    calls = 0
    def fail(*args, **kwargs):
        nonlocal calls
        if phase == "journal" and args[1] != "checkpoint_reset":
            return original(*args, **kwargs)
        calls += 1
        if calls == (2 if phase in {"adb", "object", "adb_delete", "object_delete"} else 1):
            if phase == "delta_delete":
                original(*args, **kwargs)  # The Delta commit succeeded but its acknowledgement was lost.
            raise RuntimeError("Injected history batch failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(owner, name, fail)
    identifier = docs["checkpoint_reset"]["operation_id"]
    with pytest.raises(RuntimeError, match="Injected"):
        reset.clean_history(None, objects, lake, CONFIG, identifier)
    for old in originals:
        version = old["version"]
        if phase in {"gold", "adb", "object", "journal"}:
            assert publications[version] == old
            assert lake.data["gold"][version] == {"id": version, **old}
            assert json.loads(objects.data[reset.HISTORY_PREFIX + version + ".json"]) == old
        else:
            clean = reset.prune_publication(old)
            new = clean["version"]
            assert docs["checkpoint_reset"]["replacements"][version] == new
            assert publications[new] == clean
            assert lake.data["gold"][new] == {"id": new, **clean}
            assert json.loads(objects.data[reset.HISTORY_PREFIX + new + ".json"]) == clean
    monkeypatch.setattr(owner, name, original)
    reset.clean_history(None, objects, lake, CONFIG, identifier)
    for old in originals:
        clean = reset.prune_publication(old)
        assert old["version"] not in publications and old["version"] not in lake.data["gold"]
        assert reset.HISTORY_PREFIX + old["version"] + ".json" not in objects.data
        assert publications[clean["version"]] == clean
        assert clean["evidence"] == [item for item in old["evidence"] if item["mode"] == "real"]
    assert docs["checkpoint_reset"]["counts"]["history_rewritten"] == 4


@pytest.mark.parametrize("change", [{"status": "cancelled"}, {"operation_id": "other"}, {"sensor_type": "all"}, {"ready": False}])
@pytest.mark.parametrize("when", ["before", "journal"])
def test_history_batch_checks_exact_pending_operation_before_writes_and_before_old_deletes(resetting, monkeypatch, change, when):
    _, docs, publications, lake, objects, *_ = resetting
    originals = history_records(resetting, 4)
    identifier = docs["checkpoint_reset"]["operation_id"]
    if when == "before":
        docs["checkpoint_reset"].update(change)
    else:
        put = objects.put_object
        def changed(*args, **kwargs):
            put(*args, **kwargs)
            docs["checkpoint_reset"].update(change)
        monkeypatch.setattr(objects, "put_object", changed)
    before = copy.deepcopy(docs["checkpoint_reset"])
    with pytest.raises(RuntimeError, match="no longer"):
        reset.clean_history(None, objects, lake, CONFIG, identifier)
    assert docs["checkpoint_reset"] == {**before, **change}
    for old in originals:
        assert publications[old["version"]] == old
        assert old["version"] in lake.data["gold"]
        assert json.loads(objects.data[reset.HISTORY_PREFIX + old["version"] + ".json"]) == old
    if when == "before":
        assert len(publications) == len(lake.data["gold"]) == len(objects.data) == 4


def test_history_batch_bounds_bytes_and_runs_one_large_legacy_publication_alone(resetting, monkeypatch):
    originals = history_records(resetting, 9)
    size = len(reset.encoded(reset.prune_publication(originals[0])))
    monkeypatch.setattr(reset, "HISTORY_BATCH_BYTES", size * 2)
    batches = list(reset._history_batches(((item, None) for item in originals), None))
    assert [len(batch) for batch in batches] == [2, 2, 2, 2, 1]
    assert all(sum(len(row[2]) for row in batch) <= size * 2 for batch in batches)
    monkeypatch.setattr(reset, "HISTORY_BATCH_BYTES", size - 1)
    assert all(len(batch) == 1 for batch in reset._history_batches(((item, None) for item in originals), None))


def test_reset_purges_synthetic_everywhere_preserves_real_and_restarts_from_same_landing(resetting):
    log, docs, publications, lake, objects, synthetic, real, old = resetting
    original_configuration = copy.deepcopy(docs["configuration"])
    result = run_reset(resetting)
    assert result["evidence"] == [real]
    assert docs["checkpoint_reset"]["status"] == "completed"
    assert docs["checkpoint_reset"]["operation_id"] in docs["checkpoint_reset"]["completed_ids"]
    assert docs["configuration"] == original_configuration and docs["checkpoint_x"] == {"cursor": {"since_id": "91"}}
    assert docs["checkpoint_controls"]["x"] == {"run_id": "real-control"}
    assert set(docs["checkpoint_controls"]) == {"x", "revision"}
    assert docs["checkpoint_enrichment"]["prepared"] == [real]
    assert docs["checkpoint_enrichment"]["pending_ids"] == [real["id"]]
    assert all(item["note"] == "real" for item in docs["reviews"]["items"].values())
    assert old["version"] not in publications and old["version"] not in lake.data["gold"]
    replacement = publications[docs["checkpoint_reset"]["replacements"][old["version"]]]
    assert replacement["incidents"] == [item for item in old["incidents"] if item["mode"] == "real"]
    assert replacement["published_at"] == old["published_at"]
    for snapshot in publications.values():
        assert reset.prune_publication(snapshot) is None
    lake.consume(CONFIG)
    assert set(lake.data["bronze"]) == set(lake.data["silver"]) == {real["id"]}
    assert CONFIG["landing_prefix"] + ".keep" in objects.data
    assert log.index("purge:delta") < log.index("delete:gold:" + old["version"])
    assert run_reset(resetting) is None


def test_social_reset_preserves_nested_sensor_txt_and_its_controls(resetting):
    _, docs, _, lake, objects, *_ = resetting
    key = CONFIG["landing_prefix"] + "sensors/river_level/batch-1.txt"
    objects.data[key] = b'{"is_simulated":true}\n'
    docs["checkpoint_sensors"] = {"batch_id": "retained"}
    result = run_reset(resetting)
    assert objects.data[key] == b'{"is_simulated":true}\n'
    assert docs["checkpoint_sensors"] == {"batch_id": "retained"}
    assert all(item["mode"] == "real" for item in result["evidence"])


@pytest.mark.parametrize("prefix,key", [
    ("01_landing/prisma/raw/", "01_landing/prisma/raw/sensors-other/nested.txt"),
    (reset.HISTORY_PREFIX, reset.HISTORY_PREFIX + "sensors/nested.txt"),
])
def test_sensor_exclusion_does_not_relax_other_reset_prefix_guards(resetting, prefix, key):
    objects = resetting[4]
    objects.data = {key: b"preserve"}
    with pytest.raises(ValueError, match="escaped"):
        list(reset.object_keys(objects, CONFIG, CONFIG["bucket"], prefix))
    assert objects.data == {key: b"preserve"}


def test_drain_failure_preserves_landing_and_all_data_and_requires_explicit_retry(resetting):
    _, docs, _, lake, objects, _synthetic, real, _old = resetting
    original = copy.deepcopy(objects.data)
    consume = lake.consume
    lake.consume = lambda _config: (_ for _ in ()).throw(RuntimeError("private failure"))
    with pytest.raises(RuntimeError, match="incomplete"):
        run_reset(resetting)
    assert objects.data == original and len(lake.data["bronze"]) == 2
    assert docs["checkpoint_reset"]["status"] == "error"
    assert docs["checkpoint_reset"]["stage"] == "draining"
    assert docs["checkpoint_reset"]["error"] == "RuntimeError"
    with pytest.raises(RuntimeError, match="explicitly retry"):
        run_reset(resetting)
    docs["checkpoint_reset"]["status"] = "pending"
    lake.consume = consume
    assert run_reset(resetting)["evidence"] == [real]


def test_retry_finishes_historical_replacement_after_partial_store_failure(resetting, monkeypatch):
    _, docs, publications, lake, objects, _synthetic, real, old = resetting
    delete = objects.delete_object
    fail = [True]
    def interrupted(namespace, bucket, key, **kwargs):
        if key == reset.HISTORY_PREFIX + old["version"] + ".json" and fail[0]:
            fail[0] = False
            raise RuntimeError("interrupted history cleanup")
        return delete(namespace, bucket, key, **kwargs)
    monkeypatch.setattr(objects, "delete_object", interrupted)
    with pytest.raises(RuntimeError, match="incomplete"):
        run_reset(resetting)
    assert docs["checkpoint_reset"]["status"] == "error"
    assert docs["checkpoint_reset"]["stage"] == "history"
    assert not docs["checkpoint_reset"].get("completed_ids")
    assert old["version"] not in publications and old["version"] not in lake.data["gold"]
    assert reset.HISTORY_PREFIX + old["version"] + ".json" in objects.data
    operation_id = docs["checkpoint_reset"]["operation_id"]
    docs["checkpoint_reset"]["status"] = "pending"
    assert run_reset(resetting)["evidence"] == [real]
    assert docs["checkpoint_reset"]["operation_id"] == operation_id
    assert reset.HISTORY_PREFIX + old["version"] + ".json" not in objects.data


def test_unready_reset_does_not_read_or_modify_stores(resetting):
    _, docs, _, lake, objects, *_ = resetting
    docs["checkpoint_reset"]["ready"] = False
    before = copy.deepcopy((lake.data, objects.data))
    assert run_reset(resetting) is None
    assert (lake.data, objects.data) == before


def test_foreign_prefix_is_rejected_without_deleting_data(resetting):
    _, docs, _, lake, objects, *_ = resetting
    before = copy.deepcopy((lake.data, objects.data))
    with pytest.raises(RuntimeError, match="incomplete"):
        pipeline.process_reset(object(), objects, lake, {**CONFIG, "landing_prefix": "01_landing/"}, NOW)
    assert (lake.data, objects.data) == before
    assert docs["checkpoint_reset"]["status"] == "error"


def test_stop_and_join_happens_before_reset_and_stream_restart(resetting, monkeypatch):
    log, docs, _, lake, objects, synthetic, real, _old = resetting
    docs["status_sensorstream"] = {"sensor_layers_version": 2}
    joined = []
    late = {**synthetic, "id": "x:in-flight", "source_id": "in-flight"}
    def join(name):
        log.append("join:" + name)
        if name == "csv" and not joined:
            # The callback already running at stop() must finish before reset begins.
            lake.put("bronze", [late])
            landing.write_objects(objects, CONFIG, [late], {"platform": "x", "batch": "in-flight"})
            joined.append(True)
    def query(name):
        return SimpleNamespace(stop=lambda: log.append("stop:" + name), awaitTermination=lambda: join(name),
            exception=lambda: None, isActive=True, id=name, recentProgress=[], lastProgress={})
    calls = []
    def start(*_args, **_kwargs):
        calls.append(True)
        log.append("streams:start")
        return [("json", query("json")), ("csv", query("csv"))]
    monkeypatch.setattr(pipeline, "start_landing", start)
    monkeypatch.setattr(pipeline, "_tick", lambda *_args, **_kwargs: log.append("tick"))
    monkeypatch.setattr(pipeline.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        pipeline.run_persistent(None, object(), objects, lake, CONFIG, None, None, None, lambda: NOW)
    assert len(calls) == 2
    assert max(log.index("join:json"), log.index("join:csv")) < log.index("purge:delta")
    assert docs["checkpoint_reset"]["status"] == "completed"
    lake.consume(CONFIG)
    assert set(lake.data["bronze"]) == {real["id"]}


def test_changed_object_etag_aborts_without_deleting_replacement(resetting, monkeypatch):
    _, docs, _, lake, objects, *_ = resetting
    def conflicting_delete(_namespace, _bucket, _key, **kwargs):
        assert kwargs.get("if_match")
        raise ObjectError(412)
    original = copy.deepcopy(objects.data)
    monkeypatch.setattr(objects, "delete_object", conflicting_delete)
    with pytest.raises(RuntimeError, match="incomplete"):
        run_reset(resetting)
    assert all(objects.data[key] == body for key, body in original.items())
    assert len(lake.data["bronze"]) == 2
    assert docs["checkpoint_reset"]["status"] == "error"


def test_scoped_sql_requires_active_operation_and_never_truncates_or_resets_identity():
    assert "require_reset(p_operation_id)" in database.PACKAGE_BODY
    assert "DELETE FROM ADMIN.PRISMA_SOCIAL_POSTS WHERE JSON_VALUE(payload,'$.mode') IN ('Synthetic','simulation')" in database.PACKAGE_BODY
    assert "Clean replacement publication is missing" in database.PACKAGE_BODY
    assert "TRUNCATE" not in database.PACKAGE_BODY and "DROP TABLE" not in database.PACKAGE_BODY


def test_failed_mixed_landing_replacement_preserves_original_and_can_retry(resetting, monkeypatch):
    _, docs, _, lake, objects, _synthetic, real, _old = resetting
    original = copy.deepcopy(objects.data)
    write = reset.landing.write_objects
    monkeypatch.setattr(reset.landing, "write_objects", lambda *_: (_ for _ in ()).throw(OSError("storage unavailable")))
    with pytest.raises(RuntimeError, match="incomplete"):
        run_reset(resetting)
    assert objects.data == original and len(lake.data["bronze"]) == 2
    monkeypatch.setattr(reset.landing, "write_objects", write)
    docs["checkpoint_reset"]["status"] = "pending"
    assert run_reset(resetting)["evidence"] == [real]


def test_unknown_and_mixed_review_provenance_is_preserved(resetting):
    _, docs, _, _, _, synthetic, real, _old = resetting
    reviews = {"mixed-review": {"note": "keep", "evidence_ids": [synthetic["id"], real["id"]]},
               "unknown-review": {"note": "keep"},
               "synthetic-review": {"note": "delete", "evidence_ids": [synthetic["id"]]}}
    docs["reviews"] = {"items": copy.deepcopy(reviews)}
    reset._clean_controls(object(), {synthetic["id"]}, set())
    assert docs["reviews"]["items"] == {key: value for key, value in reviews.items() if key != "synthetic-review"}


@pytest.mark.parametrize("kind", ["all", *sensors.SENSOR_TYPES])
def test_sensor_history_scopes_preserve_real_unknown_and_social_data(resetting, kind):
    synthetic = [{**item, "id": item["sensor_id"]} for item in sensors.generate_batch(NOW, sensor_count=5)]
    real = [{**item, "id": "real-" + item["id"], "mode": "real", "is_simulated": False} for item in synthetic]
    unknown = [{**synthetic[0], "id": "unknown-provenance", "is_simulated": False},
               {**synthetic[0], "id": "unknown-type", "sensor_type": "unrecognized"}]
    snapshot = {**copy.deepcopy(resetting[-1]), "sensors": [*synthetic, *real, *unknown]}
    original = copy.deepcopy(snapshot)
    result = reset.prune_publication(snapshot, kind)
    expected = [item for item in synthetic if kind != "all" and item["sensor_type"] != kind] + real + unknown
    assert result["sensors"] == expected
    assert all(result[key] == snapshot[key] for key in ("evidence", "event_posts", "published_at"))
    assert [item for item in result["incidents"] if item["mode"] == "real"] == [
        item for item in snapshot["incidents"] if item["mode"] == "real"]
    assert snapshot == original
    assert reset.prune_publication(result, kind) is None
    assert reset.prune_publication({**snapshot, "sensors": [*real, *unknown]}, kind) is None
    with pytest.raises(ValueError, match="Unknown sensor type"):
        reset.prune_publication(snapshot, "unsupported")


def test_global_sensor_reset_operation_cannot_be_reused_for_social_or_family_scope():
    from fastapi import HTTPException
    state = {"operation_id": "current", "sensor_type": "all", "completed_ids": ["previous"],
             "operation_scopes": {"previous": "rainfall"}}
    reset.check_scope(state, "current", "all")
    reset.check_scope(state, "previous", "rainfall")
    for operation, kind in (("current", None), ("current", "rainfall"), ("previous", "all")):
        with pytest.raises(HTTPException) as error:
            reset.check_scope(state, operation, kind)
        assert error.value.status_code == 409


def test_history_prefetch_is_bounded_and_keeps_all_store_mutations_on_writer_thread(resetting, monkeypatch):
    _, docs, publications, lake, objects, *_ = resetting
    snapshot = resetting[-1]
    originals = [{**snapshot, "version": "gold-" + f"{index:032x}"} for index in range(8)]
    publications.clear(); lake.data["gold"].clear()
    objects.data = {reset.HISTORY_PREFIX + item["version"] + ".json": reset.encoded(item) for item in originals}
    original_keys = set(objects.data)
    writer = threading.get_ident()
    for owner, name in ((lake, "put"), (lake, "delete_publications"), (database, "publish"),
                        (database, "mutate_document"), (database, "replace_synthetic_publication"),
                        (objects, "put_object"), (objects, "delete_object")):
        operation = getattr(owner, name)
        def serial(*args, _operation=operation, **kwargs):
            assert threading.get_ident() == writer
            return _operation(*args, **kwargs)
        monkeypatch.setattr(owner, name, serial)
    get = objects.get_object
    barrier, lock = threading.Barrier(4), threading.Lock()
    calls, active, peak = [], 0, 0
    def concurrent_get(*args, **kwargs):
        nonlocal active, peak
        assert threading.get_ident() != writer
        with lock:
            calls.append(args[2]); active += 1; peak = max(peak, active)
        try:
            barrier.wait(timeout=5)
            return get(*args, **kwargs)
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(objects, "get_object", concurrent_get)
    reset.clean_history(object(), objects, lake, CONFIG, docs["checkpoint_reset"]["operation_id"])
    assert peak == 4 and set(calls) == original_keys and len(calls) == 8
    assert not original_keys.intersection(objects.data)
    assert len(docs["checkpoint_reset"]["replacements"]) == docs["checkpoint_reset"]["counts"]["history_rewritten"] == 8


def test_prefetched_history_etag_conflict_preserves_changed_object_and_durable_retry(resetting, monkeypatch):
    _, docs, publications, lake, objects, *_ = resetting
    snapshot = resetting[-1]
    key = reset.HISTORY_PREFIX + snapshot["version"] + ".json"
    publications.clear(); lake.data["gold"].clear()
    objects.data = {key: reset.encoded(snapshot)}
    get, delete = objects.get_object, objects.delete_object
    changed = {**snapshot, "repaired": True}
    def changed_after_read(*args):
        response = get(*args)
        if args[2] == key:
            objects.data[key] = reset.encoded(changed)
        return response
    def conditional_delete(namespace, bucket, name, **kwargs):
        if kwargs.get("if_match") != hashlib.sha256(objects.data[name]).hexdigest():
            raise ObjectError(412)
        return delete(namespace, bucket, name, **kwargs)
    monkeypatch.setattr(objects, "get_object", changed_after_read)
    monkeypatch.setattr(objects, "delete_object", conditional_delete)
    operation = docs["checkpoint_reset"]["operation_id"]
    with pytest.raises(ObjectError) as error:
        reset.clean_history(object(), objects, lake, CONFIG, operation)
    assert error.value.status == 412 and json.loads(objects.data[key]) == changed
    assert docs["checkpoint_reset"]["replacements"][snapshot["version"]] in publications
    assert docs["checkpoint_reset"]["counts"]["history_rewritten"] == 1
    monkeypatch.setattr(objects, "get_object", get)
    reset.clean_history(object(), objects, lake, CONFIG, operation)
    assert key not in objects.data
    assert docs["checkpoint_reset"]["counts"]["history_rewritten"] == 1


def test_retry_after_completed_history_preserves_rewrite_count(resetting, monkeypatch):
    _, docs, *_ = resetting
    mutate, fail = database.mutate_document, [True]
    def fail_completion(connection, name, change):
        if name == "checkpoint_reset" and change(copy.deepcopy(docs[name])).get("status") == "completed" and fail[0]:
            fail[0] = False
            raise RuntimeError("Completion receipt interrupted")
        return mutate(connection, name, change)
    monkeypatch.setattr(database, "mutate_document", fail_completion)
    with pytest.raises(RuntimeError, match="incomplete"):
        run_reset(resetting)
    rewritten = docs["checkpoint_reset"]["counts"]["history_rewritten"]
    assert rewritten == len(docs["checkpoint_reset"]["replacements"]) > 0
    docs["checkpoint_reset"]["status"] = "pending"
    run_reset(resetting)
    assert docs["checkpoint_reset"]["status"] == "completed"
    assert docs["checkpoint_reset"]["counts"]["history_rewritten"] == rewritten


def test_object_reset_without_sqlite_preserves_unknown_post_count_on_retry(monkeypatch):
    from app.territorial.control_store import ObjectControlStore
    from test_territorial_control_store import Objects

    store = ObjectControlStore(Objects(), "namespace", "bucket")
    database.write_document(store, "runtime", {"analytics_store": "gold", "control_new_install": True}, 0)
    operation = str(uuid4())
    database.write_document(store, "checkpoint_reset", {
        "operation_id": operation, "status": "pending", "ready": True}, 0)
    lake = SimpleNamespace(consume=lambda _: None, synthetic_ids=lambda: set(),
                           delete_synthetic=lambda: {"bronze": 0, "silver": 0}, visible=lambda *_: [])
    monkeypatch.setattr(reset, "clean_landing", lambda *_: 0)
    monkeypatch.setattr(reset, "clean_history", lambda *_: None)
    version = "gold-" + "0" * 32
    publisher = MagicMock(side_effect=[RuntimeError("Interrupted publication"), {"version": version}])
    command = database.read_document(store, "checkpoint_reset")
    with pytest.raises(RuntimeError, match="incomplete"):
        reset.execute(store, store.objects, lake, CONFIG, NOW, command, publisher)
    failed = database.read_document(store, "checkpoint_reset")
    assert failed["stage"] == "publishing" and failed["counts"]["posts"] is None
    head = store.get_json("posts/head.json")[0]
    assert store.index_path is None and head is not None
    command = database.mutate_document(store, "checkpoint_reset", lambda doc: {**doc, "status": "pending"})
    assert reset.execute(store, store.objects, lake, CONFIG, NOW, command, publisher) == {"version": version}
    completed = database.read_document(store, "checkpoint_reset")
    assert completed["status"] == "completed" and completed["counts"] == {
        "bronze": 0, "silver": 0, "posts": None, "landing_files": 0}
    assert store.get_json("posts/head.json")[0] == head
