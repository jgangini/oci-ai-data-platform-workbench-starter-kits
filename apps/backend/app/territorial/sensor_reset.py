"""Synthetic sensor cleanup, coordinated with the existing publication reset journal."""
import json
import os
import time
from datetime import datetime
from tempfile import NamedTemporaryFile
from uuid import UUID

from . import database, sensor_capture, sensors, synthetic_reset
from .core import utc_text


def family(value):
    if value != "all" and value not in sensors.SENSOR_TYPES:
        raise ValueError("Unknown sensor type")
    return value


def state_for(state, kind):
    family(kind)
    scope = state.get("sensor_type")
    return {key: state[key] for key in ("operation_id", "sensor_type", "status", "stage", "counts", "version", "error",
                                      "replacements", "revision", "completed_at")
            if key in state} if scope in (*sensors.SENSOR_TYPES, "all") and (kind == "all" or scope in (kind, "all")) else {}


def annotated(config, state):
    return {**config, "reset": state_for(state, "all"),
            "configs": [{**item, "reset": state_for(state, item["sensor_type"])} for item in config["configs"]]}


def guard(state, kind=None):
    from fastapi import HTTPException
    if state.get("sensor_type") and state.get("status") in {"pending", "error"} and (state["sensor_type"] == "all" or kind in (None, state["sensor_type"])):
        raise HTTPException(409, "Finish or retry this sensor type's delete operation before changing its capture")


def command(state, kind, operation_id):
    from fastapi import HTTPException
    family(kind)
    operation_id = str(UUID(operation_id))
    synthetic_reset.check_scope(state, operation_id, kind)
    if state.get("operation_id") == operation_id:
        return state
    if operation_id in state.get("completed_ids", []):
        return {"operation_id": operation_id, "sensor_type": kind, "status": "completed", "stage": "completed"}
    if state.get("status") in {"pending", "error"}:
        raise HTTPException(409, "Another delete operation is unfinished; retry its original scope and operation ID")
    return {"operation_id": operation_id, "sensor_type": kind, "status": "pending", "stage": "preparing", "ready": False,
            "counts": {}, "completed_ids": state.get("completed_ids", []), "operation_scopes": {
                **state.get("operation_scopes", {}), operation_id: kind}}


def _validate_rows(rows, kind):
    selected = []
    for row in rows:
        if row.get("sensor_type") not in sensors.SENSOR_TYPES or kind not in ("all", row["sensor_type"]):
            raise ValueError("Sensor file escaped its selected family")
        if row.get("mode") == "real" and row.get("is_simulated") is False:
            continue
        sensors.validate_record({**row, "mode": "Synthetic"} if row.get("mode") == "simulation" else row)
        selected.append(row)
    return selected


def _remaining_body(body, kind):
    lines = body.splitlines(keepends=True)
    rows = [json.loads(line) for line in lines]
    _validate_rows(rows, kind)
    return b"".join(line for line, row in zip(lines, rows) if row.get("is_simulated") is False)


def _clear_controls(checkpoint, status, kind, now):
    # Keep an anchor guard: resuming in the same second must not reuse a deleted filename.
    for selected in sensors.SENSOR_TYPES if kind == "all" else (kind,):
        previous = checkpoint.get("by_type", {}).get(selected, {})
        anchor = max(int(now), previous.get("anchor", -1), (previous.get("pending") or {}).get("anchor", -1))
        checkpoint = sensor_capture._family_change(checkpoint, selected, {"anchor": anchor, "pending": None, "next_due": 0})
        status = sensor_capture._family_change(status, selected, {"last_run_at": None, "next_due": None,
                                                                "last_received_count": None, "last_error": None})
    return checkpoint, status


def _local_landing(store, kind):
    count = 0
    for selected in sensors.SENSOR_TYPES if kind == "all" else (kind,):
        directory = store.path.parent / "prisma-landing" / "sensors" / selected
        if directory.resolve() != (store.path.parent.resolve() / "prisma-landing" / "sensors" / selected):
            raise ValueError("Sensor Landing escaped its selected family")
        for path in directory.glob("*.txt"):
            if path.is_symlink() or not path.is_file():
                raise ValueError("Invalid sensor Landing file")
            body = path.read_bytes()
            retained = _remaining_body(body, selected)
            if retained == body:
                continue
            if retained:
                with NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as output:
                    temporary = output.name
                    output.write(retained)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            else:
                path.unlink()
            count += 1
    return count


