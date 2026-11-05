"""Evidence-bound consultation; tools and authorization are supplied by trusted code."""

from __future__ import annotations

import json
import asyncio
import re
from datetime import datetime, timezone
from typing import TypedDict

from .contracts import EvidenceReview, GovIntent, GovResponse, GovConsultationResponse, GovDiagnosis, TreatmentDraft
from .case_previews import validate_case_diagnosis, validate_case_treatment
from .model_output import model_json
from .consultation import SourceObservationExpired


class GovernanceState(TypedDict, total=False):
    question: str
    question_history: list[str]
    previous_completed_turn: dict | None
    intent: dict
    assets: list[dict]
    evidence: list[dict]
    review: dict
    gaps: list[str]
    response: dict
    diagnosis: dict
    treatment: dict
    case_preview: dict
    source_recovery_count: int
    source_recovery_pending: bool
    source_recovery_history: list[dict]


READ_OPERATIONS = frozenset({"read_asset_snapshot", "read_lineage", "find_code_usage", "read_quality_results",
                             "read_access_evidence", "read_case"})


def consultation_operations(intent: str, *, requested_change: str | None = None) -> tuple[str, ...]:
    if intent in {"case_followup", "decision"}:
        return ("read_case",)
    if intent == "treatment":
        return ("read_case", "read_asset_snapshot", "read_lineage", "read_quality_results", "find_code_usage")
    if intent == "impact" or (intent == "lineage" and requested_change):
        return ("read_asset_snapshot", "read_lineage", "find_code_usage")
    extra = {"lineage": "read_lineage", "impact": "read_lineage", "usage": "find_code_usage",
        "quality": "read_quality_results", "access": "read_access_evidence", "case_followup": "read_case"}
    return ("read_asset_snapshot", *([extra[intent]] if intent in extra else []))


def has_required_consultation_evidence(intent, evidence):
    required = {"quality": {"QUALITY_ASSESSMENT"}, "lineage": {"RUNTIME_LINEAGE"}, "impact": {"RUNTIME_LINEAGE"},
        "usage": {"STATIC_CODE", "EXECUTION"}, "access": {"ACCESS_OBSERVATION"}, "case_followup": {"CASE_RECORD"}}
    kinds = required.get(intent)
    if intent == "quality":
        return any(row["kind"] == "QUALITY_ASSESSMENT" and measured_quality_evidence(row) for row in evidence)
    return kinds is None or any(row["kind"] in kinds for row in evidence)


def validate_quality_window(facts):
    try:
        measured = datetime.fromisoformat(facts['measured_at'].replace('Z', '+00:00'))
        expires = datetime.fromisoformat(facts['expires_at'].replace('Z', '+00:00'))
        current = (measured.utcoffset() is not None and expires.utcoffset() is not None
            and measured <= datetime.now(timezone.utc) < expires)
    except (KeyError, TypeError, ValueError, AttributeError):
        current = False
    if not current:
        raise ValueError('Quality evidence has no current measurement window')


def validate_quality_windows(evidence):
    for proof in evidence:
        if proof['kind'] == 'QUALITY_ASSESSMENT':
            validate_quality_window(proof['facts'])


def measured_quality_evidence(row):
    facts = row["facts"]
    if (row.get("coverage") != "complete" or facts.get("status") not in {"PASS", "FAIL"} or facts.get("asset_version_id") != row["version_id"]
            or not facts.get("evidence_id") or not facts.get("rule_id")
            or type(facts.get("rule_version")) is not int or facts["rule_version"] < 1):
        return False
    try:
        validate_quality_window(facts)
    except ValueError:
        return False
    result = facts.get("result")
    return bool(isinstance(result, dict) and result.get("method") and result.get("coverage") == "COMPLETE")


