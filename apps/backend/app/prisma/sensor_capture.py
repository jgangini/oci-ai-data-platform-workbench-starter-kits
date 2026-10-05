"""TXT delivery for simulated sensors; local fixtures and native AIDP stay explicit."""
from __future__ import annotations

import json
import os
import time
from tempfile import NamedTemporaryFile

from . import capture, sensors
from .core import utc_text


def local_configuration(store):
    with store.connection() as db:
        return _view(store._get(db, "configuration_sensors", {}), store._get(db, "status_sensors", {}),
            store._get(db, "sensor_schedule", {}), store._get(db, "checkpoint_sensors", {}), store.clock())


def _view(config, status, schedule, checkpoint, now):
    result = {**sensors.configuration(config, {key: value for key, value in status.items() if key != "by_type"}),
              "configs": list(sensors.family_configs(config, status).values()), "sensor_schedule": capture.schedule(schedule)}
    if result["sensor_schedule"]["start_at"]:
        for family in result["configs"]:
            cursor = _family_checkpoint(family, checkpoint, family["sensor_type"]) if "by_type" in result else checkpoint
            due = cursor["pending"]["anchor"] if cursor.get("pending") else capture.schedule_at(schedule, now, cursor.get("anchor"))
            family.update(interval_minutes=result["sensor_schedule"]["interval_minutes"], next_due=utc_text(due) if family["capture_running"] else None)
        result["interval_minutes"] = result["sensor_schedule"]["interval_minutes"]
        result["next_due"] = min((family["next_due"] for family in result["configs"] if family["next_due"]), default=None)
    return result


def _updated(current, values, sensor_type):
    if sensor_type is not None:
        return sensors.update_family(current, sensor_type, values)
    updated = sensors.update_configuration(current, values)
    if "by_type" in current:
        configs = sensors.family_configs(current)
        counts = sensors.allocation(updated["sensor_count"], updated["families"])
        for kind, config in configs.items():
            configs[kind] = {**config, "config_version": config["config_version"] + 1,
                "capture_running": config["capture_running"] and kind in counts,
                **({"sensor_count": counts[kind], "interval_minutes": updated["interval_minutes"]} if kind in counts else {})}
        updated = sensors.family_document(updated, configs)
    return updated


def _controlled(current, running, sensor_type):
    if sensor_type is not None:
        return sensors.control_family(current, sensor_type, running)
    if "by_type" not in current:
        return {**current, "capture_running": running}
    configs = sensors.family_configs(current)
    for kind, config in configs.items():
        active = running and kind in current["families"]
        configs[kind] = {**config, "capture_running": active,
                         "config_version": config["config_version"] + int(active != config["capture_running"])}
    return sensors.family_document({**current, "config_version": current["config_version"] + 1}, configs)


def _migration(config, checkpoint, status):
    checkpoints = {}
    pending = checkpoint.get("pending")
    for kind in sensors.SENSOR_TYPES:
        checkpoints[kind] = {key: checkpoint[key] for key in ("anchor", "next_due") if key in checkpoint} if kind in config["families"] else {}
        if pending and kind in pending["families"]:
            # Reconstruct the original full shuffle, then deliver just this file.
            checkpoints[kind]["pending"] = {**pending, "sensor_type": kind}
    states = {kind: {key: value[key] for key in ("last_run_at", "next_due", "last_received_count", "last_error")}
              for kind, value in sensors.family_configs(config, status).items()}
    return {**checkpoint, "by_type": checkpoints}, {**status, "by_type": states}


def _local_migrate(store, db, current):
    if "by_type" not in current:
        checkpoint, status = _migration(current, store._get(db, "checkpoint_sensors", {}), store._get(db, "status_sensors", {}))
        store._put(db, "checkpoint_sensors", checkpoint)
        store._put(db, "status_sensors", status)


def _selected(view, sensor_type):
    return next(config for config in view["configs"] if config["sensor_type"] == sensor_type) if sensor_type else view


