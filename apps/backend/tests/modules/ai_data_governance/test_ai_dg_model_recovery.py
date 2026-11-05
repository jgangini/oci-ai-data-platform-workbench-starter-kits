import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.modules.ai_data_governance.documentary_hooks import JournalDocumentaryHooks
from app.modules.ai_data_governance.documentation import DocumentationAdmission, documentation_generation_key
from app.modules.ai_data_governance.journal import GovernanceJournal
from app.modules.ai_data_governance.inventory import identity
from tests.modules.ai_data_governance.test_ai_dg_documentary_hooks import admission
from tests.modules.ai_data_governance.test_ai_dg_journal import StorageError


class VersionedObjects:
    def __init__(self):
        self.rows = {}
        self.lock = Lock()
        self.lose_ack = False

    def get_object(self, namespace, bucket, key):
        with self.lock:
            if key not in self.rows:
                raise StorageError(404)
            body, version = self.rows[key]
        return SimpleNamespace(data=SimpleNamespace(raw=BytesIO(body), close=lambda: None),
            headers={"etag": str(version)})

    def put_object(self, namespace, bucket, key, body, **kwargs):
        assert kwargs.get("content_type") == "application/json"
        with self.lock:
            previous = self.rows.get(key)
            expected = kwargs.get("if_match")
            if kwargs.get("if_none_match") == "*":
                if previous is not None:
                    raise StorageError(412)
            elif expected is None or previous is None or str(previous[1]) != expected:
                raise StorageError(412)
            version = previous[1] + 1 if previous else 1
            self.rows[key] = body, version
        if self.lose_ack:
            raise TimeoutError("private transport details")
        return SimpleNamespace(headers={"etag": str(version)})


def make_journal():
    return GovernanceJournal(VersionedObjects(), platform_key="ocid1.aidataplatform.oc1.test",
        workspace_key="d40e4c62-71d9-4837-8609-5479d950f6d5", storage_root="oci://artifacts@namespace/oci_artifacts")


async def prepared(store, batch, row, call):
    snapshot = {"batch_ref": batch, "admissions": [row], "contexts": {row["work_id"]: "same verified context"}}
    store.create("batches", batch, {"schema_version": 1, "batch": snapshot, "execution": {"job_run_id": row["run_id"]}})
    hooks = JournalDocumentaryHooks(store, call_model=call, authorize=AsyncMock())
    await hooks.load_batch(batch, {})
    key = documentation_generation_key(DocumentationAdmission.model_validate_json(json.dumps(row)))
    assert await hooks.claim_generation("document", row, key, {})
    return hooks, {"batch_ref": batch, "admissions": [row]}


def test_new_run_cannot_repeat_an_uncertain_call_with_different_observation_ids():
    async def check():
        store = make_journal()
        call = AsyncMock(side_effect=TimeoutError("possibly charged"))
        first, payload = await prepared(store, "a" * 64, admission(), call)
        with pytest.raises(TimeoutError):
            await first.generate(payload, {}, {})
        next_row = {**admission(), "run_id": "new-run", "evidence_ids": ["new-observation"]}
        second, payload = await prepared(store, "b" * 64, next_row, call)
        with pytest.raises(RuntimeError, match="unresolved"):
            await second.generate(payload, {}, {})
        assert call.await_count == 1
    asyncio.run(check())


def test_response_receipt_unblocks_later_observations_without_expiring_an_uncertain_call():
    async def check():
        store = make_journal()
        call = AsyncMock(side_effect=TimeoutError("possibly charged"))
        first, original = await prepared(store, "a" * 64, admission(), call)
        with pytest.raises(TimeoutError):
            await first.generate(original, {}, {})
        key = documentation_generation_key(DocumentationAdmission.model_validate_json(json.dumps(admission())))
        store.create("responses", identity("model-response", "document", "a" * 64),
            {"schema_version": 1, "input_digest": identity("model-input", original, {}),
                "generation_keys": [key], "response": {"items": []}})
        next_row = {**admission(), "run_id": "new-run", "evidence_ids": ["new-observation"]}
        second, payload = await prepared(store, "b" * 64, next_row, call)
        call.side_effect, call.return_value = None, '{"items": []}'
        assert json.loads(await second.generate(payload, {}, {})) == {"items": []}
        assert await second.generate(payload, {}, {}) == await second.generate(payload, {}, {})
        assert call.await_count == 2
    asyncio.run(check())


