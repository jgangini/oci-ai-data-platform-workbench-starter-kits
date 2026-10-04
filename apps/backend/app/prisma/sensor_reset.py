"""Selected-family cleanup, coordinated with the existing publication reset journal."""
import json
import time
from datetime import datetime
from uuid import UUID

from . import database, sensor_capture, sensors, synthetic_reset
from .core import utc_text


def family(value):
    if value not in sensors.SENSOR_TYPES:
        raise ValueError("Unknown sensor type")
    return value


def state_for(state, kind):
    family(kind)
    return {key: state[key] for key in ("operation_id", "sensor_type", "status", "stage", "counts", "version", "error")
            if key in state} if state.get("sensor_type") == kind else {}


def annotated(config, state):
    return {**config, "configs": [{**item, "reset": state_for(state, item["sensor_type"])} for item in config["configs"]]}


def guard(state, kind=None):
    from fastapi import HTTPException
    if state.get("sensor_type") and state.get("status") in {"pending", "error"} and kind in (None, state["sensor_type"]):
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
    for row in rows:
        if row.get("sensor_type") != kind:
            raise ValueError("Sensor file escaped its selected family")
        sensors.validate_record(row)


def _clear_controls(checkpoint, status, kind, now):
    # Keep an anchor guard: resuming in the same second must not reuse a deleted filename.
    previous = checkpoint.get("by_type", {}).get(kind, {})
    anchor = max(int(now), previous.get("anchor", -1), (previous.get("pending") or {}).get("anchor", -1))
    checkpoints = {**checkpoint.get("by_type", {}), kind: {"anchor": anchor, "pending": None, "next_due": 0}}
    state = sensor_capture._family_change(status, kind, {"last_run_at": None, "next_due": None,
                                                        "last_received_count": None, "last_error": None})
    return {**checkpoint, "by_type": checkpoints}, state


def local(store, kind, operation_id):
    with store.connection() as db:
        saved = store._get(db, "synthetic_reset", {})
    state = command(saved, kind, operation_id)
    if state["status"] == "completed":
        return state_for(state, kind)
    try:
        if not state.get("ready"):
            sensor_capture.local_control(store, False, kind)
            state.update(ready=True, reset_at=store.clock(), stage="landing")
        state.update(status="pending", error=None)
        with store.connection() as db:
            store._put(db, "synthetic_reset", state)
        directory = store.path.parent / "prisma-landing" / "sensors" / kind
        if directory.resolve() != (store.path.parent.resolve() / "prisma-landing" / "sensors" / kind):
            raise ValueError("Sensor Landing escaped its selected family")
        for path in directory.glob("*.txt"):
            if path.is_symlink() or not path.is_file():
                raise ValueError("Invalid sensor Landing file")
            _validate_rows([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()], kind)
            path.unlink()
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [json.loads(row[0]) for row in db.execute("SELECT payload FROM sensor_events WHERE json_extract(payload,'$.sensor_type')=?", (kind,))]
            _validate_rows(rows, kind)
            db.execute("DELETE FROM sensor_events WHERE json_extract(payload,'$.sensor_type')=?", (kind,))
            checkpoint, status = _clear_controls(store._get(db, "checkpoint_sensors", {}), store._get(db, "status_sensors", {}), kind, state["reset_at"])
            store._put(db, "checkpoint_sensors", checkpoint)
            store._put(db, "status_sensors", status)
            state.update(stage="publishing", counts={"sensor_events": state["counts"].get("sensor_events", 0) + len(rows)})
            store._put(db, "synthetic_reset", state)
        state.update(status="completed", stage="completed", version=store.snapshot()["version"],
                     completed_ids=[*state.get("completed_ids", []), state["operation_id"]])
    except Exception:
        state.update(status="error", error="Sensor deletion is incomplete. Retry the same operation.")
    with store.connection() as db:
        store._put(db, "synthetic_reset", state)
    return state_for(state, kind)


def _ready(runtime):
    from fastapi import HTTPException
    config = runtime._doc("runtime")
    revision = config.get("pipeline_revision")
    if not revision or not config.get("sensor_job_key") or config.get("streaming_mode") != "persistent":
        raise HTTPException(501, "Update both AIDP workflows before deleting sensor data")
    for name in ("status_pipeline", "status_sensorstream"):
        status = runtime._doc(name)
        try:
            age = time.time() - datetime.fromisoformat(status["last_run_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, TypeError):
            age = float("inf")
        if (status.get("sensor_reset_version") != 1 or status.get("pipeline_revision") != revision or not 0 <= age <= 600
                or status.get("status") not in {"running", "ready", "pending", "needs_attention"}):
            raise HTTPException(501, "Wait for both updated AIDP workflows before deleting sensor data")


def cloud(runtime, kind, operation_id):
    current = runtime._doc("checkpoint_reset")
    state = command(current, kind, operation_id)
    if state["status"] == "completed":
        return state_for(state, kind)
    if not state.get("ready"):
        _ready(runtime)
    state = runtime._change("checkpoint_reset", lambda doc: command(doc, kind, operation_id))
    if state["status"] == "completed":
        return state_for(state, kind)
    try:
        if not state.get("ready"):
            config = sensors.configuration(runtime._doc("configuration").get("sensors"))
            sensor_capture._cloud_migrate(runtime, config)
            runtime._change("configuration", lambda doc: {**doc, "sensors": sensors.control_family(
                sensors.configuration(doc.get("sensors")), kind, False)})
            runtime._change("status_sensors", lambda doc: sensor_capture._paused(doc, kind))
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
    prefix = config["landing_prefix"] + "sensors/" + kind + "/"
    count = 0
    for key in synthetic_reset.object_keys(objects, config, config["landing_bucket"], prefix):
        if not key.endswith(".txt"):
            continue
        body, etag = synthetic_reset.object_body(objects, config, config["landing_bucket"], key)
        _validate_rows([json.loads(line) for line in body.decode("utf-8").splitlines()], kind)
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
        database.mutate_document(connection, "checkpoint_sensors", lambda doc: sensor_capture._family_change(doc, kind, checkpoint["by_type"][kind]))
        database.mutate_document(connection, "status_sensors", lambda doc: sensor_capture._family_change(doc, kind, status["by_type"][kind]))
        database.mutate_document(connection, "checkpoint_reset", lambda doc: {**doc, "stage": "publishing", "counts": {
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
