"""Durable documentary hooks; publication belongs to the downstream Python task.

The admitted batch is written by inventory, not supplied as an agent message.
Only its digest is an invocation input. Native execution admission is checked by
the injected authorization callback before each effect and model call.
"""

from __future__ import annotations

import asyncio
import re

from .documentary_graph import _admission, _batch_snapshot
from .documentation import documentation_generation_key
from .inventory import canonical, identity
from .journal import JournalConflict
from .model_output import model_json
from .contracts import DocumentationResult


class JournalDocumentaryHooks:
    def __init__(self, journal, *, call_model, authorize):
        self.journal = journal
        self.call_model = call_model
        self.authorize = authorize
        self.batch = None

    async def load_batch(self, batch_ref: str, config) -> dict:
        if not isinstance(batch_ref, str) or not re.fullmatch(r"[a-f0-9]{64}", batch_ref):
            raise ValueError("A durable documentary batch digest is required")
        batch = await asyncio.to_thread(self.journal.read, "batches", batch_ref)
        if not isinstance(batch, dict) or set(batch) != {"schema_version", "batch", "execution"} or batch["schema_version"] != 1:
            raise ValueError("The documentary batch is unavailable or incompatible")
        snapshot = batch["batch"]
        _batch_snapshot(batch_ref, snapshot)
        await self.authorize(batch, config)
        self.batch = batch
        return snapshot

    async def check_active(self, config):
        # The graph checks before load; native invocation is admitted separately.
        # Once loaded, every subsequent check binds to that exact durable batch.
        if self.batch is not None:
            await self.authorize(self.batch, config)

    async def claim_generation(self, phase: str, admission: dict, key: str, config) -> bool:
        await self.check_active(config)
        batch_ref = self._batch_ref()
        if phase not in {"document", "review"} or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("An admitted model phase and generation digest are required")
        target = identity("model-claim", phase, batch_ref, key)
        receipt = {"schema_version": 1, "phase": phase, "generation_key": key, "batch_ref": batch_ref}
        existing = await asyncio.to_thread(self.journal.read, "claims", target)
        if existing is not None:
            if existing != receipt:
                return False
            response = await asyncio.to_thread(self.journal.read, "responses", identity("model-response", phase, batch_ref))
            if isinstance(response, dict) and key in response.get("generation_keys", []):
                return True
            seal = await asyncio.to_thread(self.journal.read, "claims", identity("model-phase", phase, batch_ref))
            return (phase == "document" and isinstance(seal, dict) and bool(seal.get("reuse_source"))
                and seal.get("batch_ref") == batch_ref and key in seal.get("generation_keys", []))
        try:
            return await asyncio.to_thread(self.journal.create, "claims", target, receipt)
        except JournalConflict:
            # Another admitted batch already owns this work. Never steal its claim.
            return False

    async def generate(self, payload: dict, schema: dict, config):
        return await self._model_response("document", payload, schema, config)

    async def review(self, payload: dict, schema: dict, config):
        return await self._model_response("review", payload, schema, config)

    async def _model_response(self, phase: str, payload: dict, schema: dict, config) -> str:
        await self.check_active(config)
        batch_ref = self._batch_ref()
        if payload.get("batch_ref") != batch_ref:
            raise ValueError("Model call refers to a different documentary batch")
        target = identity("model-response", phase, batch_ref)
        digest = identity("model-input", payload, schema)
        keys = _response_generation_keys(phase, payload, self.batch["batch"])
        previous = await asyncio.to_thread(self.journal.read, "responses", target)
        if previous is not None:
            return canonical(_cached_model_response(previous, digest, keys))
        reused = await asyncio.to_thread(self._generation_reuse, payload, schema, digest, keys) if phase == "document" else None
        await self._reserve_model_work(phase, payload, digest, keys, target)
        await self._claim_model_phase(phase, batch_ref, digest, keys,
            reuse_source=reused[1]["source_batch_ref"] if reused else None)
        if reused is not None:
            await asyncio.to_thread(self.journal.create, "reuses", target, reused[1])
        await self.check_active(config)
        # A phase claim also excludes parallel invocations that split work claims.
        result = reused[0] if reused else model_json(await self.call_model(phase, payload, schema, config))
        await self.check_active(config)
        receipt = {"schema_version": 1, "input_digest": digest, "generation_keys": keys, "response": result}
        await asyncio.to_thread(self.journal.create, "responses", target, receipt)
        return canonical(result)

    def _generation_reuse(self, payload, schema, digest, keys):
        target = identity("model-response", "document", self._batch_ref())
        receipt = self.journal.read("reuses", target)
        if receipt is not None:
            source = receipt.get("source_batch_ref") if isinstance(receipt, dict) else None
        else:
            source = self._reuse_source(payload, digest, keys)
        if not isinstance(source, str) or not re.fullmatch(r"[a-f0-9]{64}", source) or source == self._batch_ref():
            if receipt is not None:
                raise ValueError("Invalid documentary reuse source")
            return None
        candidate = _reusable_generation(self.journal, source, payload, schema, keys)
        if candidate is None:
            if receipt is not None:
                raise ValueError("Documentary reuse receipt no longer matches its admitted input")
            return None
        response, source_digest = candidate
        expected = {"schema_version": 1, "source_batch_ref": source, "source_input_digest": source_digest,
            "input_digest": digest, "generation_keys": keys}
        if receipt is not None and receipt != expected:
            raise ValueError("Documentary reuse receipt has different provenance")
        return {**response, "batch_ref": self._batch_ref()}, expected

    def _reuse_source(self, payload, digest, keys):
        seal = self.journal.read("claims", identity("model-phase", "document", self._batch_ref()))
        if (isinstance(seal, dict) and seal.get("input_digest") == digest
                and seal.get("generation_keys") == keys and seal.get("reuse_source")):
            return seal["reuse_source"]
        owners = [self.journal.read("model_work", identity("model-work", "document", r["scope_id"], r["work_id"]))
            for r in payload["admissions"]]
        if not owners or any(not isinstance(r, dict) for r in owners):
            return None
        source = owners[0].get("batch_ref")
        return source if all(r.get("batch_ref") == source and r.get("generation_keys") == keys for r in owners) else None

    async def _reserve_model_work(self, phase, payload, digest, keys, response_key):
        rows = payload["admissions"] if phase == "document" else payload["candidates"]
        admitted = {row["work_id"]: row for row in self.batch["batch"]["admissions"]}
        owner = {"schema_version": 1, "phase": phase, "batch_ref": self._batch_ref(),
            "input_digest": digest, "generation_keys": keys, "response_key": response_key}
        try:
            for work_id in sorted({row["work_id"] for row in rows}):
                row = admitted[work_id]
                key = identity("model-work", phase, row["scope_id"], work_id)
                if not await asyncio.to_thread(self.journal.reserve_model_work, key, owner):
                    raise RuntimeError("Model work has an unresolved earlier response; repair its original attempt")
        except Exception:
            # Seal the same phase-start key before releasing work that never
            # reached a call. A racing phase start makes this create fail.
            aborted = {k: v for k, v in owner.items() if k != "response_key"}
            aborted["not_started"] = True
            await asyncio.to_thread(self.journal.create, "claims",
                identity("model-phase", phase, self._batch_ref()), aborted)
            raise

    async def _claim_model_phase(self, phase: str, batch_ref: str, digest: str, keys: list, *, reuse_source=None):
        for key in keys:
            claim = await asyncio.to_thread(self.journal.read, "claims", identity("model-claim", phase, batch_ref, key))
            if claim != {"schema_version": 1, "phase": phase, "generation_key": key, "batch_ref": batch_ref}:
                raise ValueError("The model input contains work without this batch's claim")
        receipt = {"schema_version": 1, "phase": phase, "batch_ref": batch_ref,
                   "input_digest": digest, "generation_keys": keys}
        if reuse_source is not None:
            receipt["reuse_source"] = reuse_source
        created = await asyncio.to_thread(self.journal.create, "claims", identity("model-phase", phase, batch_ref), receipt)
        if created is not True and reuse_source is None:
            raise RuntimeError("The model phase has already started; reconcile its response")

    async def save_outcomes(self, batch_ref: str, admissions: list, outcomes: list, config):
        await self.check_active(config)
        if batch_ref != self._batch_ref() or admissions != self.batch["batch"]["admissions"]:
            raise ValueError("Outcome receipt refers to a different admission")
        if len(outcomes) != len(admissions) or {row["work_id"] for row in outcomes} != {row["work_id"] for row in admissions}:
            raise ValueError("Outcome receipt does not cover the admitted work")
        receipt = {"schema_version": 1, "batch_ref": batch_ref, "outcomes": outcomes}
        await asyncio.to_thread(self.journal.create, "outcomes", batch_ref, receipt)

    def _batch_ref(self) -> str:
        if self.batch is None:
            raise ValueError("A durable batch has not been admitted")
        return self.batch["batch"]["batch_ref"]


