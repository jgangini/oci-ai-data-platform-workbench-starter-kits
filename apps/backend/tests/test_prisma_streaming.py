"""Opt-in real Spark 3.5 / Delta 3.2 check; neither AIDP nor Oracle connectivity is emulated here."""
import json
import os

import pytest

from app.prisma import capture, landing
from app.prisma.core import build_snapshot, default_source, normalize_event
from app.prisma.pipeline import DeltaLake, consume_landing


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
        consume_landing(spark, lake, str(raw), str(checkpoint))
        assert spark.table(lake.tables["bronze"]).count() == 0
        landing.write_file(raw, first)
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
        assert progress["query_id"] and spark.table(lake.tables["bronze"]).count() == 1
        state["elapsed_seconds"] = 600
        second, _ = capture.batch(default_source("x"), state, cursor, 1791209700.0)
        landing.write_file(raw, second)
        consume_landing(spark, lake, str(raw), str(checkpoint))
        events = [normalize_event(json.loads(row.payload)) for row in spark.table(lake.tables["bronze"]).collect()]
        snapshot = build_snapshot(events, {}, "local-spark-check", "2026-10-05T14:15:00Z")
        assert len(events) == 2 and len({event["id"] for event in events}) == 2
        assert {event["raw_metadata"]["producer"] for event in events} == {"vm_search"}
        assert any(incident["locality"] == "Kennedy" for incident in snapshot["incidents"])
        assert any(incident["locality"] == "Sin localizar" for incident in snapshot["incidents"])
    finally:
        spark.stop()
