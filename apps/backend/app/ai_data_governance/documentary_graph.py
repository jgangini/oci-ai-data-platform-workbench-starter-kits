"""Bounded documentary StateGraph; authority/effects belong to trusted hooks.

Hooks load admitted work, claim generation durably, call the selected model and
persist each outcome. None is selected or supplied by the model. Native adapters
must implement these hooks; compiling this graph does not deploy an AIDP agent.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from typing import TypedDict

from .contracts import DocumentReviews, DocumentationResult
from .documentation import DocumentationAdmission, AdmittedDraft, admit_documentation_items
from .documentation import documentation_generation_key
from .documentation import admit_document_reviews
from .inventory import identity
from .model_output import model_json


class DocumentaryState(TypedDict, total=False):
    batch_ref: str
    admissions: list[dict]
    contexts: dict[str, str]
    selected: list[dict]
    outcomes: list[dict]
    candidates: dict
    completed: bool


def _admission(raw: dict) -> DocumentationAdmission:
    if not isinstance(raw, dict):
        raise ValueError("A documentary admission must be an object")
    # JSON checkpoints encode tuples as arrays; strict JSON validation preserves
    # the contract without permitting Python coercion of booleans or integers.
    return DocumentationAdmission.model_validate_json(json.dumps(raw, allow_nan=False))


def _batch_snapshot(batch_ref: str, value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"batch_ref", "admissions", "contexts"} or value["batch_ref"] != batch_ref:
        raise ValueError("The durable batch does not match the requested reference")
    admissions = _batch_admissions(value["admissions"])
    contexts = _batch_contexts(admissions, value["contexts"])
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 131072:
        raise ValueError("Documentary batch exceeds its total input bound")
    return {"admissions": [a.model_dump(mode="json") for a in admissions], "contexts": contexts}


def _batch_admissions(rows: list[dict]) -> tuple[DocumentationAdmission, ...]:
    if not isinstance(rows, list) or len(rows) > 16:
        raise ValueError("The admitted work must be an array")
    admissions = tuple(_admission(row) for row in rows)
    if len({a.work_id for a in admissions}) != len(admissions):
        raise ValueError("Invalid documentary work set")
    if len({(a.scope_id, a.run_id) for a in admissions}) > 1:
        raise ValueError("A documentary batch cannot mix scopes or executions")
    return admissions


def _batch_contexts(admissions: tuple, contexts: dict) -> dict:
    if not isinstance(contexts, dict) or set(contexts) != {a.work_id for a in admissions}:
        raise ValueError("Missing or unadmitted documentary context")
    if any(not isinstance(text, str) or len(text.encode("utf-8")) > 65536 for text in contexts.values()):
        raise ValueError("Documentary context exceeds its bound")
    return dict(contexts)


def _initial_outcome(admission: DocumentationAdmission, context: str) -> AdmittedDraft | None:
    if admission.observed_description and admission.observed_description.strip():
        return AdmittedDraft(admission.work_id, "preserved", "DESCRIPTION_NOT_EMPTY")
    if not admission.update_supported:
        return AdmittedDraft(admission.work_id, "unsupported", "DESTINATION_UNSUPPORTED")
    if not context.strip() or not admission.evidence_ids:
        return AdmittedDraft(admission.work_id, "insufficient_context", "USAGE_CONTEXT_MISSING")
    return None


class DocumentaryNodes:
    def __init__(self, *, load_batch, claim_generation, generate, review, save_outcomes, check_active):
        self.load_batch = load_batch
        self.claim_generation = claim_generation
        self.generate = generate
        self.review = review
        self.save_outcomes = save_outcomes
        self.check_active = check_active

    async def load(self, state: DocumentaryState, config):
        await self.check_active(config)
        if not isinstance(state.get("batch_ref"), str) or not 1 <= len(state["batch_ref"]) <= 256:
            raise ValueError("A bounded documentary batch reference is required")
        value = await self.load_batch(state["batch_ref"], config)
        return {**_batch_snapshot(state["batch_ref"], value), "outcomes": [], "selected": [],
                "candidates": {}, "completed": False}

    async def select(self, state: DocumentaryState, config):
        selected, outcomes = [], []
        for raw in state["admissions"]:
            admission = _admission(raw)
            outcome = _initial_outcome(admission, state["contexts"][admission.work_id])
            if outcome is not None:
                outcomes.append(asdict(outcome))
                continue
            selected.append(raw)
        return {"selected": selected, "outcomes": outcomes}

    async def document(self, state: DocumentaryState, config):
        await self.check_active(config)
        selected, outcomes = await self._claim_documents(state, config)
        if not selected:
            return {"selected": [], "outcomes": outcomes}
        payload = {"batch_ref": state["batch_ref"], "admissions": selected,
                   "contexts": {row["work_id"]: state["contexts"][row["work_id"]] for row in selected}}
        await self.check_active(config)
        response = await self.generate(payload, DocumentationResult.model_json_schema(), config)
        candidates = model_json(response)
        admissions = tuple(_admission(row) for row in selected)
        drafts = admit_documentation_items(state["batch_ref"], admissions, candidates)
        return {"selected": selected, "candidates": candidates, "outcomes": [*outcomes, *[asdict(item) for item in drafts]]}

    async def _claim_documents(self, state: DocumentaryState, config) -> tuple[list, list]:
        selected, outcomes = [], list(state["outcomes"])
        for raw in state["selected"]:
            admission = _admission(raw)
            await self.check_active(config)
            claimed = await self.claim_generation("document", raw, documentation_generation_key(admission), config)
            if claimed is True:
                selected.append(raw)
            else:
                outcomes.append(asdict(AdmittedDraft(admission.work_id, "generation_pending", "GENERATION_NOT_RECONCILED")))
        return selected, outcomes

    async def validate(self, state: DocumentaryState, config):
        candidates = [outcome for outcome in state["outcomes"] if outcome["status"] == "documented"]
        if not candidates:
            return {"outcomes": state["outcomes"]}
        await self.check_active(config)
        payload = {"batch_ref": state["batch_ref"], "candidates": candidates,
                   "contexts": {item["work_id"]: state["contexts"][item["work_id"]] for item in candidates}}
        review_key = identity("AI_DG_REVIEW", sorted(item["document_id"] for item in candidates))
        if await self.claim_generation("review", {"batch_ref": state["batch_ref"]}, review_key, config) is not True:
            pending = {item["work_id"] for item in candidates}
            return {"outcomes": [asdict(AdmittedDraft(item["work_id"], "generation_pending", "REVIEW_NOT_RECONCILED"))
                    if item["work_id"] in pending else item for item in state["outcomes"]]}
        await self.check_active(config)
        response = model_json(await self.review(payload, DocumentReviews.model_json_schema(), config))
        candidate_ids = {item["work_id"] for item in candidates}
        admissions = tuple(_admission(raw) for raw in state["selected"] if raw["work_id"] in candidate_ids)
        reviews, failures = admit_document_reviews(state["batch_ref"], admissions, response)
        return {"outcomes": _reviewed_outcomes(state["outcomes"], reviews, failures)}

    async def persist(self, state: DocumentaryState, config):
        await self.check_active(config)
        await self.save_outcomes(state["batch_ref"], state["admissions"], state["outcomes"], config)
        return {"completed": True}


def _reviewed_outcomes(candidates: list[dict], reviews: dict, failures: dict) -> list[dict]:
    outcomes = []
    for outcome in candidates:
        if outcome["status"] != "documented":
            outcomes.append(outcome)
            continue
        if outcome["work_id"] in failures:
            outcomes.append(asdict(AdmittedDraft(outcome["work_id"], "invalid_output", failures[outcome["work_id"]])))
            continue
        result = reviews[outcome["work_id"]]
        if result.verdict == "supported" and not result.unsupported_claims and result.evidence_ids:
            outcomes.append(outcome)
        else:
            outcomes.append(asdict(AdmittedDraft(outcome["work_id"], "insufficient_context", "DOCUMENT_NOT_GROUNDED")))
    return outcomes


def build_documentary_graph(*, load_batch, claim_generation, generate, review, save_outcomes,
                            check_active, checkpointer):
    """One pass, no automatic model retry, no publish/catalog/DDL tools.

    claim_generation(phase, admission, key, config) must durably exclude a second
    start for each document or review key, including checkpoint resumption.
    False means unresolved or previously claimed work: do not
    call the model again. save_outcomes must reconcile its own partial commits.
    """
    from langgraph.graph import END, START, StateGraph

    if checkpointer is None or not all(callable(hook) for hook in (
        load_batch, claim_generation, generate, review, save_outcomes, check_active)):
        raise ValueError("Persistent memory and all trusted documentary hooks are required")
    nodes = DocumentaryNodes(load_batch=load_batch, claim_generation=claim_generation, generate=generate,
        review=review, save_outcomes=save_outcomes, check_active=check_active)
    graph = StateGraph(DocumentaryState)
    for name, node in (("load_batch", nodes.load), ("select_work", nodes.select), ("document", nodes.document),
                       ("validate_documents", nodes.validate), ("persist_outcomes", nodes.persist)):
        graph.add_node(name, node)
    graph.add_edge(START, "load_batch")
    graph.add_edge("load_batch", "select_work")
    graph.add_conditional_edges("select_work", lambda state: "document" if state["selected"] else "persist_outcomes")
    graph.add_edge("document", "validate_documents")
    graph.add_edge("validate_documents", "persist_outcomes")
    graph.add_edge("persist_outcomes", END)
    return graph.compile(checkpointer=checkpointer)