def _cached_model_response(receipt: dict, digest: str, keys: list) -> dict:
    if (set(receipt) != {"schema_version", "input_digest", "generation_keys", "response"} or receipt["schema_version"] != 1
            or receipt["input_digest"] != digest or receipt["generation_keys"] != keys or not isinstance(receipt["response"], dict)):
        raise ValueError("A model receipt has different input or an invalid response")
    return receipt["response"]


def _response_generation_keys(phase: str, payload: dict, batch: dict) -> list:
    if phase == "document":
        rows = payload.get("admissions")
        if not isinstance(rows, list) or not rows or any(row not in batch["admissions"] for row in rows):
            raise ValueError("Model input contains unadmitted documentary work")
        keys = [documentation_generation_key(_admission(row)) for row in rows]
    else:
        rows = payload.get("candidates")
        allowed = {row["work_id"] for row in batch["admissions"]}
        if not isinstance(rows, list) or not rows or any(row.get("work_id") not in allowed or not row.get("document_id") for row in rows):
            raise ValueError("Model review contains unadmitted documentary work")
        keys = [identity("AI_DG_REVIEW", sorted(row["document_id"] for row in rows))]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate model generation identities")
    return sorted(keys)


def _reusable_generation(journal, source, payload, schema, keys):
    batch = journal.read("batches", source)
    if not isinstance(batch, dict) or batch.get("schema_version") != 1:
        return None
    current = journal.read("batches", payload["batch_ref"])
    execution = lambda record: {k: v for k, v in record.get("execution", {}).items()
        if k not in {"job_run_id", "inventory_run_id"}}
    if not isinstance(current, dict) or execution(batch) != execution(current):
        return None
    snapshot = _batch_snapshot(source, batch.get("batch"))
    rows = [r for r in snapshot["admissions"] if documentation_generation_key(_admission(r)) in keys]
    normalize = lambda row: {k: v for k, v in row.items() if k != "run_id"}
    if ([normalize(r) for r in rows] != [normalize(r) for r in payload["admissions"]]
            or {r["work_id"]: snapshot["contexts"][r["work_id"]] for r in rows} != payload.get("contexts")):
        return None
    original = {"batch_ref": source, "admissions": rows,
        "contexts": {r["work_id"]: snapshot["contexts"][r["work_id"]] for r in rows}}
    source_digest = identity("model-input", original, schema)
    receipt = journal.read("responses", identity("model-response", "document", source))
    if receipt is None:
        return None
    response = _cached_model_response(receipt, source_digest, keys)
    parsed = DocumentationResult.model_validate(response)
    if parsed.batch_ref != source:
        raise ValueError("Reused generation belongs to another batch")
    return parsed.model_dump(mode="json"), source_digest