def local(store, kind, operation_id):
    with store.connection() as db:
        saved = store._get(db, "synthetic_reset", {})
    state = command(saved, kind, operation_id)
    if state["status"] == "completed":
        return state_for(state, kind)
    try:
        if not state.get("ready"):
            for selected in sensors.SENSOR_TYPES if kind == "all" else (kind,):
                sensor_capture.local_control(store, False, selected)
            state.update(ready=True, reset_at=store.clock(), stage="landing")
        state.update(status="pending", error=None)
        with store.connection() as db:
            store._put(db, "synthetic_reset", state)
        count = _local_landing(store, kind)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            families = tuple(sensors.SENSOR_TYPES) if kind == "all" else (kind,)
            rows = [json.loads(row[0]) for row in db.execute("SELECT payload FROM sensor_events WHERE json_extract(payload,'$.sensor_type') IN ("
                    + ",".join("?" for _ in families) + ")", families)]
            selected = _validate_rows(rows, kind)
            db.executemany("DELETE FROM sensor_events WHERE event_id=?", [(row["event_id"],) for row in selected])
            checkpoint, status = _clear_controls(store._get(db, "checkpoint_sensors", {}), store._get(db, "status_sensors", {}), kind, state["reset_at"])
            store._put(db, "checkpoint_sensors", checkpoint)
            store._put(db, "status_sensors", status)
            state.update(stage="publishing", counts={**state["counts"], "sensor_events": state["counts"].get("sensor_events", 0) + len(selected),
                "landing_files": state["counts"].get("landing_files", 0) + count})
            store._put(db, "synthetic_reset", state)
        state.update(status="completed", stage="completed", version=store.snapshot()["version"], completed_at=utc_text(store.clock()),
                     completed_ids=[*state.get("completed_ids", []), state["operation_id"]])
    except Exception:
        state.update(status="error", error="Sensor deletion is incomplete. Retry the same operation.")
    with store.connection() as db:
        store._put(db, "synthetic_reset", state)
    return state_for(state, kind)


