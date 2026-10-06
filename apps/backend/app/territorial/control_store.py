"""Conditional Object Storage writes for shared, durable runtime controls."""
import json
import re
from concurrent.futures import ThreadPoolExecutor

DOCUMENT_NAME = re.compile(r"(?:configuration|simulation|reviews|runtime|event_registry|status_[a-z]+|checkpoint_[a-z]+)")


class ControlConflict(RuntimeError):
    status = 409


class ObjectControlStore:
    def __init__(self, objects, namespace, bucket, prefix=".control/gods_eye_view/", index_path=None):
        if (not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", namespace)
                or not isinstance(bucket, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", bucket)
                or not isinstance(prefix, str) or not prefix.startswith(".control/gods_eye_view/")
                or not prefix.endswith("/") or any(part in {"", ".", ".."} for part in prefix[:-1].split("/"))
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", prefix)):
            raise ValueError("Invalid Object Storage control scope")
        self.objects, self.namespace, self.bucket = objects, namespace, bucket
        self.prefix, self.index_path = prefix, index_path

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def commit(self):
        pass  # Each conditional object write is already durable.

    def rollback(self):
        pass  # Object writes cannot be rolled back as a SQL transaction.

    def _key(self, key):
        if (not isinstance(key, str) or len(key) > 500 or not re.fullmatch(r"[A-Za-z0-9_./-]+", key)
                or any(part in {"", ".", ".."} for part in key.split("/"))):
            raise ValueError("Invalid control object key")
        return self.prefix + key

    def get_json(self, key):
        try:
            response = self.objects.get_object(self.namespace, self.bucket, self._key(key))
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None, None
            raise
        value = json.loads(response.data.content)
        etag = response.headers.get("etag") or response.headers.get("ETag")
        if not isinstance(value, dict) or not isinstance(etag, str) or not etag:
            raise ValueError("Invalid control object or missing ETag")
        return value, etag

    def put_json(self, key, value, expected_etag=None, create=False):
        target = self._key(key)
        if (not isinstance(value, dict) or type(create) is not bool
                or create and expected_etag is not None
                or not create and (not isinstance(expected_etag, str) or not expected_etag)):
            raise ValueError("A control write requires exactly one precondition")
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        try:
            response = self.objects.put_object(self.namespace, self.bucket, target, body,
                content_type="application/json", **({"if_none_match": "*"} if create else {"if_match": expected_etag}))
        except Exception as exc:
            if getattr(exc, "status", None) == 412:
                raise ControlConflict("Control revision changed; reload before retrying") from None
            raise
        etag = response.headers.get("etag") or response.headers.get("ETag")
        if not isinstance(etag, str) or not etag:
            raise RuntimeError("Control write returned no ETag; reread before retrying")
        return etag

    def require_ready(self):
        runtime = read_document(self, "runtime")
        if (not runtime or runtime.get("analytics_store") != "gold"
                or not (runtime.get("control_migration_complete") is True or runtime.get("control_new_install") is True)):
            raise RuntimeError("Object control migration has not been confirmed")

    def assert_reset(self, operation_id, sensor_type=None):
        self.require_ready()
        state = read_document(self, "checkpoint_reset")
        if (not operation_id or not state or state.get("operation_id") != operation_id
                or state.get("sensor_type") != sensor_type or state.get("status") != "pending" or state.get("ready") is not True
                or operation_id in state.get("cancelled_ids", []) or operation_id in state.get("cancelled_operations", {})):
            raise ControlConflict("Publication cleanup is no longer the active reset")
        return state


def validate_replacement(connection, operation_id, old, new, sensor_type=None):
    from .synthetic_reset import HISTORY_PREFIX, VERSION, _versioned_replacement, prune_publication
    if not isinstance(old, str) or not isinstance(new, str) or old == new or not VERSION.fullmatch(old) or not VERSION.fullmatch(new):
        raise ValueError("Invalid replacement publication identity")
    state = connection.assert_reset(operation_id, sensor_type)
    if state.get("replacements", {}).get(old) != new:
        raise ControlConflict("Clean replacement receipt is missing")
    response = connection.objects.get_object(connection.namespace, connection.bucket, HISTORY_PREFIX + new + ".json")
    snapshot = json.loads(response.data.content)
    if (not isinstance(snapshot, dict) or snapshot.get("version") != new or snapshot.get("reset_of") != old
            or not isinstance(snapshot.get("incidents"), list) or not isinstance(snapshot.get("evidence"), list)
            or sensor_type is not None and not isinstance(snapshot.get("sensors"), list)
            or _versioned_replacement(dict(snapshot), old)["version"] != new
            or prune_publication(snapshot, sensor_type) is not None):
        raise ValueError("Clean replacement publication is missing or outside its reset scope")
    current = connection.assert_reset(operation_id, sensor_type)
    if current.get("replacements", {}).get(old) != new or current.get("revision") != state.get("revision"):
        raise ControlConflict("Publication cleanup changed while checking its replacement")


def _valid_name(name):
    if not isinstance(name, str) or len(name) > 100 or not DOCUMENT_NAME.fullmatch(name):
        raise ValueError("Invalid Territorial document name")


def read_document(connection, name: str) -> dict:
    _valid_name(name)
    document, _ = connection.get_json("docs/" + name + ".json")
    if document is None:
        return {"revision": 0}
    if type(document.get("revision")) is not int or document["revision"] < 0:
        raise ValueError("Invalid Territorial document revision")
    return document


def read_documents(connection, names) -> dict:
    names = tuple(names)
    if not names or len(names) > 32 or len(set(names)) != len(names):
        raise ValueError("Invalid Territorial document batch")
    for name in names:
        _valid_name(name)
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
        return dict(zip(names, pool.map(lambda name: read_document(connection, name), names)))


def write_document(connection, name: str, data: dict, expected: int) -> dict:
    _valid_name(name)
    if not isinstance(data, dict) or type(expected) is not int or expected < 0:
        raise ValueError("Invalid Territorial document revision")
    current, etag = connection.get_json("docs/" + name + ".json")
    revision = current.get("revision") if current is not None else 0
    if type(revision) is not int or revision < 0:
        raise ValueError("Invalid Territorial document revision")
    if revision != expected:
        raise ControlConflict("Control revision changed; reload before retrying")
    document = {**data, "revision": expected + 1}
    connection.put_json("docs/" + name + ".json", document, expected_etag=etag, create=current is None)
    return document


def mutate_document(connection, name: str, change) -> dict:
    current = read_document(connection, name)
    return write_document(connection, name, change(dict(current)), current["revision"])


def publish(connection, snapshot: dict):
    raise RuntimeError("Analytical publications must be written to Gold and Object Storage")


def reset_version(connection):
    connection.require_ready()
    return 3


def sensor_reset_version(connection):
    return reset_version(connection)


def publications(connection):
    connection.require_ready()
    return iter(())


def replace_synthetic_publication(connection, operation_id, old, new):
    validate_replacement(connection, operation_id, old, new)


def replace_sensor_publication(connection, operation_id, sensor_type, old, new):
    from .sensors import SENSOR_TYPES
    if not isinstance(sensor_type, str) or sensor_type not in (*SENSOR_TYPES, "all"):
        raise ValueError("Invalid sensor reset type")
    validate_replacement(connection, operation_id, old, new, sensor_type)


def upsert_posts(connection, records, analysis_status, ingested_at=None, batch_key=None):
    from .post_index import upsert_posts as append
    connection.require_ready()
    return append(connection, records, analysis_status, ingested_at, batch_key)


def query_posts(connection, platform, limit, before_seq=None, max_seq=None):
    from .post_index import query_posts as query
    connection.require_ready()
    return query(connection, platform, limit, before_seq, max_seq)


def query_ordered_posts(connection, platform, limit, position=None, maximum=None, sort="published_at", order="desc"):
    from .post_index import query_ordered_posts as query
    connection.require_ready()
    return query(connection, platform, limit, position, maximum, sort, order)


def purge_synthetic_posts(connection, operation_id):
    from .post_index import purge_synthetic_posts as purge
    return purge(connection, operation_id)
