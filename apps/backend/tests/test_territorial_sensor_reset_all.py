"""Global sensor deletion preserves real data and the immutable legacy reset scope."""
import asyncio
import copy
import hashlib
import json
import sqlite3
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import create_app
from app.territorial import database, pipeline, sensor_capture, sensor_pipeline, sensor_reset, sensors, synthetic_reset
from app.territorial.local import LocalTerritorialRuntime
from app.territorial.core import PLATFORMS, default_source
from test_territorial_access import admin_login, local_settings
from test_territorial_sensor_reset import cloud_runtime
from test_territorial_pipeline import CONFIG, NOW, runtime
from test_territorial_synthetic_reset import resetting


@pytest.mark.parametrize("scope", ["all", "rainfall"])
def test_local_mixed_readings_keep_real_bytes_social_locations_and_config(tmp_path, scope):
    local = LocalTerritorialRuntime(tmp_path, clock=lambda: NOW)
    for kind in sensors.SENSOR_TYPES:
        sensor_capture.local_save(local.store, {"sensor_count": 1}, kind)
        sensor_capture.local_control(local.store, True, kind)
    before = local.store.snapshot()
    selected = next(row for row in before["sensors"] if row["sensor_type"] == "rainfall")
    real = {**selected, "event_id": "real-event", "sensor_id": "real-station", "mode": "real", "is_simulated": False}
    real_line = json.dumps(real, ensure_ascii=False).encode() + b"\r\n"
    mixed = next((tmp_path / "prisma-landing/sensors/rainfall").glob("*.txt"))
    mixed.write_bytes(mixed.read_bytes() + real_line)
    with local.store.connection() as db:
        db.execute("INSERT INTO sensor_events VALUES (?,?,?,?)", (real["event_id"], real["sensor_id"], real["observed_at"], json.dumps(real)))
        local.store._put(db, "sensor_locations", {selected["sensor_id"]: {"lat": 4.6, "lon": -74.1}})
    configs = asyncio.run(local.sensors())["configs"]
    result = asyncio.run(local.reset_sensors(scope, str(uuid4())))
    assert result["status"] == "completed" and result["completed_at"]
    assert result["counts"] == {"sensor_events": 5 if scope == "all" else 1, "landing_files": 5 if scope == "all" else 1}
    assert mixed.read_bytes() == real_line
    after = local.store.snapshot()
    assert any(row["event_id"] == "real-event" and row["mode"] == "real" for row in after["sensors"])
    assert len(after["sensors"]) == (1 if scope == "all" else 5)
    assert all(after[key] == before[key] for key in ("evidence", "incidents", "event_posts"))
    view = asyncio.run(local.sensors())
    assert view["reset"] == result
    for old, current in zip(configs, view["configs"]):
        assert (current["sensor_count"], current["interval_minutes"]) == (old["sensor_count"], old["interval_minutes"])
        assert current["capture_running"] == (scope != "all" and current["sensor_type"] != scope)
    with local.store.connection() as db:
        assert local.store._get(db, "sensor_locations", {})[selected["sensor_id"]] == {"lat": 4.6, "lon": -74.1}
        checkpoint = local.store._get(db, "checkpoint_sensors", {})
        assert all(checkpoint["by_type"][kind]["anchor"] >= NOW for kind in (sensors.SENSOR_TYPES if scope == "all" else (scope,)))
    assert asyncio.run(local.reset_sensors(scope, result["operation_id"])) == result
    assert mixed.read_bytes() == real_line


def test_global_api_auth_confirmation_alias_and_legacy_scope_discovery(tmp_path):
    client = TestClient(create_app(local_settings(tmp_path)))
    path = "/api/admin/territorial/sensors/reset"
    op = str(uuid4())
    assert client.get(path).status_code == 401
    assert client.post(path, json={"operation_id": op, "confirm": True}).status_code == 401
    admin_login(client)
    assert client.post(path, json={"operation_id": op, "confirm": False}).status_code == 422
    result = client.post(path, json={"operation_id": op, "confirm": True})
    assert result.status_code == 200 and result.json()["sensor_type"] == "all"
    assert client.get(path.replace("territorial", "prisma")).json() == result.json()
    assert client.get(path.removesuffix("/reset")).json()["reset"] == result.json()
    legacy = client.post(path.replace("sensors/reset", "sensors/rainfall/reset"), json={"operation_id": str(uuid4()), "confirm": True})
    assert client.get(path).json() == legacy.json()
    assert client.post(path, json={"operation_id": legacy.json()["operation_id"], "confirm": True}).status_code == 409


