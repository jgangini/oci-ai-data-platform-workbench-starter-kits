"""Coordinated synthetic-only cleanup, executed by the single native AIDP writer."""
import hashlib
import json
import re
from uuid import UUID

from . import database, landing
from .core import SYNTHETIC_MODES, PLATFORMS, default_source, simulation_state, utc_text

HISTORY_PREFIX = "04_gold/prisma/snapshots/"
VERSION = re.compile(r"gold-[a-f0-9]{32}")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _synthetic_ids(items):
    return {item["id"] for item in items if item.get("mode") in SYNTHETIC_MODES}


def check_scope(state, operation_id, sensor_type=None):
    from fastapi import HTTPException
    known = operation_id == state.get("operation_id") or operation_id in state.get("completed_ids", [])
    scope = state.get("sensor_type") if operation_id == state.get("operation_id") else state.get("operation_scopes", {}).get(operation_id)
    if known and scope != sensor_type:
        raise HTTPException(409, "A delete operation ID cannot be reused for another scope")


def prune_publication(snapshot, sensor_type=None):
    """Preserve real incident fields verbatim; never recalculate historical facts."""
    if sensor_type is not None:
        from .sensors import SENSOR_TYPES
        if sensor_type not in SENSOR_TYPES:
            raise ValueError("Unknown sensor type")
        selected = [item for item in snapshot.get("sensors", []) if item.get("sensor_type") == sensor_type]
        if not selected:
            return None
        if any(item.get("mode") not in SYNTHETIC_MODES or item.get("is_simulated") is not True for item in selected):
            raise ValueError("Sensor deletion requires synthetic provenance")
        clean = {**snapshot, "sensors": [item for item in snapshot["sensors"] if item.get("sensor_type") != sensor_type]}
        return _versioned_replacement(clean, snapshot["version"])
    removed_posts = _synthetic_ids(snapshot["evidence"])
    removed_events = _synthetic_ids(snapshot["incidents"])
    if not removed_posts and not removed_events:
        return None
    clean = dict(snapshot)
    clean.pop("id", None)
    clean.pop("version")
    clean["evidence"] = [item for item in snapshot["evidence"] if item["id"] not in removed_posts]
    clean["incidents"] = [item for item in snapshot["incidents"] if item["id"] not in removed_events]
    if any(removed_posts.intersection(item["evidence_ids"]) for item in clean["incidents"]):
        raise ValueError("A real incident references synthetic evidence; cleanup requires repair")
    clean["event_posts"] = [item for item in snapshot.get("event_posts", [])
                            if item["event_id"] not in removed_events and item["post_key"] not in removed_posts]
    return _versioned_replacement(clean, snapshot["version"])


def _versioned_replacement(clean, old_version):
    clean.pop("id", None)
    clean.pop("version", None)
    clean["reset_of"] = old_version
    clean["version"] = "gold-" + hashlib.sha256(encoded(clean)).hexdigest()[:32]
    return clean


def object_keys(objects, config, bucket, prefix):
    start, seen = None, set()
    while True:
        response = objects.list_objects(config["namespace"], bucket, prefix=prefix, start=start, fields="name")
        for item in response.data.objects:
            if prefix == "01_landing/prisma/raw/" and item.name.startswith(prefix + "sensors/"):
                continue  # The social reset must preserve the separate sensor history and checkpoint.
            if not item.name.startswith(prefix) or "/" in item.name[len(prefix):] or ".." in item.name:
                raise ValueError("Object escaped the synthetic reset prefix")
            yield item.name
        start = response.data.next_start_with
        if not start:
            return
        if start in seen:
            raise ValueError("Object listing did not advance")
        seen.add(start)


def object_body(objects, config, bucket, key):
    response = objects.get_object(config["namespace"], bucket, key)
    body = response.data.content
    if len(body) > 64 * 1024 * 1024:
        raise ValueError("Synthetic reset object exceeds the 64 MiB demo limit")
    headers = getattr(response, "headers", {})
    return body, headers.get("etag") or headers.get("ETag")


def delete_object(objects, config, bucket, key, etag=None):
    try:
        objects.delete_object(config["namespace"], bucket, key, **({"if_match": etag} if etag else {}))
    except Exception as exc:
        if getattr(exc, "status", None) != 404:
            raise


def clean_landing(objects, config):
    if config["landing_prefix"] != "01_landing/prisma/raw/":
        raise ValueError("Synthetic reset requires the verified project Landing prefix")
    count = 0
    for key in object_keys(objects, config, config["landing_bucket"], config["landing_prefix"]):
        suffix = ".ndjson" if key.endswith(".ndjson") else ".csv" if key.endswith(".csv") else None
        if suffix is None:
            continue
        body, etag = object_body(objects, config, config["landing_bucket"], key)
        events = landing.records(body, suffix)
        retained = [item for item in events if item["mode"] not in SYNTHETIC_MODES]
        if len(retained) == len(events):
            continue
        if retained:
            # Commit a new immutable real-only envelope before removing its mixed predecessor.
            landing.write_objects(objects, config, retained, {"reset_source": key})
        delete_object(objects, config, config["landing_bucket"], key, etag)
        count += 1
    return count


