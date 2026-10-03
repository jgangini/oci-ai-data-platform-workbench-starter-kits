"""Opt-in real Spark 3.5 / Delta 3.2 check; neither AIDP nor Oracle connectivity is emulated here."""
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from app.prisma import capture, landing
from app.prisma.core import build_snapshot, default_source, normalize_event
from app.prisma.pipeline import DeltaLake, consume_landing


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


@pytest.mark.skipif(os.getenv("PRISMA_SPARK_INTEGRATION") != "1", reason="Requires Spark 3.5, Delta 3.2 and a supported JDK")
def test_real_stream_resumes_after_bronze_commit_without_duplicate_events(tmp_path):
    from delta import configure_spark_with_delta_pip
    from pyspark.sql import SparkSession
    builder = (SparkSession.builder.master("local[2]").appName("prisma-stream-acceptance")
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")
        .config("spark.sql.shuffle.partitions", "2").config("spark.sql.warehouse.dir", str(tmp_path / "warehouse")))
    spark = configure_spark_with_delta_pip(builder).getOrCreate()
    try:
        # Local Spark has no AIDP catalog/volume DDL. Exercise the identical streaming callback and Delta MERGE.
        lake = object.__new__(DeltaLake)
        lake.spark, lake.tables = spark, {"bronze": "prisma_stream_acceptance"}
        spark.sql(f"CREATE TABLE prisma_stream_acceptance (id STRING, payload STRING) USING DELTA LOCATION '{(tmp_path / 'bronze').as_posix()}'")
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
        landing.write_file(raw, [], {"platform": "x", "window": 123})
        consume_landing(spark, lake, str(raw), str(checkpoint))
        assert spark.table(lake.tables["bronze"]).count() == 3
        (raw / "invalid.csv").write_text("payload,id\nwrong,data\n", encoding="utf-8")
        with pytest.raises(Exception):
            consume_landing(spark, lake, str(raw), str(checkpoint))
        assert spark.table(lake.tables["bronze"]).count() == 3
    finally:
        spark.stop()
