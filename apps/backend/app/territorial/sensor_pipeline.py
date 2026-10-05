"""Sensor TXT → immutable Bronze history → latest Silver state, with stable checkpoints."""
from __future__ import annotations

import json
import re
import time
from datetime import date

from .core import utc_text


MAX_BATCH_RECORDS = 25000
MAX_VISIBLE_SENSORS = 5000
SENSOR_SCHEMA = """event_id STRING, sensor_id STRING, sensor_type STRING, observed_at STRING,
    event_date DATE, lat DOUBLE, lon DOUBLE, locality STRING, municipality STRING,
    department STRING, country STRING, metric STRING, value DOUBLE, unit STRING,
    status STRING, mode STRING, is_simulated BOOLEAN, batch_id STRING,
    source_object STRING, ingested_at STRING, payload STRING"""


def decode_batch(rows, now):
    """One bounded microbatch; malformed or conflicting events never advance its checkpoint."""
    from .sensors import validate_record

    if len(rows) > MAX_BATCH_RECORDS:
        raise ValueError("Sensor microbatch exceeds its 25000-record limit")
    records = {}
    for row in rows:
        if not isinstance(row.value, str) or len(row.value) > 16384:
            raise ValueError("Invalid sensor TXT record")
        record = validate_record(json.loads(row.value))
        record.update({key: float(record[key]) for key in ("lat", "lon", "value")})
        location = re.search(r"/sensors/([a-z_]+)/[^/]+\.txt$", row.source_object)
        if not location or location[1] != record["sensor_type"]:
            raise ValueError("Sensor record does not match its Landing family")
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        previous = records.get(record["event_id"])
        if previous and previous["payload"] != payload:
            raise ValueError("Conflicting sensor event identity")
        records.setdefault(record["event_id"], {**record, "event_date": date.fromisoformat(record["event_date"]),
            "source_object": row.source_object, "ingested_at": utc_text(now), "payload": payload})
    return list(records.values())


