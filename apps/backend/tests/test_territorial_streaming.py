"""Opt-in real Spark 3.5 / Delta 3.2 check; neither AIDP nor Oracle connectivity is emulated here."""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from app.territorial import capture, landing
from app.territorial.core import build_snapshot, default_source, normalize_event, utc_text
from app.territorial.pipeline import DeltaLake, consume_landing, install_post_views, start_landing


def test_delta_writes_canonical_mode_and_deletes_both_synthetic_spellings(monkeypatch):
    delta, functions, spark = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setitem(sys.modules, "delta.tables", SimpleNamespace(DeltaTable=delta))
    monkeypatch.setitem(sys.modules, "pyspark.sql", SimpleNamespace(functions=functions))
    lake = object.__new__(DeltaLake)
    lake.spark, lake.tables = spark, {"bronze": "bronze", "silver": "silver"}
    records = [{"id": "canonical", "mode": "Synthetic"}, {"id": "legacy", "mode": "simulation"}, {"id": "real", "mode": "real"}]
    lake.put("bronze", records)
    rows = spark.createDataFrame.call_args.args[0]
    assert [json.loads(payload)["mode"] for _, payload in rows] == ["Synthetic", "Synthetic", "real"]
    assert records[1]["mode"] == "simulation"
    spark.table.return_value.where.return_value.count.return_value = 2
    assert lake.delete_synthetic() == {"bronze": 2, "silver": 2}
    predicate = functions.get_json_object.return_value
    assert predicate.isin.call_args_list == [call("Synthetic", "simulation")] * 4
    assert delta.forName.return_value.delete.call_args_list == [call(predicate.isin.return_value)] * 2


@pytest.mark.parametrize("path", ["/Volumes/oci_medallion/prisma_ingest/landing", "/tmp/prisma/landing"])
def test_mounted_source_uri_matches_native_leaves_without_moving_checkpoints(path):
    readers = [MagicMock(), MagicMock()]
    for reader in readers:
        for method in ("schema", "option", "options", "format", "load", "foreachBatch", "trigger"):
            getattr(reader, method).return_value = reader
        reader.writeStream = reader
        reader.start.return_value = SimpleNamespace(id="existing-query", recentProgress=[], lastProgress={}, awaitTermination=lambda: None)
    pending = iter(readers)
    class Spark:
        @property
        def readStream(self):
            return next(pending)
    checkpoint = "/Volumes/oci_medallion/prisma_ingest/checkpoints/bronze-v1"
    consume_landing(Spark(), object(), path, checkpoint)
    for reader, suffix in zip(readers, ("", "-csv")):
        reader.load.assert_called_once_with("file:" + path if path.startswith("/Volumes/") else path)
        assert call("checkpointLocation", checkpoint + suffix) in reader.option.call_args_list
        bases = [args.args[1] for args in reader.option.call_args_list if args.args[0] == "basePath"]
        assert bases == []  # Spark derives the inner basePath from the load URI, overriding this option.


@pytest.mark.parametrize("path", ["/Volumes", "/Volumes-sibling/catalog/prisma_ingest/landing",
    "/Volumes/catalog/prisma_ingest/landing/../other", "/Volumes/catalog/prisma_ingest/landing/",
    "/Volumes/catalog/prisma_ingest/landing%2fother", "/Volumes//catalog/prisma_ingest/landing",
    "/Volumes/catalog/other/landing", "/Volumes/catalog/prisma_ingest/landing\\other"])
def test_invalid_mounted_paths_fail_before_starting_a_stream(path):
    spark = MagicMock()
    with pytest.raises(ValueError, match="governed volume path"):
        consume_landing(spark, object(), path, "/Volumes/catalog/prisma_ingest/checkpoints/bronze-v1")
    assert not any(name.endswith((".load", ".start")) for name, _args, _kwargs in spark.mock_calls)


def test_persistent_stream_starts_both_formats_without_awaiting_termination():
    readers, started = [MagicMock(), MagicMock()], []
    for index, reader in enumerate(readers):
        for method in ("schema", "option", "options", "format", "load", "foreachBatch", "trigger"):
            getattr(reader, method).return_value = reader
        reader.writeStream = reader
        reader.start.side_effect = lambda index=index: started.append(index) or MagicMock()
    pending = iter(readers)
    class Spark:
        @property
        def readStream(self):
            return next(pending)
    queries = start_landing(Spark(), object(), "/Volumes/catalog/prisma_ingest/landing",
                            "/Volumes/catalog/prisma_ingest/checkpoints/bronze-v1", persistent=True)
    assert started == [0, 1]
    for reader, (_, query) in zip(readers, queries):
        reader.trigger.assert_called_once_with(processingTime="30 seconds")
        query.awaitTermination.assert_not_called()


