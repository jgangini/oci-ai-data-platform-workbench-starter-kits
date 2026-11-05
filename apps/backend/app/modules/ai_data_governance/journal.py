"""Installation-bound operating receipts in the artifacts bucket.

Conditional Object Storage creates exclude duplicate model starts; Delta remains
the authority for metadata, documents and publication state. Claims are never
released by a timeout. A lost create response must be reconciled without starting
the model. Attempt receipts are immutable; work ownership advances with ETag CAS
only after a response or sealed pre-start abort. This is deliberately independent
of the viewer's Gold/reset controls.
"""

from __future__ import annotations

import json
import re

from .inventory import identity
from .model_output import model_json


class JournalConflict(RuntimeError):
    pass


class GovernanceJournal:
    MAX_BYTES = 524288
    KINDS = frozenset({"batches", "claims", "responses", "outcomes", "materialized", "publications", "executions", "model_work", "reuses"})

    def __init__(self, objects, *, platform_key: str, workspace_key: str, storage_root: str):
        match = re.fullmatch(r"oci://([A-Za-z0-9][A-Za-z0-9_.-]{0,254})@([A-Za-z0-9]{1,128})/oci_artifacts", storage_root)
        if not match or not isinstance(platform_key, str) or not platform_key.startswith("ocid1.aidataplatform."):
            raise ValueError("The installation artifacts binding is required")
        if not isinstance(workspace_key, str) or not re.fullmatch(r"[a-f0-9-]{36}", workspace_key):
            raise ValueError("A native workspace binding is required")
        self.objects = objects
        self.bucket, self.namespace = match.groups()
        self.prefix = ".control/ai_data_governance/" + identity(platform_key, workspace_key) + "/"

    def _key(self, kind: str, key: str) -> str:
        if kind not in self.KINDS or not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("An admitted journal kind and digest are required")
        return self.prefix + kind + "/" + key + ".json"

    def read(self, kind: str, key: str) -> dict | None:
        return self._read_revision(kind, key)[0]

    def _read_revision(self, kind: str, key: str) -> tuple:
        target = self._key(kind, key)
        try:
            response = self.objects.get_object(self.namespace, self.bucket, target)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None, None
            raise RuntimeError("Governance journal read failed") from None
        try:
            body = response.data.raw.read(self.MAX_BYTES + 1)
            if len(body) > self.MAX_BYTES:
                raise ValueError("Journal document exceeds its bound")
            value = model_json(body.decode("utf-8"), max_bytes=self.MAX_BYTES)
            revision = response.headers.get("etag") or response.headers.get("ETag")
            if not revision:
                raise ValueError("Journal response has no revision")
            return value, revision
        finally:
            response.data.close()

    def create(self, kind: str, key: str, value: dict) -> bool:
        """True only for this acknowledged winner; 412 never means a fresh claim."""
        if kind == "model_work":
            raise ValueError("Model work reservations require conditional admission")
        target = self._key(kind, key)
        if not isinstance(value, dict):
            raise ValueError("A journal document must be an object")
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        if len(body) > self.MAX_BYTES:
            raise ValueError("Journal document exceeds its bound")
        try:
            response = self.objects.put_object(self.namespace, self.bucket, target, body,
                content_type="application/json", if_none_match="*")
        except Exception as exc:
            if getattr(exc, "status", None) != 412:
                raise RuntimeError("Governance journal create is uncertain; reconcile before continuing") from None
            if self.read(kind, key) != value:
                raise JournalConflict("Journal identity already has different content") from None
            return False
        if not (response.headers.get("etag") or response.headers.get("ETag")):
            raise RuntimeError("Journal create acknowledgement is incomplete; reconcile before continuing")
        if self.read(kind, key) != value:
            raise JournalConflict("Journal create could not be verified")
        return True

    def reserve_model_work(self, key: str, owner: dict) -> bool:
        """Advance one work/phase only after its previous response is durable.

        This pointer is conditional mutable state; all attempt/response receipts
        remain immutable. An uncertain call never expires or releases ownership.
        """
        _validate_model_owner(owner)
        previous, revision = self._read_revision("model_work", key)
        if previous == owner:
            return True
        if not model_work_may_advance(previous, self.read):
            return False
        body = json.dumps(owner, sort_keys=True, allow_nan=False).encode("utf-8")
        condition = {"if_match": revision} if previous is not None else {"if_none_match": "*"}
        try:
            result = self.objects.put_object(self.namespace, self.bucket, self._key("model_work", key), body,
                content_type="application/json", **condition)
        except Exception as exc:
            if getattr(exc, "status", None) == 412:
                return False
            raise RuntimeError("Model work reservation is uncertain; reconcile before continuing") from None
        if not (result.headers.get("etag") or result.headers.get("ETag")):
            raise RuntimeError("Model work acknowledgement is incomplete")
        if self.read("model_work", key) != owner:
            raise JournalConflict("Model work ownership changed")
        return True


def _validate_model_owner(owner):
    required = {"schema_version", "phase", "batch_ref", "input_digest", "generation_keys", "response_key"}
    if not isinstance(owner, dict) or set(owner) != required or owner["schema_version"] != 1:
        raise ValueError("Invalid model work owner")
    if owner["phase"] not in {"document", "review"}:
        raise ValueError("Invalid model work phase")
    keys = owner["generation_keys"]
    if not isinstance(keys, list) or not 1 <= len(keys) <= 16 or keys != sorted(set(keys)):
        raise ValueError("Invalid model work generation keys")
    if any(not isinstance(k, str) or not re.fullmatch(r"[a-f0-9]{64}", k)
            for k in [owner["batch_ref"], owner["input_digest"], owner["response_key"], *keys]):
        raise ValueError("Invalid model work digest")
    if owner["response_key"] != identity("model-response", owner["phase"], owner["batch_ref"]):
        raise ValueError("Model work response belongs to another attempt")


def model_work_may_advance(previous, read):
    if previous is None:
        return True
    _validate_model_owner(previous)
    phase = read("claims", identity("model-phase", previous["phase"], previous["batch_ref"]))
    if _phase_precludes_model_work(phase, previous):
        return True
    result = read("responses", previous["response_key"])
    if result is None:
        return False
    expected = {"schema_version", "input_digest", "generation_keys", "response"}
    if (not isinstance(result, dict) or set(result) != expected or result["schema_version"] != 1
            or result["input_digest"] != previous["input_digest"]
            or result["generation_keys"] != previous["generation_keys"] or not isinstance(result["response"], dict)):
        raise JournalConflict("Earlier model response cannot reconcile its work reservation")
    return True


def _phase_precludes_model_work(phase, previous):
    """An exclusive phase seal proves another input could not start."""
    if phase is None:
        return False
    if not isinstance(phase, dict):
        raise JournalConflict("Invalid model phase seal")
    aborted = phase.get("not_started") is True
    reuse_source = phase.get("reuse_source")
    if reuse_source is not None and (not isinstance(reuse_source, str) or not re.fullmatch(r"[a-f0-9]{64}", reuse_source)):
        raise JournalConflict("Invalid model phase reuse source")
    candidate = {k: v for k, v in phase.items() if k != "reuse_source" and (k != "not_started" or not aborted)}
    candidate["response_key"] = previous["response_key"]
    _validate_model_owner(candidate)
    if candidate["phase"] != previous["phase"] or candidate["batch_ref"] != previous["batch_ref"]:
        raise JournalConflict("Model phase seal belongs to another batch")
    return aborted or candidate != previous