class SensorLake:
    def __init__(self, spark, config, lock):
        self.spark, self.lock = spark, lock
        catalog = config.get("catalog", "oci_medallion")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
            raise ValueError("Invalid sensor catalog")
        self.table = f"{catalog}.oci_bronze.sensor_events"
        self.current_table = f"{catalog}.oci_silver.sensors_current"
        self.legacy_table = f"{catalog}.oci_silver.sensor_events"
        for table, prefix, name, partition in (
            (self.legacy_table, "03_silver", "sensor_events", "PARTITIONED BY (event_date)"),
            (self.table, "02_bronze", "sensor_events", "PARTITIONED BY (event_date)"),
            (self.current_table, "03_silver", "sensors_current", ""),
        ):
            uri = f"oci://{config['bucket']}@{config['namespace']}/{prefix}/prisma/{name}"
            if "'" in uri:
                raise ValueError("Invalid sensor storage location")
            spark.sql(f"CREATE SCHEMA IF NOT EXISTS {table.rsplit('.', 1)[0]}")
            spark.sql(f"CREATE TABLE IF NOT EXISTS {table} ({SENSOR_SCHEMA}) USING DELTA {partition} LOCATION '{uri}'")

    def _append_history(self, frame):
        from delta.tables import DeltaTable
        from pyspark.sql import functions as F
        existing = self.spark.table(self.table).select("event_id", F.col("payload").alias("existing_payload"))
        parsed = [F.from_json(F.col(column), SENSOR_SCHEMA).withField("observed_at",
            F.to_timestamp(F.get_json_object(F.col(column), "$.observed_at"))) for column in ("payload", "existing_payload")]
        if frame.join(existing, "event_id").where(~parsed[0].eqNullSafe(parsed[1])).limit(1).count():
            raise ValueError("Previously ingested sensor event changed")
        (DeltaTable.forName(self.spark, self.table).alias("target")
         .merge(frame.alias("source"), "target.event_id = source.event_id").whenNotMatchedInsertAll().execute())

    def _merge_current(self, frame):
        from delta.tables import DeltaTable
        from pyspark.sql import Window, functions as F
        newest = Window.partitionBy("sensor_id").orderBy(F.to_timestamp("observed_at").desc(), F.col("event_id").desc())
        frame = frame.withColumn("latest_rank", F.row_number().over(newest)).where(F.col("latest_rank") == 1).drop("latest_rank")
        later = """to_timestamp(source.observed_at) > to_timestamp(target.observed_at) OR
            (to_timestamp(source.observed_at) = to_timestamp(target.observed_at) AND source.event_id > target.event_id)"""
        (DeltaTable.forName(self.spark, self.current_table).alias("target")
         .merge(frame.alias("source"), "target.sensor_id = source.sensor_id")
         .whenMatchedUpdateAll(condition=later).whenNotMatchedInsertAll().execute())

    def restore_history(self):
        """Only the sensor workflow calls this before reusing its existing file checkpoint."""
        if getattr(self, "history_restored", False):
            return
        with self.lock:
            # ponytail: restart scans Delta history, not Landing; a migration watermark can replace this if history grows costly.
            self._append_history(self.spark.table(self.legacy_table))
            self._merge_current(self.spark.table(self.table))
            self.history_restored = True

    def put(self, records):
        if not records:
            return
        frame = self.spark.createDataFrame(records, SENSOR_SCHEMA)
        with self.lock:
            self._append_history(frame)
            self._merge_current(frame)

    def latest(self, now):
        from pyspark.sql import functions as F

        # Current state is not an as-of query: a paused sensor remains visible with its last observed_at.
        frame = self.spark.table(self.current_table).withColumn("observed_time", F.to_timestamp("observed_at"))
        rows = (frame.orderBy(F.col("observed_time").desc(), "sensor_id").limit(MAX_VISIBLE_SENSORS)
                .orderBy("sensor_id").select("event_id", "payload", "source_object", "ingested_at").collect())
        return [{**json.loads(row.payload), "id": row.event_id, "source_object": row.source_object,
                 "ingested_at": row.ingested_at} for row in rows]

    def delete_family(self, kind):
        from delta.tables import DeltaTable
        from pyspark.sql import functions as F
        from .sensors import SENSOR_TYPES
        if kind != "all" and kind not in SENSOR_TYPES:
            raise ValueError("Unknown sensor type")
        selected = F.col("sensor_type").isin(*SENSOR_TYPES) if kind == "all" else F.col("sensor_type") == kind
        synthetic = F.col("mode").isin("Synthetic", "simulation") & (F.col("is_simulated") == True)
        real = (F.col("mode") == "real") & (F.col("is_simulated") == False)
        with self.lock:
            tables = (self.table, self.current_table, self.legacy_table)
            for table in tables:
                frame = self.spark.table(table).where(selected)
                if frame.where(~F.coalesce(synthetic | real, F.lit(False))).limit(1).count():
                    raise ValueError("Sensor deletion requires unambiguous provenance")
            count = self.spark.table(self.table).where(selected & synthetic).count()
            for table in tables:
                DeltaTable.forName(self.spark, table).delete(selected & synthetic)
            # A removed synthetic latest row may have hidden an earlier real reading of the same station.
            self._merge_current(self.spark.table(self.table).where(selected))
            return count

    def start(self, path, checkpoint, *, persistent=False):
        if path.startswith("oci:") or checkpoint.startswith("oci:"):
            raise ValueError("Sensors require governed streaming volumes")
        if path.startswith("/Volumes"):
            if not re.fullmatch(r"/Volumes/[A-Za-z_][A-Za-z0-9_]*/prisma_ingest/landing/sensors", path):
                raise ValueError("Invalid sensor governed volume path")
            path = "file:" + path

        def commit(frame, _batch_id):
            from pyspark.sql import functions as F
            # ponytail: 25000 rows per microbatch bounds driver memory; larger batches need distributed validation.
            rows = frame.withColumn("source_object", F.input_file_name()).limit(MAX_BATCH_RECORDS + 1).collect()
            self.put(decode_batch(rows, time.time()))

        stream = (self.spark.readStream.format("text").option("maxFilesPerTrigger", 5)
            .option("recursiveFileLookup", True).option("pathGlobFilter", "*.txt").load(path))
        trigger = {"processingTime": "30 seconds"} if persistent else {"availableNow": True}
        return stream.writeStream.foreachBatch(commit).option("checkpointLocation", checkpoint).trigger(**trigger).start()


