from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from threading import Lock
from types import SimpleNamespace

import pytest

from app.modules.ai_data_governance.journal import GovernanceJournal, JournalConflict


class StorageError(Exception):
    def __init__(self, status):
        self.status = status


class Objects:
    def __init__(self):
        self.rows = {}
        self.lock = Lock()
        self.lost_response = False
        self.calls = []

    def put_object(self, namespace, bucket, key, body, **kwargs):
        assert kwargs == {"content_type": "application/json", "if_none_match": "*"}
        with self.lock:
            self.calls.append((namespace, bucket, key))
            if key in self.rows:
                raise StorageError(412)
            self.rows[key] = body
        if self.lost_response:
            raise TimeoutError("private-details")
        return SimpleNamespace(headers={"etag": "revision"})

    def get_object(self, namespace, bucket, key):
        if key not in self.rows:
            raise StorageError(404)
        return SimpleNamespace(data=SimpleNamespace(raw=BytesIO(self.rows[key]), close=lambda: None),
                               headers={"etag": "revision"})


def journal(objects=None, **changes):
    return GovernanceJournal(objects or Objects(), platform_key="ocid1.aidataplatform.oc1.test",
        workspace_key="d40e4c62-71d9-4837-8609-5479d950f6d5",
        storage_root="oci://artifacts@namespace/oci_artifacts", **changes)


def test_conditional_claim_has_exactly_one_winner_and_keeps_the_receipt():
    store = journal()
    body = {"phase": "document", "work_id": "work"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: store.create("claims", "a" * 64, body), range(2)))
    assert sorted(result) == [False, True]
    assert store.read("claims", "a" * 64) == body
    assert store.objects.calls[0][:2] == ("namespace", "artifacts")
    assert store.objects.calls[0][2].startswith(".control/ai_data_governance/")


def test_lost_create_response_never_becomes_a_second_fresh_claim():
    objects = Objects()
    store = journal(objects)
    objects.lost_response = True
    with pytest.raises(RuntimeError, match="uncertain") as error:
        store.create("claims", "a" * 64, {"input_digest": "original"})
    assert "private-details" not in str(error.value)
    objects.lost_response = False
    assert not store.create("claims", "a" * 64, {"input_digest": "original"})
    with pytest.raises(JournalConflict):
        store.create("claims", "a" * 64, {"input_digest": "changed"})


@pytest.mark.parametrize("kind,key", [("../outside", "a" * 64), ("claims", "../outside"),
    ("claims", "b" * 63), ("credentials", "c" * 64), ("claims", "d" * 64 + "/other")])
def test_journal_paths_cannot_escape_the_installation(kind, key):
    store = journal()
    with pytest.raises(ValueError):
        store.create(kind, key, {})
    assert not store.objects.calls


def test_only_404_is_absence_and_content_is_bounded_before_parsing():
    store = journal()
    assert store.read("claims", "a" * 64) is None
    store.objects.get_object = lambda *args: (_ for _ in ()).throw(StorageError(403))
    with pytest.raises(RuntimeError, match="read failed"):
        store.read("claims", "a" * 64)
    store.objects.get_object = lambda *args: SimpleNamespace(
        data=SimpleNamespace(raw=BytesIO(b"x" * (store.MAX_BYTES + 1)), close=lambda: None), headers={"etag": "r"})
    with pytest.raises(ValueError, match="bound"):
        store.read("claims", "a" * 64)


def test_invalid_or_oversized_document_is_rejected_before_storage():
    store = journal()
    for value in ([1], {"value": float("nan")}, {"value": "x" * store.MAX_BYTES}):
        with pytest.raises(ValueError):
            store.create("responses", "b" * 64, value)
    assert not store.objects.calls


def test_another_workspace_does_not_read_or_claim_the_same_operation():
    objects = Objects()
    first = journal(objects)
    other = GovernanceJournal(objects, platform_key="ocid1.aidataplatform.oc1.test",
        workspace_key="a40e4c62-71d9-4837-8609-5479d950f6d5", storage_root="oci://artifacts@namespace/oci_artifacts")
    assert first.create("claims", "a" * 64, {"work": "first"})
    assert other.read("claims", "a" * 64) is None
    assert other.create("claims", "a" * 64, {"work": "other"})
