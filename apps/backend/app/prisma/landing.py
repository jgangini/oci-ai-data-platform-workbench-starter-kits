"""Immutable CSV envelopes for VM simulations and real capture; both carry identical event fields."""
import csv
import hashlib
import io
import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

from .core import SYNTHETIC_MODES, canonical_mode


def ensure_volumes(spark, config):
    """Verify bootstrap-provisioned volumes before any streaming or table writes."""
    catalog = config.get("catalog", "oci_medallion")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
        raise ValueError("Invalid PRISMA catalog")
    schema = catalog + ".prisma_ingest"
    uri = f"oci://{config['landing_bucket']}@{config['namespace']}/{config['landing_prefix']}"
    if "'" in uri or config["landing_volume_path"] != f"/Volumes/{catalog}/prisma_ingest/landing" or config["checkpoint_volume_path"] != f"/Volumes/{catalog}/prisma_ingest/checkpoints/bronze-v1":
        raise ValueError("Invalid PRISMA governed streaming path")
    for name, kind in (("landing", "EXTERNAL"), ("checkpoints", "MANAGED")):
        rows = spark.sql(f"DESCRIBE VOLUME {schema}.{name}").collect()
        if len(rows) != 1:
            raise RuntimeError("PRISMA volume description must contain exactly one row")
        details = rows[0].asDict()
        if any(details.get(field) != value for field, value in {"name": name, "catalog": catalog, "database": "prisma_ingest"}.items()):
            raise RuntimeError("PRISMA volume identity does not match the deployment")
        if str(details.get("volumeType", "")).upper() != kind or (kind == "EXTERNAL" and str(details.get("storageLocation", "")).rstrip("/") != uri.rstrip("/")):
            raise RuntimeError("PRISMA volume type or storage location does not match the deployment")


def stream_progress(queries):
    streams = [{"format": name, "query_id": str(query.id), "microbatches": len(query.recentProgress),
                "last_input_rows": (query.lastProgress or {}).get("numInputRows", 0)} for name, query in queries]
    return {"query_id": streams[-1]["query_id"], "streams": streams,
            "microbatches": sum(item["microbatches"] for item in streams),
            "last_input_rows": sum(item["last_input_rows"] for item in streams)}


def page(events, batch_key=None):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("id", "payload"))
    for event in events:
        event_id = f"{event['platform']}:{event['source_id']}"
        document = decode_record(event_id, json.dumps(event, ensure_ascii=False, allow_nan=False))
        writer.writerow((event_id, json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False)))
    body = output.getvalue().encode("utf-8")
    marker = json.dumps(batch_key, sort_keys=True, ensure_ascii=False, allow_nan=False).encode() if batch_key else b""
    platform = str(batch_key.get("platform", "")) if batch_key else ""
    if platform and not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", platform):
        raise ValueError("Invalid Landing platform")
    return (platform + "-" if platform else "") + hashlib.sha256(marker + b"\0" + body).hexdigest() + ".csv", body


def decode_record(event_id, payload):
    event = json.loads(payload)
    if (not isinstance(event, dict) or event.get("mode") not in ("real", *SYNTHETIC_MODES)
            or event_id != f"{event.get('platform')}:{event.get('source_id')}"):
        raise ValueError("Invalid PRISMA Landing envelope")
    simulated = event["mode"] in SYNTHETIC_MODES
    if "is_simulated" in event and (type(event["is_simulated"]) is not bool or event["is_simulated"] != simulated):
        raise ValueError("PRISMA simulation provenance conflicts with its mode")
    return {**event, "id": event_id, "mode": canonical_mode(event["mode"]), "is_simulated": simulated}


def records(body, suffix=".csv"):
    """Small local reader for the same envelope; Spark uses its native CSV/JSON file sources."""
    text = body.decode("utf-8")
    if suffix == ".ndjson":
        rows = (json.loads(line) for line in text.splitlines() if line.strip())
    elif suffix == ".csv":
        rows = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if rows.fieldnames != ["id", "payload"]:
            raise ValueError("Invalid PRISMA CSV header")
    else:
        raise ValueError("Unsupported PRISMA Landing format")
    events = []
    for row in rows:
        if set(row) != {"id", "payload"}:
            raise ValueError("Invalid PRISMA Landing columns")
        events.append(decode_record(row["id"], row["payload"]))
    return events


def write_objects(objects, config, events, batch_key=None):
    if not events and batch_key is None:
        return None
    name, body = page(events, batch_key)
    key = config["landing_prefix"] + name
    objects.put_object(config["namespace"], config["landing_bucket"], key, body, content_type="text/csv; charset=utf-8")
    return key


def write_file(directory, events, batch_key=None):
    if not events and batch_key is None:
        return None
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name, body = page(events, batch_key)
    destination = directory / name
    with NamedTemporaryFile(dir=directory, prefix=".", delete=False) as output:
        temporary = Path(output.name)
        output.write(body)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return name
