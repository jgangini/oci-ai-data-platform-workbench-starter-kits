import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from app.ai_data_governance.documentary_hooks import JournalDocumentaryHooks
from app.ai_data_governance.documentation import DocumentationAdmission
from app.ai_data_governance.documentation import documentation_generation_key
from app.ai_data_governance.inventory import identity
from app.ai_data_governance.journal import JournalConflict, model_work_may_advance


BATCH = "a" * 64


class Store:
    def __init__(self):
        self.rows = {}

    def read(self, kind, key):
        return deepcopy(self.rows.get((kind, key)))

    def create(self, kind, key, value):
        previous = self.read(kind, key)
        if previous is not None:
            if previous != value:
                raise JournalConflict("Different content")
            return False
        self.rows[kind, key] = deepcopy(value)
        return True

    def reserve_model_work(self, key, owner):
        previous = self.read("model_work", key)
        if previous == owner:
            return True
        if not model_work_may_advance(previous, self.read):
            return False
        self.rows["model_work", key] = deepcopy(owner)
        return True


def admission():
    return DocumentationAdmission(scope_id="scope", run_id="run", work_id="work", asset_id="asset",
        asset_version_id="revision", source_version="observed", field_path="description",
        observed_description=None, context_hash="context", prompt_version="DGD-PROMPTS-2", model_ref="model",
        evidence_ids=("evidence",), update_supported=True, max_description_length=512).model_dump(mode="json")


def harness():
    store = Store()
    batch = {"batch_ref": BATCH, "admissions": [admission()], "contexts": {"work": "Allowed evidence"}}
    store.create("batches", BATCH, {"schema_version": 1, "batch": batch, "execution": {"job_run_id": "native"}})
    call = AsyncMock(return_value=json.dumps({"batch_ref": BATCH, "items": []}))
    authorize = AsyncMock()
    hooks = JournalDocumentaryHooks(store, call_model=call, authorize=authorize)
    return hooks, store, call, authorize, batch


async def claim_document(hooks, row=None):
    row = row or admission()
    key = documentation_generation_key(DocumentationAdmission.model_validate_json(json.dumps(row)))
    return key, await hooks.claim_generation("document", row, key, {})


def test_receipt_replays_after_interruption_without_a_second_model_call():
    async def check():
        hooks, store, call, authorize, batch = harness()
        await hooks.load_batch(BATCH, {})
        payload = {"batch_ref": BATCH, "admissions": batch["admissions"]}
        key, claimed = await claim_document(hooks)
        assert claimed
        response = await hooks.generate(payload, {"type": "object"}, {})
        assert store.read("responses", identity("model-response", "document", BATCH))
        resumed = JournalDocumentaryHooks(store, call_model=call, authorize=authorize)
        await resumed.load_batch(BATCH, {})
        assert await resumed.claim_generation("document", admission(), key, {})
        assert await resumed.generate(payload, {"type": "object"}, {}) == response
        assert call.await_count == 1
    asyncio.run(check())


def test_claim_with_no_response_is_pending_and_cannot_be_stolen_by_another_batch():
    async def check():
        hooks, store, call, authorize, _ = harness()
        await hooks.load_batch(BATCH, {})
        assert await hooks.claim_generation("document", admission(), "b" * 64, {})
        assert not await hooks.claim_generation("document", admission(), "b" * 64, {})
        receipt = store.rows["claims", identity("model-claim", "document", BATCH, "b" * 64)]
        receipt["batch_ref"] = "c" * 64
        assert not await hooks.claim_generation("document", admission(), "b" * 64, {})
        call.assert_not_called()
    asyncio.run(check())


def test_replay_with_changed_input_is_rejected_without_another_call():
    async def check():
        hooks, store, call, _, _ = harness()
        await hooks.load_batch(BATCH, {})
        payload = {"batch_ref": BATCH, "admissions": [admission()], "value": "original"}
        await claim_document(hooks)
        await hooks.generate(payload, {}, {})
        with pytest.raises(ValueError, match="different input"):
            await hooks.generate({**payload, "value": "changed"}, {}, {})
        assert call.await_count == 1
    asyncio.run(check())


