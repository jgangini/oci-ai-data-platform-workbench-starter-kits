"""Inactive final Activity result codec; no installed runtime activation.

The caller resolves and authorizes Run/Scope/ResultRef, authenticates object reads,
then supplies existing contextual application validators. Integrity is not Gold
publication proof. Chat and Gold payloads remain unchanged; no chart, citation or
CoreReport validator is duplicated here. No object keys, URLs or paths resolve.
"""
import importlib.util
from copy import deepcopy
from pathlib import Path
import sys
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BeforeValidator, Field, TypeAdapter, field_validator, model_validator

_spec = importlib.util.spec_from_file_location('_sonda_activity_results_producer', Path(__file__).with_name('producer.py'))
p = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = p
_spec.loader.exec_module(p)

RESULT_VERSION = 'activity.result.v1.candidate.1'
CanonicalUUID = Annotated[UUID, BeforeValidator(p._producer_uuid)]
Count = Annotated[int, Field(strict=True, ge=0, le=9007199254740991)]


class ResultError(ValueError):
    """Fixed operational messages exclude untrusted content and validator errors."""


class ReportRef(p.Closed):
    report_id: p.Hash64
    revision: p.Positive


class NativeStep(p.Closed):
    work_id: CanonicalUUID
    input_sha256: p.Hash64
    investigation_id: p.Hash64
    phase: Literal['evaluated', 'report_prepared', 'no_change']
    report_refs: Annotated[list[ReportRef], Field(max_length=100)]
    evaluation_count: Count
    request_count: Count
    proposal_count: Count
    publication_status: Literal['pending']

    @field_validator('report_refs')
    @classmethod
    def distinct_reports(cls, values):
        keys = [(row.report_id, row.revision) for row in values]
        if len(keys) != len(set(keys)):
            raise ValueError('Result report references must be unique')
        return values

    @model_validator(mode='after')
    def phase_reports(self):
        if bool(self.report_refs) != (self.phase == 'report_prepared'):
            raise ValueError('Prepared report phase requires report references; evaluation has none')
        if self.phase=='no_change' and any((self.evaluation_count,self.request_count,self.proposal_count)):
            raise ValueError('An unchanged native step cannot claim new business outputs')
        return self


class ResultEnvelope(p.Closed):
    schema_version: Literal['activity.result.v1.candidate.1']
    scope: p.Scope
    result_id: CanonicalUUID
    run_id: CanonicalUUID
    attempt_id: CanonicalUUID

    @field_validator('scope', mode='before')
    @classmethod
    def canonical_scope(cls, value):
        scope = p.Scope.model_validate(value)
        if isinstance(value, dict):
            p._producer_uuid(value['registry_id'])
            p._producer_uuid(value['generation_id'])
        return scope


class ChatResponse(ResultEnvelope):
    kind: Literal['chat_response']
    payload: dict[str, Any]


class NativeStepResult(ResultEnvelope):
    kind: Literal['native_step']
    payload: NativeStep


class GoldReport(ResultEnvelope):
    kind: Literal['gold_report']
    payload: dict[str, Any]


RESULTS = TypeAdapter(Annotated[Union[ChatResponse, NativeStepResult, GoldReport], Field(discriminator='kind')])


def result_bytes(value):
    """Structural encoding only. Semantic approval still requires parse_result."""
    model = RESULTS.validate_python(value)
    raw = p.producer_encoded(model.model_dump(mode='json'))
    if len(raw) > 1048576:
        raise ResultError('activity_result_size')
    return raw


def _result_run_binding(result, run, work_sha):
    if result.kind == 'chat_response':
        if (run.execution_kind, run.source) != ('native_invocation', 'huginn_chat'):
            raise ResultError('activity_result_kind_binding')
    elif result.kind == 'gold_report':
        if run.execution_kind != 'gold_publication':
            raise ResultError('activity_result_kind_binding')
    else:
        if (run.execution_kind, run.source) != ('native_invocation', 'autonomous_study'):
            raise ResultError('activity_result_kind_binding')
        expected = (run.work_id, run.investigation_id, TypeAdapter(p.Hash64).validate_python(work_sha))
        actual = (result.payload.work_id, result.payload.investigation_id, result.payload.input_sha256)
        if actual != expected:
            raise ResultError('activity_result_work_binding')


def _result_semantics(result, scope, run, chat_validator, gold_validator):
    if result.kind == 'native_step':
        return
    callback = chat_validator if result.kind == 'chat_response' else gold_validator
    if not callable(callback):
        raise ResultError('activity_result_validator_required')
    payload = deepcopy(result.payload)
    approved = callback(payload, scope=scope.model_dump(mode='json'), run=run.model_dump(mode='json'))
    if approved is not True or payload != result.payload:
        raise ResultError('activity_result_semantic_rejection')


def parse_result(raw, *, expected_scope, expected_run, expected_ref,
        chat_validator=None, gold_report_validator=None, expected_work_input_sha256=None):
    """Verify exact protected reference and require contextual semantic approval.

    Validators are synchronous adapters to existing application validation and
    must return exactly True after validating the unchanged payload. A worker's
    private committed receipt is never sufficient proof of a Gold publication.
    """
    try:
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= 1048576:
            raise ResultError('activity_result_size')
        scope = p.Scope.model_validate(expected_scope)
        run = p.Run.model_validate(expected_run)
        ref = p.ResultRef.model_validate(expected_ref)
        p._producer_uuid(ref.result_id if not isinstance(expected_ref, dict) else expected_ref['result_id'])
        if (len(raw), p.producer_sha256(raw)) != (ref.byte_length, ref.sha256):
            raise ResultError('activity_result_reference_mismatch')
        result = RESULTS.validate_python(p.json.loads(raw, object_pairs_hook=p._producer_unique))
        if raw != result_bytes(result):
            raise ResultError('activity_result_noncanonical')
        if (result.scope, result.result_id, result.run_id, result.attempt_id) != (scope, ref.result_id, run.run_id, run.attempt_id):
            raise ResultError('activity_result_identity_mismatch')
        _result_run_binding(result, run, expected_work_input_sha256)
        _result_semantics(result, scope, run, chat_validator, gold_report_validator)
        return result
    except ResultError:
        raise
    except Exception:
        raise ResultError('activity_result_invalid') from None
