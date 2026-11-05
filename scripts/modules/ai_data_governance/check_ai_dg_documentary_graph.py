"""Execute the documentary graph against deterministic hooks, without OCI/model calls."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "apps/backend"))

from langgraph.checkpoint.memory import InMemorySaver
from app.modules.ai_data_governance.documentary_graph import build_documentary_graph
from app.modules.ai_data_governance.documentation import DocumentationAdmission


def admission(work="work-1", **changes):
    return DocumentationAdmission(scope_id="scope", run_id="run", work_id=work, asset_id="asset-" + work,
        asset_version_id="version-" + work, source_version="metadata-revision", field_path="description",
        observed_description=None, context_hash="context", prompt_version="DGD-PROMPTS-2", model_ref="model",
        evidence_ids=("static-code-1",), update_supported=True, max_description_length=512, **changes).model_dump(mode="json")


class Harness:
    def __init__(self, admissions=None, contexts=None):
        self.admissions = [admission()] if admissions is None else admissions
        self.batch = {"batch_ref": "batch", "admissions": self.admissions,
                      "contexts": contexts if contexts is not None else {a["work_id"]: "Permitted static code excerpt." for a in self.admissions}}
        result = {"batch_ref": "batch", "items": [{"work_id": a["work_id"], "status": "documented",
            "description_draft": "Valor derivado por la transformación observada.", "evidence_ids": ["static-code-1"],
            "supported_facts": ["La expresión aparece en el fragmento permitido."]} for a in self.admissions]}
        reviews = {"batch_ref": "batch", "reviews": [{"work_id": a["work_id"], "verdict": "supported",
            "unsupported_claims": [], "evidence_ids": ["static-code-1"], "reason_code": None} for a in self.admissions]}
        self.claimed = set()
        self.cancel_on_review_claim = False
        self.cancelled = False
        self.load = AsyncMock(return_value=self.batch)
        self.generate = AsyncMock(return_value=json.dumps(result))
        self.review = AsyncMock(return_value=json.dumps(reviews))
        self.save = AsyncMock()
        self.active = AsyncMock()
        self.memory = InMemorySaver()
        self.graph = build_documentary_graph(load_batch=self.load, claim_generation=self.claim,
            generate=self.generate, review=self.review, save_outcomes=self.save,
            check_active=self.active, checkpointer=self.memory)
        self.config = {"configurable": {"thread_id": "test-thread"}, "recursion_limit": 12}

    async def claim(self, phase, admission, key, config):
        if (phase, key) in self.claimed:
            return False
        self.claimed.add((phase, key))
        if phase == "review" and self.cancel_on_review_claim:
            self.cancelled = True
        return True

    async def run(self, value=None):
        return await self.graph.ainvoke(value, config=self.config)


class DocumentaryGraphChecks(unittest.IsolatedAsyncioTestCase):
    async def test_empty_first_use_does_not_call_model_or_create_documents(self):
        h = Harness([])
        result = await h.run({"batch_ref": "batch"})
        self.assertTrue(result["completed"])
        self.assertEqual(result["outcomes"], [])
        h.generate.assert_not_called()
        h.review.assert_not_called()

    async def test_next_batch_clears_previous_candidates_in_the_same_thread(self):
        h = Harness()
        first = await h.run({"batch_ref": "batch"})
        self.assertTrue(first["candidates"])
        h.load.return_value = {"batch_ref": "next-batch", "admissions": [], "contexts": {}}
        result = await h.run({"batch_ref": "next-batch"})
        self.assertTrue(result["completed"])
        self.assertEqual(result["candidates"], {})
        self.assertEqual(result["outcomes"], [])
        self.assertEqual(h.generate.await_count, 1)

    async def test_json_checkpoints_and_one_final_marker(self):
        h = Harness()
        result = await h.run({"batch_ref": "batch"})
        self.assertTrue(result["completed"])
        self.assertTrue(result["outcomes"][0]["description_text"].endswith(" [IA]"))
        self.assertEqual(result["outcomes"][0]["description_text"].count("[IA]"), 1)
        self.assertEqual(h.generate.await_count, 1)
        self.assertEqual(h.review.await_count, 1)
        self.assertEqual(h.save.await_count, 1)
        state = await h.graph.aget_state(h.config)
        self.assertTrue(state.values["completed"])

    async def test_existing_ia_description_is_preserved_without_model(self):
        row = admission()
        row["observed_description"] = "Existing [IA]"
        h = Harness([row])
        result = await h.run({"batch_ref": "batch"})
        self.assertEqual(result["outcomes"][0]["status"], "preserved")
        self.assertIsNone(result["outcomes"][0]["document_id"])
        h.generate.assert_not_called()

    async def test_missing_context_is_an_abstention(self):
        h = Harness(contexts={"work-1": ""})
        result = await h.run({"batch_ref": "batch"})
        self.assertEqual(result["outcomes"][0]["status"], "insufficient_context")
        h.generate.assert_not_called()

    async def test_uncertain_generation_is_not_restarted_on_checkpoint_resume(self):
        h = Harness()
        h.generate.side_effect = TimeoutError("Possibly started")
        with self.assertRaises(TimeoutError):
            await h.run({"batch_ref": "batch"})
        result = await h.run(None)
        self.assertEqual(result["outcomes"][0]["status"], "generation_pending")
        self.assertEqual(h.generate.await_count, 1)
        h.review.assert_not_called()

    async def test_uncertain_review_is_not_restarted_on_checkpoint_resume(self):
        h = Harness()
        h.review.side_effect = TimeoutError("Possibly started")
        with self.assertRaises(TimeoutError):
            await h.run({"batch_ref": "batch"})
        result = await h.run(None)
        self.assertEqual(result["outcomes"][0]["reason_code"], "REVIEW_NOT_RECONCILED")
        self.assertEqual(h.generate.await_count, 1)
        self.assertEqual(h.review.await_count, 1)

    async def test_batch_review_uses_one_model_call_for_all_columns(self):
        h = Harness([admission("one"), admission("two")])
        result = await h.run({"batch_ref": "batch"})
        self.assertEqual(len(result["outcomes"]), 2)
        self.assertEqual(h.generate.await_count, 1)
        self.assertEqual(h.review.await_count, 1)

    async def test_rejected_review_never_persists_a_document(self):
        h = Harness()
        h.review.return_value = json.dumps({"batch_ref": "batch", "reviews": [{"work_id": "work-1",
            "verdict": "rejected", "unsupported_claims": ["Unsupported meaning"],
            "evidence_ids": ["static-code-1"], "reason_code": "NOT_GROUNDED"}]})
        result = await h.run({"batch_ref": "batch"})
        self.assertEqual(result["outcomes"][0]["status"], "insufficient_context")
        self.assertIsNone(result["outcomes"][0]["description_text"])

    async def test_review_with_foreign_evidence_is_an_isolated_known_work_failure(self):
        h = Harness()
        h.review.return_value = json.dumps({"batch_ref": "batch", "reviews": [{"work_id": "work-1",
            "verdict": "supported", "unsupported_claims": [], "evidence_ids": ["foreign"], "reason_code": None}]})
        result = await h.run({"batch_ref": "batch"})
        self.assertEqual(result["outcomes"][0]["reason_code"], "REVIEW_INVALID")
        self.assertIsNone(result["outcomes"][0]["document_id"])
        h.save.assert_awaited_once()

    async def test_valid_review_survives_an_invalid_sibling_without_another_model_call(self):
        h = Harness([admission("one"), admission("two")])
        response = json.loads(h.review.return_value)
        response["reviews"][1]["evidence_ids"] = ["foreign"]
        h.review.return_value = json.dumps(response)
        result = await h.run({"batch_ref": "batch"})
        self.assertEqual(result["outcomes"][0]["status"], "documented")
        self.assertEqual(result["outcomes"][1]["status"], "invalid_output")
        self.assertEqual(h.generate.await_count, 1)
        self.assertEqual(h.review.await_count, 1)
        h.save.assert_awaited_once()

    async def test_unknown_work_in_review_rejects_the_batch(self):
        h = Harness()
        response = json.loads(h.review.return_value)
        response["reviews"][0]["work_id"] = "unknown"
        h.review.return_value = json.dumps(response)
        with self.assertRaises(ValueError):
            await h.run({"batch_ref": "batch"})
        h.save.assert_not_called()

    async def test_mixed_scope_is_rejected_before_claiming_or_model_calls(self):
        rows = [admission("one"), admission("two")]
        rows[1]["scope_id"] = "other"
        h = Harness(rows)
        with self.assertRaises(ValueError):
            await h.run({"batch_ref": "batch"})
        self.assertFalse(h.claimed)
        h.generate.assert_not_called()

    async def test_cancellation_before_persistence_is_not_reported_completed(self):
        h = Harness()
        async def active(config):
            if h.review.await_count:
                raise RuntimeError("cancelled")
        h.active.side_effect = active
        with self.assertRaises(RuntimeError):
            await h.run({"batch_ref": "batch"})
        h.save.assert_not_called()

    async def test_cancellation_during_review_claim_does_not_start_review(self):
        h = Harness()
        h.cancel_on_review_claim = True
        async def active(config):
            if h.cancelled:
                raise RuntimeError("cancelled")
        h.active.side_effect = active
        with self.assertRaises(RuntimeError):
            await h.run({"batch_ref": "batch"})
        h.review.assert_not_called()
        h.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
