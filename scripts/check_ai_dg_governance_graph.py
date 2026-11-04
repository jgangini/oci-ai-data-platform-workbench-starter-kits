import asyncio
import json
from unittest.mock import AsyncMock

import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/backend"))
from langgraph.checkpoint.memory import InMemorySaver

from app.ai_data_governance.governance_graph import build_governance_graph
from app.ai_data_governance.prompts import governance_prompts


def harness(evidence=None):
    assets = [{"asset_id": "asset", "version_id": "version", "name": "Facturas"}]
    evidence = [{"evidence_id": "proof", "asset_id": "asset", "version_id": "version", "kind": "CATALOG",
        "facts": {"description": "Facturas emitidas"}, "coverage": "complete"}] if evidence is None else evidence
    intent = {"intent": "meaning", "asset_mentions": ["Facturas"]}
    review = {"evidence_ids": ["proof"], "supported_claims": [{"text": "Contiene facturas emitidas.",
        "evidence_ids": ["proof"], "kind": "observed"}], "conflicts": [], "gaps": [],
        "coverage": "sufficient", "next_action": "answer"}
    response = {"answer_markdown": "Contiene facturas emitidas.", "cited_evidence_ids": ["proof"],
        "limitations": [], "case_ref": None, "result_state": "answered"}
    outputs = {"G1": intent, "G3": review, "G6": response}
    model = AsyncMock(side_effect=lambda node, *_: json.dumps(outputs[node]))
    authorize = AsyncMock()
    resolve = AsyncMock(return_value=assets)
    read = AsyncMock(return_value=evidence)
    graph = build_governance_graph(authorize=authorize, resolve_assets=resolve, read=read, call_model=model,
        capabilities={"read_asset_snapshot"}, checkpointer=InMemorySaver())
    return graph, authorize, resolve, read, model, outputs


def invoke(graph, question="¿Qué contiene Facturas?", thread="test-user-thread"):
    return asyncio.run(graph.ainvoke({"question": question}, config={"configurable": {"thread_id": thread}, "recursion_limit": 10}))


def test_actual_stategraph_answers_with_admitted_evidence_and_no_mutation_tools():
    graph, auth, resolve, read, model, _ = harness()
    result = invoke(graph)
    assert result["response"]["answer_markdown"] == "Contiene facturas emitidas."
    assert result["response"]["cited_evidence_ids"] == ["proof"]
    assert [c.args[0] for c in model.call_args_list] == ["G1", "G3", "G6"]
    assert read.call_args.args[0] == "read_asset_snapshot"
    assert auth.await_count >= 5


def test_denied_execution_does_not_call_model_resolver_or_tools():
    graph, auth, resolve, read, model, _ = harness()
    auth.side_effect = PermissionError("denied")
    with unittest.TestCase().assertRaises(PermissionError):
        invoke(graph)
    resolve.assert_not_called()
    read.assert_not_called()
    model.assert_not_called()


def test_empty_catalog_returns_no_evidence_without_fake_quality_or_extra_model_calls():
    graph, _, resolve, read, model, _ = harness([])
    resolve.return_value = []
    result = invoke(graph)
    assert result["response"]["result_state"] == "not_evaluable"
    assert result["response"]["cited_evidence_ids"] == []
    assert model.await_count == 1


def test_tool_evidence_for_another_version_is_rejected_before_answer():
    graph, _, _, read, model, _ = harness()
    read.return_value[0]["version_id"] = "unadmitted-version"
    with unittest.TestCase().assertRaisesRegex(ValueError, "unadmitted"):
        invoke(graph)
    assert model.await_count == 1


def test_model_cannot_invent_sources_action_receipts_or_a_verified_result():
    cases = [("G3", {"evidence_ids": ["invented"]}), ("G6", {"cited_evidence_ids": ["invented"]}),
             ("G6", {"result_state": "verified"}), ("G6", {"case_ref": "invented-case"})]
    for target, changes in cases:
        graph, _, _, _, _, outputs = harness()
        outputs[target].update(changes)
        with unittest.TestCase().assertRaises(ValueError):
            invoke(graph)