def validate_consultation_evidence(rows, assets):
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError("Consultation evidence exceeds its budget")
    admitted = {(a["asset_id"], a["version_id"]) for a in assets}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"evidence_id", "asset_id", "version_id", "kind", "facts", "coverage"}:
            raise ValueError("Invalid consultation evidence fields")
        if (row["asset_id"], row["version_id"]) not in admitted or row["evidence_id"] in seen:
            raise ValueError("Evidence is duplicated or belongs to an unadmitted asset revision")
        if row["coverage"] not in {"complete", "partial"} or not isinstance(row["facts"], dict):
            raise ValueError("Unsupported evidence cannot support a consultation")
        seen.add(row["evidence_id"])
    if len(json.dumps(rows, ensure_ascii=False, allow_nan=False).encode()) > 131072:
        raise ValueError("Consultation evidence exceeds its byte budget")
    return rows


def validate_source_reobservation(previous, current, assets):
    validate_consultation_evidence(current, assets)
    if len(current) != len(previous):
        raise PermissionError('Source recovery changed evidence coverage')
    for old, new in zip(previous, current, strict=True):
        expected = old
        if old['kind'] == 'STATIC_CODE':
            facts = {**old['facts'], **{key:new['facts'][key]
                for key in ('observed_at','expires_at','source_evidence_id')}}
            expected = {**old, 'evidence_id':new['evidence_id'], 'facts':facts}
            if new['evidence_id'] == old['evidence_id']:
                raise PermissionError('Source recovery reused an old proof')
        if new != expected:
            raise PermissionError('Source recovery changed the admitted references')
    validate_quality_windows(current)
    return current


def validate_review(review: EvidenceReview, evidence: list):
    known = {row["evidence_id"] for row in evidence}
    cited = set(review.evidence_ids)
    cited.update(ref for claim in review.supported_claims for ref in claim.evidence_ids)
    if not cited.issubset(known):
        raise ValueError("Evidence review cites an unavailable source")
    if review.coverage == "sufficient" and not review.supported_claims:
        raise ValueError("Sufficient evidence requires a supported finding")
    return review


def validate_governance_response(response: GovResponse, review: EvidenceReview, evidence: list):
    known = {row["evidence_id"] for row in evidence}
    supported = {ref for claim in review.supported_claims for ref in claim.evidence_ids}
    if not set(response.cited_evidence_ids).issubset(known & supported):
        raise ValueError("Response cites evidence not supported by the review")
    if len(response.cited_evidence_ids) != len(set(response.cited_evidence_ids)):
        raise ValueError("Response has duplicate source identifiers")
    visible_text = "\n".join([response.answer_markdown, *response.limitations])
    if re.search(r"(?<![\w`\\])\[\d+\]\(", visible_text):
        raise ValueError("Response citation links are not supported")
    markers = {int(n) for n in re.findall(r"(?<![\w`\\])\[(\d+)\]", visible_text)}
    if not markers.issubset(set(range(1, len(response.cited_evidence_ids) + 1))):
        raise ValueError("Response has an out-of-range citation")
    if response.result_state in {"assigned", "verified", "waiting_decision", "rejected", "verification_failed"} or response.case_ref:
        raise ValueError("A consultation cannot manufacture an action or decision receipt")
    if response.result_state in {"answered", "partial"} and not response.cited_evidence_ids:
        raise ValueError("A factual answer requires admitted evidence")
    if review.coverage == "insufficient" and response.result_state not in {"not_evaluable", "clarification"}:
        raise ValueError("Insufficient evidence cannot produce a factual conclusion")
    return response


def governance_reference_cards(state):
    assets = {(a["asset_id"], a["version_id"]): a["name"] for a in state["assets"]}
    cards = []
    seen = set()
    for proof in state["evidence"]:
        key = proof["evidence_id"]
        name = assets.get((proof["asset_id"], proof["version_id"]))
        if key in seen or not isinstance(name, str) or not name.strip():
            raise ValueError("A source must identify one admitted asset revision")
        seen.add(key)
        facts = proof["facts"]
        description = facts.get("description")
        cards.append({"evidence_id": key, "kind": proof["kind"], "asset_name": name,
            "description_origin": facts.get("description_origin"),
            "description_marked_ia": proof["kind"] == "CATALOG" and isinstance(description, str)
                and description.rstrip().endswith("[IA]"),
            "metadata_observed_at": facts.get("observed_at")})
        if proof["kind"] in {"STATIC_CODE", "RUNTIME_LINEAGE"}:
            rows = facts.get("references" if proof["kind"] == "STATIC_CODE" else "links")
            cards[-1]["relationship_observations"] = {"basis": proof["kind"],
                "count": len(rows) if isinstance(rows, list) else None}
    return cards


