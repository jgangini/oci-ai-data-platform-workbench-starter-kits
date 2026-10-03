"""Immutable CSV envelopes for VM simulations and real capture; both carry identical event fields."""
import csv
import hashlib
import io
import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile


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
    if (not isinstance(event, dict) or event.get("mode") not in {"real", "simulation"}
            or event_id != f"{event.get('platform')}:{event.get('source_id')}"):
        raise ValueError("Invalid PRISMA Landing envelope")
    simulated = event["mode"] == "simulation"
    if "is_simulated" in event and (type(event["is_simulated"]) is not bool or event["is_simulated"] != simulated):
        raise ValueError("PRISMA simulation provenance conflicts with its mode")
    return {**event, "id": event_id, "is_simulated": simulated}


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