def local_save(store, values, sensor_type=None):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current = sensors.configuration(store._get(db, "configuration_sensors", {}))
        result = _updated(current, values, sensor_type)
        if sensor_type is not None:
            _local_migrate(store, db, current)
            checkpoint, status = _retimed(result, sensor_type,
                store._get(db, "checkpoint_sensors", {}), store._get(db, "status_sensors", {}),
                store._get(db, "sensor_schedule", {}), store.clock())
            store._put(db, "checkpoint_sensors", checkpoint)
            store._put(db, "status_sensors", status)
        store._put(db, "configuration_sensors", result)
    return _selected(local_configuration(store), sensor_type)


def local_control(store, running, sensor_type=None):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current = sensors.configuration(store._get(db, "configuration_sensors", {}))
        updated = _controlled(current, running, sensor_type)
        if sensor_type is not None:
            _local_migrate(store, db, current)
        store._put(db, "configuration_sensors", updated)
        if not running:
            status = store._get(db, "status_sensors", {})
            store._put(db, "status_sensors", _paused(status, sensor_type))
    if running:
        local_tick(store, force=True, sensor_type=sensor_type)
        with store.connection() as db:
            status = _resumed(store._get(db, "status_sensors", {}), store._get(db, "checkpoint_sensors", {}), updated, sensor_type)
            store._put(db, "status_sensors", status)
    return _selected(local_configuration(store), sensor_type)


def _paused(status, sensor_type):
    if "by_type" not in status:
        return {**status, "next_due": None}
    return {**status, "by_type": {kind: {**value, "next_due": None} if sensor_type in (None, kind) else value
                                  for kind, value in status["by_type"].items()}}


def _resumed(status, checkpoint, config, sensor_type):
    if "by_type" not in config:
        due = checkpoint.get("next_due")
        return {**status, "next_due": utc_text(due) if due else None}
    for kind, value in config["by_type"].items():
        if sensor_type in (None, kind) and value["capture_running"]:
            due = checkpoint.get("by_type", {}).get(kind, {}).get("next_due")
            status = _family_change(status, kind, {"next_due": utc_text(due) if due else None})
    return status


def _retimed(current, kind, checkpoint, status, schedule, now):
    after = current["by_type"][kind]
    cursor = checkpoint.get("by_type", {}).get(kind, {})
    if "anchor" in cursor and cursor.get("next_due") != 0 and not cursor.get("pending"):
        due = capture.schedule_at(schedule, now, cursor["anchor"])
        if due is None:
            due = cursor["anchor"] + after["interval_minutes"] * 60
        checkpoint = _family_change(checkpoint, kind, {"next_due": due})
        status = _family_change(status, kind, {"next_due": utc_text(due) if after["capture_running"] else None})
    return checkpoint, status


def _batch(config, now, checkpoint, force, locations, schedule=None):
    if not config["capture_running"]:
        return None
    if checkpoint.get("pending"):
        return checkpoint["pending"]
    scheduled = capture.schedule_at(schedule, now, checkpoint.get("anchor"))
    if scheduled is not None:
        if scheduled > now:
            return None
    elif not force and checkpoint.get("next_due", 0) > now:
        return None
    anchor = int(now if scheduled is None else scheduled)
    if anchor <= checkpoint.get("anchor", -1):
        return None
    # A retry must deliver the same immutable event bytes even if a station is moved meanwhile.
    return {"anchor": anchor, "locations": locations, **{key: config[key] for key in ("interval_minutes", "sensor_count", "families")},
            **({"interval_minutes": capture.schedule(schedule)["interval_minutes"]} if scheduled is not None else {})}


def _family_ticks(config, sensor_type, run):
    total, failure = 0, None
    for kind in ([sensor_type] if sensor_type else sensors.SENSOR_TYPES):
        if not config["by_type"][kind]["capture_running"]:
            continue
        try:
            total += run(kind)
        except Exception as exc:
            failure = failure or exc
    if failure:
        raise failure
    return total


def _family_change(document, kind, values):
    return {**document, "by_type": {**document.get("by_type", {}),
            kind: {**document.get("by_type", {}).get(kind, {}), **values}}}


