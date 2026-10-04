"""Sensor TXT admission, durable publication and opt-in real Spark recovery."""
import copy
import json
import os
from datetime import date, datetime, timezone
from threading import RLock
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from app.prisma import pipeline, sensors
from app.prisma.sensor_pipeline import MAX_BATCH_RECORDS, SensorLake, decode_batch
from test_prisma_pipeline import CONFIG, NOW, runtime


def reading():
    return next(row for row in sensors.generate_batch(NOW, sensor_count=100) if row["sensor_type"] == "river_level")


def raw(record, path=None):
    return SimpleNamespace(value=json.dumps(record), source_object=path or
        f"file:/Volumes/oci_medallion/prisma_ingest/landing/sensors/{record['sensor_type']}/{record['batch_id']}.txt")


def test_txt_batch_is_validated_and_exact_retries_deduplicate_without_losing_provenance():
    record = reading()
    result = decode_batch([raw(record), raw(record)], NOW)
    assert len(result) == 1 and result[0]["event_date"] == date.fromisoformat(record["event_date"])
    assert result[0]["is_simulated"] is True and result[0]["mode"] == "Synthetic"
    assert json.loads(result[0]["payload"]) == record
    assert result[0]["source_object"].endswith(".txt") and result[0]["ingested_at"] == pipeline.utc_text(NOW)
    assert isinstance(result[0]["value"], float) and isinstance(result[0]["lat"], float)
    changed = {**record, "locality": "Other location"}
    with pytest.raises(ValueError, match="Conflicting"):
        decode_batch([raw(record), raw(changed)], NOW)


@pytest.mark.parametrize("changes", [
    {"mode": "real"}, {"is_simulated": False}, {"is_simulated": "true"}, {"country": "Other"},
    {"event_date": "1900-01-01"}, {"observed_at": "2026-10-03T01:00:00"},
    {"value": float("nan")}, {"lat": True}, {"unit": "wrong"}, {"status": "verified"},
])
def test_invalid_sensor_rows_never_enter_delta(changes):
    with pytest.raises(ValueError):
        decode_batch([raw({**reading(), **changes})], NOW)


def test_txt_malformed_wrong_family_and_oversize_batches_fail_before_commit():
    record = reading()
    with pytest.raises(ValueError, match="Landing family"):
        decode_batch([raw(record, "file:/sensors/rainfall/batch.txt")], NOW)
    with pytest.raises(ValueError):
        decode_batch([SimpleNamespace(value="not-json", source_object="file:/sensors/river_level/batch.txt")], NOW)
    with pytest.raises(ValueError, match="record"):
        decode_batch([SimpleNamespace(value="x" * 16385, source_object="")], NOW)
    with pytest.raises(ValueError, match="25000"):
        decode_batch([raw(record)] * (MAX_BATCH_RECORDS + 1), NOW)


def test_sensor_ddl_uses_existing_catalog_delta_and_only_date_partition():
    spark = MagicMock()
    lake = SensorLake(spark, CONFIG, RLock())
    ddl = [item.args[0] for item in spark.sql.call_args_list if item.args[0].startswith("CREATE TABLE")]
    assert lake.table == "oci_medallion.oci_bronze.sensor_events"
    assert lake.legacy_table == "oci_medallion.oci_silver.sensor_events"
    assert lake.current_table == "oci_medallion.oci_silver.sensors_current"
    assert len(ddl) == 3 and all("event_date DATE" in item and "is_simulated BOOLEAN" in item for item in ddl)
    assert "PARTITIONED BY (event_date)" in ddl[0] and "PARTITIONED BY (event_date)" in ddl[1]
    assert "PARTITIONED BY" not in ddl[2]
    assert "oci://gold-bucket@namespace/03_silver/prisma/sensor_events" in ddl[0]
    assert "oci://gold-bucket@namespace/02_bronze/prisma/sensor_events" in ddl[1]
    with pytest.raises(ValueError, match="catalog"):
        SensorLake(spark, {**CONFIG, "catalog": "foreign; DROP TABLE"}, RLock())


@pytest.mark.parametrize("persistent", [False, True])
def test_sensor_stream_uses_txt_recursive_families_and_a_separate_stable_checkpoint(persistent):
    reader = MagicMock()
    for method in ("format", "option", "load", "foreachBatch", "trigger"):
        getattr(reader, method).return_value = reader
    reader.writeStream = reader
    lake = object.__new__(SensorLake)
    lake.spark = SimpleNamespace(readStream=reader)
    query = lake.start("/Volumes/oci_medallion/prisma_ingest/landing/sensors",
                       "/Volumes/oci_medallion/prisma_ingest/checkpoints/sensors-v1", persistent=persistent)
    reader.format.assert_called_once_with("text")
    reader.load.assert_called_once_with("file:/Volumes/oci_medallion/prisma_ingest/landing/sensors")
    assert call("recursiveFileLookup", True) in reader.option.call_args_list
    assert call("pathGlobFilter", "*.txt") in reader.option.call_args_list
    assert call("checkpointLocation", "/Volumes/oci_medallion/prisma_ingest/checkpoints/sensors-v1") in reader.option.call_args_list
    reader.trigger.assert_called_once_with(**({"processingTime": "30 seconds"} if persistent else {"availableNow": True}))
    assert query is reader.start.return_value