def test_global_cloud_requires_both_v2_heartbeats_and_pauses_all_without_config_loss(monkeypatch):
    cloud = cloud_runtime(monkeypatch)
    for kind in sensors.SENSOR_TYPES:
        sensor_capture.cloud_save(cloud, {"sensor_count": 1}, kind)
    cloud._change("configuration", lambda doc: {**doc, "sensors": sensor_capture._controlled(doc["sensors"], True, None)})
    configs = copy.deepcopy(cloud.documents["configuration"])
    op = str(uuid4())
    for updated in (None, "status_pipeline"):
        if updated:
            cloud.documents[updated]["sensor_reset_version"] = 2
        before = copy.deepcopy(cloud.documents)
        with pytest.raises(HTTPException) as error:
            asyncio.run(cloud.reset_sensors("all", op))
        assert error.value.status_code == 501 and cloud.documents == before
    cloud.documents["status_sensorstream"]["sensor_reset_version"] = 2
    result = asyncio.run(cloud.reset_sensors("all", op))
    assert result["status"] == "pending" and result["sensor_type"] == "all"
    current = cloud.documents["configuration"]
    for kind, row in current["sensors"]["by_type"].items():
        old = configs["sensors"]["by_type"][kind]
        assert row == {**old, "capture_running": False, "config_version": old["config_version"] + 1}
        for call in (cloud.control_sensors(True, kind), cloud.update_sensors({"sensor_count": 2}, kind)):
            with pytest.raises(HTTPException) as error:
                asyncio.run(call)
            assert error.value.status_code == 409
    state = cloud.documents["checkpoint_reset"]
    state.update(stage="history", replacements={"old": "new"}, revision=99, completed_at="future-receipt")
    view = asyncio.run(cloud.sensors())
    assert view["reset"] == asyncio.run(cloud.sensor_reset_status("all"))
    assert view["reset"]["replacements"] == {"old": "new"} and view["reset"]["revision"] == 99
    assert all(item["reset"]["sensor_type"] == "all" for item in view["configs"])
    assert asyncio.run(cloud.synthetic_reset_status()) == {}
    for kind in ("rainfall", "temperature"):
        with pytest.raises(HTTPException) as error:
            asyncio.run(cloud.reset_sensors(kind, op))
        assert error.value.status_code == 409


def test_global_read_returns_legacy_pending_but_never_expands_it(monkeypatch):
    cloud = cloud_runtime(monkeypatch)
    op = str(uuid4())
    result = asyncio.run(cloud.reset_sensors("rainfall", op))
    assert asyncio.run(cloud.sensor_reset_status("all")) == result
    before = copy.deepcopy(cloud.documents)
    for identifier in (op, str(uuid4())):
        with pytest.raises(HTTPException) as error:
            asyncio.run(cloud.reset_sensors("all", identifier))
        assert error.value.status_code == 409 and cloud.documents == before


def test_local_global_reset_retains_unknown_family(tmp_path):
    local = LocalTerritorialRuntime(tmp_path, clock=lambda: NOW)
    sensor_capture.local_save(local.store, {"sensor_count": 1}, "rainfall")
    sensor_capture.local_control(local.store, True, "rainfall")
    unknown = {**local.store.snapshot()["sensors"][0], "sensor_type": "future_sensor", "event_id": "future-event", "sensor_id": "future-station"}
    with local.store.connection() as db:
        db.execute("INSERT INTO sensor_events VALUES (?,?,?,?)", (unknown["event_id"], unknown["sensor_id"], unknown["observed_at"], json.dumps(unknown)))
    assert asyncio.run(local.reset_sensors("all", str(uuid4())))["status"] == "completed"
    assert [row["event_id"] for row in local.store.snapshot()["sensors"]] == ["future-event"]