def test_cancellation_before_response_persistence_leaves_no_replayable_success():
    async def check():
        hooks, store, call, authorize, _ = harness()
        await hooks.load_batch(BATCH, {})
        await claim_document(hooks)
        async def cancelled(*args):
            authorize.side_effect = RuntimeError("cancelled")
            return json.dumps({"result": "value"})
        call.side_effect = cancelled
        with pytest.raises(RuntimeError, match="cancelled"):
            await hooks.generate({"batch_ref": BATCH, "admissions": [admission()]}, {}, {})
        assert store.read("responses", identity("model-response", "document", BATCH)) is None
    asyncio.run(check())


def test_partial_generation_receipt_does_not_reactivate_an_unanswered_claim():
    async def check():
        hooks, store, call, authorize, _ = harness()
        second = {**admission(), "work_id": "other-work", "asset_id": "other-asset"}
        store.rows["batches", BATCH]["batch"]["admissions"].append(second)
        store.rows["batches", BATCH]["batch"]["contexts"]["other-work"] = "Other evidence"
        await hooks.load_batch(BATCH, {})
        first_key, claimed = await claim_document(hooks)
        assert claimed
        assert not (await claim_document(hooks))[1]
        second_key, claimed = await claim_document(hooks, second)
        assert claimed
        payload = {"batch_ref": BATCH, "admissions": [second]}
        original = await hooks.generate(payload, {}, {})
        resumed = JournalDocumentaryHooks(store, call_model=call, authorize=authorize)
        await resumed.load_batch(BATCH, {})
        assert not await resumed.claim_generation("document", admission(), first_key, {})
        assert await resumed.claim_generation("document", second, second_key, {})
        assert await resumed.generate(payload, {}, {}) == original
        assert call.await_count == 1
    asyncio.run(check())


def test_model_call_requires_claims_and_an_exclusive_phase_start():
    async def check():
        hooks, store, call, _, _ = harness()
        await hooks.load_batch(BATCH, {})
        payload = {"batch_ref": BATCH, "admissions": [admission()]}
        with pytest.raises(ValueError, match="without"):
            await hooks.generate(payload, {}, {})
        call.assert_not_called()
        await claim_document(hooks)
        call.side_effect = TimeoutError("possibly started")
        with pytest.raises(TimeoutError):
            await hooks.generate(payload, {}, {})
        with pytest.raises(RuntimeError, match="already started"):
            await hooks.generate(payload, {}, {})
        assert call.await_count == 1
    asyncio.run(check())


def test_outcomes_are_immutable_and_must_cover_exact_admission():
    async def check():
        hooks, store, call, _, batch = harness()
        await hooks.load_batch(BATCH, {})
        with pytest.raises(ValueError, match="cover"):
            await hooks.save_outcomes(BATCH, batch["admissions"], [], {})
        outcome = [{"work_id": "work", "status": "insufficient_context"}]
        await hooks.save_outcomes(BATCH, batch["admissions"], outcome, {})
        await hooks.save_outcomes(BATCH, batch["admissions"], outcome, {})
        with pytest.raises(JournalConflict):
            await hooks.save_outcomes(BATCH, batch["admissions"], [{"work_id": "work", "status": "different"}], {})
        assert store.read("outcomes", BATCH)["outcomes"] == outcome
        call.assert_not_called()
    asyncio.run(check())


def test_batch_not_supplied_by_user_and_authorization_precedes_model_access():
    async def check():
        hooks, store, call, authorize, _ = harness()
        authorize.side_effect = PermissionError("rejected")
        with pytest.raises(PermissionError):
            await hooks.load_batch(BATCH, {})
        assert hooks.batch is None
        call.assert_not_called()
        with pytest.raises(ValueError):
            await hooks.load_batch("../outside", {})
    asyncio.run(check())