def governance_response_input(state):
    payload = {"question": state["question"], "review": state["review"],
        "assets": state["assets"], "references": governance_reference_cards(state),
        "diagnosis": state.get("diagnosis", {}), "treatment": state.get("treatment", {}),
        "action_receipts": []}
    if state["intent"]["intent"] in {"lineage", "impact", "usage"}:
        payload["presentation"] = {"relationship_view_owner": "runtime",
            "model_controls_graph_inclusion": False}
    return payload


def consultation_limitations(*groups):
    limits = list(dict.fromkeys(text for group in groups for text in group))
    if len(limits) <= 32:
        return limits
    return [*limits[:31], f"Hay {len(limits) - 31} limitaciones adicionales en la revisión; el alcance de la respuesta sigue incompleto."]


def explicit_case_reference_requested(question):
    markers = re.finditer(r"(?<![\w.])(?:caso|case)(?![\w.])", question, re.IGNORECASE)
    creation = re.compile(r"\b(?:(?:nuevo|new)|(?:apertura\s+de\s+un|opening\s+of\s+a)|"
        r"(?:abre|abrir|crea|crear|prepara|preparar|propón|propon|proponer|open|create|prepare|propose)\s+(?:(?:un|a)\s+)?)\s*$", re.IGNORECASE)
    existing = re.compile(r"^\s+(?:ya\s+)?(?:existente|anterior|previo|registrado|existing|previous|that\s+already\s+exists)\b", re.IGNORECASE)
    return any(not creation.search(question[:marker.start()]) or existing.search(question[marker.end():]) for marker in markers)


def admitted_case_intent(intent, question, previous_questions):
    if intent.intent not in {"case_followup", "treatment", "decision"} or intent.clarification:
        return intent
    pattern = r"(?<![A-Za-z0-9_-])[0-9a-f]{64}(?![A-Za-z0-9_-])"
    current = set(re.findall(pattern, question))
    explicit_case = explicit_case_reference_requested(question)
    if intent.intent == "treatment" and intent.case_reference is None and intent.asset_mentions and not current and not explicit_case:
        return intent
    selected = next(iter(current)) if len(current) == 1 else None
    if not current and not re.search(r"[A-Za-z0-9_-]{32,}", question):
        history = {ref for text in previous_questions for ref in re.findall(pattern, text)}
        if len(history) == 1 and intent.case_reference in history:
            selected = intent.case_reference
    if selected is None:
        return intent.model_copy(update={"case_reference":None,
            "clarification":"¿Cuál es la referencia del caso de gobierno que quieres consultar?"})
    return intent.model_copy(update={"case_reference":selected})


def previous_completed_turn(state, history):
    """Keep prior context as references; current evidence is admitted again."""
    previous_response, previous_intent = state.get("response"), state.get("intent")
    try:
        previous_intent = GovIntent.model_validate(previous_intent)
    except (ValueError, TypeError):
        return None
    if (not history or not isinstance(previous_response, dict)
            or previous_response.get("result_state") not in ("answered", "partial", "clarification", "not_evaluable")):
        return None
    previous_assets = state.get("assets") or []
    previous_assets = previous_assets if isinstance(previous_assets, list) else []
    names = list(dict.fromkeys(asset["name"] for asset in previous_assets[:32]
        if isinstance(asset, dict) and isinstance(asset.get("name"), str) and 1 <= len(asset["name"]) <= 1024))
    return {"question": history[-1], "intent": previous_intent.intent,
        "asset_names": names[:8], "asset_names_omitted": max(0, len(previous_assets) - min(len(names), 8)),
        "clarification": previous_intent.clarification,
        "result_state": previous_response["result_state"], "reference_only": True}