def test_failed_global_pause_blocks_automatic_capture_and_retries_original_scope(tmp_path, monkeypatch):
    cloud = cloud_runtime(monkeypatch)
    for kind in sensors.SENSOR_TYPES:
        sensor_capture.cloud_save(cloud, {"sensor_count": 1}, kind)
    cloud._change("configuration", lambda doc: {**doc, "sensors": sensor_capture._controlled(doc["sensors"], True, None)})
    for name in ("status_pipeline", "status_sensorstream"):
        cloud.documents[name]["sensor_reset_version"] = 2
    change = cloud._change
    def fail_pause(name, mutation):
        if name == "configuration":
            raise RuntimeError("Unavailable")
        return change(name, mutation)
    monkeypatch.setattr(cloud, "_change", fail_pause)
    op = str(uuid4())
    assert asyncio.run(cloud.reset_sensors("all", op))["status"] == "error"
    assert cloud.documents["configuration"]["sensors"]["capture_running"]
    assert sensor_capture.cloud_tick(cloud, force=True) == 0
    monkeypatch.setattr(cloud, "_change", change)
    assert asyncio.run(cloud.reset_sensors("all", op))["status"] == "pending"
    assert not cloud.documents["configuration"]["sensors"]["capture_running"]

    local = LocalTerritorialRuntime(tmp_path, clock=lambda: NOW)
    sensor_capture.local_save(local.store, {"sensor_count": 1}, "rainfall")
    sensor_capture.local_control(local.store, True, "rainfall")
    before = local.store.snapshot()["sensors"]
    with local.store.connection() as db:
        local.store._put(db, "synthetic_reset", {"operation_id": op, "sensor_type": "all", "status": "error", "ready": False})
    assert sensor_capture.local_tick(local.store, force=True) == 0
    assert local.store.snapshot()["sensors"] == before


def test_all_anchor_guard_preserves_future_pending_and_other_metadata():
    checkpoint = {"revision": 8, "by_type": {"rainfall": {"anchor": NOW, "pending": {"anchor": NOW + 100}, "schedule_marker": "keep"}}}
    saved, status = sensor_reset._clear_controls(checkpoint, {"revision": 4}, "all", NOW)
    assert saved["revision"] == 8 and status["revision"] == 4
    assert saved["by_type"]["rainfall"]["anchor"] == NOW + 100
    assert saved["by_type"]["rainfall"]["schedule_marker"] == "keep"
    assert all(row["pending"] is None and row["next_due"] == 0 for row in saved["by_type"].values())


def test_cloud_mixed_landing_rewrites_same_path_conditionally_and_preserves_real(resetting):
    _, _, _, _, objects, *_ = resetting
    sample = sensors.generate_batch(NOW, sensor_count=1, families=["rainfall"])[0]
    real = {**sample, "event_id": "real-event", "mode": "real", "is_simulated": False}
    real_line = json.dumps(real).encode() + b"\r\n"
    mixed = CONFIG["landing_prefix"] + "sensors/rainfall/mixed.txt"
    all_real = CONFIG["landing_prefix"] + "sensors/rainfall/real.txt"
    original = json.dumps(sample).encode() + b"\n" + real_line
    objects.data.update({mixed: original, all_real: real_line})
    social = {key: value for key, value in objects.data.items() if "/sensors/" not in key}
    put = objects.put_object
    def conditional(namespace, bucket, key, body, **kwargs):
        assert key == mixed and kwargs["if_match"] == hashlib.sha256(original).hexdigest()
        return put(namespace, bucket, key, body, **kwargs)
    objects.put_object = conditional
    assert sensor_reset.clean_landing(objects, CONFIG, "all") == 1
    assert objects.data[mixed] == objects.data[all_real] == real_line
    assert sensor_reset.clean_landing(objects, CONFIG, "all") == 0
    assert all(objects.data[key] == body for key, body in social.items())
    objects.data[mixed] = json.dumps({**sample, "is_simulated": False}).encode() + b"\n"
    before = copy.deepcopy(objects.data)
    with pytest.raises(ValueError):
        sensor_reset.clean_landing(objects, CONFIG, "all")
    assert objects.data == before