def owner(batch, phase="document"):
    return {"schema_version": 1, "phase": phase, "batch_ref": batch, "input_digest": "d" * 64,
        "generation_keys": ["e" * 64], "response_key": identity("model-response", phase, batch)}


def test_work_reservation_has_one_owner_under_competing_batches():
    store = make_journal()
    key = identity("model-work", "document", "scope", "work")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda batch: store.reserve_model_work(key, owner(batch)), ["a" * 64, "b" * 64]))
    assert sorted(results) == [False, True]
    assert store.read("model_work", key) in [owner("a" * 64), owner("b" * 64)]


def test_conditional_advance_cannot_replace_a_newer_owner_using_an_old_etag():
    store = make_journal()
    key = identity("model-work", "document", "scope", "work")
    previous = owner("a" * 64)
    assert store.reserve_model_work(key, previous)
    store.create("responses", previous["response_key"], {"schema_version": 1,
        "input_digest": previous["input_digest"], "generation_keys": previous["generation_keys"], "response": {}})
    barrier = Barrier(2)
    put = store.objects.put_object
    def competing_put(*args, **kwargs):
        if "if_match" in kwargs:
            barrier.wait(timeout=3)
        return put(*args, **kwargs)
    store.objects.put_object = competing_put
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda batch: store.reserve_model_work(key, owner(batch)), ["b" * 64, "c" * 64]))
    assert sorted(results) == [False, True]
    assert store.read("model_work", key)["batch_ref"] in {"b" * 64, "c" * 64}


def test_started_phase_cannot_be_released_using_an_abort_receipt():
    store = make_journal()
    key = identity("model-work", "document", "scope", "work")
    previous = owner("a" * 64)
    assert store.reserve_model_work(key, previous)
    start = {k: v for k, v in previous.items() if k != "response_key"}
    phase_key = identity("model-phase", "document", previous["batch_ref"])
    store.create("claims", phase_key, start)
    with pytest.raises(RuntimeError):
        store.create("claims", phase_key, {**start, "not_started": True})
    assert not store.reserve_model_work(key, owner("b" * 64))


def test_lost_work_reservation_acknowledgement_requires_reconciliation():
    store = make_journal()
    key = identity("model-work", "document", "scope", "work")
    store.objects.lose_ack = True
    with pytest.raises(RuntimeError, match="uncertain") as error:
        store.reserve_model_work(key, owner("a" * 64))
    assert "private transport details" not in str(error.value)
    store.objects.lose_ack = False
    assert store.reserve_model_work(key, owner("a" * 64))
    assert not store.reserve_model_work(key, owner("b" * 64))


def test_an_unrelated_response_cannot_release_a_work_reservation():
    store = make_journal()
    key = identity("model-work", "document", "scope", "work")
    original = owner("a" * 64)
    assert store.reserve_model_work(key, original)
    store.create("responses", original["response_key"], {"schema_version": 1, "input_digest": "f" * 64,
        "generation_keys": original["generation_keys"], "response": {"items": []}})
    with pytest.raises(RuntimeError, match="reconcile"):
        store.reserve_model_work(key, owner("b" * 64))