class GovernanceNodes:
    def __init__(self, *, authorize, resolve_assets, read, call_model, capabilities, reauthorize_evidence=None, prepare_case=None,
                 reobserve_evidence=None):
        self.authorize, self.resolve_assets, self.read = authorize, resolve_assets, read
        self.call_model, self.capabilities = call_model, frozenset(capabilities)
        self.reauthorize_evidence = reauthorize_evidence
        self.prepare_case = prepare_case
        self.reobserve_evidence = reobserve_evidence

    async def check_evidence(self, state, config):
        await self.authorize(config, assets=state['assets'])
        if self.reauthorize_evidence is not None:
            await self.reauthorize_evidence(config, assets=state['assets'], evidence=state['evidence'])
        elif any(proof['kind'] == 'STATIC_CODE' for proof in state['evidence']):
            raise PermissionError('Static source evidence requires current caller admission')
        validate_quality_windows(state['evidence'])

    async def admission(self, state, config):
        await self.authorize(config, assets=[])
        question = state.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise ValueError("A bounded governance question is required")
        history = state.get("question_history", [])
        if not isinstance(history, list) or len(history) > 6 or any(not isinstance(q, str) or not 1 <= len(q) <= 4000 for q in history):
            raise ValueError("Invalid prior user-question context")
        previous_turn = previous_completed_turn(state, history)
        if not history or history[-1] != question.strip():
            history = [*history, question.strip()][-6:]
        return {"question": question.strip(), "question_history": history,
            "previous_completed_turn": previous_turn,
            "assets": [], "evidence": [], "review": {}, "intent": {}, "gaps": [], "response": {},
            "diagnosis": {}, "treatment": {}, "case_preview": {},
            "source_recovery_count":0, "source_recovery_pending":False, "source_recovery_history":[]}

    async def reobserve(self, state, config):
        if (type(state.get('source_recovery_count')) is not int or state['source_recovery_count'] != 1
                or state.get('source_recovery_pending') is not True or not state.get('source_recovery_history')):
            raise PermissionError('Source recovery requires its checkpointed admission')
        if state['source_recovery_history'] != [proof for proof in state['evidence'] if proof['kind']=='STATIC_CODE']:
            raise PermissionError('Source recovery history differs from its observations')
        await self.authorize(config, assets=state['assets'])
        validate_quality_windows(state['evidence'])
        fresh = await self.reobserve_evidence(config, assets=state['assets'], evidence=state['evidence'])
        return {'evidence':validate_source_reobservation(state['evidence'],fresh,state['assets']),
            'source_recovery_pending':False}

    async def understand(self, state, config):
        await self.authorize(config, assets=state.get("assets", []))
        payload = {"question": state["question"], "previous_questions": state["question_history"][:-1]}
        if state.get("previous_completed_turn") is not None:
            payload["previous_completed_turn"] = state["previous_completed_turn"]
        value = await self.call_model("G1", payload, GovIntent.model_json_schema(), config)
        intent = GovIntent.model_validate_json(json.dumps(model_json(value)))
        intent = admitted_case_intent(intent, state["question"], state["question_history"][:-1])
        return {"intent": intent.model_dump(mode="json")}

    async def retrieve(self, state, config):
        await self.authorize(config, assets=state.get("assets", []))
        intent = GovIntent.model_validate(state["intent"])
        if intent.clarification:
            return {"assets": [], "evidence": [], "gaps": []}
        assets = await self.resolve_assets(intent, config)
        if not isinstance(assets, list) or len(assets) > 32:
            raise ValueError("The resolved asset set exceeds its budget")
        for asset in assets:
            if (not isinstance(asset, dict) or set(asset) != {"asset_id", "version_id", "name"}
                    or any(not isinstance(v, str) or not 1 <= len(v) <= 1024 for v in asset.values())):
                raise ValueError("Invalid resolved asset identity")
        evidence, gaps = [], []
        for operation in consultation_operations(intent.intent, requested_change=intent.requested_change):
            if operation not in self.capabilities:
                gaps.append("La evidencia solicitada aún no está disponible: " + operation)
                continue
            if operation in {"read_lineage", "find_code_usage"} and sum(not a['asset_id'].startswith('case:') for a in assets) > 4:
                gaps.append("El alcance excede el límite de cuatro activos para esa lectura: " + operation)
                continue
            await self.authorize(config, assets=assets)
            rows = await self.read(operation, assets, config)
            evidence.extend(validate_consultation_evidence(rows, assets))
        validate_consultation_evidence(evidence, assets)
        return {"assets": assets, "evidence": evidence, "gaps": gaps}

    async def assess(self, state, config):
        await self.check_evidence(state, config)
        if not state["evidence"] or not has_required_consultation_evidence(state["intent"]["intent"], state["evidence"]):
            review = EvidenceReview(evidence_ids=[], supported_claims=[], conflicts=[], gaps=state["gaps"],
                coverage="insufficient", next_action="not_evaluable")
        else:
            payload = {"question": state["question"], "intent": state["intent"], "evidence": state["evidence"], "gaps": state["gaps"]}
            raw = await self.call_model("G3", payload, EvidenceReview.model_json_schema(), config)
            review = EvidenceReview.model_validate_json(json.dumps(model_json(raw)))
            validate_review(review, state["evidence"])
        result = {"review": review.model_dump(mode="json")}
        if state['intent']['intent'] == 'treatment' and review.coverage in {'sufficient','partial'}:
            result.update(await self.prepare_treatment({**state, **result}, config))
        return result

    async def prepare_treatment(self, state, config):
        assets = [row for row in state['assets'] if not row['asset_id'].startswith('case:')]
        if not assets:
            return {}
        await self.check_evidence(state, config)
        payload = {key:state[key] for key in ('question','assets','evidence','review')}
        raw = await self.call_model('G4', payload, GovDiagnosis.model_json_schema(), config)
        diagnosis = validate_case_diagnosis(model_json(raw), state['review'], state['evidence'])
        if not diagnosis['treatment_needed']:
            return {'diagnosis':diagnosis}
        await self.check_evidence(state, config)
        raw = await self.call_model('G5', {'question':state['question'],'assets':assets,'diagnosis':diagnosis,
            'evidence':state['evidence']}, TreatmentDraft.model_json_schema(), config)
        return {'diagnosis':diagnosis, 'treatment':validate_case_treatment(model_json(raw), assets, evidence=state['evidence'])}

    async def respond(self, state, config):
        await self.check_evidence(state, config)
        review = EvidenceReview.model_validate(state["review"])
        if not state["evidence"] or review.coverage == "insufficient":
            text = state["intent"].get("clarification") or "No hay evidencia disponible en el ámbito autorizado para responder esa consulta."
            if state["intent"]["intent"] == "case_followup" and not state["intent"].get("clarification"):
                text = "No encontré ese caso en los registros de gobierno disponibles. No se ha confirmado ninguna acción."
            if state["assets"] and not state["intent"].get("clarification"):
                text = {"quality": "No dispongo de mediciones de calidad vigentes y suficientes para estos activos. Sus descripciones no demuestran la calidad de los datos.",
                    "usage": "La evidencia de uso disponible no basta para responder esta consulta. Una referencia en código no confirma que se haya ejecutado.",
                    "lineage": "No dispongo de linaje verificado suficiente para estos activos; no puedo concluir que estén aislados.",
                    "impact": "No dispongo de linaje verificado suficiente para determinar el impacto del cambio. No se ha confirmado la ausencia de dependencias."}.get(state["intent"]["intent"], text)
            response = GovConsultationResponse(answer_markdown=text, cited_evidence_ids=[], limitations=consultation_limitations(state["gaps"], review.gaps), case_ref=None,
                result_state="clarification" if state["intent"].get("clarification") else "not_evaluable")
        else:
            raw = await self.call_model("G6", governance_response_input(state),
                GovConsultationResponse.model_json_schema(), config)
            if any(proof['kind'] == 'STATIC_CODE' for proof in state['evidence']):
                await self.check_evidence(state, config)
            response = GovConsultationResponse.model_validate_json(json.dumps(model_json(raw)))
            validate_governance_response(response, review, state["evidence"])
        result = {"response": response.model_dump(mode="json")}
        if self.prepare_case is not None and state['intent']['intent'] in {'treatment','decision'} and not state['intent'].get('clarification'):
            await self.check_evidence(state, config)
            result['case_preview'] = await self.prepare_case({**state, **result}, config)
        return result