def test_landing_missing_etag_or_changed_version_never_overwrites_mixed_real(resetting):
    _, _, _, _, objects, *_ = resetting
    sample = sensors.generate_batch(NOW, sensor_count=1, families=["rainfall"])[0]
    real = {**sample, "mode": "real", "is_simulated": False}
    key = CONFIG["landing_prefix"] + "sensors/rainfall/mixed.txt"
    body = (json.dumps(sample) + "\n" + json.dumps(real) + "\n").encode()
    objects.data[key] = body
    get = objects.get_object
    objects.get_object = lambda *args: SimpleNamespace(data=get(*args).data, headers={})
    with pytest.raises(ValueError, match="exact object version"):
        sensor_reset.clean_landing(objects, CONFIG, "all")
    assert objects.data[key] == body
    objects.get_object = get
    def conflict(*_, **kwargs):
        assert kwargs["if_match"] == hashlib.sha256(body).hexdigest()
        raise RuntimeError("Precondition failed")
    objects.put_object = conflict
    with pytest.raises(RuntimeError, match="Precondition"):
        sensor_reset.clean_landing(objects, CONFIG, "all")
    assert objects.data[key] == body


def test_global_native_history_retry_preserves_social_real_and_checkpoints(resetting, monkeypatch):
    _, docs, publications, lake, objects, *_ = resetting
    rows = sensors.generate_batch(NOW, sensor_count=5)
    real = {**rows[0], "event_id": "real-event", "sensor_id": "real-station", "mode": "real", "is_simulated": False}
    rows.append(real)
    def delete(kind):
        assert kind == "all"
        count = sum(row["is_simulated"] for row in rows)
        rows[:] = [row for row in rows if not row["is_simulated"]]
        return count
    lake.sensors = SimpleNamespace(latest=lambda _: copy.deepcopy(rows), delete_family=delete)
    monkeypatch.setattr(database, "replace_sensor_publication", lambda _db, _op, scope, old, new: publications.pop(old, None))
    rules = {name: {**default_source(name), **docs["configuration"]["sources"].get(name, {})} for name in PLATFORMS}
    old = pipeline.publish_snapshot(object(), objects, lake, CONFIG, list(lake.data["silver"].values()), docs["reviews"]["items"], {}, NOW, rules=rules)
    social = {name: copy.deepcopy(docs[name]) for name in ("checkpoint_x", "checkpoint_controls", "checkpoint_enrichment", "reviews", "configuration")}
    for key, body in sensors.text_files(rows[:-1]).items():
        objects.data[CONFIG["landing_prefix"] + key] = body
    op = str(uuid4())
    docs["checkpoint_reset"] = {"operation_id": op, "sensor_type": "all", "status": "pending", "ready": True,
        "reset_at": NOW, "sensor_drained_operation_id": op, "sensor_drained_revision": "revision"}
    objects.fail = "04_gold/prisma/current.json"
    with pytest.raises(RuntimeError, match="incomplete"):
        pipeline.process_reset(object(), objects, lake, {**CONFIG, "pipeline_revision": "revision"}, NOW)
    assert docs["checkpoint_reset"]["status"] == "error" and rows == [real]
    objects.fail = None
    docs["checkpoint_reset"]["status"] = "pending"
    docs["checkpoint_reset"]["counts"]["history_rewritten"] = 7
    history = synthetic_reset.clean_history
    def resume_history(*args, **kwargs):
        assert docs["checkpoint_reset"]["counts"]["history_rewritten"] == 7
        return history(*args, **kwargs)
    monkeypatch.setattr(synthetic_reset, "clean_history", resume_history)
    result = pipeline.process_reset(object(), objects, lake, {**CONFIG, "pipeline_revision": "revision"}, NOW)
    assert result["sensors"] == [real]
    assert all(docs[name] == value for name, value in social.items())
    assert result["evidence"] == old["evidence"]
    assert result["event_posts"] == old["event_posts"]
    assert [(row["id"], row["category"], row["mode"] ) for row in result["incidents"]] == [
        (row["id"], row["category"], row["mode"]) for row in old["incidents"]]
    assert docs["checkpoint_reset"]["status"] == "completed"
    assert docs["checkpoint_reset"]["counts"]["history_rewritten"] == len(docs["checkpoint_reset"]["replacements"])
    assert set(docs["checkpoint_sensors"]["by_type"]) == set(sensors.SENSOR_TYPES)
    assert all(synthetic_reset.prune_publication(item, "all") is None for item in publications.values())
    assert not any("/sensors/" in key for key in objects.data)


