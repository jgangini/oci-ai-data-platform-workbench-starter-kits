"""Explicit Oracle-to-Object migration; this module never starts or stops writers.

The operator must first block portal writes and verify both stream runs terminal.
Export using a fresh dedicated Oracle connection; SET TRANSACTION READ ONLY keeps
documents, posts and publication history in one consistent database snapshot.
Sequence LAST_NUMBER is a reserved cache upper bound, not the last value consumed.
Keep the returned snapshot and streamed publication export private and unchanged for retries. Stage uses create-only
writes and exact readback; activation separately requires matching Gold history.
Before activation rollback is simply keeping the Oracle runtime selected. After
activation do not return to Oracle once Object writers have started without a new
reverse migration: this tool deliberately never merges diverging live stores.
"""
import hashlib
import json
import re

from .control_store import ControlConflict, _valid_name
from .post_index import seed_posts


RUNTIME_FIELDS = frozenset("revision namespace bucket workbench_base region model_id compartment_id catalog streaming_mode pipeline_revision analytics_store oci_identity_sha256 oci_credential_name landing_bucket landing_prefix landing_volume_path checkpoint_volume_path sensor_landing_prefix sensor_landing_volume_path sensor_checkpoint_volume_path workspace_key job_key sensor_job_key social_compute_key sensor_compute_key gold_query_compute_id agent_compute_id agent_id publication post_index_revision synthetic_reset_version sensor_reset_version cleanup_cancel_operation_id cleanup_cancel_requested_at cleanup_previous_streaming_mode".split())
REMOVED_RUNTIME_FIELDS = frozenset(("writer_credential_name", "reader_credential_name"))
HISTORY_PREFIX = "04_gold/prisma/snapshots/"


def _migration_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _migration_json(value):
    value = value.read() if hasattr(value, "read") else value
    return value if isinstance(value, dict) else json.loads(value)


def _runtime_document(value):
    unknown = set(value) - RUNTIME_FIELDS - REMOVED_RUNTIME_FIELDS
    if unknown:
        # Do not print values or persist an unreviewed credential field.
        raise ValueError("Runtime contains unreviewed fields; migration refused")
    result = {key: value for key, value in value.items() if key in RUNTIME_FIELDS}
    for key, item in result.items():
        if key == "publication":
            if not isinstance(item, dict) or set(item) - {"version", "published_at"} or any(not isinstance(v, str) for v in item.values()):
                raise ValueError("Invalid runtime publication metadata")
        elif not isinstance(item, (str, int, bool)) and item is not None:
            raise ValueError("Invalid runtime metadata")
    return result


def _publication_hash(version, publication):
    if (not isinstance(version, str) or not re.fullmatch(r"gold-[a-f0-9]{32}", version)
            or not isinstance(publication, dict) or publication.get("version") != version
            or not isinstance(publication.get("incidents"), list) or not isinstance(publication.get("evidence"), list)):
        raise ValueError("Invalid migrated publication")
    return _migration_hash(publication)