def test_social_job_does_not_start_or_own_the_sensor_checkpoint(monkeypatch):
    existing = [MagicMock(), MagicMock()]
    pending = iter(existing)
    monkeypatch.setattr(pipeline, "_consume_format", lambda *_: next(pending))
    sensor = SimpleNamespace(start=MagicMock(side_effect=AssertionError("Foreign sensor checkpoint")))
    result = pipeline.start_landing(None, SimpleNamespace(sensors=sensor), "/Volumes/catalog/prisma_ingest/landing",
                                   "/Volumes/catalog/prisma_ingest/checkpoints/bronze-v1", persistent=True)
    assert [name for name, _ in result] == ["json", "csv"]
    sensor.start.assert_not_called()
    for query in existing:
        query.stop.assert_not_called()
        query.awaitTermination.assert_not_called()


def test_sensor_entrypoint_owns_only_its_heartbeat_and_stops_failed_query(monkeypatch):
    from app.prisma import sensor_pipeline, database, landing
    query = SimpleNamespace(id="sensor-query", recentProgress=[{}], lastProgress={"numInputRows": 4000},
                            isActive=True, exception=MagicMock(side_effect=[None, RuntimeError("stream failed")]), stop=MagicMock())
    lake = SimpleNamespace(start=MagicMock(return_value=query), restore_history=MagicMock())
    writes = []
    monkeypatch.setattr(database, "mutate_document", lambda _connection, name, update: writes.append((name, update({}))))
    monkeypatch.setattr(database, "read_document", lambda *_: {})
    monkeypatch.setattr(database, "sensor_reset_version", lambda _: 1)
    monkeypatch.setattr(landing, "ensure_volumes", lambda *_: None)
    monkeypatch.setattr(sensor_pipeline.time, "sleep", lambda _: None)
    config = {**CONFIG, "sensor_landing_volume_path": "/Volumes/oci_medallion/prisma_ingest/landing/sensors",
              "sensor_checkpoint_volume_path": "/Volumes/oci_medallion/prisma_ingest/checkpoints/sensors-v1", "pipeline_revision": "revision"}
    with pytest.raises(RuntimeError, match="checkpoint retained"):
        sensor_pipeline.run(None, None, config, connection=object(), lake=lake, clock=lambda: NOW)
    lake.start.assert_called_once_with(config["sensor_landing_volume_path"], config["sensor_checkpoint_volume_path"], persistent=True)
    assert [name for name, _ in writes] == ["status_sensorstream"] * 2
    assert [value["status"] for _, value in writes] == ["running", "error"]
    assert writes[0][1]["pipeline_revision"] == "revision"
    assert writes[0][1]["sensor_layers_version"] == 2
    lake.restore_history.assert_called_once_with()
    query.stop.assert_called_once()


def test_restart_with_delete_receipt_does_not_restore_history_until_reset_completes(monkeypatch):
    from app.prisma import sensor_pipeline, database, landing
    state = {"operation_id": "selected-reset", "sensor_type": "rainfall", "status": "pending", "ready": True,
             "sensor_drained_operation_id": "selected-reset", "sensor_drained_revision": "revision"}
    monkeypatch.setattr(database, "read_document", lambda *_: copy.deepcopy(state))
    monkeypatch.setattr(database, "sensor_reset_version", lambda _: 1)
    monkeypatch.setattr(database, "mutate_document", lambda _db, _name, update: update({}))
    monkeypatch.setattr(landing, "ensure_volumes", lambda *_: None)
    restored = []
    def restore():
        assert state["status"] == "completed"
        restored.append(True)
    query = MagicMock()
    query.exception.side_effect = RuntimeError("end test")
    lake = SimpleNamespace(restore_history=restore, start=MagicMock(return_value=query))
    def finish(_seconds):
        assert not restored
        lake.start.assert_not_called()
        state["status"] = "completed"
    monkeypatch.setattr(sensor_pipeline.time, "sleep", finish)
    config = {**CONFIG, "pipeline_revision": "revision", "sensor_landing_volume_path": "landing", "sensor_checkpoint_volume_path": "checkpoint"}
    with pytest.raises(RuntimeError, match="checkpoint retained"):
        sensor_pipeline.run(None, None, config, connection=object(), lake=lake, clock=lambda: NOW)
    assert restored == [True]


