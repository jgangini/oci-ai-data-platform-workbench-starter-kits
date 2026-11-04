import asyncio
import json
from contextlib import nullcontext
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.ai_data_governance import native_governance
from app.ai_data_governance.contracts import EvidenceReview, GovResponse
from app.ai_data_governance.governance_graph import GovernanceNodes, validate_governance_response


def answer_state():
    return {
        "assets": [{"asset_id": "asset", "version_id": "version", "name": "oci_medallion.oci_artifacts.ai_mi_research_runs"}],
        "evidence": [{"evidence_id": "proof", "asset_id": "asset", "version_id": "version", "kind": "CATALOG",
            "facts": {"description": "Registra investigaciones. [IA]", "description_origin": "EXTERNAL_UNVERIFIED"},
            "coverage": "complete"}],
        "response": {"answer_markdown": "Según el catálogo, registra investigaciones. [1]", "cited_evidence_ids": ["proof"],
            "limitations": [], "case_ref": None, "result_state": "answered"},
    }


def reviewed(state):
    ids = [e["evidence_id"] for e in state["evidence"]]
    return EvidenceReview(evidence_ids=ids,
        supported_claims=[{"text": "El catálogo describe investigaciones.", "evidence_ids": ids, "kind": "observed"}],
        conflicts=[], gaps=[], coverage="sufficient", next_action="answer")


def test_native_answer_contains_usable_sources_without_additional_kwargs():
    text = native_governance.render_governance_response(answer_state())
    assert text.startswith("Según el catálogo, registra investigaciones. [1]")
    assert "[1] Catálogo:" in text and "ai_mi_research_runs" in text
    assert "marcada [IA]" in text
    assert "proof" not in text and "EXTERNAL_UNVERIFIED" not in text
    assert "verificada por IA" not in text and "cumple calidad" not in text


def test_source_numbers_follow_cited_ids_instead_of_evidence_storage_order():
    state = answer_state()
    state["assets"].append({"asset_id": "other", "version_id": "v2", "name": "catalog.schema.orders"})
    state["evidence"].append({"evidence_id": "second", "asset_id": "other", "version_id": "v2",
        "kind": "STATIC_CODE", "facts": {}, "coverage": "partial"})
    state["response"].update(answer_markdown="Referencia estática [1]; descripción del catálogo [2].",
        cited_evidence_ids=["second", "proof"])
    text = native_governance.render_governance_response(state)
    assert text.index("[1] Referencia estática:") < text.index("[2] Catálogo:")
    assert "catalog.schema.orders" in text and "ai_mi_research_runs" in text


@pytest.mark.parametrize("name", ["catalog.schema.col`umn", "catalog.schema.<img src=x>\n[enlace](https://example.com)"])
def test_asset_names_cannot_create_markdown_links_or_extra_lines(name):
    state = answer_state()
    state["assets"][0]["name"] = name
    text = native_governance.render_governance_response(state)
    assert "\n[enlace]" not in text
    assert "` catalog.schema." in text or "`` catalog.schema." in text


def test_empty_answer_has_no_manufactured_source_list_and_shows_material_limit_once():
    state = {"assets": [], "evidence": [], "response": {"answer_markdown": "No hay mediciones vigentes.",
        "cited_evidence_ids": [], "limitations": ["No hay mediciones vigentes."], "case_ref": None,
        "result_state": "not_evaluable"}}
    assert native_governance.render_governance_response(state) == "No hay mediciones vigentes."


def test_structured_material_limit_is_visible_if_not_already_in_answer():
    state = answer_state()
    state["response"]["limitations"] = ["No hay mediciones de calidad vigentes."]
    text = native_governance.render_governance_response(state)
    assert text.count("No hay mediciones de calidad vigentes.") == 1


@pytest.mark.parametrize("change", ["missing-proof", "wrong-version", "duplicate-proof"])
def test_renderer_rejects_unresolvable_or_duplicate_sources(change):
    state = answer_state()
    if change == "missing-proof":
        state["response"]["cited_evidence_ids"] = ["unknown"]
    elif change == "wrong-version":
        state["evidence"][0]["version_id"] = "foreign-version"
    else:
        state["response"]["cited_evidence_ids"] *= 2
    with pytest.raises(ValueError):
        native_governance.render_governance_response(state)