@pytest.mark.parametrize("kind", ["all", "rainfall"])
def test_delta_predicate_preserves_real_and_unknown_and_rejects_ambiguous_before_any_delete(monkeypatch, kind):
    # Execute the generated Spark predicate with SQL null semantics, without a Spark installation.
    class Expr(str):
        def __eq__(self, other): return Expr(f"({self} = {json.dumps(other) if isinstance(other, str) else int(other)})")
        def __and__(self, other): return Expr(f"({self} AND {other})")
        def __or__(self, other): return Expr(f"({self} OR {other})")
        def __invert__(self): return Expr(f"NOT ({self})")
        def isin(self, *values): return Expr(f"{self} IN ({','.join(json.dumps(value) for value in values)})")
    connection = sqlite3.connect(":memory:")
    functions = SimpleNamespace(col=Expr, lit=lambda value: Expr(str(int(value))), coalesce=lambda a, b: Expr(f"coalesce({a},{b})"))
    monkeypatch.setitem(sys.modules, "pyspark.sql", SimpleNamespace(functions=functions))
    class Frame:
        def __init__(self, table, predicate="1"): self.table, self.predicate = table, predicate
        def where(self, predicate): return Frame(self.table, f"({self.predicate}) AND ({predicate})")
        def limit(self, _): return self
        def count(self): return connection.execute(f"SELECT count(*) FROM {self.table} WHERE {self.predicate}").fetchone()[0]
    class Delta:
        @staticmethod
        def forName(_, table): return SimpleNamespace(delete=lambda predicate: connection.execute(f"DELETE FROM {table} WHERE {predicate}"))
    monkeypatch.setitem(sys.modules, "delta.tables", SimpleNamespace(DeltaTable=Delta))
    lake = object.__new__(sensor_pipeline.SensorLake)
    lake.table, lake.current_table, lake.legacy_table = "bronze", "current", "legacy"
    lake.spark, lake.lock = SimpleNamespace(table=Frame), nullcontext()
    rebuilt = []
    def merge(frame):
        rebuilt.append(frame)
        connection.execute(f"INSERT INTO current SELECT * FROM {frame.table} WHERE {frame.predicate} EXCEPT SELECT * FROM current")
    lake._merge_current = merge
    data = [(family, mode, simulated) for family in sensors.SENSOR_TYPES for mode, simulated in (("Synthetic", 1), ("simulation", 1), ("real", 0))]
    data.append(("future_sensor", "Synthetic", 1))
    for table in (lake.table, lake.current_table, lake.legacy_table):
        connection.execute(f"CREATE TABLE {table}(sensor_type TEXT, mode TEXT, is_simulated INTEGER)")
        connection.executemany(f"INSERT INTO {table} VALUES(?,?,?)", data)
    connection.execute("DELETE FROM current WHERE sensor_type='rainfall' AND mode='real'")  # Hidden real predecessor remains in Bronze.
    connection.execute("INSERT INTO legacy VALUES ('rainfall','Synthetic',NULL)")
    with pytest.raises(ValueError, match="provenance"):
        lake.delete_family(kind)
    assert connection.execute("SELECT count(*) FROM bronze").fetchone()[0] == len(data)
    assert not rebuilt
    connection.execute("DELETE FROM legacy WHERE is_simulated IS NULL")
    assert lake.delete_family(kind) == (10 if kind == "all" else 2)
    assert len(rebuilt) == 1 and rebuilt[0].table == "bronze"
    for table in (lake.table, lake.current_table, lake.legacy_table):
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        assert len(rows) == (6 if kind == "all" else 14)
        assert all(row in rows for row in data if row[1] == "real" or row[0] == "future_sensor")
    connection.close()