def reset_barrier(connection, lake, config, query):
    """Only the sensor workflow may drain its checkpoint; the publisher waits for this receipt."""
    from .database import read_document, mutate_document
    command = read_document(connection, "checkpoint_reset")
    if not command.get("sensor_type") or command.get("status") not in {"pending", "error"} or not command.get("ready"):
        return query, False
    if query is not None:
        query.stop()
        query.awaitTermination()
    if command["status"] == "error":
        return None, True
    operation = command["operation_id"]
    if command.get("sensor_drained_operation_id") != operation or command.get("sensor_drained_revision") != config["pipeline_revision"]:
        # Migration must finish before issuing the receipt that permits the publisher to delete history.
        lake.restore_history()
        drain = lake.start(config["sensor_landing_volume_path"], config["sensor_checkpoint_volume_path"])
        try:
            if drain.awaitTermination(600) is False or drain.exception():
                raise RuntimeError("Sensor checkpoint could not be drained")
        finally:
            drain.stop()
            drain.awaitTermination()
        mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "sensor_drained_operation_id": operation,
            "sensor_drained_revision": config["pipeline_revision"], "stage": "waiting_for_publication"}
            if doc.get("operation_id") == operation and doc.get("status") == "pending" else doc)
    return None, True


def run(spark, secret_get, config, *, clock=time.time, connection=None, lake=None):
    """Independent permanent TXT workflow: only Sensor Delta and its own heartbeat are writable."""
    from contextlib import ExitStack
    from threading import RLock
    from .database import mutate_document, sensor_reset_version
    from .landing import ensure_volumes, stream_progress
    from .runtime_secrets import database_connection

    ensure_volumes(spark, config)
    with ExitStack() as stack:
        connection = connection or stack.enter_context(database_connection(secret_get, "PrismaWriterRuntime"))
        if sensor_reset_version(connection) != 2:
            raise RuntimeError("Sensor reset database contract is not installed")
        lake = lake or SensorLake(spark, config, RLock())
        query, restored = None, False
        try:
            while True:
                query, held = reset_barrier(connection, lake, config, query)
                if not held and not restored:
                    lake.restore_history()
                    restored = True
                if query is None and not held:
                    query = lake.start(config["sensor_landing_volume_path"], config["sensor_checkpoint_volume_path"], persistent=True)
                if query is not None and (query.exception() or not query.isActive):
                    raise RuntimeError("The persistent sensor stream stopped")
                mutate_document(connection, "status_sensorstream", lambda current: {**current,
                    "status": "resetting" if held else "running", "pipeline_revision": config["pipeline_revision"], "sensor_reset_version": 2,
                    "sensor_layers_version": 2, "last_error": None,
                    "last_run_at": utc_text(clock()), "stream": stream_progress([("sensor_txt", query)]) if query is not None else {}})
                time.sleep(10)
        except Exception as exc:
            from .database import read_document
            command = read_document(connection, "checkpoint_reset")
            if command.get("sensor_type") and command.get("status") == "pending":
                mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "status": "error", "error": type(exc).__name__}
                    if doc.get("operation_id") == command["operation_id"] and doc.get("status") == "pending" else doc)
            mutate_document(connection, "status_sensorstream", lambda current: {**current,
                "status": "error", "last_error": type(exc).__name__, "last_run_at": utc_text(clock())})
            raise RuntimeError("Sensor ingestion failed; Delta history and checkpoint retained") from None
        finally:
            if query is not None:
                query.stop()