@pytest.mark.parametrize("text,ids", [("Texto [2]", ["proof"]), ("Texto [0]", ["proof"]),
    ("Texto [1]", ["proof", "proof"])])
def test_response_rejects_out_of_range_citations_and_duplicate_source_ids(text, ids):
    state = answer_state()
    response = GovResponse.model_validate({**state["response"], "answer_markdown": text, "cited_evidence_ids": ids})
    with pytest.raises(ValueError, match="citation|source"):
        validate_governance_response(response, reviewed(state), state["evidence"])


@pytest.mark.parametrize("field,text", [("answer_markdown", "Texto [2](https://example.invalid)"),
    ("answer_markdown", "Texto [1](https://example.invalid)"), ("limitations", "Sin medición [2]")])
def test_all_visible_response_citations_must_resolve_without_untrusted_links(field, text):
    state = answer_state()
    state["response"][field] = [text] if field == "limitations" else text
    response = GovResponse.model_validate(state["response"])
    with pytest.raises(ValueError, match="citation"):
        validate_governance_response(response, reviewed(state), state["evidence"])


@pytest.mark.parametrize("origin", [None, "EXTERNAL_UNVERIFIED", "unknown"])
def test_unverified_description_does_not_become_verified_authorship(origin):
    state = answer_state()
    state["evidence"][0]["facts"]["description_origin"] = origin
    text = native_governance.render_governance_response(state)
    assert "Descripción marcada [IA]." in text
    assert "verificada" not in text


def test_final_model_receives_numberable_reference_cards_without_raw_metadata():
    state = answer_state()
    state.update(question="Qué representa la tabla?", intent={"intent": "meaning"}, gaps=[],
        review=reviewed(state).model_dump(mode="json"))
    model = AsyncMock(return_value=json.dumps(state["response"]))
    nodes = GovernanceNodes(authorize=AsyncMock(), resolve_assets=AsyncMock(), read=AsyncMock(),
        call_model=model, capabilities=())
    assert asyncio.run(nodes.respond(state, {}))["response"] == GovResponse.model_validate(state["response"]).model_dump(mode="json")
    phase, payload, _, _ = model.call_args.args
    assert phase == "G6"
    assert payload["references"] == [{"evidence_id": "proof", "kind": "CATALOG",
        "asset_name": state["assets"][0]["name"], "description_origin": "EXTERNAL_UNVERIFIED",
        "description_marked_ia": True, "metadata_observed_at": None}]
    assert "facts" not in payload["references"][0]


def test_g6_schema_excludes_controller_states_before_a_case_is_registered():
    state = answer_state()
    state.update(question="Prepara un tratamiento, sin registrarlo.", intent={"intent": "treatment"},
        gaps=[], review=reviewed(state).model_dump(mode="json"))
    model = AsyncMock(return_value=json.dumps(state["response"]))
    nodes = GovernanceNodes(authorize=AsyncMock(), resolve_assets=AsyncMock(), read=AsyncMock(),
        call_model=model, capabilities=())
    asyncio.run(nodes.respond(state, {}))
    schema = model.call_args.args[2]
    assert "waiting_decision" in GovResponse.model_json_schema()["properties"]["result_state"]["enum"]
    assert set(schema["properties"]["result_state"]["enum"]) == {
        "answered", "partial", "clarification", "denied", "not_evaluable"}
    assert schema["properties"]["case_ref"]["type"] == "null"


@pytest.mark.parametrize("invalid", [{"result_state": value} for value in
    ("waiting_decision", "rejected", "assigned", "verification_failed", "verified")]
    + [{"case_ref": "unregistered-case"}])