def test_sensors_use_same_gold_adb_version_and_pointer_remains_previous_on_failure(runtime):
    _, docs, publications, lake, objects = runtime
    item = reading()
    latest = [{**item, "id": item["event_id"]}]
    lake.sensors = SimpleNamespace(latest=lambda _now: copy.deepcopy(latest))
    first = pipeline.publish_snapshot(object(), objects, lake, CONFIG, [], {}, {}, NOW)
    assert first["sensors"] == latest and first["evidence"] == []
    assert publications[first["version"]]["sensors"] == latest
    assert lake.data["gold"][first["version"]]["sensors"] == latest
    key = f"04_gold/prisma/snapshots/{first['version']}.json"
    assert json.loads(objects.data[key])["sensors"] == latest
    pointer = objects.data["04_gold/prisma/current.json"]
    latest[0] = {**latest[0], "id": "next-event", "event_id": "next-event"}
    objects.fail = "04_gold/prisma/snapshots/"
    with pytest.raises(RuntimeError, match="Object Storage"):
        pipeline.publish_snapshot(object(), objects, lake, CONFIG, [], {}, {}, NOW + 300)
    assert objects.data["04_gold/prisma/current.json"] == pointer
    objects.fail = None
    retried = pipeline.publish_snapshot(object(), objects, lake, CONFIG, [], {}, {}, NOW + 330)
    assert retried["version"] != first["version"] and retried["sensors"] == latest
    assert retried["published_at"] == pipeline.utc_text(NOW + 300)
    assert publications[retried["version"]] == retried


