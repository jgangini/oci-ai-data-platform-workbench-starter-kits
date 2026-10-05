"""Reset ordering and recovery with mixed real/synthetic durable stores."""
import copy
import json
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.territorial import database, landing, pipeline, synthetic_reset as reset
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

    def delete_publication(self, version):
        self.log.append("delete:gold:" + version)
        self.data["gold"].pop(version, None)


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