def test_consultative_response_never_repairs_a_manufactured_transition(invalid):
    state = answer_state()
    state.update(question="Prepara un tratamiento.", intent={"intent": "treatment"},
        gaps=[], review=reviewed(state).model_dump(mode="json"))
    model = AsyncMock(return_value=json.dumps({**state["response"], **invalid}))
    preview = AsyncMock()
    nodes = GovernanceNodes(authorize=AsyncMock(), resolve_assets=AsyncMock(), read=AsyncMock(),
        call_model=model, capabilities=(), prepare_case=preview)
    with pytest.raises(ValueError):
        asyncio.run(nodes.respond(state, {}))
    preview.assert_not_awaited()


def test_valid_consultative_treatment_reaches_preview_exactly_once():
    state = answer_state()
    state.update(question="Prepara un tratamiento, sin registrarlo.", intent={"intent": "treatment"},
        gaps=[], review=reviewed(state).model_dump(mode="json"))
    response = {**state["response"], "answer_markdown": "Propongo obtener una medición pertinente. [1]",
        "result_state": "partial"}
    preview = AsyncMock(return_value={"available": True, "action": "OPEN"})
    nodes = GovernanceNodes(authorize=AsyncMock(), resolve_assets=AsyncMock(), read=AsyncMock(),
        call_model=AsyncMock(return_value=json.dumps(response)), capabilities=(), prepare_case=preview)
    result = asyncio.run(nodes.respond(state, {}))
    preview.assert_awaited_once()
    assert preview.call_args.args[0]["response"]["result_state"] == "partial"
    assert result["case_preview"] == {"available": True, "action": "OPEN"}
    assert result["response"]["case_ref"] is None


def test_normal_native_invoke_exposes_sources_in_message_content(monkeypatch):
    state = answer_state()
    helper = SimpleNamespace(pre_invoke_setup=lambda **kw: {}, flow_setup=lambda **kw: nullcontext())
    monkeypatch.setitem(sys.modules, "aidputils.agents.toolkit.agent_helper", helper)
    monkeypatch.setitem(sys.modules, "aidputils.agents.auth.util", SimpleNamespace(auth_utils=SimpleNamespace(get_auth_context=Mock())))
    monkeypatch.setattr(native_governance, "consultation_config", lambda *args: {})
    caller = SimpleNamespace(check_identity=Mock(), session=SimpleNamespace(close=Mock()))
    monkeypatch.setattr(native_governance, "native_central_transport", Mock(return_value=caller))
    hooks = SimpleNamespace(authorize=AsyncMock(), resolve_assets=AsyncMock(), read=AsyncMock(), _read_admitted=AsyncMock(),
        capabilities=(), reauthorize_evidence=AsyncMock(), _validate_admitted_evidence=AsyncMock(), _reobserve_admitted_evidence=AsyncMock())
    monkeypatch.setattr(native_governance, "ConsultationHooks", Mock(return_value=hooks))
    graph = SimpleNamespace(ainvoke=AsyncMock(return_value=state))
    monkeypatch.setattr(native_governance, "build_governance_graph", Mock(return_value=graph))
    agent = native_governance.DataGovernanceAgent()
    agent.binding = {"component_revision": "test-revision"}
    agent.memory = object()
    message = asyncio.run(agent.invoke("Qué representa la tabla?"))["messages"][0]
    assert "[1] Catálogo:" in message.content and "ai_mi_research_runs" in message.content
    assert message.additional_kwargs["governance"] == state["response"]
    assert message.additional_kwargs["component_revision"] == "test-revision"
    caller.session.close.assert_called_once()