def _family_checkpoint(config, checkpoints, kind):
    cursor = checkpoints.get("by_type", {}).get(kind, {})
    if "anchor" in cursor and cursor.get("next_due") != 0 and not cursor.get("pending"):
        # Config commits before cloud checkpoint writes; recover its current interval.
        cursor = {**cursor, "next_due": cursor["anchor"] + config["interval_minutes"] * 60}
    # A formerly excluded family only inherits the same-second overwrite guard.
    return {"anchor": checkpoints.get("anchor", -1), **cursor}


def _family_rows(batch, kind):
    rows = sensors.generate_batch(batch["anchor"], sensor_count=batch["sensor_count"], families=batch["families"])
    return sensors.apply_locations([row for row in rows if row["sensor_type"] == kind], batch.get("locations", {}))


def _local_deliver(store, rows):
    for relative, content in sensors.text_files(rows).items():
        target = store.path.parent / "prisma-landing" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as output:
            temporary = output.name
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        # The fixture adapter ingests the delivered bytes, never a second dataset.
        delivered = [sensors.validate_record(json.loads(line)) for line in target.read_text(encoding="utf-8").splitlines()]
        with store.connection() as db:
            db.executemany("INSERT OR IGNORE INTO sensor_events VALUES (?,?,?,?)", [
                (row["event_id"], row["sensor_id"], row["observed_at"], json.dumps(row, ensure_ascii=False)) for row in delivered])


def _local_family_tick(store, kind, now, force, schedule):
    with store.connection() as db:
        config = sensors.family_configs(store._get(db, "configuration_sensors", {}))[kind]
        checkpoints = store._get(db, "checkpoint_sensors", {})
        checkpoint = _family_checkpoint(config, checkpoints, kind)
        locations = store._get(db, "sensor_locations", {})
    batch = _batch({**config, "families": [kind]}, now, checkpoint, force, locations, schedule)
    if not batch:
        return 0
    try:
        with store.connection() as db:
            store._put(db, "checkpoint_sensors", _family_change(store._get(db, "checkpoint_sensors", {}), kind, {"pending": batch}))
        rows = _family_rows(batch, kind)
        _local_deliver(store, rows)
        due = capture.schedule_at(schedule, now, batch["anchor"]) if schedule["start_at"] else batch["anchor"] + config["interval_minutes"] * 60
        with store.connection() as db:
            store._put(db, "checkpoint_sensors", _family_change(store._get(db, "checkpoint_sensors", {}), kind,
                {"anchor": batch["anchor"], "next_due": due, "pending": None}))
            store._put(db, "status_sensors", _family_change(store._get(db, "status_sensors", {}), kind,
                {"last_run_at": utc_text(now), "next_due": utc_text(due), "last_received_count": len(rows), "last_error": None}))
        return len(rows)
    except Exception as exc:
        with store.connection() as db:
            store._put(db, "status_sensors", _family_change(store._get(db, "status_sensors", {}), kind, {"last_error": type(exc).__name__}))
        raise


def local_tick(store, force=False, sensor_type=None):
    config, now = local_configuration(store), store.clock()
    with store.connection() as db:
        config = _capture_config(config, store._get(db, "synthetic_reset", {}))
    if "by_type" in config:
        return _family_ticks(config, sensor_type, lambda kind: _local_family_tick(store, kind, now, force, config["sensor_schedule"]))
    with store.connection() as db:
        checkpoint = store._get(db, "checkpoint_sensors", {})
        locations = store._get(db, "sensor_locations", {})
    batch = _batch(config, now, checkpoint, force, locations, config["sensor_schedule"])
    if not batch:
        return 0
    try:
        with store.connection() as db:
            store._put(db, "checkpoint_sensors", {**checkpoint, "pending": batch})
        rows = sensors.apply_locations(sensors.generate_batch(batch["anchor"], sensor_count=batch["sensor_count"], families=batch["families"]), batch.get("locations", {}))
        _local_deliver(store, rows)
        anchor = batch["anchor"]
        due = capture.schedule_at(config["sensor_schedule"], now, anchor) if config["sensor_schedule"]["start_at"] else anchor + batch["interval_minutes"] * 60
        with store.connection() as db:
            store._put(db, "checkpoint_sensors", {"anchor": anchor, "next_due": due})
            store._put(db, "status_sensors", {"last_run_at": utc_text(now), "next_due": utc_text(due),
                                             "last_received_count": len(rows), "last_error": None})
        return len(rows)
    except Exception as exc:
        with store.connection() as db:
            store._put(db, "status_sensors", {**store._get(db, "status_sensors", {}), "last_error": type(exc).__name__})
        raise