def recoverable_source_node(function):
    async def invoke(state,config):
        try:
            return await function(state,config)
        except SourceObservationExpired:
            count = state.get('source_recovery_count',0)
            if type(count) is not int or count != 0:
                raise
            static = [proof for proof in state['evidence'] if proof['kind'] == 'STATIC_CODE']
            if not static:
                raise
            return {'source_recovery_count':1, 'source_recovery_pending':True,
                'source_recovery_history':static, 'review':{}, 'diagnosis':{}, 'treatment':{},
                'response':{}, 'case_preview':{}}
    return invoke


def observed_governance_node(name, function, observer):
    async def invoke(state,config):
        observer(name,"started",state)
        try:
            result=await function(state,config)
        except (Exception,asyncio.CancelledError):
            observer(name,"failed",state)
            raise
        observer(name,"completed",{**state,**result})
        return result
    return invoke


def build_governance_graph(*, authorize, resolve_assets, read, call_model, capabilities, checkpointer, reauthorize_evidence=None,
                           observer=None, prepare_case=None, reobserve_evidence=None):
    from langgraph.graph import START, END, StateGraph

    if checkpointer is None or not all(callable(v) for v in (authorize, resolve_assets, read, call_model)):
        raise ValueError("Native authorization, tools and persistent memory are required")
    if not frozenset(capabilities).issubset(READ_OPERATIONS):
        raise ValueError("Unknown consultation capability")
    for callback,message in ((reauthorize_evidence,'Evidence admission must be callable'),
            (observer,'Activity observation must be callable'),
            (prepare_case,'Case preview requires an admitted callback'),
            (reobserve_evidence,'Source recovery requires an admitted callback')):
        if callback is not None and not callable(callback):
            raise ValueError(message)
    nodes = GovernanceNodes(authorize=authorize, resolve_assets=resolve_assets, read=read,
        call_model=call_model, capabilities=capabilities, reauthorize_evidence=reauthorize_evidence,
        prepare_case=prepare_case,reobserve_evidence=reobserve_evidence)
    graph = StateGraph(GovernanceState)
    order = ("admission", "understand", "retrieve", "assess", "respond")
    for name in order:
        function=getattr(nodes,name)
        if observer is not None:
            function=observed_governance_node(name,function,observer)
        if reobserve_evidence is not None and name in {'assess','respond'}:
            function=recoverable_source_node(function)
        graph.add_node(name,function)
    graph.add_edge(START, order[0])
    for before, after in zip(order[:3], order[1:4]):
        graph.add_edge(before, after)
    if reobserve_evidence is not None:
        function=nodes.reobserve
        if observer is not None:
            function=observed_governance_node('reobserve',function,observer)
        graph.add_node('reobserve',function)
        graph.add_conditional_edges('assess',lambda state: bool(state.get('source_recovery_pending')),
            {True:'reobserve',False:'respond'})
        graph.add_conditional_edges('respond',lambda state: bool(state.get('source_recovery_pending')),
            {True:'reobserve',False:END})
        graph.add_edge('reobserve','assess')
    else:
        graph.add_edge('assess','respond')
        graph.add_edge('respond', END)
    return graph.compile(checkpointer=checkpointer)