def test_review_work_is_protected_across_runs_too():
    async def check():
        store = make_journal()
        call = AsyncMock(side_effect=TimeoutError("possibly charged"))
        for index, batch in enumerate(("a" * 64, "b" * 64)):
            row = {**admission(), "run_id": str(index), "evidence_ids": ["evidence-" + str(index)]}
            hooks, _ = await prepared(store, batch, row, call)
            candidates = [{"work_id": "work", "document_id": "document-" + str(index)}]
            review_key = identity("AI_DG_REVIEW", [candidates[0]["document_id"]])
            assert await hooks.claim_generation("review", {"batch_ref": batch}, review_key, {})
            failure = TimeoutError if index == 0 else RuntimeError
            with pytest.raises(failure):
                await hooks.review({"batch_ref": batch, "candidates": candidates}, {}, {})
        assert call.await_count == 1
    asyncio.run(check())


def test_blocked_item_does_not_permanently_poison_other_unstarted_work():
    async def check():
        store = make_journal()
        call = AsyncMock(side_effect=TimeoutError("possibly charged"))
        blocked = {**admission(), "work_id": "z-blocked"}
        first, payload = await prepared(store, "a" * 64, blocked, call)
        with pytest.raises(TimeoutError):
            await first.generate(payload, {}, {})
        rows = [admission(), {**blocked, "evidence_ids": ["later-observation"]}]
        batch = "b" * 64
        store.create("batches", batch, {"schema_version": 1, "batch": {"batch_ref": batch,
            "admissions": rows, "contexts": {r["work_id"]: "same verified context" for r in rows}},
            "execution": {"job_run_id": "new-run"}})
        hooks = JournalDocumentaryHooks(store, call_model=call, authorize=AsyncMock())
        await hooks.load_batch(batch, {})
        for row in rows:
            key = documentation_generation_key(DocumentationAdmission.model_validate_json(json.dumps(row)))
            assert await hooks.claim_generation("document", row, key, {})
        with pytest.raises(RuntimeError, match="unresolved"):
            await hooks.generate({"batch_ref": batch, "admissions": rows}, {}, {})
        assert call.await_count == 1
        later, payload = await prepared(store, "c" * 64, admission(), call)
        call.side_effect, call.return_value = None, '{"items": []}'
        assert json.loads(await later.generate(payload, {}, {})) == {"items": []}
        assert call.await_count == 2
        blocked_owner = store.read("model_work", identity("model-work", "document", "scope", "z-blocked"))
        assert blocked_owner["batch_ref"] == "a" * 64
    asyncio.run(check())


def test_same_batch_split_claims_release_the_input_that_lost_the_exclusive_phase():
    async def check():
        store = make_journal()
        rows = [admission(), {**admission(), "work_id": "second-work"}]
        batch = "a" * 64
        store.create("batches", batch, {"schema_version": 1, "batch": {"batch_ref": batch,
            "admissions": rows, "contexts": {r["work_id"]: "same verified context" for r in rows}},
            "execution": {"job_run_id": "run"}})
        started, finish = asyncio.Event(), asyncio.Event()
        async def provider(*args):
            started.set()
            await finish.wait()
            return '{"items": []}'
        call = AsyncMock(side_effect=provider)
        hooks = [JournalDocumentaryHooks(store, call_model=call, authorize=AsyncMock()) for _ in rows]
        for hook, row in zip(hooks, rows):
            await hook.load_batch(batch, {})
            key = documentation_generation_key(DocumentationAdmission.model_validate_json(json.dumps(row)))
            assert await hook.claim_generation("document", row, key, {})
        winning = asyncio.create_task(hooks[0].generate({"batch_ref": batch, "admissions": [rows[0]]}, {}, {}))
        await asyncio.wait_for(started.wait(), timeout=3)
        try:
            with pytest.raises(RuntimeError):
                await hooks[1].generate({"batch_ref": batch, "admissions": [rows[1]]}, {}, {})
            assert call.await_count == 1
        finally:
            finish.set()
            await winning
        later, payload = await prepared(store, "b" * 64, rows[1], call)
        assert json.loads(await later.generate(payload, {}, {})) == {"items": []}
        assert call.await_count == 2
    asyncio.run(check())
