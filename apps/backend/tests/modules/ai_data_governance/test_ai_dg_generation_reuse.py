import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.modules.ai_data_governance.contracts import DocumentationResult
from app.modules.ai_data_governance.inventory import identity
from app.modules.ai_data_governance.outcome_receipts import _generation_receipt
from app.modules.ai_data_governance.documentary_graph import build_documentary_graph
from app.modules.ai_data_governance.documentary_hooks import JournalDocumentaryHooks
from app.modules.ai_data_governance.outcome_receipts import verified_documentary_receipt
from tests.modules.ai_data_governance.test_ai_dg_documentary_hooks import admission
from tests.modules.ai_data_governance.test_ai_dg_model_recovery import make_journal, prepared


def generated(phase, payload, *_):
    return json.dumps({"batch_ref": payload["batch_ref"], "items": [{"work_id": row["work_id"],
        "status": "documented", "description_draft": "Valor producido por la transformación observada.",
        "evidence_ids": row["evidence_ids"], "supported_facts": ["Transformación observada."]}
        for row in payload["admissions"]]})


async def invoked(store, batch, row, call):
    hooks, payload = await prepared(store, batch, row, call)
    payload["contexts"] = store.read("batches", batch)["batch"]["contexts"]
    return hooks, payload


def test_same_evidence_context_and_configuration_reuse_generation_across_runs():
    async def check():
        store, call = make_journal(), AsyncMock(side_effect=generated)
        first, payload = await invoked(store, "a" * 64, admission(), call)
        original = json.loads(await first.generate(payload, DocumentationResult.model_json_schema(), {}))
        second, payload = await invoked(store, "b" * 64, {**admission(), "run_id": "new-run"}, call)
        reused = json.loads(await second.generate(payload, DocumentationResult.model_json_schema(), {}))
        assert reused["batch_ref"] == "b" * 64
        assert reused["items"][0]["description_draft"] == original["items"][0]["description_draft"]
        assert reused["items"][0]["evidence_ids"] == ["evidence"]
        assert call.await_count == 1
        receipt = store.read("reuses", identity("model-response", "document", "b" * 64))
        assert receipt["source_batch_ref"] == "a" * 64
        assert json.loads(await second.generate(payload, DocumentationResult.model_json_schema(), {})) == reused
        assert call.await_count == 1
    asyncio.run(check())


@pytest.mark.parametrize("change", [{"model_ref": "other-model"}, {"prompt_version": "other-prompt"},
    {"asset_version_id": "other-version"}, {"evidence_ids": ["fresh-evidence"]}, {"context_hash": "changed-context"}])
def test_changed_admission_cannot_reuse_a_previous_generation(change):
    async def check():
        store, call = make_journal(), AsyncMock(side_effect=generated)
        first, payload = await invoked(store, "a" * 64, admission(), call)
        await first.generate(payload, DocumentationResult.model_json_schema(), {})
        row = {**admission(), "run_id": "new-run", **change}
        second, payload = await invoked(store, "b" * 64, row, call)
        await second.generate(payload, DocumentationResult.model_json_schema(), {})
        assert call.await_count == 2
        assert store.read("reuses", identity("model-response", "document", "b" * 64)) is None
    asyncio.run(check())


def test_interrupted_reuse_repairs_response_persistence_without_another_provider_call():
    async def check():
        store, call = make_journal(), AsyncMock(side_effect=generated)
        first, payload = await invoked(store, "a" * 64, admission(), call)
        await first.generate(payload, DocumentationResult.model_json_schema(), {})
        second, payload = await invoked(store, "b" * 64, {**admission(), "run_id": "new-run"}, call)
        create = store.create
        target = identity("model-response", "document", "b" * 64)
        def interrupted(kind, key, value):
            if kind == "responses" and key == target:
                raise TimeoutError("response persistence unavailable")
            return create(kind, key, value)
        store.create = interrupted
        with pytest.raises(TimeoutError):
            await second.generate(payload, DocumentationResult.model_json_schema(), {})
        assert store.read("reuses", target) and store.read("responses", target) is None
        store.create = create
        assert json.loads(await second.generate(payload, DocumentationResult.model_json_schema(), {}))["batch_ref"] == "b" * 64
        assert call.await_count == 1
    asyncio.run(check())


def test_materializer_verifies_reuse_origin_instead_of_trusting_the_copied_response():
    async def check():
        store, call = make_journal(), AsyncMock(side_effect=generated)
        first, payload = await invoked(store, "a" * 64, admission(), call)
        await first.generate(payload, DocumentationResult.model_json_schema(), {})
        second, payload = await invoked(store, "b" * 64, {**admission(), "run_id": "new-run"}, call)
        await second.generate(payload, DocumentationResult.model_json_schema(), {})
        snapshot = store.read("batches", "b" * 64)["batch"]
        assert _generation_receipt(store, "b" * 64, snapshot)[0]["status"] == "documented"
        read = store.read
        def corrupted(kind, key):
            value = read(kind, key)
            return {**value, "source_input_digest": "f" * 64} if kind == "reuses" and value else value
        store.read = corrupted
        with pytest.raises(ValueError, match="original response"):
            _generation_receipt(store, "b" * 64, snapshot)
        assert call.await_count == 1
    asyncio.run(check())