def test_checkpoint_reuse_clears_previous_evidence_and_rechecks_authority():
    async def check():
        graph, auth, resolve, read, model, _ = harness()
        config = {"configurable": {"thread_id": "same-session"}, "recursion_limit": 10}
        first = await graph.ainvoke({"question": "¿Qué contiene Facturas?"}, config=config)
        assert first["evidence"]
        resolve.return_value = []
        read.return_value = []
        second = await graph.ainvoke({"question": "¿Y ahora?"}, config=config)
        assert second["evidence"] == [] and second["response"]["result_state"] == "not_evaluable"
        auth.side_effect = PermissionError("revoked")
        with unittest.TestCase().assertRaises(PermissionError):
            await graph.ainvoke({"question": "¿Y ahora?"}, config=config)
    asyncio.run(check())


def test_only_available_capabilities_are_called_and_prompts_load_from_canonical_source():
    graph, _, _, read, _, outputs = harness()
    outputs["G1"]["intent"] = "quality"
    result = invoke(graph, "¿La calidad está aprobada?")
    assert read.await_count == 1
    assert "read_quality_results" in result["gaps"][0]
    assert result["response"]["result_state"] == "not_evaluable"
    assert set(governance_prompts()) == {"G0", "G1", "G2", "G3", "G4", "G5", "G6"}


def test_padded_oversize_question_is_rejected_before_model_or_tool_calls():
    graph, _, resolve, read, model, _ = harness()
    with unittest.TestCase().assertRaises(ValueError):
        invoke(graph, " " * 5000 + "?")
    model.assert_not_called()
    resolve.assert_not_called()
    read.assert_not_called()


def test_unmeasured_expired_or_foreign_quality_cannot_enable_a_factual_quality_answer():
    from datetime import datetime, timedelta, timezone
    from app.ai_data_governance.governance_graph import measured_quality_evidence
    now = datetime.now(timezone.utc)
    facts = {"status": "PASS", "asset_version_id": "version", "evidence_id": "measurement",
        "rule_id": "null-rate", "rule_version": 1, "measured_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(), "result": {"method": "COUNT_NULLS", "coverage": "COMPLETE"}}
    row = {"version_id": "version", "facts": facts, "coverage": "complete"}
    assert measured_quality_evidence(row)
    for changes in ({"status": "NOT_MEASURED"}, {"measured_at": None}, {"asset_version_id": "other"},
            {"expires_at": (now - timedelta(minutes=1)).isoformat()}, {"result": {}}):
        assert not measured_quality_evidence({**row, "facts": {**facts, **changes}})
    assert not measured_quality_evidence({**row, "coverage": "partial"})


def test_follow_up_reuses_only_user_questions_and_reauthorizes_admitted_assets():
    async def check():
        graph, auth, _, _, model, _ = harness()
        config = {"configurable": {"thread_id": "history-session"}, "recursion_limit": 10}
        await graph.ainvoke({"question": "¿Qué contiene Facturas?"}, config=config)
        await graph.ainvoke({"question": "¿Y su linaje?"}, config=config)
        intents = [c for c in model.call_args_list if c.args[0] == "G1"]
        assert intents[-1].args[1]["previous_questions"] == ["¿Qué contiene Facturas?"]
        assert "evidence" not in intents[-1].args[1] and "response" not in intents[-1].args[1]
        assert any(c.kwargs.get("assets") == [{"asset_id": "asset", "version_id": "version", "name": "Facturas"}]
            for c in auth.await_args_list)
    asyncio.run(check())


if __name__ == "__main__":
    suite = unittest.TestSuite(unittest.FunctionTestCase(value) for name, value in list(globals().items())
        if name.startswith("test_") and callable(value))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