def local_latest(store):
    with store.connection() as db:
        records = db.execute("""SELECT payload FROM (SELECT payload,sensor_id,observed_at FROM (
            SELECT payload, sensor_id, observed_at, ROW_NUMBER() OVER (PARTITION BY sensor_id ORDER BY julianday(observed_at) DESC,event_id DESC) rank
            FROM sensor_events WHERE julianday(observed_at) >= julianday(?) AND julianday(observed_at) <= julianday(?)) WHERE rank=1
            ORDER BY julianday(observed_at) DESC,sensor_id LIMIT 5000) ORDER BY sensor_id""",
            (utc_text(store.clock() - 86400), utc_text(store.clock()))).fetchall()
        locations = store._get(db, "sensor_locations", {})
    return sensors.apply_locations([{**(payload := json.loads(row[0])), "id": payload["event_id"]} for row in records], locations)


def local_location(store, sensor_id, values):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT payload FROM sensor_events WHERE sensor_id=? ORDER BY julianday(observed_at) DESC,event_id DESC LIMIT 1", (sensor_id,)).fetchone()
        if row is None:
            raise KeyError(sensor_id)
        locations = sensors.update_location(json.loads(row[0]), store._get(db, "sensor_locations", {}), values, store.clock())
        store._put(db, "sensor_locations", locations)
    return {"sensor_id": sensor_id, **locations[sensor_id], "location_saved": True, "location_pending_publication": False}


def cloud_configuration(runtime):
    config = runtime._doc("configuration")
    return _view(config.get("sensors"), runtime._doc("status_sensors"), config.get("sensor_schedule"),
        runtime._doc("checkpoint_sensors"), time.time())


def _cloud_migrate(runtime, current):
    if "by_type" not in current:
        # Config is the commit barrier. Rebuild from current legacy fields after
        # an interrupted migration; uncommitted by_type copies are not authority.
        checkpoint, status = _migration(current, runtime._doc("checkpoint_sensors"),
                                        {key: value for key, value in runtime._doc("status_sensors").items() if key != "by_type"})
        runtime._change("checkpoint_sensors", lambda _: checkpoint)
        runtime._change("status_sensors", lambda _: status)


def cloud_save(runtime, values, sensor_type=None):
    current = sensors.configuration(runtime._doc("configuration").get("sensors"))
    _updated(current, values, sensor_type)
    if sensor_type is not None:
        _cloud_migrate(runtime, current)
    def change(document):
        updated = _updated(sensors.configuration(document.get("sensors")), values, sensor_type)
        return {**document, "sensors": updated}
    result = runtime._change("configuration", change)
    if sensor_type is not None:
        old_checkpoint, old_status = runtime._doc("checkpoint_sensors"), runtime._doc("status_sensors")
        checkpoint, status = _retimed(result["sensors"], sensor_type, old_checkpoint, old_status,
            result.get("sensor_schedule"), time.time())
        if checkpoint != old_checkpoint:
            runtime._change("checkpoint_sensors", lambda doc: _family_change(doc, sensor_type,
                {"next_due": checkpoint["by_type"][sensor_type]["next_due"]}))
        if status != old_status:
            runtime._change("status_sensors", lambda doc: _family_change(doc, sensor_type,
                {"next_due": status["by_type"][sensor_type]["next_due"]}))
    return _selected(cloud_configuration(runtime), sensor_type)


def cloud_control(runtime, running, sensor_type=None):
    current = sensors.configuration(runtime._doc("configuration").get("sensors"))
    _controlled(current, running, sensor_type)
    if sensor_type is not None:
        _cloud_migrate(runtime, current)
    runtime._change("configuration", lambda doc: {**doc, "sensors": {
        **_controlled(sensors.configuration(doc.get("sensors")), running, sensor_type)}})
    if running:
        cloud_tick(runtime, force=True, sensor_type=sensor_type)
        config, checkpoint = runtime._doc("configuration").get("sensors", {}), runtime._doc("checkpoint_sensors")
        runtime._change("status_sensors", lambda doc: _resumed(doc, checkpoint, config, sensor_type))
    else:
        runtime._change("status_sensors", lambda doc: _paused(doc, sensor_type))
    runtime._wake("sensors-control-" + str(time.time_ns()))
    return _selected(cloud_configuration(runtime), sensor_type)