def export_snapshot(connection, *, publication_sink, writers_frozen=False):
    """Stream validated history to the caller's private sink; return only its hashes."""
    if not callable(publication_sink):
        raise ValueError("Migration requires a publication sink")
    if writers_frozen is not True or getattr(connection, "transaction_in_progress", False) or getattr(connection, "autocommit", False):
        raise ValueError("Migration requires frozen writers and a fresh Oracle connection")
    cursor = connection.cursor()
    cursor.execute("SET TRANSACTION READ ONLY")
    try:
        cursor.execute("SELECT name,payload FROM ADMIN.PRISMA_CONTROL_DOCS ORDER BY name")
        documents, removed = {}, []
        for name, payload in cursor:
            _valid_name(name)
            value = _migration_json(payload)
            if not isinstance(value, dict) or type(value.get("revision")) is not int or value["revision"] < 0 or name in documents:
                raise ValueError("Invalid legacy control document")
            if name == "runtime":
                removed = sorted(set(value) & REMOVED_RUNTIME_FIELDS)
                value = _runtime_document(value)
            documents[name] = value
        if "runtime" not in documents or documents.get("checkpoint_reset", {}).get("status") == "pending":
            raise ValueError("Migration requires a runtime and no pending cleanup")
        cursor.execute("SELECT payload,capture_seq,listing_revision,captured_at,analysis_status FROM ADMIN.PRISMA_SOCIAL_POSTS ORDER BY capture_seq")
        posts = [{"payload": _migration_json(payload), "capture_seq": int(capture), "listing_revision": int(revision),
                  "captured_at": captured, "analysis_status": status} for payload, capture, revision, captured, status in cursor]
        cursor.execute("SELECT sequence_name FROM ALL_TAB_IDENTITY_COLS WHERE owner='ADMIN' AND table_name='PRISMA_SOCIAL_POSTS' AND column_name='CAPTURE_SEQ'")
        identities = list(cursor)
        if len(identities) != 1:
            raise ValueError("Missing capture identity sequence")
        cursor.execute("SELECT sequence_name,last_number,increment_by,cycle_flag FROM ALL_SEQUENCES WHERE sequence_owner='ADMIN' AND sequence_name IN (:capture,:listing)",
                       capture=identities[0][0], listing="PRISMA_POST_LIST_REVISION")
        sequences = {name: (int(last), int(step), cycle) for name, last, step, cycle in cursor}
        if set(sequences) != {identities[0][0], "PRISMA_POST_LIST_REVISION"} or any(step != 1 or cycle != "N" for _, step, cycle in sequences.values()):
            raise ValueError("Unsupported legacy post sequences")
        bounds = {}
        for key, field, sequence in (("capture_sequence", "capture_seq", identities[0][0]),
                                     ("listing_sequence", "listing_revision", "PRISMA_POST_LIST_REVISION")):
            row_max = max((row[field] for row in posts), default=0)
            bounds[key] = {"row_max": row_max, "reserved_upper_bound": max(row_max, sequences[sequence][0] - 1)}
        cursor.arraysize = cursor.prefetchrows = 1
        cursor.execute("SELECT version,payload FROM ADMIN.PRISMA_PUBLICATIONS ORDER BY version")
        publication_hashes = {}
        for version, payload in cursor:
            document = _migration_json(payload)
            digest = _publication_hash(version, document)
            if version in publication_hashes:
                raise ValueError("Invalid legacy publication identity")
            publication_sink(version, document)
            publication_hashes[version] = digest
        result = {"documents": documents, "posts": posts, "sequence_bounds": bounds,
                  "publication_hashes": publication_hashes,
                  "removed_runtime_fields": removed, "consistency": "oracle_read_only_transaction"}
        _validate_snapshot(result)
        return result
    finally:
        connection.rollback()
        cursor.close()


