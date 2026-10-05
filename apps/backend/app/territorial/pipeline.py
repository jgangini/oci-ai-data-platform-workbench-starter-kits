"""AIDP ingestion and enrichment; the finite job remains the default entrypoint."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from threading import RLock

from .core import SYNTHETIC_MODES, canonical_mode, PLATFORMS, build_snapshot, default_source, simulation_state, utc_text, aidp_credential_name
from .database import mutate_document, publish, read_document, upsert_posts, reset_version, sensor_reset_version
from .landing import decode_record, write_objects, ensure_volumes, stream_progress
from .runtime_secrets import database_connection, runtime_auth
from .x import XFailure, poll_queries
from .sensor_pipeline import SensorLake
from .capture import schedule_at
from .core import publication_revisions


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class DeltaLake:
    def __init__(self, spark, config):
        self.spark, self.tables = spark, {}
        self.ingest_lock, self.on_ingested = RLock(), None
        catalog = config.get("catalog", "oci_medallion")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
            raise ValueError("Invalid Territorial catalog")
        ensure_volumes(spark, config)
        for layer, prefix in (("bronze", "02_bronze"), ("silver", "03_silver"), ("gold", "04_gold")):
            schema = f"{catalog}.oci_{layer}"
            table = f"{schema}.prisma_{'publications' if layer == 'gold' else 'events'}"
            uri = f"oci://{config['bucket']}@{config['namespace']}/{prefix}/prisma/{'publications' if layer == 'gold' else 'events'}"
            if "'" in uri:
                raise ValueError("Invalid Territorial storage location")
            spark.sql(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            spark.sql(f"CREATE TABLE IF NOT EXISTS {table} (id STRING, payload STRING) USING DELTA LOCATION '{uri}'")
            self.tables[layer] = table
        self.tables["current"] = f"{catalog}.oci_silver.prisma_current"
        uri = f"oci://{config['bucket']}@{config['namespace']}/03_silver/prisma/current"
        spark.sql(f"CREATE TABLE IF NOT EXISTS {self.tables['current']} (id STRING, payload STRING) USING DELTA LOCATION '{uri}'")
        install_post_views(spark, catalog, self.tables)
        self.sensors = SensorLake(spark, config, self.ingest_lock)

    def consume(self, config):
        return consume_landing(self.spark, self, config["landing_volume_path"], config["checkpoint_volume_path"])

    def put(self, layer, records):
        if not records:
            return
        from delta.tables import DeltaTable
        records = [{**item, "mode": canonical_mode(item["mode"])} if "mode" in item else item for item in records]
        rows = list({item["id"]: (item["id"], encoded(item).decode()) for item in records}.values())
        frame = self.spark.createDataFrame(rows, "id STRING, payload STRING")
        (DeltaTable.forName(self.spark, self.tables[layer]).alias("target")
         .merge(frame.alias("source"), "target.id = source.id").whenNotMatchedInsertAll().execute())

    def stage_snapshot(self, snapshot):
        """One publisher owns this Silver state; Gold always reads its durable, exact version."""
        from delta.tables import DeltaTable
        frame = self.spark.createDataFrame([("current", encoded(snapshot).decode())], "id STRING, payload STRING")
        (DeltaTable.forName(self.spark, self.tables["current"]).alias("target")
         .merge(frame.alias("source"), "target.id = source.id").whenMatchedUpdateAll().whenNotMatchedInsertAll().execute())
        rows = self.spark.table(self.tables["current"]).where("id = 'current'").select("payload").take(2)
        if len(rows) != 1 or json.loads(rows[0].payload) != snapshot:
            raise RuntimeError("Silver publication state did not round-trip")
        return json.loads(rows[0].payload)

    def pending(self, ids=None):
        frame = self.spark.table(self.tables["bronze"]).join(self.spark.table(self.tables["silver"]).select("id"), "id", "left_anti")
        if ids:
            frame = frame.where(frame.id.isin(ids))
        # Bound each tick to ten one-post LLM calls; the finite job retains its 600-second timeout.
        return [json.loads(row.payload) for row in frame.orderBy("id").limit(10).collect()]

    def pending_count(self):
        return self.spark.table(self.tables["bronze"]).join(self.spark.table(self.tables["silver"]).select("id"), "id", "left_anti").count()

    def _synthetic(self, layer):
        from pyspark.sql import functions as F
        return self.spark.table(self.tables[layer]).where(F.get_json_object("payload", "$.mode").isin(*SYNTHETIC_MODES))

    def synthetic_ids(self):
        for layer in ("bronze", "silver"):
            for row in self._synthetic(layer).select("id").toLocalIterator():
                yield row.id

    def delete_synthetic(self):
        from delta.tables import DeltaTable
        from pyspark.sql import functions as F
        counts = {}
        for layer in ("bronze", "silver"):
            counts[layer] = self._synthetic(layer).count()
            DeltaTable.forName(self.spark, self.tables[layer]).delete(F.get_json_object("payload", "$.mode").isin(*SYNTHETIC_MODES))
        return counts

    def publications(self):
        for row in self.spark.table(self.tables["gold"]).toLocalIterator():
            snapshot = json.loads(row.payload)
            if snapshot.get("version") != row.id:
                raise ValueError("Gold publication identity mismatch")
            yield snapshot

    def delete_publication(self, version):
        from delta.tables import DeltaTable
        from pyspark.sql import functions as F
        if not re.fullmatch(r"gold-[a-f0-9]{32}", version):
            raise ValueError("Invalid Gold publication identity")
        DeltaTable.forName(self.spark, self.tables["gold"]).delete(F.col("id") == version)

    def backfill_posts(self, connection):
        if read_document(connection, "runtime").get("post_index_revision") == 1:
            return
        for layer, status in (("bronze", "ingested"), ("silver", "processed")):
            batch = []
            for row in self.spark.table(self.tables[layer]).orderBy("id").toLocalIterator():
                batch.append(json.loads(row.payload))
                if len(batch) == 100:
                    upsert_posts(connection, batch, status)
                    batch = []
            upsert_posts(connection, batch, status)
        mutate_document(connection, "runtime", lambda current: {**current, "post_index_revision": 1})

    def visible(self, run_id, now):
        from pyspark.sql import functions as F
        frame = self.spark.table(self.tables["silver"])
        mode = F.get_json_object("payload", "$.mode")
        scenario = F.get_json_object("payload", "$.raw_metadata.scenario_run_id")
        continuous = F.get_json_object("payload", "$.raw_metadata.capture_run_id")
        created_at = F.get_json_object("payload", "$.created_at")
        frame = frame.where((mode.isin(*SYNTHETIC_MODES) & (scenario == (run_id or ""))) |
                            (mode.isin(*SYNTHETIC_MODES) & continuous.isNotNull() & (created_at >= utc_text(now - 86400))) |
                            ((mode == "real") & (created_at >= utc_text(now - 86400))))
        # ponytail: a bounded demo snapshot; production should page evidence through the serving API.
        rows = frame.orderBy("id").limit(5001).collect()
        if len(rows) > 5000:
            raise RuntimeError("Territorial publication exceeds the 5000-event demo limit")
        return [json.loads(row.payload) for row in rows]

    def apply_activity(self, snapshot, rules, now):
        evidence = {item["id"]: item for item in snapshot["evidence"]}
        records = []
        for incident in snapshot["incidents"]:
            for post_key in incident["evidence_ids"]:
                post = evidence[post_key]
                rule = rules.get(post["platform"], {})
                thresholds = rule.get("report_thresholds", {"low": 5, "medium": 10, "high": 20})
                records.append((incident["id"], post["platform"], post["content_hash"], post["created_at"], utc_text(now),
                    rule.get("correlation_window_minutes", 30), thresholds["low"], thresholds["medium"], thresholds["high"], rule.get("config_version", 1)))
        if not records:
            return
        frame = self.spark.createDataFrame(records, "event_id STRING, platform STRING, content_hash STRING, published_at STRING, evaluation_time STRING, window_minutes LONG, low_threshold LONG, medium_threshold LONG, high_threshold LONG, config_version LONG")
        frame.createOrReplaceTempView("territorial_activity_inputs")
        try:
            rows = self.spark.sql("""WITH counted AS (
              SELECT event_id,platform,low_threshold,medium_threshold,high_threshold,config_version,
                COUNT(DISTINCT CASE WHEN CAST(published_at AS TIMESTAMP) BETWEEN
                  CAST(evaluation_time AS TIMESTAMP) - window_minutes * INTERVAL 1 MINUTE
                  AND CAST(evaluation_time AS TIMESTAMP) THEN content_hash END) AS report_count
              FROM territorial_activity_inputs GROUP BY event_id,platform,low_threshold,medium_threshold,high_threshold,config_version)
              SELECT event_id,platform,report_count,config_version,
                CASE WHEN report_count>=high_threshold THEN 'high' WHEN report_count>=medium_threshold THEN 'medium'
                  WHEN report_count>=low_threshold THEN 'low' ELSE 'below_threshold' END AS report_activity
              FROM counted""").collect()
        finally:
            self.spark.catalog.dropTempView("territorial_activity_inputs")
        by_id = {item["id"]: item for item in snapshot["incidents"]}
        for row in rows:
            incident = by_id[row.event_id]
            incident["report_counts"][row.platform] = row.report_count
            incident["report_activity_by_platform"][row.platform] = row.report_activity
            incident["rule_versions"][row.platform] = row.config_version
        order = {"below_threshold": 0, "low": 1, "medium": 2, "high": 3}
        for incident in snapshot["incidents"]:
            incident["report_activity"] = max(incident["report_activity_by_platform"].values(), key=order.get)


def _put_object(objects, config, key, document):
    objects.put_object(config["namespace"], config["bucket"], key, encoded(document), content_type="application/json")


def install_post_views(spark, catalog, tables):
    """Add business names over existing Delta data; keep its paths and publication history."""
    spark.sql(f"""CREATE OR REPLACE VIEW {catalog}.oci_bronze.social_posts_raw AS
      SELECT id AS post_key,get_json_object(payload,'$.platform') AS platform,
        get_json_object(payload,'$.source_id') AS original_id,
        get_json_object(payload,'$.created_at') AS published_at,
        get_json_object(payload,'$.ingested_at') AS ingested_at,
        get_json_object(payload,'$.raw_metadata') AS provenance,
        get_json_object(payload,'$.source_object') AS source_object,
        get_json_object(payload,'$.source_hash') AS source_hash,
        get_json_object(payload,'$.source_hash_kind') AS source_hash_kind,
        COALESCE(get_json_object(payload,'$.schema_version'),'1') AS schema_version,payload AS original_payload
      FROM {tables['bronze']}""")
    spark.sql(f"""CREATE OR REPLACE VIEW {catalog}.oci_silver.social_posts AS
      SELECT id AS post_key,get_json_object(payload,'$.platform') AS platform,
        get_json_object(payload,'$.username') AS username,get_json_object(payload,'$.display_name') AS display_name,
        get_json_object(payload,'$.text') AS message,get_json_object(payload,'$.country') AS country,
        get_json_object(payload,'$.city') AS city,get_json_object(payload,'$.locality') AS locality,
        get_json_object(payload,'$.created_at') AS published_at,get_json_object(payload,'$.category') AS category,
        get_json_object(payload,'$.classification_method') AS analysis_version,
        get_json_object(payload,'$.model_version') AS model_version,get_json_object(payload,'$.prompt_version') AS prompt_version,
        get_json_object(payload,'$.claims') AS claims,
        'processed' AS analysis_status,payload
      FROM {tables['silver']}""")
    event_schema = ('ARRAY<STRUCT<id:STRING,title:STRING,summary:STRING,revision:BIGINT,updated_at:STRING,category:STRING,locality:STRING,mode:STRING,'
        'severity:STRING,review_status:STRING,review_note:STRING,reviewed_evidence_ids:ARRAY<STRING>,created_at:STRING,last_observed_at:STRING,'
        'lat:DOUBLE,lon:DOUBLE,location_method:STRING,corroboration_score:DOUBLE,corroboration_status:STRING,'
        'report_activity:STRING,report_counts:MAP<STRING,BIGINT>,rule_versions:MAP<STRING,BIGINT>,'
        'correlation_windows_minutes:MAP<STRING,BIGINT>>>')
    spark.sql(f"""CREATE OR REPLACE VIEW {catalog}.oci_gold.events AS
      SELECT p.id AS publication_version,event.id AS event_id,event.* FROM {tables['gold']} p
      LATERAL VIEW explode(from_json(get_json_object(payload,'$.incidents'),'{event_schema}')) records AS event""")
    relation_schema = 'ARRAY<STRUCT<event_id:STRING,post_key:STRING,relation:STRING,explanation:STRING,analysis_version:STRING,claim_relation:STRING,duplicate_of:STRING>>'
    spark.sql(f"""CREATE OR REPLACE VIEW {catalog}.oci_gold.event_posts AS
      SELECT p.id AS publication_version,relation.* FROM {tables['gold']} p
      LATERAL VIEW explode(from_json(get_json_object(payload,'$.event_posts'),'{relation_schema}')) records AS relation""")
    spark.sql(f"""CREATE OR REPLACE VIEW {catalog}.oci_silver.events AS
      SELECT get_json_object(p.payload,'$.version') AS publication_version,event.id AS event_id,event.*
      FROM {tables['current']} p
      LATERAL VIEW explode(from_json(get_json_object(payload,'$.incidents'),'{event_schema}')) records AS event""")
    spark.sql(f"""CREATE OR REPLACE VIEW {catalog}.oci_silver.event_posts AS
      SELECT get_json_object(p.payload,'$.version') AS publication_version,relation.* FROM {tables['current']} p
      LATERAL VIEW explode(from_json(get_json_object(payload,'$.event_posts'),'{relation_schema}')) records AS relation""")


def ingest_page(connection, objects, lake, config, platform, events, checkpoint=None):
    """A source cursor advances after immutable Landing; the separate stream checkpoint owns Bronze delivery."""
    key = write_objects(objects, config, events, {"platform": platform, "checkpoint": checkpoint})
    upsert_posts(connection, events, "captured", batch_key=key)
    if checkpoint is not None:
        mutate_document(connection, "checkpoint_" + platform, lambda current: {**current, **checkpoint})


def start_landing(spark, lake, path, checkpoint, *, persistent=False):
    """Preserve the legacy JSON checkpoint and independently drain CSV into the same idempotent Bronze sink."""
    if path.startswith("oci:") or checkpoint.startswith("oci:"):
        raise ValueError("AIDP streaming requires governed volume paths, not oci://")
    streams = []
    lock = getattr(lake, "ingest_lock", RLock())
    try:
        for format_name, suffix in (("json", ""), ("csv", "-csv")):
            streams.append((format_name, _consume_format(spark, lake, path, checkpoint + suffix, format_name, persistent, lock)))
        return streams
    except Exception:
        for _, query in streams:
            query.stop()
        raise


def consume_landing(spark, lake, path, checkpoint):
    queries = start_landing(spark, lake, path, checkpoint)
    try:
        # Start CSV before waiting for the legacy JSON source, including after upgrades.
        for _, query in queries:
            query.awaitTermination()
        return stream_progress(queries)
    finally:
        for _, query in queries:
            if getattr(query, "isActive", False):
                query.stop()


def _consume_format(spark, lake, path, checkpoint, format_name, persistent=False, lock=None):
    lock = lock or RLock()
    def commit(frame, _batch_id):
        from pyspark.sql import functions as F
        rows = frame.withColumn("source_object", F.input_file_name()).limit(5001).collect()
        if len(rows) > 5000:
            raise RuntimeError("Territorial microbatch exceeds its 5000-record bound")
        events = []
        for row in rows:
            match = re.search(r"([a-f0-9]{64})\.csv$", row.source_object)
            events.append({**decode_record(row.id, row.payload), "source_object": row.source_object,
                "source_hash": match[1] if match else None, "source_hash_kind": "batch_identity_sha256" if match else None,
                "schema_version": 1, "ingested_at": utc_text(time.time())})
        # ponytail: serialize the two format writers within this concurrency-one job;
        # additional ingestion jobs need distinct checkpoints and a coordinated Delta writer.
        with lock:
            lake.put("bronze", events)
            if getattr(lake, "on_ingested", None):
                lake.on_ingested(events, {"format": format_name, "batch_id": _batch_id})
    reader = (spark.readStream.schema("id STRING, payload STRING").option("maxFilesPerTrigger", 5)
              .option("pathGlobFilter", "*.ndjson" if format_name == "json" else "*.csv").option("mode", "FAILFAST"))
    if path.startswith("/Volumes"):
        if not re.fullmatch(r"/Volumes/[A-Za-z_][A-Za-z0-9_]*/prisma_ingest/landing", path):
            raise ValueError("Invalid Territorial governed volume path")
        # AIDP needs the same file: URI form for the mounted root and its enumerated leaves.
        path = "file:" + path
    if format_name == "csv":
        reader = reader.options(header=True, enforceSchema=False, multiLine=True, quote='"', escape='"', encoding="UTF-8")
    stream = reader.format(format_name).load(path)
    trigger = {"processingTime": "30 seconds"} if persistent else {"availableNow": True}
    return stream.writeStream.foreachBatch(commit).option("checkpointLocation", checkpoint).trigger(**trigger).start()


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
    if (not source["enabled"] and status.get("requested_action") != "test") or not (source.get("capture_running", False) or status.get("requested_action")) or not _due(status, revision, now):
        return
    slot = schedule_at(config.get("social_schedule"), now, status.get("capture_slot"))
    if status.get("requested_action") != "test" and slot is not None and slot > now:
        _status(connection, source["platform"], {"status": "scheduled", "next_due": utc_text(slot)}, status.get("request_id"))
        return
    values = {"status": "simulation", "last_error": None, "next_due": utc_text(now + source["interval_minutes"] * 60)}
    try:
        if source["mode"] == "real":
            if source["platform"] != "x":
                raise XFailure("connector_not_available")
            values = _poll_x(connection, objects, lake, config, source, status, secret_get, now, client)
        if status.get("requested_action") != "test" and slot is not None:
            values.update(capture_slot=slot, next_due=utc_text(schedule_at(config.get("social_schedule"), now, slot)))
    except XFailure as exc:
        values = {"status": exc.code, "last_error": exc.code, "next_due": utc_text(exc.retry_at) if exc.retry_at else None}
        if exc.code == "rate_limited":
            mutate_document(connection, "checkpoint_" + source["platform"], lambda current: {**current, "retry_at": exc.retry_at})
    except httpx.RequestError:
        values = {"status": "network_error", "last_error": "network_error", "next_due": utc_text(now + 60)}
    _status(connection, source["platform"], {**values, "last_run_at": utc_text(now), "configuration_revision": revision}, status.get("request_id"))


def publish_snapshot(connection, objects, lake, config, events, reviews, simulation, now, *, rules=None):
    registry = read_document(connection, "event_registry")
    previous = registry.get("items")
    if rules is not None and previous is None:
        if read_document(connection, "runtime").get("publication"):
            try:
                response = objects.get_object(config["namespace"], config["bucket"], "04_gold/prisma/current.json")
            except Exception as exc:
                if getattr(exc, "status", None) != 404:
                    raise
            else:
                pointer = json.loads(response.data.content)
                key = f"04_gold/prisma/snapshots/{pointer['version']}.json"
                if pointer.get("snapshot_key") != key or not re.fullmatch(r"gold-[a-f0-9]{32}", pointer["version"]):
                    raise ValueError("Invalid previous publication pointer")
                response = objects.get_object(config["namespace"], config["bucket"], key)
                published = json.loads(response.data.content)
                if published.get("version") != pointer["version"]:
                    raise ValueError("Previous publication version does not match its pointer")
                previous = published["incidents"]
    sensors = None
    if getattr(lake, "sensors", None) is not None:
        from .sensors import apply_locations
        sensors = apply_locations(lake.sensors.latest(now), read_document(connection, "reviews").get("sensor_locations", {}))
    snapshot = build_snapshot(events, reviews, "", "", rules=rules, previous=previous, now=now, sensors=sensors)
    if rules is not None and hasattr(lake, "apply_activity"):
        lake.apply_activity(snapshot, rules, now)
    snapshot.update(runtime="aidp", simulation=simulation)
    snapshot.update(publication_revisions(snapshot))
    digest = hashlib.sha256(encoded(snapshot)).hexdigest()
    version = "gold-" + digest[:32]
    runtime = read_document(connection, "runtime")
    pending = runtime.get("publication", {})
    if pending.get("version") != version:
        pending = {"version": version, "published_at": utc_text(now)}
        mutate_document(connection, "runtime", lambda current: {**current, "publication": pending})
    snapshot.update(version=version, published_at=pending["published_at"])
    snapshot = lake.stage_snapshot(snapshot)
    lake.put("gold", [{"id": version, **snapshot}])
    publish(connection, snapshot)
    key = f"04_gold/prisma/snapshots/{version}.json"
    _put_object(objects, config, key, snapshot)
    # The previous pointer remains usable if any preceding durable write fails.
    _put_object(objects, config, "04_gold/prisma/current.json", {"version": version, "snapshot_key": key,
        "published_at": snapshot["published_at"], "social_revision": snapshot["social_revision"], "sensor_revision": snapshot["sensor_revision"]})
    if rules is not None and registry.get("items") != snapshot["incidents"]:
        mutate_document(connection, "event_registry", lambda current: {**current, "items": snapshot["incidents"]})
    return snapshot


def _finish_enrichment(connection, lake, prepared):
    lake.put("silver", prepared)
    upsert_posts(connection, prepared, "processed")
    completed = {item["id"] for item in prepared}
    return mutate_document(connection, "checkpoint_enrichment", lambda current: {**current, "prepared": [],
        "attempts": 0, "retry_at": 0, "last_error": None, "last_error_reason": None, "circuit_open": False,
        "pending_ids": [key for key in current.get("pending_ids", []) if key not in completed]})


def enrich_pending(connection, lake, classifier, now, configuration_revision=None):
    checkpoint = read_document(connection, "checkpoint_enrichment")
    if checkpoint.get("prepared"):
        checkpoint = _finish_enrichment(connection, lake, checkpoint["prepared"])
    if checkpoint.get("configuration_revision", configuration_revision) != configuration_revision:
        checkpoint = mutate_document(connection, "checkpoint_enrichment", lambda current: {**current,
            "configuration_revision": configuration_revision, "attempts": 0, "retry_at": 0,
            "last_error": None, "last_error_reason": None, "circuit_open": False, "pending_ids": []})
    pending = lake.pending(checkpoint.get("pending_ids"))
    count = lake.pending_count()
    if not pending:
        return {"pending_count": count, "last_error": None, "last_error_reason": None, "retry_at": 0, "needs_attention": False}
    upsert_posts(connection, pending, "ingested", ingested_at=utc_text(now))
    if checkpoint.get("circuit_open") or (checkpoint.get("retry_at") or 0) > now:
        return {"pending_count": count, "last_error": checkpoint.get("last_error"), "retry_at": checkpoint.get("retry_at"),
                "last_error_reason": checkpoint.get("last_error_reason"),
                "needs_attention": bool(checkpoint.get("circuit_open"))}
    for index, event in enumerate(pending):
        remaining = [item["id"] for item in pending[index:]]
        try:
            classified = classifier([event])
            if len(classified) != 1 or classified[0]["id"] != event["id"]:
                raise ValueError("Classifier returned incomplete evidence")
        except Exception as exc:
            reason = "nonliteral_claim" if isinstance(exc, ValueError) and str(exc) == "Claim evidence must quote the original post literally" else None
            attempts = min(5, int(checkpoint.get("attempts", 0)) + 1)
            retry_at = now + min(300, 30 * 2 ** (attempts - 1)) if attempts < 5 else None
            mutate_document(connection, "checkpoint_enrichment", lambda current: {**current,
                "attempts": attempts, "retry_at": retry_at, "last_error": type(exc).__name__, "last_error_reason": reason, "circuit_open": attempts == 5,
                "pending_ids": remaining, "configuration_revision": configuration_revision})
            return {"pending_count": lake.pending_count(), "last_error": type(exc).__name__, "retry_at": retry_at,
                    "last_error_reason": reason, "needs_attention": attempts == 5}
        # Journal each validated post before Silver; recovery must finish its ADB projection before the next model call.
        mutate_document(connection, "checkpoint_enrichment", lambda current: {**current, "prepared": classified,
            "pending_ids": remaining, "configuration_revision": configuration_revision})
        checkpoint = _finish_enrichment(connection, lake, classified)
    return {"pending_count": lake.pending_count(), "last_error": None, "last_error_reason": None, "retry_at": 0, "needs_attention": False}


def _tick(connection, objects, lake, config, secret_get, now, classifier, client, *, progress=None):
    document = read_document(connection, "configuration")
    sources = [{**default_source(platform), **document.get("sources", {}).get(platform, {})} for platform in PLATFORMS]
    state = simulation_state(read_document(connection, "simulation"), now)
    config = {**config, "configuration_revision": document.get("revision", 0), "social_schedule": document.get("social_schedule")}
    for source in sources:
        if source["mode"] == "real":
            poll_source(connection, objects, lake, config, source, read_document(connection, "status_" + source["platform"]), secret_get, now, client)
    progress = lake.consume(config) if progress is None else progress
    enrichment = enrich_pending(connection, lake, classifier, now, config["configuration_revision"])
    events = lake.visible(state.get("run_id"), now)
    reviews = read_document(connection, "reviews").get("items", {})
    snapshot = publish_snapshot(connection, objects, lake, config, events, reviews, state, now,
                                rules={source["platform"]: source for source in sources})
    mutate_document(connection, "status_pipeline", lambda current: {**current, **enrichment,
        "status": "needs_attention" if enrichment["needs_attention"] else "pending" if enrichment["pending_count"] else "ready", "last_run_at": utc_text(now),
        "pipeline_revision": config.get("pipeline_revision"),
        "sensor_reset_version": 2,
        "configuration_revision": config["configuration_revision"], "version": snapshot["version"], "stream": progress})
    return snapshot


def process_reset(connection, objects, lake, config, now):
    command = read_document(connection, "checkpoint_reset")
    if command.get("status") == "error":
        raise RuntimeError("Synthetic reset is blocked; explicitly retry the same operation")
    if command.get("status") != "pending" or command.get("ready") is not True:
        return None
    if command.get("sensor_type"):
        if (command.get("sensor_drained_operation_id") != command.get("operation_id") or
                command.get("sensor_drained_revision") != config.get("pipeline_revision")):
            return None
        from .sensor_reset import execute
    else:
        from .synthetic_reset import execute
    snapshot = execute(connection, objects, lake, config, now, command, publish_snapshot)
    mutate_document(connection, "status_pipeline", lambda doc: {**doc, "status": "ready",
        "version": snapshot["version"], "last_run_at": utc_text(now), "pending_count": lake.pending_count(), "last_error": None})
    return snapshot


def stop_streams(queries):
    for _, query in queries:
        query.stop()
    for _, query in queries:
        query.awaitTermination()


def run_persistent(spark, connection, objects, lake, config, secret_get, classifier, client, clock):
    queries = start_landing(spark, lake, config["landing_volume_path"], config["checkpoint_volume_path"], persistent=True)
    try:
        while True:
            sensor_status = read_document(connection, "status_sensorstream")
            if (sensor_status.get("sensor_layers_version") != 2 or
                    sensor_status.get("pipeline_revision") != config.get("pipeline_revision")):
                time.sleep(10)
                continue
            command = read_document(connection, "checkpoint_reset")
            if command.get("sensor_type") and command.get("status") == "pending" and (
                    not command.get("ready") or command.get("sensor_drained_operation_id") != command.get("operation_id") or
                    command.get("sensor_drained_revision") != config.get("pipeline_revision")):
                time.sleep(10)
                continue
            if command.get("status") in {"pending", "error"} and command.get("ready") is True:
                stop_streams(queries)
                queries = []
                process_reset(connection, objects, lake, config, clock())
                queries = start_landing(spark, lake, config["landing_volume_path"], config["checkpoint_volume_path"], persistent=True)
            for _, query in queries:
                if query.exception() or not query.isActive:
                    raise RuntimeError("The persistent Landing stream stopped")
            _tick(connection, objects, lake, config, secret_get, clock(), classifier, client, progress=stream_progress(queries))
            time.sleep(10)
    finally:
        for _, query in queries:
            query.stop()


def run(spark, secret_get, config, *, clock=time.time, classifier=None, connection=None, objects=None, lake=None, client=None):
    """Production notebook entrypoint; optional injections make durable ordering testable offline."""
    from contextlib import ExitStack
    import oci
    import httpx
    from .classification import classify
    with ExitStack() as stack:
        connection = connection or stack.enter_context(database_connection(secret_get, "PrismaWriterRuntime"))
        sdk_config, signed = runtime_auth(secret_get, config["region"], config.get("oci_credential_name", "PrismaWriterRuntime"),
            config.get("oci_identity_sha256", "")) if objects is None or classifier is None else ({}, None)
        objects = objects or oci.object_storage.ObjectStorageClient(sdk_config, signer=signed)
        client = client or stack.enter_context(httpx.Client())
        lake = lake or DeltaLake(spark, config)
        if reset_version(connection) != 2:
            raise RuntimeError("Synthetic reset database contract is not installed")
        if sensor_reset_version(connection) != 2:
            raise RuntimeError("Sensor reset database contract is not installed")
        if read_document(connection, "runtime").get("synthetic_reset_version") != 2:
            mutate_document(connection, "runtime", lambda doc: {**doc, "synthetic_reset_version": 2})
        def on_ingested(events, batch_key):
            # Streaming callbacks run on other threads; never share the enrichment connection.
            with database_connection(secret_get, "PrismaWriterRuntime") as ingestion_connection:
                upsert_posts(ingestion_connection, events, "ingested", ingested_at=utc_text(clock()), batch_key=batch_key)
        lake.on_ingested = on_ingested
        reset_snapshot = process_reset(connection, objects, lake, config, clock())
        if reset_snapshot is not None and config.get("streaming_mode", "finite") != "persistent":
            return reset_snapshot
        if hasattr(lake, "backfill_posts"):
            lake.backfill_posts(connection)
        if classifier is None:
            inference = oci.generative_ai_inference.GenerativeAiInferenceClient(sdk_config, signer=signed,
                retry_strategy=oci.retry.NoneRetryStrategy(), timeout=(10, 30))
            classifier = lambda events: classify(events, config, signed, client=inference)
        try:
            if config.get("streaming_mode", "finite") == "persistent":
                return run_persistent(spark, connection, objects, lake, config, secret_get, classifier, client, clock)
            snapshot = _tick(connection, objects, lake, config, secret_get, clock(), classifier, client)
            if config.get("workbench_base"):
                from .scheduling import reconcile_after_tick, workbench_request
                from .runtime_secrets import signer
                request = workbench_request(config["workbench_base"], config["region"], signed or signer(secret_get,
                    config.get("oci_credential_name", "PrismaWriterRuntime"), config.get("oci_identity_sha256", "")))
                reconcile_after_tick(connection, request, clock())
            return snapshot
        except Exception as exc:
            mutate_document(connection, "status_pipeline", lambda current: {**current, "status": "error",
                "last_error": type(exc).__name__, "last_run_at": utc_text(clock())})
            raise RuntimeError("Territorial pipeline failed; cursor/publication retained, inspect status_pipeline") from None