def _clean_controls(connection, removed_posts, removed_events):
    def enrichment(document):
        prepared = [item for item in document.get("prepared", []) if item.get("mode") not in SYNTHETIC_MODES]
        pending = [key for key in document.get("pending_ids", []) if key not in removed_posts]
        return {**document, "prepared": prepared, "pending_ids": pending}
    database.mutate_document(connection, "checkpoint_enrichment", enrichment)
    database.mutate_document(connection, "reviews", lambda doc: {**doc, "items": {
        key: value for key, value in doc.get("items", {}).items()
        if key not in removed_events and not (value.get("evidence_ids") and set(value["evidence_ids"]) <= removed_posts)}})
    database.mutate_document(connection, "event_registry", lambda doc: {**doc, "items": [
        item for item in doc.get("items", []) if item.get("mode") not in SYNTHETIC_MODES]})
    sources = database.read_document(connection, "configuration").get("sources", {})
    database.mutate_document(connection, "checkpoint_controls", lambda doc: {
        key: value for key, value in doc.items() if key == "revision" or sources.get(key, {}).get("mode") == "real"})
    database.mutate_document(connection, "checkpoint_synthetic", lambda doc: {"sources": {}})
    database.mutate_document(connection, "status_synthetic", lambda doc: {"status": "idle", "landing_count": 0})


def _save_clean_history(connection, objects, lake, config, snapshot, operation_id, sensor_type=None):
    clean = prune_publication(snapshot, sensor_type)
    if clean is None:
        return
    old, new = snapshot["version"], clean["version"]
    if not VERSION.fullmatch(old):
        raise ValueError("Invalid publication identity during synthetic reset")
    lake.put("gold", [{"id": new, **clean}])
    database.publish(connection, clean)
    objects.put_object(config["namespace"], config["bucket"], HISTORY_PREFIX + new + ".json", encoded(clean), content_type="application/json")
    # The cleanup journal survives failure between any two independent stores.
    database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc,
        "replacements": {**doc.get("replacements", {}), old: new}})
    if sensor_type is None:
        _clean_controls(connection, _synthetic_ids(snapshot["evidence"]), _synthetic_ids(snapshot["incidents"]))
    lake.delete_publication(old)
    if sensor_type is None:
        database.replace_synthetic_publication(connection, operation_id, old, new)
    else:
        database.replace_sensor_publication(connection, operation_id, sensor_type, old, new)
    _delete_history_object(objects, config, snapshot)


def _delete_history_object(objects, config, snapshot):
    key = HISTORY_PREFIX + snapshot["version"] + ".json"
    try:
        body, etag = object_body(objects, config, config["bucket"], key)
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            return
        raise
    expected = {name: value for name, value in snapshot.items() if name != "id"}
    if json.loads(body) != expected:
        raise ValueError("Publication object changed during synthetic reset")
    delete_object(objects, config, config["bucket"], key, etag)


def clean_history(connection, objects, lake, config, operation_id, sensor_type=None):
    # Each store is scanned: interrupted publication can exist in only one or two stores.
    for snapshot in lake.publications():
        _save_clean_history(connection, objects, lake, config, snapshot, operation_id, sensor_type)
    for snapshot in database.publications(connection):
        _save_clean_history(connection, objects, lake, config, snapshot, operation_id, sensor_type)
    for key in object_keys(objects, config, config["bucket"], HISTORY_PREFIX):
        if not re.fullmatch(re.escape(HISTORY_PREFIX) + r"gold-[a-f0-9]{32}\.json", key):
            raise ValueError("Invalid publication object during synthetic reset")
        body, _etag = object_body(objects, config, config["bucket"], key)
        snapshot = json.loads(body)
        if key != HISTORY_PREFIX + snapshot["version"] + ".json":
            raise ValueError("Publication object identity mismatch")
        _save_clean_history(connection, objects, lake, config, snapshot, operation_id, sensor_type)


def execute(connection, objects, lake, config, now, command, publish_snapshot):
    """Retry each durable step; caller guarantees streams have stopped and joined."""
    if command.get("sensor_type") is not None:
        raise ValueError("The social reset cannot delete sensor data")
    operation_id = str(UUID(command["operation_id"]))
    try:
        if config["landing_prefix"] != "01_landing/prisma/raw/":
            raise ValueError("Synthetic reset requires the verified project Landing prefix")
        command = database.mutate_document(connection, "checkpoint_reset", lambda doc: {
            **doc, "reset_at": doc.get("reset_at", now), "stage": "draining", "error": None})
        # A stopped query may still have an uncommitted batch. Drain with the SAME checkpoints,
        # while all original files exist, before deleting anything. No classification is run.
        lake.consume(config)
        removed_posts = set(lake.synthetic_ids())
        registry = database.read_document(connection, "event_registry").get("items", [])
        removed_events = {item["id"] for item in registry if item.get("mode") in SYNTHETIC_MODES}
        _clean_controls(connection, removed_posts, removed_events)
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "stage": "landing"})
        landing_count = clean_landing(objects, config)
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "stage": "delta"})
        counts = lake.delete_synthetic()
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "stage": "database"})
        counts.update(posts=database.purge_synthetic_posts(connection, operation_id), landing_files=landing_count)
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "stage": "publishing", "counts": {
            key: doc.get("counts", {}).get(key, 0) + value for key, value in counts.items()}})
        sources = database.read_document(connection, "configuration").get("sources", {})
        reviews = database.read_document(connection, "reviews").get("items", {})
        state = simulation_state(database.read_document(connection, "simulation"), command["reset_at"])
        snapshot = publish_snapshot(connection, objects, lake, config, lake.visible(None, command["reset_at"]),
            reviews, state, command["reset_at"], rules={name: {**default_source(name), **sources.get(name, {})} for name in PLATFORMS})
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "stage": "history"})
        clean_history(connection, objects, lake, config, operation_id)
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "status": "completed", "stage": "completed",
            "version": snapshot["version"], "error": None, "completed_at": utc_text(now),
            "completed_ids": list(dict.fromkeys([*doc.get("completed_ids", []), operation_id]))})
        return snapshot
    except Exception as exc:
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "status": "error",
            "error": type(exc).__name__})
        raise RuntimeError("Synthetic reset is incomplete; retry the same operation") from None
