"""God's Eye View: readable native sensor_stream processing. No project imports."""
from __future__ import annotations

# Deployment fills only this configuration block; secret values remain in AIDP.
RUNTIME_CONFIG = {}


# ---- core ----

import hashlib

import json

import math

import re

import unicodedata

from datetime import datetime, timedelta, timezone



def utc_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


# ---- sensors ----

import hashlib

import json

import math

import random

import re

from datetime import datetime, timezone


# These are simulation thresholds, not official disaster warning thresholds.
SENSOR_TYPES = {
    "river_level": {"unit": "m", "minimum": 0, "maximum": 20, "warning": 3, "critical": 5},
    "rainfall": {"unit": "mm/h", "minimum": 0, "maximum": 250, "warning": 15, "critical": 35},
    "temperature": {"unit": "°C", "minimum": -20, "maximum": 60, "warning": 35, "critical": 40},
    "soil_moisture": {"unit": "%", "minimum": 0, "maximum": 100, "warning": 75, "critical": 90},
    "wind_speed": {"unit": "km/h", "minimum": 0, "maximum": 300, "warning": 40, "critical": 65},
}



def validate_record(record):
    if not isinstance(record, dict) or record.get("is_simulated") is not True or record.get("mode") != "Synthetic" or record.get("country") != "Colombia":
        raise ValueError("Sensor readings must be explicitly simulated in Colombia")
    kind = record.get("sensor_type")
    rules = SENSOR_TYPES.get(kind) if isinstance(kind, str) else None
    if not rules or record.get("metric") != kind or record.get("unit") != rules["unit"]:
        raise ValueError("Invalid sensor metric or unit")
    for key in ("event_id", "sensor_id", "batch_id"):
        if not isinstance(record.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", record[key]):
            raise ValueError("Invalid sensor identifier")
    for key in ("locality", "municipality", "department"):
        if not isinstance(record.get(key), str) or not 1 <= len(record[key]) <= 100:
            raise ValueError("Invalid sensor location")
    for key, low, high in (("lat", -4.3, 13.6), ("lon", -81.8, -66.7), ("value", rules["minimum"], rules["maximum"])):
        value = record.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("Invalid sensor coordinate or measurement")
    stamp = datetime.fromisoformat(str(record.get("observed_at", "")).replace("Z", "+00:00"))
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0 or record.get("event_date") != stamp.date().isoformat():
        raise ValueError("Sensor time must be UTC with a matching event_date")
    expected = "critical" if record["value"] >= rules["critical"] else "warning" if record["value"] >= rules["warning"] else "normal"
    if record.get("status") != expected:
        raise ValueError("Sensor status does not match its simulation threshold")
    fields = ("event_id", "sensor_id", "sensor_type", "observed_at", "event_date", "lat", "lon", "locality", "municipality", "department", "country", "metric", "value", "unit", "status", "mode", "is_simulated", "batch_id")
    return {**{name: record[name] for name in fields}, "observed_at": stamp.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            **{name: float(record[name]) for name in ("lat", "lon", "value")}}


# ---- control store ----
import json

import re

from concurrent.futures import ThreadPoolExecutor


DOCUMENT_NAME = re.compile(r"(?:configuration|simulation|reviews|runtime|event_registry|status_[a-z]+|checkpoint_[a-z]+)")



class ControlConflict(RuntimeError):
    status = 409



class ObjectControlStore:
    def __init__(self, objects, namespace, bucket, prefix=".control/gods_eye_view/", index_path=None):
        if (not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", namespace)
                or not isinstance(bucket, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", bucket)
                or not isinstance(prefix, str) or not prefix.startswith(".control/gods_eye_view/")
                or not prefix.endswith("/") or any(part in {"", ".", ".."} for part in prefix[:-1].split("/"))
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", prefix)):
            raise ValueError("Invalid Object Storage control scope")
        self.objects, self.namespace, self.bucket = objects, namespace, bucket
        self.prefix, self.index_path = prefix, index_path

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def commit(self):
        pass  # Each conditional object write is already durable.

    def rollback(self):
        pass  # Object writes cannot be rolled back as a SQL transaction.

    def _key(self, key):
        if (not isinstance(key, str) or len(key) > 500 or not re.fullmatch(r"[A-Za-z0-9_./-]+", key)
                or any(part in {"", ".", ".."} for part in key.split("/"))):
            raise ValueError("Invalid control object key")
        return self.prefix + key

    def get_json(self, key):
        try:
            response = self.objects.get_object(self.namespace, self.bucket, self._key(key))
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None, None
            raise
        value = json.loads(response.data.content)
        etag = response.headers.get("etag") or response.headers.get("ETag")
        if not isinstance(value, dict) or not isinstance(etag, str) or not etag:
            raise ValueError("Invalid control object or missing ETag")
        return value, etag

    def put_json(self, key, value, expected_etag=None, create=False):
        target = self._key(key)
        if (not isinstance(value, dict) or type(create) is not bool
                or create and expected_etag is not None
                or not create and (not isinstance(expected_etag, str) or not expected_etag)):
            raise ValueError("A control write requires exactly one precondition")
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        try:
            response = self.objects.put_object(self.namespace, self.bucket, target, body,
                content_type="application/json", **({"if_none_match": "*"} if create else {"if_match": expected_etag}))
        except Exception as exc:
            if getattr(exc, "status", None) == 412:
                raise ControlConflict("Control revision changed; reload before retrying") from None
            raise
        etag = response.headers.get("etag") or response.headers.get("ETag")
        if not isinstance(etag, str) or not etag:
            raise RuntimeError("Control write returned no ETag; reread before retrying")
        return etag

    def require_ready(self):
        runtime = read_document(self, "runtime")
        if (not runtime or runtime.get("analytics_store") != "gold"
                or not (runtime.get("control_migration_complete") is True or runtime.get("control_new_install") is True)):
            raise RuntimeError("Object control migration has not been confirmed")

    def assert_reset(self, operation_id, sensor_type=None):
        self.require_ready()
        state = read_document(self, "checkpoint_reset")
        if (not operation_id or not state or state.get("operation_id") != operation_id
                or state.get("sensor_type") != sensor_type or state.get("status") != "pending" or state.get("ready") is not True
                or operation_id in state.get("cancelled_ids", []) or operation_id in state.get("cancelled_operations", {})):
            raise ControlConflict("Publication cleanup is no longer the active reset")
        return state



def _valid_name(name):
    if not isinstance(name, str) or len(name) > 100 or not DOCUMENT_NAME.fullmatch(name):
        raise ValueError("Invalid Gods Eye View document name")



def read_document(connection, name: str) -> dict:
    _valid_name(name)
    document, _ = connection.get_json("docs/" + name + ".json")
    if document is None:
        return {"revision": 0}
    if type(document.get("revision")) is not int or document["revision"] < 0:
        raise ValueError("Invalid Gods Eye View document revision")
    return document



def write_document(connection, name: str, data: dict, expected: int) -> dict:
    _valid_name(name)
    if not isinstance(data, dict) or type(expected) is not int or expected < 0:
        raise ValueError("Invalid Gods Eye View document revision")
    current, etag = connection.get_json("docs/" + name + ".json")
    revision = current.get("revision") if current is not None else 0
    if type(revision) is not int or revision < 0:
        raise ValueError("Invalid Gods Eye View document revision")
    if revision != expected:
        raise ControlConflict("Control revision changed; reload before retrying")
    document = {**data, "revision": expected + 1}
    connection.put_json("docs/" + name + ".json", document, expected_etag=etag, create=current is None)
    return document



def mutate_document(connection, name: str, change) -> dict:
    current = read_document(connection, name)
    return write_document(connection, name, change(dict(current)), current["revision"])



def reset_version(connection):
    connection.require_ready()
    return 3



def sensor_reset_version(connection):
    return reset_version(connection)


# ---- landing ----
import csv

import hashlib

import io

import json

import os

import re

from pathlib import Path

from tempfile import NamedTemporaryFile



def ensure_volumes(spark, config):
    """Verify bootstrap-provisioned volumes before any streaming or table writes."""
    catalog = config.get("catalog", "oci_medallion")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
        raise ValueError("Invalid Gods Eye View catalog")
    # Compatibility: keep the installed volumes and streaming checkpoint identity.
    schema = catalog + ".prisma_ingest"
    uri = f"oci://{config['landing_bucket']}@{config['namespace']}/{config['landing_prefix']}"
    if "'" in uri or config["landing_volume_path"] != f"/Volumes/{catalog}/prisma_ingest/landing" or config["checkpoint_volume_path"] != f"/Volumes/{catalog}/prisma_ingest/checkpoints/bronze-v1":
        raise ValueError("Invalid Gods Eye View governed streaming path")
    for name, kind in (("landing", "EXTERNAL"), ("checkpoints", "MANAGED")):
        rows = spark.sql(f"DESCRIBE VOLUME {schema}.{name}").collect()
        if len(rows) != 1:
            raise RuntimeError("Gods Eye View volume description must contain exactly one row")
        details = rows[0].asDict()
        if any(details.get(field) != value for field, value in {"name": name, "catalog": catalog, "database": "prisma_ingest"}.items()):
            raise RuntimeError("Gods Eye View volume identity does not match the deployment")
        if str(details.get("volumeType", "")).upper() != kind or (kind == "EXTERNAL" and str(details.get("storageLocation", "")).rstrip("/") != uri.rstrip("/")):
            raise RuntimeError("Gods Eye View volume type or storage location does not match the deployment")



def stream_progress(queries):
    streams = [{"format": name, "query_id": str(query.id), "microbatches": len(query.recentProgress),
                "last_input_rows": (query.lastProgress or {}).get("numInputRows", 0)} for name, query in queries]
    return {"query_id": streams[-1]["query_id"], "streams": streams,
            "microbatches": sum(item["microbatches"] for item in streams),
            "last_input_rows": sum(item["last_input_rows"] for item in streams)}


# ---- runtime secrets ----
import base64

import hashlib

import io

import json

import tempfile

import zipfile

from contextlib import contextmanager

from pathlib import Path


SHARED_OCI_CREDENTIAL_NAME = "AidpRuntime"

OCI_CREDENTIALS = (SHARED_OCI_CREDENTIAL_NAME, "AidpDataGovernanceExtension")



def identity_hash(config):
    identity = [config.get(key) for key in ("tenancy", "user", "fingerprint")]
    if any(not isinstance(value, str) or not value for value in identity):
        raise RuntimeError("OCI runtime identity incomplete")
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()



def values(secret_get, name, keys):
    result = {key: secret_get(name=name, key=key) for key in keys}
    if any(not isinstance(value, str) or not value for value in result.values()):
        raise RuntimeError("Gods Eye View runtime credential incomplete")
    return result



def _oci_values(secret_get, credential_name, expected_identity):
    if credential_name not in OCI_CREDENTIALS:
        raise RuntimeError("Unsupported OCI runtime credential")
    config = values(secret_get, credential_name, ("tenancy", "user", "fingerprint"))
    if expected_identity and identity_hash(config) != expected_identity:
        raise RuntimeError("OCI runtime credential identity mismatch")
    config.update(values(secret_get, credential_name, ("private_key",)))
    return config



def _signer(config):
    import oci
    return oci.signer.Signer(tenancy=config["tenancy"], user=config["user"], fingerprint=config["fingerprint"],
                            private_key_file_location=None, private_key_content=config["private_key"])



def runtime_auth(secret_get, region, credential_name=SHARED_OCI_CREDENTIAL_NAME, expected_identity=""):
    """Ordinary OCI Signer clients still validate a complete SDK config; keep it only in memory."""
    credential = _oci_values(secret_get, credential_name, expected_identity)
    config = {key: credential[key] for key in ("tenancy", "user", "fingerprint")}
    config.update(region=region, key_content=credential["private_key"])
    return config, _signer(credential)


# ---- sensor pipeline ----

import json

import re

import time

from datetime import date



MAX_BATCH_RECORDS = 25000

MAX_VISIBLE_SENSORS = 5000

SENSOR_SCHEMA = """event_id STRING, sensor_id STRING, sensor_type STRING, observed_at STRING,
    event_date DATE, lat DOUBLE, lon DOUBLE, locality STRING, municipality STRING,
    department STRING, country STRING, metric STRING, value DOUBLE, unit STRING,
    status STRING, mode STRING, is_simulated BOOLEAN, batch_id STRING,
    source_object STRING, ingested_at STRING, payload STRING"""



def decode_batch(rows, now):
    """One bounded microbatch; malformed or conflicting events never advance its checkpoint."""

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
            print(json.dumps({"workflow": "sensor_stream", "stage": "silver", "batch_id": _batch_id,
                              "rows": len(rows), "status": "committed"}), flush=True)

        stream = (self.spark.readStream.format("text").option("maxFilesPerTrigger", 5)
            .option("recursiveFileLookup", True).option("pathGlobFilter", "*.txt").load(path))
        trigger = {"processingTime": "30 seconds"} if persistent else {"availableNow": True}
        return stream.writeStream.foreachBatch(commit).option("checkpointLocation", checkpoint).trigger(**trigger).start()



def reset_barrier(connection, lake, config, query):
    """Only the sensor workflow may drain its checkpoint; the publisher waits for this receipt."""
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



def run_sensor_stream(spark, secret_get, config, *, clock=time.time, connection=None, lake=None, objects=None):
    """Independent permanent TXT workflow: only Sensor Delta and its own heartbeat are writable."""
    from contextlib import ExitStack
    from threading import RLock

    with ExitStack() as stack:
        if connection is None:
            if objects is None:
                import oci
                sdk_config, signed = runtime_auth(secret_get, config["region"], config.get("oci_credential_name", "AidpRuntime"),
                    config.get("oci_identity_sha256", ""))
                objects = oci.object_storage.ObjectStorageClient(sdk_config, signer=signed)
            connection = stack.enter_context(ObjectControlStore(objects, config["namespace"], config["bucket"]))
        if isinstance(connection, ObjectControlStore):
            connection.require_ready()
        if sensor_reset_version(connection) < (3 if config.get("analytics_store") == "gold" else 2):
            raise RuntimeError("Sensor reset control contract is not installed")
        ensure_volumes(spark, config)
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
            print(json.dumps({"workflow": "sensor_stream", "status": "stopped"}), flush=True)


def main():
    import argparse
    from pyspark.sql import SparkSession

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-runtime", action="store_true")
    args = parser.parse_args()
    session = SparkSession.builder.getOrCreate()
    # AIDP Python tasks inject aidputils; it is not an importable Spark package.
    secret_get = aidputils.secrets.get
    if not callable(secret_get):
        raise RuntimeError("Native AIDP secret access is unavailable")
    print(json.dumps({"workflow": "sensor_stream", "stage": "runtime", "status": "ready",
                      "revision": RUNTIME_CONFIG["pipeline_revision"]}), flush=True)
    if not args.check_runtime:
        run_sensor_stream(session, secret_get, RUNTIME_CONFIG)


if __name__ == "__main__":
    main()