def _ready(runtime, kind):
    from fastapi import HTTPException
    config = runtime._doc("runtime")
    revision = config.get("pipeline_revision")
    required = 2 if kind == "all" else 1
    if not revision or not config.get("sensor_job_key") or config.get("streaming_mode") != "persistent":
        raise HTTPException(501, "Update both AIDP workflows before deleting sensor data")
    for name in ("status_pipeline", "status_sensorstream"):
        status = runtime._doc(name)
        try:
            age = time.time() - datetime.fromisoformat(status["last_run_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, TypeError):
            age = float("inf")
        if (type(status.get("sensor_reset_version")) is not int or status["sensor_reset_version"] < required
                or status.get("pipeline_revision") != revision or not 0 <= age <= 600
                or status.get("status") not in {"running", "ready", "pending", "needs_attention"}):
            raise HTTPException(501, "Wait for both updated AIDP workflows before deleting sensor data")


def cloud(runtime, kind, operation_id):
    current = runtime._doc("checkpoint_reset")
    state = command(current, kind, operation_id)
    if state["status"] == "completed":
        return state_for(state, kind)
    if not state.get("ready"):
        _ready(runtime, kind)
    state = runtime._change("checkpoint_reset", lambda doc: command(doc, kind, operation_id))
    if state["status"] == "completed":
        return state_for(state, kind)
    try:
        if not state.get("ready"):
            config = sensors.configuration(runtime._doc("configuration").get("sensors"))
            sensor_capture._cloud_migrate(runtime, config)
            def pause(doc):
                current = sensors.configuration(doc.get("sensors"))
                for selected in sensors.SENSOR_TYPES if kind == "all" else (kind,):
                    current = sensors.control_family(current, selected, False)
                return {**doc, "sensors": current}
            runtime._change("configuration", pause)
            runtime._change("status_sensors", lambda doc: sensor_capture._paused(doc, None if kind == "all" else kind))
            runtime._change("checkpoint_reset", lambda doc: {**doc, "ready": True, "reset_at": time.time(),
                "stage": "waiting_for_sensor_stream", "status": "pending", "error": None})
        elif state["status"] == "error":
            runtime._change("checkpoint_reset", lambda doc: {**doc, "status": "pending", "error": None})
        runtime._wake(operation_id)
    except Exception:
        runtime._change("checkpoint_reset", lambda doc: {**doc, "status": "error",
            "error": "Sensor deletion could not be scheduled. Retry this operation."}
            if doc.get("operation_id") == operation_id and doc.get("status") != "completed" else doc)
    return state_for(runtime._doc("checkpoint_reset"), kind)


def clean_landing(objects, config, kind):
    family(kind)
    if config["landing_prefix"] != "01_landing/prisma/raw/":
        raise ValueError("Invalid sensor Landing root")
    count = 0
    for selected in sensors.SENSOR_TYPES if kind == "all" else (kind,):
        prefix = config["landing_prefix"] + "sensors/" + selected + "/"
        for key in synthetic_reset.object_keys(objects, config, config["landing_bucket"], prefix):
            if not key.endswith(".txt"):
                continue
            body, etag = synthetic_reset.object_body(objects, config, config["landing_bucket"], key)
            retained = _remaining_body(body, selected)
            if retained == body:
                continue
            if not etag:
                raise ValueError("Sensor Landing cleanup requires its exact object version")
            if retained:
                # Keep the consumed path; a new filename would replay retained real readings.
                objects.put_object(config["namespace"], config["landing_bucket"], key, retained,
                                   if_match=etag, content_type="text/plain; charset=utf-8")
            else:
                synthetic_reset.delete_object(objects, config, config["landing_bucket"], key, etag)
            count += 1
    return count


def execute(connection, objects, lake, config, now, command, publish_snapshot):
    kind = family(command["sensor_type"])
    operation = str(UUID(command["operation_id"]))
    if command.get("sensor_drained_operation_id") != operation:
        raise RuntimeError("Sensor ingestion has not drained this delete operation")
    try:
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "stage": "landing"})
        count = clean_landing(objects, config, kind)
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "stage": "delta"})
        removed = lake.sensors.delete_family(kind)
        checkpoint, status = _clear_controls(database.read_document(connection, "checkpoint_sensors"),
            database.read_document(connection, "status_sensors"), kind, command["reset_at"])
        selected = sensors.SENSOR_TYPES if kind == "all" else (kind,)
        database.mutate_document(connection, "checkpoint_sensors", lambda doc: {**doc, "by_type": {
            **doc.get("by_type", {}), **{key: checkpoint["by_type"][key] for key in selected}}})
        database.mutate_document(connection, "status_sensors", lambda doc: {**doc, "by_type": {
            **doc.get("by_type", {}), **{key: status["by_type"][key] for key in selected}}})
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "stage": "publishing", "counts": {
            **doc.get("counts", {}),
            "landing_files": doc.get("counts", {}).get("landing_files", 0) + count,
            "sensor_events": doc.get("counts", {}).get("sensor_events", 0) + removed}})
        from .core import PLATFORMS, default_source, simulation_state
        sources = database.read_document(connection, "configuration").get("sources", {})
        simulation = simulation_state(database.read_document(connection, "simulation"), now)
        snapshot = publish_snapshot(connection, objects, lake, config, lake.visible(simulation.get("run_id"), now),
            database.read_document(connection, "reviews").get("items", {}),
            simulation, now,
            rules={name: {**default_source(name), **sources.get(name, {})} for name in PLATFORMS})
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "stage": "history"})
        synthetic_reset.clean_history(connection, objects, lake, config, operation, sensor_type=kind)
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "status": "completed", "stage": "completed",
            "version": snapshot["version"], "error": None, "completed_at": utc_text(now),
            "completed_ids": list(dict.fromkeys([*doc.get("completed_ids", []), operation]))})
        return snapshot
    except Exception as exc:
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "status": "error", "error": type(exc).__name__}
            if doc.get("operation_id") == operation and doc.get("status") != "completed" else doc)
        raise RuntimeError("Sensor deletion is incomplete; retry the same operation") from None