@pytest.mark.skipif(os.getenv("PRISMA_SPARK_INTEGRATION") != "1", reason="Requires Spark 3.5, Delta 3.2 and a supported JDK")
def test_real_sensor_txt_stream_recovers_delta_commit_and_preserves_history(tmp_path):
    from delta import configure_spark_with_delta_pip
    from pyspark.sql import SparkSession

    builder = (SparkSession.builder.master("local[2]").appName("sensor-stream-acceptance")
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")
        .config("spark.sql.shuffle.partitions", "2").config("spark.sql.warehouse.dir", str(tmp_path / "warehouse")))
    spark = configure_spark_with_delta_pip(builder).getOrCreate()
    try:
        from app.prisma.sensor_pipeline import SENSOR_SCHEMA
        lake = object.__new__(SensorLake)
        lake.spark, lake.lock, lake.table = spark, RLock(), "sensor_stream_acceptance"
        lake.legacy_table, lake.current_table = "sensor_legacy_history", "sensor_current_state"
        for table, partition in ((lake.table, "PARTITIONED BY (event_date)"),
                                 (lake.legacy_table, "PARTITIONED BY (event_date)"), (lake.current_table, "")):
            spark.sql(f"CREATE TABLE {table} ({SENSOR_SCHEMA}) USING DELTA {partition} LOCATION '{(tmp_path / table).as_posix()}'")
        root = tmp_path / "landing"
        source, checkpoint = root / "sensors", str(tmp_path / "checkpoints" / "sensors-v1")
        source.mkdir(parents=True)
        (source / ".keep").write_bytes(b"")
        now = datetime(2026, 10, 3, 0, 0, 30, tzinfo=timezone.utc).timestamp()
        def write(at):
            for name, body in sensors.text_files(sensors.generate_batch(at)).items():
                destination = root / name
                destination.parent.mkdir(exist_ok=True)
                destination.write_bytes(body)
        write(now)
        # Upgrade an already-consumed TXT checkpoint; no files are renamed or replayed from scratch.
        legacy = object.__new__(SensorLake)
        legacy.spark, legacy.table = spark, lake.legacy_table
        put = lake.put
        lake.put = lambda records: legacy._append_history(spark.createDataFrame(records, SENSOR_SCHEMA))
        old_query = lake.start(str(source), checkpoint)
        old_query.awaitTermination()
        assert spark.table(lake.legacy_table).count() == 4000 and spark.table(lake.table).count() == 0
        lake.put = put
        lake.restore_history()
        lake.history_restored = False  # A new sensor process repeats migration without resetting its source checkpoint.
        lake.restore_history()  # Retry has no duplicate measurements and retains the old Delta table.
        assert spark.table(lake.table).count() == spark.table(lake.current_table).count() == 4000
        assert spark.table(lake.legacy_table).count() == 4000
        write(now + 300)
        merge, failed = lake._merge_current, [False]
        def commit_then_fail(frame):
            if not failed[0]:
                failed[0] = True
                raise RuntimeError("Injected failure between sensor Bronze and Silver")
            merge(frame)
        lake._merge_current = commit_then_fail
        first = lake.start(str(source), checkpoint)
        with pytest.raises(Exception):
            first.awaitTermination()
        assert spark.table(lake.table).count() == 8000
        assert {row["batch_id"] for row in lake.latest(now + 300)} == {f"sim-{int(now)}-0"}
        lake._merge_current = merge
        resumed = lake.start(str(source), checkpoint)
        resumed.awaitTermination()
        assert resumed.id == first.id == old_query.id
        assert spark.table(lake.table).count() == 8000
        assert spark.sql(f"DESCRIBE DETAIL {lake.table}").first().partitionColumns == ["event_date"]
        assert spark.table(lake.table).select("event_date").distinct().count() == 2
        write(now + 300)
        lake.start(str(source), checkpoint).awaitTermination()
        latest = lake.latest(now + 300)
        assert len(latest) == 4000 and all(row["is_simulated"] is True for row in latest)
        assert len({row["sensor_id"] for row in latest}) == 4000
        assert {row["batch_id"] for row in latest} == {f"sim-{int(now + 300)}-0"}
        write(now - 300)  # Late historical readings are stored without replacing newer observations.
        lake.start(str(source), checkpoint).awaitTermination()
        assert spark.table(lake.table).count() == 12000
        assert lake.latest(now + 300) == latest
        assert lake.latest(now + 86400 * 3) == latest  # Paused families retain their last observation.
        assert lake.latest(now) == latest  # A social-reset/tick start timestamp cannot hide a newer current reading.
        # Changing configured families/station count can leave more than 5000 historical identities.
        added = [{**row, "sensor_id": "new-" + row["sensor_id"]}
                 for row in sensors.generate_batch(now + 600, sensor_count=2000)]
        lake.put(decode_batch([raw(row) for row in added], now + 600))
        bounded = lake.latest(now + 600)
        assert len(bounded) == 5000 and sum(row["sensor_id"].startswith("new-") for row in bounded) == 2000
        assert [row["sensor_id"] for row in bounded] == sorted(row["sensor_id"] for row in bounded)
        assert spark.table(lake.table).count() == 14000
        # Existing Delta may mix whole-second Z strings with fractional UTC observations.
        legacy = {**sensors.generate_batch(now + 900, sensor_count=100)[0],
                  "event_id": "legacy-second", "sensor_id": "mixed-time-station", "observed_at": pipeline.utc_text(now + 900)}
        admitted = decode_batch([raw(legacy)], now + 900)[0]
        admitted.update(observed_at=legacy["observed_at"], payload=json.dumps(legacy))
        lake.put([admitted])
        lake.put(decode_batch([raw(legacy)], now + 900))  # Same legacy JSON/time representation normalizes on replay.
        assert any(row["id"] == "legacy-second" for row in lake.latest(now + 900.625))
        fractional = {**legacy, "event_id": "fractional-second", "observed_at": pipeline.utc_text(now + 900.25)}
        lake.put(decode_batch([raw(fractional)], now + 900.25))
        assert next(row for row in lake.latest(now + 900.625) if row["sensor_id"] == "mixed-time-station")["id"] == "fractional-second"
        # Social CSV/legacy JSON share the Landing root without ingesting nested sensor TXT.
        from app.prisma import landing
        social = object.__new__(pipeline.DeltaLake)
        social.spark, social.ingest_lock, social.sensors = spark, lake.lock, lake
        social.tables = {"bronze": "social_sensor_coexistence"}
        spark.sql(f"CREATE TABLE social_sensor_coexistence (id STRING,payload STRING) USING DELTA LOCATION '{(tmp_path / 'social').as_posix()}'")
        legacy = {"platform": "x", "source_id": "legacy-social", "mode": "Synthetic", "text": "Bogotá test"}
        (root / "legacy.ndjson").write_text(json.dumps({"id": "x:legacy-social", "payload": json.dumps(legacy)}) + "\n", encoding="utf-8")
        landing.write_file(root, [{**legacy, "source_id": "csv-social"}])
        progress = pipeline.consume_landing(spark, social, str(root), str(tmp_path / "checkpoints" / "bronze-v1"))
        assert {item["format"] for item in progress["streams"]} == {"json", "csv"}
        assert spark.table(social.tables["bronze"]).count() == 2
        assert spark.table(lake.table).count() == 14002
        # Delete clears legacy history too; a subsequent migration/restart cannot resurrect the family.
        expected = spark.table(lake.table).where("sensor_type = 'rainfall'").count()
        assert lake.delete_family("rainfall") == expected
        lake.history_restored = False
        lake.restore_history()
        for table in (lake.table, lake.current_table, lake.legacy_table):
            assert spark.table(table).where("sensor_type = 'rainfall'").count() == 0
            assert spark.table(table).where("sensor_type = 'temperature'").count() > 0
    finally:
        spark.stop()