def _cloud_deliver(runtime, rows):
    location = runtime._doc("runtime")
    client = runtime.aidp_factory()
    prefix = location.get("landing_prefix", "01_landing/prisma/raw/")
    if prefix != "01_landing/prisma/raw/":
        raise ValueError("Invalid sensor Landing prefix")
    for relative, content in sensors.text_files(rows).items():
        client.object_storage.put_object(location["namespace"], location.get("landing_bucket") or location["bucket"], prefix + relative,
                                        content, content_type="text/plain; charset=utf-8")


def _cloud_family_tick(runtime, kind, now, force, schedule):
    config = sensors.family_configs(runtime._doc("configuration").get("sensors"))[kind]
    checkpoints = runtime._doc("checkpoint_sensors")
    checkpoint = _family_checkpoint(config, checkpoints, kind)
    batch = _batch({**config, "families": [kind]}, now, checkpoint, force, runtime._doc("reviews").get("sensor_locations", {}), schedule)
    if not batch:
        return 0
    try:
        runtime._change("checkpoint_sensors", lambda doc: _family_change(doc, kind, {"pending": batch}))
        rows = _family_rows(batch, kind)
        _cloud_deliver(runtime, rows)
        due = capture.schedule_at(schedule, now, batch["anchor"]) if schedule["start_at"] else batch["anchor"] + config["interval_minutes"] * 60
        runtime._change("checkpoint_sensors", lambda doc: _family_change(doc, kind,
            {"anchor": batch["anchor"], "next_due": due, "pending": None}))
        runtime._change("status_sensors", lambda doc: _family_change(doc, kind,
            {"last_run_at": utc_text(now), "next_due": utc_text(due), "last_received_count": len(rows), "last_error": None}))
        return len(rows)
    except Exception as exc:
        runtime._change("status_sensors", lambda doc: _family_change(doc, kind, {"last_error": type(exc).__name__}))
        raise


def cloud_tick(runtime, force=False, sensor_type=None):
    config, now = cloud_configuration(runtime), time.time()
    config = _capture_config(config, runtime._doc("checkpoint_reset"))
    if "by_type" in config:
        return _family_ticks(config, sensor_type, lambda kind: _cloud_family_tick(runtime, kind, now, force, config["sensor_schedule"]))
    batch = _batch(config, now, runtime._doc("checkpoint_sensors"), force, runtime._doc("reviews").get("sensor_locations", {}), config["sensor_schedule"])
    if not batch:
        return 0
    try:
        runtime._change("checkpoint_sensors", lambda doc: {**doc, "pending": batch})
        rows = sensors.apply_locations(sensors.generate_batch(batch["anchor"], sensor_count=batch["sensor_count"], families=batch["families"]), batch.get("locations", {}))
        _cloud_deliver(runtime, rows)
        anchor = batch["anchor"]
        due = capture.schedule_at(config["sensor_schedule"], now, anchor) if config["sensor_schedule"]["start_at"] else anchor + batch["interval_minutes"] * 60
        runtime._change("checkpoint_sensors", lambda doc: {**doc, "anchor": anchor, "next_due": due, "pending": None})
        runtime._change("status_sensors", lambda doc: {**doc, "last_run_at": utc_text(now), "next_due": utc_text(due),
            "last_received_count": len(rows), "last_error": None})
        return len(rows)
    except Exception as exc:
        runtime._change("status_sensors", lambda doc: {**doc, "last_error": type(exc).__name__})
        raise


def _capture_config(config, reset):
    kind = reset.get("sensor_type")
    if kind in sensors.SENSOR_TYPES and reset.get("status") in {"pending", "error"}:
        if "by_type" not in config:
            return {**config, "capture_running": False}
        return {**config, "by_type": {**config["by_type"], kind: {**config["by_type"][kind], "capture_running": False}}}
    return config
