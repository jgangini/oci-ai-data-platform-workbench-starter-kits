"""Immutable Object Storage journal; SQLite is a disposable, incremental read index."""
import hashlib
import json
import re
from datetime import datetime, timezone


POST_HEAD = "posts/head.json"
POST_RANK = {"captured": 1, "ingested": 2, "processed": 3}


def published_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds") if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _post_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _post_hash(value):
    return hashlib.sha256(_post_json(value).encode("utf-8")).hexdigest()


def _post_integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 9223372036854775807


def _post_head(store):
    head, etag = store.get_json(POST_HEAD)
    if head is None:
        return {"node": None, "sequence": 0, "event_id": None}, None
    if (set(head) != {"node", "sequence", "event_id"} or not re.fullmatch(r"[a-f0-9]{64}", str(head["node"]))
            or not re.fullmatch(r"[a-f0-9]{64}", str(head["event_id"]))
            or not _post_integer(head["sequence"]) or not etag):
        raise ValueError("Invalid post journal head")
    return head, etag


def _post_append(store, event):
    """Bounded CAS rebase; unreferenced immutable nodes are harmless after a conflict."""
    event_id = _post_hash(event)
    for attempt in range(5):
        head, etag = _post_head(store)
        if event["kind"] == "purge":
            store.assert_reset(event["operation_id"])
        if head["event_id"] == event_id:
            existing, _ = store.get_json(f"posts/nodes/{head['node']}.json")
            if not isinstance(existing, dict) or _post_hash(existing) != head["node"] or existing.get("event") != event:
                raise ValueError("Post journal replay differs")
            return event_id
        if event["kind"] == "seed" and head["node"] is not None:
            raise ValueError("Post migration requires an empty journal")
        sequence = (max(event["capture_sequence"], event["listing_sequence"]) if event["kind"] == "seed"
                    else head["sequence"] + max(1, len(event.get("records", []))))
        if not _post_integer(sequence):
            raise ValueError("Post journal sequence exhausted")
        node = {"previous": head["node"], "start": head["sequence"], "sequence": sequence,
                "event_id": event_id, "event": event}
        key = f"posts/nodes/{_post_hash(node)}.json"
        try:
            store.put_json(key, node, create=True)
        except Exception as exc:
            if getattr(exc, "status", None) not in (409, 412):
                raise
            existing, _ = store.get_json(key)
            if existing != node:
                raise ValueError("Post journal node differs") from None
        if event["kind"] == "purge":
            store.assert_reset(event["operation_id"])
        try:
            store.put_json(POST_HEAD, {"node": key.removeprefix("posts/nodes/").removesuffix(".json"),
                "sequence": sequence, "event_id": event_id}, expected_etag=etag, create=etag is None)
            return event_id
        except Exception as exc:
            if getattr(exc, "status", None) not in (409, 412) or attempt == 4:
                raise