def _validate_snapshot(snapshot):
    from .post_index import _post_validate_event
    if not isinstance(snapshot, dict) or snapshot.get("consistency") != "oracle_read_only_transaction":
        raise ValueError("Invalid migration snapshot")
    for name, value in snapshot["documents"].items():
        _valid_name(name)
        if not isinstance(value, dict) or type(value.get("revision")) is not int or value["revision"] < 0:
            raise ValueError("Invalid migrated document revision")
    if _runtime_document(snapshot["documents"]["runtime"]) != snapshot["documents"]["runtime"]:
        raise ValueError("Runtime still contains obsolete database credential references")
    if snapshot["documents"].get("checkpoint_reset", {}).get("status") == "pending":
        raise ValueError("Pending cleanup cannot be migrated")
    event = {"kind": "seed", "records": snapshot["posts"],
             **{key: bounds["reserved_upper_bound"] for key, bounds in snapshot["sequence_bounds"].items()}}
    _post_validate_event(event)
    hashes = snapshot.get("publication_hashes")
    if not isinstance(hashes, dict) or "publications" in snapshot:
        raise ValueError("Invalid migration publication hashes")
    for version, digest in hashes.items():
        if (not isinstance(version, str) or not re.fullmatch(r"gold-[a-f0-9]{32}", version)
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise ValueError("Invalid migration publication hashes")
    return event


def _create_exact(store, key, value):
    current, _ = store.get_json(key)
    if current is None:
        try:
            store.put_json(key, value, create=True)
        except ControlConflict:
            pass
        current, _ = store.get_json(key)
    if current != value:
        raise ControlConflict("Migration destination differs; no merge or overwrite is allowed")


def _verify_stage(store, snapshot, *, activated=False):
    event = _validate_snapshot(snapshot)
    runtime = snapshot["documents"]["runtime"]
    if runtime.get("namespace") != store.namespace or runtime.get("bucket") != store.bucket:
        raise ValueError("Migration target differs from its publication storage scope")
    for name, document in snapshot["documents"].items():
        if name == "runtime" and activated:
            document = _activated_runtime(snapshot)
        if store.get_json("docs/" + name + ".json")[0] != document:
            raise ControlConflict("Migration documents differ from the frozen snapshot")
    head, _ = store.get_json("posts/head.json")
    if not head:
        raise ValueError("Migrated post journal is missing")
    node, _ = store.get_json("posts/nodes/" + head["node"] + ".json")
    expected = {"previous": None, "start": 0, "sequence": max(event["capture_sequence"], event["listing_sequence"]),
                "event_id": _migration_hash(event), "event": event}
    if node != expected or head != {"node": _migration_hash(expected), "sequence": expected["sequence"], "event_id": expected["event_id"]}:
        raise ControlConflict("Migrated post journal differs from the frozen snapshot")


def stage_snapshot(store, snapshot):
    """Create-only, resumable staging; deliberately does not enable the runtime."""
    event = _validate_snapshot(snapshot)
    runtime = snapshot["documents"]["runtime"]
    if runtime.get("namespace") != store.namespace or runtime.get("bucket") != store.bucket:
        raise ValueError("Migration target differs from its publication storage scope")
    for name, document in snapshot["documents"].items():
        _create_exact(store, "docs/" + name + ".json", document)
    seed_posts(store, event["records"], event["capture_sequence"], event["listing_sequence"])
    _verify_stage(store, snapshot)
    return {"snapshot_sha256": _migration_hash(snapshot), "document_count": len(snapshot["documents"]),
            "post_count": len(snapshot["posts"]), "sequence_bounds": snapshot["sequence_bounds"]}


def _activated_runtime(snapshot):
    runtime = snapshot["documents"]["runtime"]
    return {**runtime, "revision": runtime["revision"] + 1, "analytics_store": "gold", "control_migration_complete": True,
            "control_migration_sha256": _migration_hash(snapshot)}


def activate_snapshot(store, snapshot, *, gold_publications, writers_frozen=False):
    """Gold hashes must come from the operator's verified query of actual Gold rows."""
    if writers_frozen is not True or not isinstance(gold_publications, dict):
        raise ValueError("Migration activation requires frozen writers and Gold history proof")
    current, etag = store.get_json("docs/runtime.json")
    activated = current == _activated_runtime(snapshot)
    _verify_stage(store, snapshot, activated=activated)
    for version, digest in snapshot["publication_hashes"].items():
        response = store.objects.get_object(store.namespace, store.bucket, HISTORY_PREFIX + version + ".json")
        if _publication_hash(version, _migration_json(response.data.content)) != digest or gold_publications.get(version) != digest:
            raise ValueError("Legacy publication history is not verified in both Gold and Objects")
    pointer = _migration_json(store.objects.get_object(store.namespace, store.bucket, "04_gold/prisma/current.json").data.content)
    version = pointer.get("version") if isinstance(pointer, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r"gold-[a-f0-9]{32}", version) or pointer.get("snapshot_key") != HISTORY_PREFIX + version + ".json":
        raise ValueError("Invalid Gold publication pointer")
    publication = _migration_json(store.objects.get_object(store.namespace, store.bucket, pointer["snapshot_key"]).data.content)
    if gold_publications.get(version) != _publication_hash(version, publication):
        raise ValueError("Current publication has not been verified in Gold")
    if not activated:
        store.put_json("docs/runtime.json", _activated_runtime(snapshot), expected_etag=etag)
    store.require_ready()
    return {"snapshot_sha256": _migration_hash(snapshot), "runtime_revision": _activated_runtime(snapshot)["revision"]}
