"""One immutable NDJSON contract for VM simulations and AIDP X capture."""
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def page(events):
    records = [{"id": f"{event['platform']}:{event['source_id']}",
                "payload": json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False)} for event in events]
    body = b"".join((json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode() for record in records)
    return hashlib.sha256(body).hexdigest() + ".ndjson", body


def write_objects(objects, config, events):
    if not events:
        return None
    name, body = page(events)
    key = config["landing_prefix"] + name
    objects.put_object(config["namespace"], config["landing_bucket"], key, body, content_type="application/x-ndjson")
    return key


def write_file(directory, events):
    if not events:
        return None
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name, body = page(events)
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