def test_model_uses_short_sources_and_runtime_restores_exact_native_identifiers():
    state = answer_state()
    state['evidence'] = [{**state['evidence'][0], 'evidence_id': str(n).zfill(64)} for n in range(14)]
    payload, aliases = native_governance.consultation_model_sources('G3', {'evidence': state['evidence']})
    assert [e['evidence_id'] for e in payload['evidence']] == ['E' + str(n) for n in range(1, 15)]
    review = {'evidence_ids':['E1','E14'], 'supported_claims':[{'text':'Inventario observado',
        'evidence_ids':['E1','E14'], 'kind':'observed'}], 'conflicts':[], 'gaps':[],
        'coverage':'sufficient','next_action':'answer','schema_version':1}
    restored = json.loads(native_governance.restore_consultation_sources('G3', json.dumps(review), aliases))
    assert restored['evidence_ids'] == [str(0).zfill(64), str(13).zfill(64)]
    assert restored['supported_claims'][0]['evidence_ids'] == restored['evidence_ids']
    assert state['evidence'][0]['evidence_id'] == str(0).zfill(64)
    refs = [{'evidence_id':e['evidence_id']} for e in state['evidence']]
    output_payload, output_aliases = native_governance.consultation_model_sources('G6', {'references':refs,'review':restored})
    assert output_payload['review']['evidence_ids'] == ['E1','E14']
    response = native_governance.restore_consultation_sources('G6', json.dumps({'cited_evidence_ids':['E14','E1']}), output_aliases)
    assert json.loads(response)['cited_evidence_ids'] == [str(13).zfill(64), str(0).zfill(64)]


@pytest.mark.parametrize('phase,output', [('G3', {'evidence_ids':['E15'],'supported_claims':[]}),
    ('G3', {'evidence_ids':['E1'],'supported_claims':[{'evidence_ids':['unknown']}]}),
    ('G6', {'cited_evidence_ids':['unknown']})])
def test_model_source_aliases_do_not_repair_or_guess_unavailable_references(phase, output):
    with pytest.raises(ValueError, match='admitted source'):
        native_governance.restore_consultation_sources(phase, json.dumps(output), {'E1':'proof'})


@pytest.mark.parametrize('intent,required', [('quality','mediciones de calidad'), ('usage','evidencia de uso'),
    ('lineage','linaje verificado'), ('impact','impacto del cambio')])
def test_insufficient_evidence_names_the_missing_information_without_a_model_claim(intent, required):
    state = answer_state()
    state.update(intent={'intent':intent}, gaps=[], review={'evidence_ids':[], 'supported_claims':[],
        'conflicts':[], 'gaps':[], 'coverage':'insufficient', 'next_action':'not_evaluable'})
    model = AsyncMock()
    nodes = GovernanceNodes(authorize=AsyncMock(), resolve_assets=AsyncMock(), read=AsyncMock(),
        call_model=model, capabilities=())
    response = asyncio.run(nodes.respond(state, {}))['response']
    assert required in response['answer_markdown']
    assert response['result_state'] == 'not_evaluable' and response['cited_evidence_ids'] == []
    model.assert_not_awaited()


def test_insufficient_execution_evidence_does_not_deny_an_available_static_reference():
    state = answer_state()
    state['evidence'][0].update(kind='STATIC_CODE', coverage='partial')
    state.update(intent={'intent':'usage'}, gaps=[], review={'evidence_ids':['proof'], 'supported_claims':[],
        'conflicts':[], 'gaps':['Falta ejecución comprobada'], 'coverage':'insufficient', 'next_action':'not_evaluable'})
    nodes = GovernanceNodes(authorize=AsyncMock(), resolve_assets=AsyncMock(), read=AsyncMock(),
        call_model=AsyncMock(), capabilities=(), reauthorize_evidence=AsyncMock())
    text = asyncio.run(nodes.respond(state, {}))['response']['answer_markdown']
    assert 'no basta' in text and 'no confirma que se haya ejecutado' in text
    assert 'No dispongo de referencias' not in text


@pytest.mark.parametrize('missing_fields', [[], ['table']])
def test_clarification_does_not_read_tools_or_expose_unrelated_missing_capabilities(missing_fields):
    intent = {'intent':'case_followup', 'asset_mentions':[], 'missing_fields':missing_fields,
        'clarification':'¿A qué tabla te refieres?'}
    resolve, read = AsyncMock(return_value=[]), AsyncMock(return_value=[])
    nodes = GovernanceNodes(authorize=AsyncMock(), resolve_assets=resolve, read=read,
        call_model=AsyncMock(), capabilities=('read_asset_snapshot',))
    result = asyncio.run(nodes.retrieve({'assets':[], 'intent':intent}, {}))
    assert result == {'assets':[], 'evidence':[], 'gaps':[]}
    resolve.assert_not_awaited()
    read.assert_not_awaited()