@pytest.mark.skipif(os.getenv("PRISMA_SPARK_INTEGRATION") != "1", reason="Requires Spark 3.5, Delta 3.2 and a supported JDK")
def test_real_stream_resumes_after_bronze_commit_without_duplicate_events(tmp_path):
    from delta import configure_spark_with_delta_pip
    from pyspark.sql import SparkSession
    builder = (SparkSession.builder.master("local[2]").appName("territorial-stream-acceptance")
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")
        .config("spark.sql.shuffle.partitions", "2").config("spark.sql.warehouse.dir", str(tmp_path / "warehouse")))
    spark = configure_spark_with_delta_pip(builder).getOrCreate()
    try:
        # Local Spark has no AIDP catalog/volume DDL. Exercise the identical streaming callback and Delta MERGE.
        lake = object.__new__(DeltaLake)
        lake.spark, lake.tables = spark, {"bronze": "territorial_stream_acceptance"}
        spark.sql(f"CREATE TABLE territorial_stream_acceptance (id STRING, payload STRING) USING DELTA LOCATION '{(tmp_path / 'bronze').as_posix()}'")
        state = {"status": "running", "run_id": "spark-run", "anchor_at": 1791209100.0, "elapsed_seconds": 0}
        first, cursor = capture.batch(default_source("x"), state, {}, 1791209100.0)
        raw, checkpoint = tmp_path / "landing", tmp_path / "checkpoints"
        raw.mkdir()
        (raw / ".keep").write_text("", encoding="utf-8")  # Empty-prefix bootstrap marker is never an event.
        # Existing deployments used JSON without a glob. Keep its query ID/checkpoint while adding CSV separately.
        old = (spark.readStream.schema("id STRING, payload STRING").json(str(raw)).writeStream
            .foreachBatch(lambda frame, _: lake.put("bronze", [landing.decode_record(row.id, row.payload) for row in frame.collect()]))
            .option("checkpointLocation", str(checkpoint)).trigger(availableNow=True).start())
        old.awaitTermination()
        progress = consume_landing(spark, lake, str(raw), str(checkpoint))
        assert progress["streams"][0]["query_id"] == str(old.id)
        assert spark.table(lake.tables["bronze"]).count() == 0
        legacy = {"id": "x:" + first[0]["source_id"], "payload": json.dumps(first[0])}
        (raw / "legacy.ndjson").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        consume_landing(spark, lake, str(raw), str(checkpoint))
        quoted = {**first[0], "source_id": "quoted", "text": 'Bogotá, "lluvia"\r\nsegunda línea\\n ☔'}
        landing.write_file(raw, first + [quoted])
        original_put = lake.put
        failed = [False]
        def commit_then_fail(layer, records):
            original_put(layer, records)
            if not failed[0]:
                failed[0] = True
                raise RuntimeError("Injected failure after durable Bronze merge")
        lake.put = commit_then_fail
        with pytest.raises(Exception):
            consume_landing(spark, lake, str(raw), str(checkpoint))
        lake.put = original_put
        progress = consume_landing(spark, lake, str(raw), str(checkpoint))
        assert progress["query_id"] and spark.table(lake.tables["bronze"]).count() == 2
        state["elapsed_seconds"] = 600
        second, _ = capture.batch(default_source("x"), state, cursor, 1791209700.0)
        landing.write_file(raw, second)
        consume_landing(spark, lake, str(raw), str(checkpoint))
        events = [normalize_event(json.loads(row.payload)) for row in spark.table(lake.tables["bronze"]).collect()]
        snapshot = build_snapshot(events, {}, "local-spark-check", "2026-10-05T14:15:00Z")
        assert len(events) == 3 and len({event["id"] for event in events}) == 3
        assert next(item["text"] for item in events if item["source_id"] == "quoted") == quoted["text"]
        assert all(item["is_simulated"] is True for item in events)
        assert {event["raw_metadata"]["producer"] for event in events} == {"vm_search"}
        assert any(incident["locality"] == "Kennedy" for incident in snapshot["incidents"])
        assert any(incident["locality"] == "Sin localizar" for incident in snapshot["incidents"])
        # Native SQL and the local reference agree at 5/10/20, inclusive window edges,
        # duplicate content, future timestamps and custom per-network rule versions.
        now = 1791209700.0
        rules = {platform: {**default_source(platform), "config_version": 7}
                 for platform in ("x", "facebook", "instagram", "tiktok")}
        samples = []
        for platform, count in (("x", 20), ("facebook", 10), ("instagram", 5), ("tiktok", 3)):
            for index in range(count):
                samples.append(normalize_event({**first[0], "platform": platform, "source_id": f"sql-{platform}-{index}",
                    "text": f"Bogotá Kennedy inundación reporte {platform} {index}",
                    "created_at": utc_text(now - (1800 if index == 0 else index))}))
        samples.extend([{**samples[0], "id": "x:duplicate", "source_id": "duplicate"},
                        {**samples[1], "id": "x:future", "source_id": "future", "created_at": utc_text(now + 1)},
                        {**samples[2], "id": "x:old", "source_id": "old", "created_at": utc_text(now - 1801)}])
        activity_snapshot = build_snapshot(samples, {}, "view-test", utc_text(now), rules=rules, now=now)
        expected = json.loads(json.dumps(activity_snapshot))
        lake.apply_activity(activity_snapshot, rules, now)
        assert activity_snapshot == expected
        assert any(item["report_counts"].get("x") == 20 for item in activity_snapshot["incidents"])
        for layer in ("bronze", "silver", "gold"):
            spark.sql(f"CREATE DATABASE IF NOT EXISTS oci_{layer}")
        for table, values in (("territorial_silver_view_fixture", samples), ("territorial_gold_view_fixture", [activity_snapshot]),
                              ("territorial_current_fixture", [])):
            rows = [(item.get("id", item.get("version")), json.dumps(item)) for item in values]
            spark.createDataFrame(rows, "id STRING,payload STRING").write.format("delta").saveAsTable(table)
        lake.tables["current"] = "territorial_current_fixture"
        assert lake.stage_snapshot(activity_snapshot) == activity_snapshot
        install_post_views(spark, "spark_catalog", {"bronze": lake.tables["bronze"],
            "silver": "territorial_silver_view_fixture", "gold": "territorial_gold_view_fixture", "current": lake.tables["current"]})
        assert spark.table("oci_bronze.social_posts_raw").count() == 3
        assert "source_hash_kind" in spark.table("oci_bronze.social_posts_raw").columns
        assert spark.table("oci_silver.social_posts").count() == len(samples)
        assert spark.table("oci_gold.events").count() == len(activity_snapshot["incidents"])
        published_event = spark.table("oci_gold.events").first().asDict()
        expected_event = next(item for item in activity_snapshot["incidents"] if item["id"] == published_event["event_id"])
        for field in ("summary", "revision", "updated_at", "rule_versions", "correlation_windows_minutes"):
            assert published_event[field] == expected_event[field]
        assert spark.table("oci_gold.event_posts").count() == len(activity_snapshot["event_posts"])
        assert spark.table("oci_silver.events").count() == len(activity_snapshot["incidents"])
        assert spark.table("oci_silver.event_posts").count() == len(activity_snapshot["event_posts"])
        for name in ("oci_silver.event_posts", "oci_gold.event_posts"):
            projected = [row.asDict() for row in spark.table(name).select("event_id", "post_key", "relation", "claim_relation", "duplicate_of").collect()]
            expected_links = [{key: item[key] for key in ("event_id", "post_key", "relation", "claim_relation", "duplicate_of")} for item in activity_snapshot["event_posts"]]
            assert sorted(projected, key=lambda item: (item["event_id"], item["post_key"])) == sorted(expected_links, key=lambda item: (item["event_id"], item["post_key"]))
        next_state = {**activity_snapshot, "version": "next-state", "incidents": [], "event_posts": []}
        assert lake.stage_snapshot(next_state) == next_state
        assert spark.table("oci_silver.events").count() == spark.table("oci_silver.event_posts").count() == 0
        assert spark.table("oci_gold.events").count() == len(activity_snapshot["incidents"])
        landing.write_file(raw, [], {"platform": "x", "window": 123})
        consume_landing(spark, lake, str(raw), str(checkpoint))
        assert spark.table(lake.tables["bronze"]).count() == 3
        queries = start_landing(spark, lake, str(raw), str(checkpoint), persistent=True)
        try:
            landing.write_file(raw, [{**first[0], "source_id": "continuous-arrival"}])
            for _, query in queries:
                query.processAllAvailable()
            assert all(query.isActive for _, query in queries)
            assert spark.table(lake.tables["bronze"]).count() == 4
        finally:
            for _, query in queries:
                query.stop()
        (raw / "invalid.csv").write_text("payload,id\nwrong,data\n", encoding="utf-8")
        with pytest.raises(Exception):
            consume_landing(spark, lake, str(raw), str(checkpoint))
        assert spark.table(lake.tables["bronze"]).count() == 4
    finally:
        spark.stop()
