"""Finite AIDP Job tick. Credentials and Spark are supplied by the notebook runtime."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime

from .core import PLATFORMS, build_snapshot, default_source, simulation_state, utc_text, aidp_credential_name
from .database import mutate_document, publish, read_document
from .landing import decode_record, write_objects
from .runtime_secrets import database_connection, runtime_auth
from .x import XFailure, poll_queries


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class DeltaLake:
    def __init__(self, spark, config):
        self.spark, self.tables = spark, {}
        catalog = config.get("catalog", "oci_medallion")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
            raise ValueError("Invalid PRISMA catalog")
        ensure_volumes(spark, config)
        for layer, prefix in (("bronze", "02_bronze"), ("silver", "03_silver"), ("gold", "04_gold")):
            schema = f"{catalog}.oci_{layer}"
            table = f"{schema}.prisma_{'publications' if layer == 'gold' else 'events'}"
            uri = f"oci://{config['bucket']}@{config['namespace']}/{prefix}/prisma/{'publications' if layer == 'gold' else 'events'}"
            if "'" in uri:
                raise ValueError("Invalid PRISMA storage location")
            spark.sql(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            spark.sql(f"CREATE TABLE IF NOT EXISTS {table} (id STRING, payload STRING) USING DELTA LOCATION '{uri}'")
            self.tables[layer] = table

    def consume(self, config):
        return consume_landing(self.spark, self, config["landing_volume_path"], config["checkpoint_volume_path"])

    def put(self, layer, records):
        if not records:
            return
        from delta.tables import DeltaTable
        rows = list({item["id"]: (item["id"], encoded(item).decode()) for item in records}.values())
        frame = self.spark.createDataFrame(rows, "id STRING, payload STRING")
        (DeltaTable.forName(self.spark, self.tables[layer]).alias("target")
         .merge(frame.alias("source"), "target.id = source.id").whenNotMatchedInsertAll().execute())

    def pending(self):
        frame = self.spark.table(self.tables["bronze"]).join(self.spark.table(self.tables["silver"]).select("id"), "id", "left_anti")
        return [json.loads(row.payload) for row in frame.orderBy("id").limit(100).collect()]

    def visible(self, run_id, now):
        from pyspark.sql import functions as F
        frame = self.spark.table(self.tables["silver"])
        mode = F.get_json_object("payload", "$.mode")
        scenario = F.get_json_object("payload", "$.raw_metadata.scenario_run_id")
        continuous = F.get_json_object("payload", "$.raw_metadata.capture_run_id")
        created_at = F.get_json_object("payload", "$.created_at")
        frame = frame.where(((mode == "simulation") & (scenario == (run_id or ""))) |
                            ((mode == "simulation") & continuous.isNotNull() & (created_at >= utc_text(now - 86400))) |
                            ((mode == "real") & (created_at >= utc_text(now - 86400))))
        # ponytail: a bounded demo snapshot; production should page evidence through the serving API.
        rows = frame.orderBy("id").limit(5001).collect()
        if len(rows) > 5000:
            raise RuntimeError("PRISMA publication exceeds the 5000-event demo limit")
        return [json.loads(row.payload) for row in rows]


def _put_object(objects, config, key, document):
    objects.put_object(config["namespace"], config["bucket"], key, encoded(document), content_type="application/json")


def ingest_page(connection, objects, lake, config, platform, events, checkpoint=None):
    """A source cursor advances after immutable Landing; the separate stream checkpoint owns Bronze delivery."""
    write_objects(objects, config, events, {"platform": platform, "checkpoint": checkpoint})
    if checkpoint is not None:
        mutate_document(connection, "checkpoint_" + platform, lambda current: {**current, **checkpoint})


def ensure_volumes(spark, config):
    catalog = config.get("catalog", "oci_medallion")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
        raise ValueError("Invalid PRISMA catalog")
    schema = catalog + ".prisma_ingest"
    uri = f"oci://{config['landing_bucket']}@{config['namespace']}/{config['landing_prefix']}"
    if "'" in uri or config["landing_volume_path"] != f"/Volumes/{catalog}/prisma_ingest/landing" or config["checkpoint_volume_path"] != f"/Volumes/{catalog}/prisma_ingest/checkpoints/bronze-v1":
        raise ValueError("Invalid PRISMA governed streaming path")
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    for name, kind in (("landing", "EXTERNAL"), ("checkpoints", "MANAGED")):
        declaration = "EXTERNAL " if kind == "EXTERNAL" else ""
        location = f" LOCATION '{uri}'" if kind == "EXTERNAL" else ""
        spark.sql(f"CREATE {declaration}VOLUME IF NOT EXISTS {schema}.{name}{location}")
        details = {str(row[0]).strip().lower().replace("_", " "): str(row[1]).strip() for row in spark.sql(f"DESCRIBE VOLUME {schema}.{name}").collect()}
        if details.get("volume type", "").upper() != kind or (kind == "EXTERNAL" and details.get("location", "").rstrip("/") != uri.rstrip("/")):
            raise RuntimeError("PRISMA volume type or storage location does not match the deployment")


def consume_landing(spark, lake, path, checkpoint):
    """Preserve the legacy JSON checkpoint and independently drain CSV into the same idempotent Bronze sink."""
    if path.startswith("oci:") or checkpoint.startswith("oci:"):
        raise ValueError("AIDP streaming requires governed volume paths, not oci://")
    streams = [_consume_format(spark, lake, path, checkpoint, "json"),
               _consume_format(spark, lake, path, checkpoint + "-csv", "csv")]
    return {"query_id": streams[-1]["query_id"], "streams": streams,
            "microbatches": sum(item["microbatches"] for item in streams),
            "last_input_rows": sum(item["last_input_rows"] for item in streams)}


def _consume_format(spark, lake, path, checkpoint, format_name):
    def commit(frame, _batch_id):
        rows = frame.limit(5001).collect()
        if len(rows) > 5000:
            raise RuntimeError("PRISMA microbatch exceeds its 5000-record bound")
        events = [decode_record(row.id, row.payload) for row in rows]
        lake.put("bronze", events)  # MERGE by event ID makes replay after callback failure idempotent.
    reader = (spark.readStream.schema("id STRING, payload STRING").option("maxFilesPerTrigger", 5)
              .option("pathGlobFilter", "*.ndjson" if format_name == "json" else "*.csv").option("mode", "FAILFAST"))
    if format_name == "csv":
        reader = reader.options(header=True, enforceSchema=False, multiLine=True, quote='"', escape='"', encoding="UTF-8")
    stream = reader.format(format_name).load(path)
    query = stream.writeStream.foreachBatch(commit).option("checkpointLocation", checkpoint).trigger(availableNow=True).start()
    query.awaitTermination()
    return {"format": format_name, "query_id": str(query.id), "microbatches": len(query.recentProgress),
            "last_input_rows": (query.lastProgress or {}).get("numInputRows", 0)}


def _status(connection, platform, values, request_id=None):
    def change(current):
        if current.get("requested_action") and current.get("request_id") != request_id:
            return {**current, "last_run_at": values.get("last_run_at", current.get("last_run_at"))}
        return {**current, **values, "requested_action": None}
    return mutate_document(connection, "status_" + platform, change)


def _due(status, revision, now):
    if status.get("requested_action") or status.get("configuration_revision") != revision:
        return True
    if status.get("next_due"):
        return datetime.fromisoformat(status["next_due"].replace("Z", "+00:00")).timestamp() <= now
    return bool(status.get("requested_action") or not status.get("last_error") or status.get("configuration_revision") != revision)


def _source_token(secret_get, source):
    if not source.get("credential_configured"):
        raise XFailure("credential_required")
    try:
        token = secret_get(name=aidp_credential_name(source["platform"], source["secret_ref"]), key="bearer_token")
    except Exception:
        raise XFailure("credential_unavailable") from None
    if not isinstance(token, str) or not token:
        raise XFailure("credential_required")
    return token


def _poll_x(connection, objects, lake, config, source, status, secret_get, now, client):
    platform = source["platform"]
    saved = read_document(connection, "checkpoint_" + platform)
    test = status.get("requested_action") == "test"
    if saved.get("retry_at", 0) > now:
        raise XFailure("rate_limited", saved["retry_at"])
    token = _source_token(secret_get, source)
    def on_page(events, checkpoint):
        ingest_page(connection, objects, lake, config, platform, events, checkpoint)
    def on_checkpoint(checkpoint):
        mutate_document(connection, "checkpoint_" + platform, lambda current: {**current, **checkpoint})
    return poll_queries(client, token, source, saved, now, on_page, on_checkpoint, test=test)


def poll_source(connection, objects, lake, config, source, status, secret_get, now, client):
    import httpx
    revision = config.get("configuration_revision", 0)
    if not source["enabled"] or not (source.get("capture_running", False) or status.get("requested_action")) or not _due(status, revision, now):
        return
    values = {"status": "simulation", "last_error": None, "next_due": utc_text(now + source["interval_minutes"] * 60)}
    try:
        if source["mode"] == "real":
            if source["platform"] != "x":
                raise XFailure("connector_not_available")
            values = _poll_x(connection, objects, lake, config, source, status, secret_get, now, client)
    except XFailure as exc:
        values = {"status": exc.code, "last_error": exc.code, "next_due": utc_text(exc.retry_at) if exc.retry_at else None}
        if exc.code == "rate_limited":
            mutate_document(connection, "checkpoint_" + source["platform"], lambda current: {**current, "retry_at": exc.retry_at})
    except httpx.RequestError:
        values = {"status": "network_error", "last_error": "network_error", "next_due": utc_text(now + 60)}
    _status(connection, source["platform"], {**values, "last_run_at": utc_text(now), "configuration_revision": revision}, status.get("request_id"))


def publish_snapshot(connection, objects, lake, config, events, reviews, simulation, now):
    snapshot = build_snapshot(events, reviews, "", "")
    snapshot.update(runtime="aidp", simulation=simulation)
    digest = hashlib.sha256(encoded(snapshot)).hexdigest()
    version = "gold-" + digest[:32]
    runtime = read_document(connection, "runtime")
    pending = runtime.get("publication", {})
    if pending.get("version") != version:
        pending = {"version": version, "published_at": utc_text(now)}
        mutate_document(connection, "runtime", lambda current: {**current, "publication": pending})
    snapshot.update(version=version, published_at=pending["published_at"])
    lake.put("gold", [{"id": version, **snapshot}])
    publish(connection, snapshot)
    key = f"04_gold/prisma/snapshots/{version}.json"
    _put_object(objects, config, key, snapshot)
    # The previous pointer remains usable if any preceding durable write fails.
    _put_object(objects, config, "04_gold/prisma/current.json", {"version": version, "snapshot_key": key})
    return snapshot


def _tick(connection, objects, lake, config, secret_get, now, classifier, client):
    document = read_document(connection, "configuration")
    sources = [{**default_source(platform), **document.get("sources", {}).get(platform, {})} for platform in PLATFORMS]
    state = simulation_state(read_document(connection, "simulation"), now)
    config = {**config, "configuration_revision": document.get("revision", 0)}
    for source in sources:
        if source["mode"] == "real":
            poll_source(connection, objects, lake, config, source, read_document(connection, "status_" + source["platform"]), secret_get, now, client)
    progress = lake.consume(config)
    pending = lake.pending()
    while pending:
        classified = classifier(pending)
        if {item["id"] for item in classified} != {item["id"] for item in pending}:
            raise ValueError("Classifier returned incomplete evidence")
        lake.put("silver", classified)
        pending = lake.pending()
    events = lake.visible(state.get("run_id"), now)
    reviews = read_document(connection, "reviews").get("items", {})
    snapshot = publish_snapshot(connection, objects, lake, config, events, reviews, state, now)
    mutate_document(connection, "status_pipeline", lambda current: {**current, "status": "ready", "last_error": None,
        "last_run_at": utc_text(now), "version": snapshot["version"], "stream": progress})
    return snapshot


def run(spark, secret_get, config, *, clock=time.time, classifier=None, connection=None, objects=None, lake=None, client=None):
    """Production notebook entrypoint; optional injections make durable ordering testable offline."""
    from contextlib import ExitStack
    import oci
    import httpx
    from .classification import classify
    with ExitStack() as stack:
        connection = connection or stack.enter_context(database_connection(secret_get, "PrismaWriterRuntime"))
        sdk_config, signed = runtime_auth(secret_get, config["region"]) if objects is None or classifier is None else ({}, None)
        objects = objects or oci.object_storage.ObjectStorageClient(sdk_config, signer=signed)
        client = client or stack.enter_context(httpx.Client())
        lake = lake or DeltaLake(spark, config)
        if classifier is None:
            inference = oci.generative_ai_inference.GenerativeAiInferenceClient(sdk_config, signer=signed)
            classifier = lambda events: classify(events, config, signed, client=inference)
        try:
            snapshot = _tick(connection, objects, lake, config, secret_get, clock(), classifier, client)
            if config.get("workbench_base"):
                from .scheduling import reconcile_after_tick, workbench_request
                from .runtime_secrets import signer
                request = workbench_request(config["workbench_base"], config["region"], signed or signer(secret_get))
                reconcile_after_tick(connection, request, clock())
            return snapshot
        except Exception as exc:
            mutate_document(connection, "status_pipeline", lambda current: {**current, "status": "error",
                "last_error": type(exc).__name__, "last_run_at": utc_text(clock())})
            raise RuntimeError("PRISMA pipeline failed; cursor/publication retained, inspect status_pipeline") from None