def _post_document(record, status, ingested_at=None, batch_key=None):
    if (not isinstance(record, dict) or not isinstance(record.get("source_id"), str) or not record["source_id"]
            or not isinstance(record.get("platform"), str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", record["platform"])
            or len(f"{record['platform']}:{record['source_id']}") > 200 or not isinstance(record.get("created_at"), str)):
        raise ValueError("Invalid post projection identity")
    document = {**record, "id": f"{record['platform']}:{record['source_id']}",
                "listing_published_at": published_time(record["created_at"])}
    if document.get("mode") == "simulation":
        document["mode"] = "Synthetic"
    captured = record.get("captured_at") or (ingested_at if status == "captured" else None)
    if captured is None and record.get("mode") == "real":
        captured = record.get("observed_at")
    for name, value in (("captured_at", captured), ("ingested_at", ingested_at), ("batch_key", batch_key)):
        if value is not None:
            if not isinstance(value, str):
                raise ValueError("Invalid post projection timestamp or batch")
            document[name] = value
    _post_json(document)
    return document


def upsert_posts(store, records, analysis_status, ingested_at=None, batch_key=None):
    if analysis_status not in POST_RANK:
        raise ValueError("Invalid post analysis status")
    documents = {}
    for record in records:
        document = _post_document(record, analysis_status, ingested_at, batch_key)
        documents[document["id"]] = document
    if documents:
        _post_append(store, {"kind": "upsert", "status": analysis_status, "records": list(documents.values())})


def seed_posts(store, rows, capture_sequence, listing_sequence):
    """Explicit one-time migration, including sequence gaps and deleted-row high-water marks."""
    event = {"kind": "seed", "records": rows, "capture_sequence": capture_sequence, "listing_sequence": listing_sequence}
    _post_validate_event(event)
    _post_append(store, event)


def append_purge(store, operation_id):
    """Spark-facing mutation: only a guarded journal event, never a local SQL index."""
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 100:
        raise ValueError("Invalid post purge operation")
    _post_append(store, {"kind": "purge", "operation_id": operation_id})


def purge_synthetic_posts(store, operation_id):
    append_purge(store, operation_id)
    if store.index_path is None:
        return None  # Spark appends only; a missing derived count is never reported as zero.
    event_id = _post_hash({"kind": "purge", "operation_id": operation_id})
    connection = _post_sync(store)
    try:
        return connection.execute("SELECT affected FROM post_events WHERE id=?", (event_id,)).fetchone()[0]
    finally:
        connection.close()


def _post_validate_event(event):
    kind = event.get("kind") if isinstance(event, dict) else None
    if kind == "purge" and set(event) == {"kind", "operation_id"} and isinstance(event["operation_id"], str) and 1 <= len(event["operation_id"]) <= 100:
        return
    if kind not in {"seed", "upsert"} or not isinstance(event.get("records"), list):
        raise ValueError("Invalid post journal event")
    if kind == "upsert":
        if set(event) != {"kind", "status", "records"} or event["status"] not in POST_RANK or not event["records"]:
            raise ValueError("Invalid post journal status")
        identities = [record.get("id") for record in event["records"] if isinstance(record, dict)]
        for record in event["records"]:
            if _post_document(record, event["status"]) != record:
                raise ValueError("Invalid post journal document")
    else:
        if (set(event) != {"kind", "records", "capture_sequence", "listing_sequence"}
                or not all(_post_integer(event[name]) for name in ("capture_sequence", "listing_sequence"))):
            raise ValueError("Invalid post migration sequence")
        identities, captures, revisions = [], [], []
        for row in event["records"]:
            if (not isinstance(row, dict) or set(row) != {"payload", "capture_seq", "listing_revision", "analysis_status", "captured_at"}
                    or row["analysis_status"] not in POST_RANK or not _post_integer(row["capture_seq"], 1)
                    or not _post_integer(row["listing_revision"], 1)
                    or row["captured_at"] is not None and not isinstance(row["captured_at"], str)):
                raise ValueError("Invalid post migration row")
            document = _post_document(row["payload"], row["analysis_status"])
            if document["id"] != row["payload"].get("id"):
                raise ValueError("Invalid post migration identity")
            identities.append(document["id"])
            captures.append(row["capture_seq"])
            revisions.append(row["listing_revision"])
        if (len(set(captures)) != len(captures) or len(set(revisions)) != len(revisions)
                or max(captures, default=0) > event["capture_sequence"] or max(revisions, default=0) > event["listing_sequence"]):
            raise ValueError("Invalid post migration high-water marks")
    if len(identities) != len(event["records"]) or len(set(identities)) != len(identities):
        raise ValueError("Duplicate post journal identity")


def _post_apply(connection, node):
    event = node["event"]
    _post_validate_event(event)
    if connection.execute("SELECT 1 FROM post_events WHERE id=?", (node["event_id"],)).fetchone():
        return
    if event["kind"] == "purge":
        affected = connection.execute("DELETE FROM posts WHERE json_extract(payload,'$.mode') IN ('Synthetic','simulation')").rowcount
    else:
        affected = 0
        for offset, record in enumerate(event["records"], 1):
            seed = event["kind"] == "seed"
            payload, status = (record["payload"], record["analysis_status"]) if seed else (record, event["status"])
            sequence = record["capture_seq"] if seed else node["start"] + offset
            revision = record["listing_revision"] if seed else sequence
            captured = record["captured_at"] if seed else payload.get("captured_at")
            old = connection.execute("SELECT status,payload,captured_at FROM posts WHERE id=?", (payload["id"],)).fetchone()
            if old:
                captured = old[2] or (json.loads(old[1]).get("ingested_at") if old[0] == "captured" else None) or captured
                if POST_RANK[status] <= POST_RANK[old[0]]:
                    connection.execute("UPDATE posts SET captured_at=? WHERE id=?", (captured, payload["id"]))
                    continue
            connection.execute("INSERT INTO posts VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "payload=excluded.payload,status=excluded.status,captured_at=excluded.captured_at,"
                "published_at=excluded.published_at,listing_revision=excluded.listing_revision",
                (payload["id"], payload["platform"], sequence, _post_json(payload), status, captured,
                 _post_document(payload, status)["listing_published_at"], revision))
    connection.execute("INSERT INTO post_events VALUES (?,?)", (node["event_id"], affected))


def _post_sync(store):
    import sqlite3
    if store.index_path is None:
        raise ValueError("A persistent post index path is required for reads")
    connection = sqlite3.connect(store.index_path, timeout=30)
    try:
        connection.executescript("""
          CREATE TABLE IF NOT EXISTS post_head (singleton INTEGER PRIMARY KEY CHECK(singleton=1), node TEXT, sequence INTEGER NOT NULL);
          INSERT OR IGNORE INTO post_head VALUES (1,NULL,0);
          CREATE TABLE IF NOT EXISTS post_events (id TEXT PRIMARY KEY, affected INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY, platform TEXT NOT NULL, capture_seq INTEGER UNIQUE NOT NULL,
            payload TEXT NOT NULL, status TEXT NOT NULL, captured_at TEXT, published_at TEXT, listing_revision INTEGER NOT NULL);
          CREATE INDEX IF NOT EXISTS post_platform_sequence ON posts(platform,capture_seq);
          CREATE INDEX IF NOT EXISTS post_publication ON posts(platform,published_at,id);
          CREATE INDEX IF NOT EXISTS post_all_publication ON posts(published_at,id);
        """)
        for _ in range(5):
            cached, sequence = connection.execute("SELECT node,sequence FROM post_head WHERE singleton=1").fetchone()
            head, _ = _post_head(store)
            if (head["node"], head["sequence"]) == (cached, sequence):
                return connection
            pointer, expected, nodes = head["node"], head["sequence"], []
            # Network reads hold no SQLite transaction or process-global lock.
            while pointer != cached:
                if pointer is None:
                    raise ValueError("Cached post head is not reachable")
                node, _ = store.get_json(f"posts/nodes/{pointer}.json")
                if (not isinstance(node, dict) or set(node) != {"previous", "start", "sequence", "event_id", "event"}
                        or _post_hash(node) != pointer or _post_hash(node["event"]) != node["event_id"]
                        or not nodes and node["event_id"] != head["event_id"]
                        or not _post_integer(node["start"]) or node["sequence"] != expected
                        or node["start"] > expected or node["previous"] is not None and not re.fullmatch(r"[a-f0-9]{64}", str(node["previous"]))):
                    raise ValueError("Invalid or missing post journal node")
                _post_validate_event(node["event"])
                size = max(node["event"]["capture_sequence"], node["event"]["listing_sequence"]) if node["event"]["kind"] == "seed" else max(1, len(node["event"].get("records", [])))
                if node["sequence"] != node["start"] + size or node["event"]["kind"] == "seed" and node["previous"] is not None:
                    raise ValueError("Invalid post journal sequence")
                nodes.append(node)
                pointer, expected = node["previous"], node["start"]
            if expected != sequence:
                raise ValueError("Invalid cached post sequence")
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT node,sequence FROM post_head WHERE singleton=1").fetchone() != (cached, sequence):
                connection.rollback()
                continue
            for node in reversed(nodes):
                _post_apply(connection, node)
            connection.execute("UPDATE post_head SET node=?,sequence=? WHERE singleton=1", (head["node"], head["sequence"]))
            connection.commit()
            return connection
        raise RuntimeError("Post index changed during synchronization")
    except BaseException:
        connection.close()
        raise


def query_ordered_posts(store, platform, limit, position=None, maximum=None, sort="published_at", order="desc"):
    if (platform is not None and platform not in ("x", "facebook", "instagram", "tiktok")
            or not _post_integer(limit, 1) or limit > 100 or sort not in {"published_at", "captured_at"}
            or order not in {"asc", "desc"} or maximum is not None and not _post_integer(maximum)):
        raise ValueError("Invalid ordered post pagination")
    if position is not None and (not isinstance(position, dict) or not isinstance(position.get("key"), (list, tuple)) or len(position["key"]) != 2):
        raise ValueError("Invalid post pagination anchor")
    before, identity = (None, None) if position is None else position["key"]
    if position is not None and (not isinstance(identity, str) or not 1 <= len(identity) <= 200
            or (not _post_integer(before, 1) if sort == "captured_at" else not isinstance(before, str))):
        raise ValueError("Invalid post pagination anchor")
    connection = _post_sync(store)
    try:
        connection.execute("BEGIN")
        scope, args = "platform IN ('x','facebook','instagram','tiktok')", []
        if platform is not None:
            scope += " AND platform=?"
            args.append(platform)
        if maximum is None:
            maximum = connection.execute(f"SELECT COALESCE(MAX(capture_seq),0) FROM posts WHERE {scope}", args).fetchone()[0]
        scope += " AND capture_seq<=?"
        args.append(maximum)
        total, revision = connection.execute(f"SELECT COUNT(*),COALESCE(SUM(listing_revision),0) FROM posts WHERE {scope}", args).fetchone()
        column = "capture_seq" if sort == "captured_at" else "COALESCE(published_at,'" + ("!" if order == "desc" else "~") + "')"
        if position is not None:
            if sort == "published_at" and before in {"", "\uffff"}:
                before = "!" if order == "desc" else "~"
            scope += f" AND ({column},id) {'<' if order == 'desc' else '>'} (?,?)"
            args += [before, identity]
        rows = connection.execute(f"SELECT payload,capture_seq,status,captured_at FROM posts WHERE {scope} "
                                  f"ORDER BY {column} {order},id {order} LIMIT ?", [*args, limit + 1]).fetchall()
        return {"max_seq": maximum, "total": total, "listing_revision": revision,
                "items": [{**json.loads(row[0]), "capture_seq": row[1], "analysis_status": row[2], "captured_at": row[3]} for row in rows]}
    finally:
        connection.close()


def query_posts(store, platform, limit, before_seq=None, max_seq=None):
    if before_seq is not None and not _post_integer(before_seq, 1):
        raise ValueError("Invalid post pagination")
    position = {"key": [before_seq, "!"]} if before_seq is not None else None
    page = query_ordered_posts(store, platform, limit, position, max_seq, "captured_at", "desc")
    items = page["items"][:limit]
    return {"items": items, "max_seq": page["max_seq"], "total": page["total"],
            "next_seq": items[-1]["capture_seq"] if len(page["items"]) > limit else None}