@pytest.mark.parametrize("failed_kind", ["reuses", "responses"])
@pytest.mark.parametrize("resume", [True, False])
def test_actual_graph_repairs_interrupted_reuse_without_regenerating(failed_kind, resume):
    async def check():
        from langgraph.checkpoint.memory import InMemorySaver
        async def provider(phase, payload, *_):
            if phase == "document":
                return generated(phase, payload)
            return json.dumps({"batch_ref": payload["batch_ref"], "reviews": [{"work_id": c["work_id"],
                "verdict": "supported", "unsupported_claims": [],
                "evidence_ids": json.loads(c["manifest_snapshot"])["used_evidence_ids"], "reason_code": None}
                for c in payload["candidates"]]})
        store, call = make_journal(), AsyncMock(side_effect=provider)
        first, payload = await invoked(store, "a" * 64, admission(), call)
        await first.generate(payload, DocumentationResult.model_json_schema(), {})
        batch = "b" * 64
        row = {**admission(), "run_id": "new-run"}
        store.create("batches", batch, {"schema_version": 1, "batch": {"batch_ref": batch,
            "admissions": [row], "contexts": {row["work_id"]: "same verified context"}},
            "execution": {"job_run_id": "new-run"}})
        hooks = JournalDocumentaryHooks(store, call_model=call, authorize=AsyncMock())
        graph = build_documentary_graph(load_batch=hooks.load_batch, claim_generation=hooks.claim_generation,
            generate=hooks.generate, review=hooks.review, save_outcomes=hooks.save_outcomes,
            check_active=hooks.check_active, checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "repair-reuse"}, "recursion_limit": 12}
        create = store.create
        target = identity("model-response", "document", batch)
        def interrupted(kind, key, value):
            if kind == failed_kind and key == target:
                raise TimeoutError("receipt write unavailable")
            return create(kind, key, value)
        store.create = interrupted
        with pytest.raises(TimeoutError):
            await graph.ainvoke({"batch_ref": batch}, config=config)
        store.create = create
        result = await graph.ainvoke(None if resume else {"batch_ref": batch}, config=config)
        assert result["completed"] and result["outcomes"][0]["status"] == "documented"
        assert [c.args[0] for c in call.await_args_list] == ["document", "review"]
        assert verified_documentary_receipt(store, batch)["outcomes"] == result["outcomes"]
    asyncio.run(check())


def test_losing_reuse_cannot_poison_another_inputs_winning_paid_response():
    async def check():
        store, call = make_journal(), AsyncMock(side_effect=generated)
        first, payload = await invoked(store, "a" * 64, admission(), call)
        await first.generate(payload, DocumentationResult.model_json_schema(), {})
        batch = "b" * 64
        rows = [{**admission(), "run_id": "new-run"},
            {**admission(), "run_id": "new-run", "work_id": "new-work"}]
        contexts = {r["work_id"]: "same verified context" for r in rows}
        snapshot = {"batch_ref": batch, "admissions": rows, "contexts": contexts}
        store.create("batches", batch, {"schema_version": 1, "batch": snapshot, "execution": {"job_run_id": "new-run"}})
        hooks = [JournalDocumentaryHooks(store, call_model=call, authorize=AsyncMock()) for _ in rows]
        from app.modules.ai_data_governance.documentation import documentation_generation_key, DocumentationAdmission
        for hook, row in zip(hooks, rows):
            await hook.load_batch(batch, {})
            key = documentation_generation_key(DocumentationAdmission.model_validate_json(json.dumps(row)))
            assert await hook.claim_generation("document", row, key, {})
        ready, paid_started, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        claim = hooks[0]._claim_model_phase
        async def delayed_claim(*args, **kwargs):
            ready.set()
            await paid_started.wait()
            return await claim(*args, **kwargs)
        hooks[0]._claim_model_phase = delayed_claim
        async def paid(phase, value, *_):
            paid_started.set()
            await finish.wait()
            return generated(phase, value)
        call.side_effect = paid
        def selected(row):
            return {"batch_ref": batch, "admissions": [row], "contexts": {row["work_id"]: contexts[row["work_id"]]}}
        losing = asyncio.create_task(hooks[0].generate(selected(rows[0]), DocumentationResult.model_json_schema(), {}))
        await asyncio.wait_for(ready.wait(), timeout=3)
        winning = asyncio.create_task(hooks[1].generate(selected(rows[1]), DocumentationResult.model_json_schema(), {}))
        try:
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(losing, timeout=3)
            assert store.read("reuses", identity("model-response", "document", batch)) is None
        finally:
            finish.set()
            await winning
        assert _generation_receipt(store, batch, snapshot)[0]["work_id"] == "new-work"
        # Old orphan records are not authority over a different winning input.
        store.create("reuses", identity("model-response", "document", batch), {"source_batch_ref": "a" * 64})
        assert _generation_receipt(store, batch, snapshot)[0]["work_id"] == "new-work"
        assert call.await_count == 2
    asyncio.run(check())
