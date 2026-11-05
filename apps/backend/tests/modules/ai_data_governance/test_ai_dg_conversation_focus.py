import asyncio
from copy import deepcopy
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from app.modules.ai_data_governance.contracts import GovIntent
from app.modules.ai_data_governance.governance_graph import GovernanceNodes


TABLE = "catalog.schema.sources"


def previous_turn():
    return {
        "question": "¿Qué demuestra esa revisión del origen?",
        "question_history": ["Analiza catalog.schema.older", "¿Qué significa verified_at de sources?"],
        "intent": GovIntent(intent="meaning", asset_mentions=[TABLE, TABLE + ".verified_at"]).model_dump(),
        "assets": [{"asset_id": "table", "version_id": "v1", "name": TABLE},
                   {"asset_id": "field", "version_id": "v2", "name": TABLE + ".verified_at"}],
        "evidence": [{"evidence_id": "old-proof", "facts": {"quality": "not-for-reuse"}}],
        "response": {"result_state": "answered", "answer_markdown": "Es la revisión administrativa.",
                     "action_receipts": [{"grant": "old-consent-must-not-transfer"}]},
        "case_preview": {"token": "old-preview-must-not-transfer"},
    }


def harness(node_type=GovernanceNodes):
    return node_type(authorize=AsyncMock(), resolve_assets=AsyncMock(), read=AsyncMock(),
        call_model=AsyncMock(return_value=GovIntent(intent="meaning", asset_mentions=[TABLE]).model_dump_json()),
        capabilities={"read_asset_snapshot"})


def test_recent_completed_focus_reaches_g1_without_reusing_evidence_or_consent():
    nodes = harness()
    state = previous_turn()
    admitted = asyncio.run(nodes.admission(state, {}))
    asyncio.run(nodes.understand(admitted, {}))
    payload = nodes.call_model.await_args.args[1]
    focus = payload["previous_completed_turn"]
    assert focus == {"question": state["question_history"][-1], "intent": "meaning",
                     "asset_names": [TABLE, TABLE + ".verified_at"], "asset_names_omitted": 0,
                     "clarification": None, "result_state": "answered", "reference_only": True}
    assert "old-proof" not in str(focus) and "old-consent" not in str(focus) and "old-preview" not in str(focus)
    assert admitted["assets"] == admitted["evidence"] == []
    assert admitted["case_preview"] == admitted["treatment"] == {}
    nodes.resolve_assets.assert_not_called()
    nodes.read.assert_not_called()
    assert payload["question"] == state["question"]


@pytest.mark.parametrize("change", ["fresh-session", "unfinished-response"])
def test_no_completed_turn_does_not_inherit_stale_focus(change):
    state = previous_turn()
    if change == "fresh-session":
        state["question_history"] = []
    else:
        state["response"] = {}
    state["previous_completed_turn"] = {"asset_names": ["untrusted.stale.focus"]}
    nodes = harness()
    admitted = asyncio.run(nodes.admission(state, {}))
    asyncio.run(nodes.understand(admitted, {}))
    assert admitted["previous_completed_turn"] is None
    assert "previous_completed_turn" not in nodes.call_model.await_args.args[1]


def test_previous_clarification_is_available_to_interpret_the_users_answer():
    state = previous_turn()
    state["question"] = "La segunda."
    clarification = "¿Te refieres a catalog.schema.older o a catalog.schema.sources?"
    state["intent"] = GovIntent(intent="meaning", clarification=clarification).model_dump()
    state["assets"] = []
    state["response"] = {"result_state": "clarification", "answer_markdown": clarification}
    admitted = asyncio.run(harness().admission(state, {}))
    assert admitted["previous_completed_turn"]["clarification"] == clarification
    assert admitted["previous_completed_turn"]["asset_names"] == []


def test_focus_is_bounded_and_reports_omissions_without_rewriting_the_question():
    state = previous_turn()
    state["question"] = "Ahora vuelve expresamente a catalog.schema.older."
    state["assets"] = [{"name": f"catalog.schema.table_{index}"} for index in range(12)]
    admitted = asyncio.run(harness().admission(state, {}))
    assert len(admitted["previous_completed_turn"]["asset_names"]) == 8
    assert admitted["previous_completed_turn"]["asset_names_omitted"] == 4
    assert admitted["question"] == state["question"]


@pytest.mark.parametrize("clarification", ["x" * 2001, {"nested": "unbounded-context"}])
def test_malformed_checkpoint_intent_is_not_forwarded_to_the_model(clarification):
    state = previous_turn()
    state["intent"]["clarification"] = clarification
    nodes = harness()
    admitted = asyncio.run(nodes.admission(state, {}))
    asyncio.run(nodes.understand(admitted, {}))
    assert admitted["previous_completed_turn"] is None
    assert "previous_completed_turn" not in nodes.call_model.await_args.args[1]


def test_assembled_native_runtime_keeps_the_same_completed_focus(monkeypatch):
    from app.modules.ai_data_governance.governance_source import governance_agent_source
    from tests.support.ai_dg_fixtures import binding

    module = ModuleType("governance_completed_focus_fixture")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(governance_agent_source(binding()), "governance_runtime.py", "exec"), module.__dict__)
    state = previous_turn()
    actual = asyncio.run(harness(module.GovernanceNodes).admission(deepcopy(state), {}))
    expected = asyncio.run(harness().admission(deepcopy(state), {}))
    assert actual["previous_completed_turn"] == expected["previous_completed_turn"]
