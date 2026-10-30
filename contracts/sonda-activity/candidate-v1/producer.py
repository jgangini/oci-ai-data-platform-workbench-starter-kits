"""Inactive Activity producer candidate: closed codecs and pure binding checks.

No IO, admission CAS, issuer authentication, ingestion or execution authority is
implemented here. Protected reads and atomic one-use writes belong to callers.
In particular an expired claim is still consumed. A native completion receipt is
private evidence for the broker, never a public terminal event. ResultRef is an
opaque binding only: final-artifact payload validation remains unimplemented.
"""
import hashlib
import importlib.util
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

_read_spec = importlib.util.spec_from_file_location(
    '_sonda_activity_producer_read_candidate', Path(__file__).with_name('reference.py'))
_read = importlib.util.module_from_spec(_read_spec)
sys.modules[_read_spec.name] = _read
_read_spec.loader.exec_module(_read)

PRODUCER_VERSION = 'activity.producer.v1.candidate.1'
SchemaVersion = Literal['activity.producer.v1.candidate.1']
Hash64 = Annotated[str, Field(strict=True, pattern=r'^[a-f0-9]{64}$')]
Positive = Annotated[int, Field(strict=True, ge=1, le=9007199254740991)]
ProducerSequence = Annotated[int, Field(strict=True, ge=1, le=10000)]
StreamCount = Annotated[int, Field(strict=True, ge=0, le=10000)]
Closed, Scope, Run, ResultRef = _read.Closed, _read.Scope, _read.Run, _read.ResultRef


def producer_encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def producer_sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _producer_uuid(value):
    parsed = UUID(str(value))
    if not isinstance(value, UUID) and (not isinstance(value, str) or str(parsed) != value):
        raise ValueError('Producer invocation references require canonical UUIDs')
    return parsed


class AdmissionDescriptor(Closed):
    schema_version: SchemaVersion
    admission_id: UUID
    scope: Scope
    run: Run
    native_session_sha256: Hash64
    input_sha256: Hash64
    config_revision: Positive
    tool_catalog_sha256: Hash64
    created_at: AwareDatetime
    expires_at: AwareDatetime
    work_input_sha256: Hash64 | None

    @model_validator(mode='after')
    def admission_lifetime(self):
        duration = (self.expires_at - self.created_at).total_seconds()
        if not 0 < duration <= 3600 or self.run.created_at != self.created_at:
            raise ValueError('Admission requires its exact run creation time and a lifetime up to one hour')
        if self.run.execution_kind != 'native_invocation':
            raise ValueError('Only native invocations use the AIDP admission protocol')
        if self.run.source == 'autonomous_study':
            if self.work_input_sha256 is None:
                raise ValueError('Autonomous admission requires its protected work input digest')
        elif any(value is not None for value in (self.work_input_sha256, self.run.work_id, self.run.investigation_id)):
            raise ValueError('Chat admission cannot borrow autonomous work identities')
        return self


class CurrentPointer(Closed):
    schema_version: SchemaVersion
    scope: Scope
    agent_id: UUID
    native_session_sha256: Hash64
    admission_id: UUID
    run_id: UUID
    request_id: UUID
    attempt_id: UUID
    epoch: Positive
    status: Literal['admitted', 'running', 'uncertain', 'terminal']
    owner_nonce: UUID | None
    expires_at: AwareDatetime

    @model_validator(mode='after')
    def invocation_owner(self):
        if (self.status == 'admitted') != (self.owner_nonce is None):
            raise ValueError('Only an unclaimed admitted pointer has no invocation owner')
        return self


class AdmissionClaim(Closed):
    schema_version: SchemaVersion
    admission_id: UUID
    scope: Scope
    run_id: UUID
    attempt_id: UUID
    owner_nonce: UUID
    claimed_at: AwareDatetime


def _producer_now(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('Admission clock must be an aware datetime')
    return value.astimezone(timezone.utc)


def validate_admission(descriptor, current, *, admission_id, scope, agent_id,
        native_session_sha256, input_sha256, now, existing_claim=None,
        work_id=None, work_input_sha256=None, investigation_id=None):
    """Check supplied protected facts; this neither claims nor authorizes a POST.

    The caller derives hashes from the real invocation, reads the exact immutable
    claim key and uses the retained current-pointer ETag for CAS. Never recover a
    stale reference from session defaults or a previous checkpoint.
    """
    descriptor = AdmissionDescriptor.model_validate(descriptor)
    current = CurrentPointer.model_validate(current)
    expected_scope = Scope.model_validate(scope)
    identity = (descriptor.scope, descriptor.run.agent_id, descriptor.admission_id,
        descriptor.native_session_sha256, descriptor.input_sha256)
    expected = (expected_scope, _producer_uuid(agent_id), _producer_uuid(admission_id),
        TypeAdapter(Hash64).validate_python(native_session_sha256), TypeAdapter(Hash64).validate_python(input_sha256))
    if identity != expected:
        raise ValueError('Admission differs from the authenticated invocation binding')
    _producer_work_binding(descriptor, work_id, work_input_sha256, investigation_id)
    pointer = (current.scope, current.agent_id, current.admission_id, current.run_id,
        current.request_id, current.attempt_id, current.native_session_sha256, current.expires_at)
    bound = (descriptor.scope, descriptor.run.agent_id, descriptor.admission_id, descriptor.run.run_id,
        descriptor.run.request_id, descriptor.run.attempt_id, descriptor.native_session_sha256, descriptor.expires_at)
    if pointer != bound:
        raise ValueError('Current pointer does not select this exact admission')
    if existing_claim is not None or current.status != 'admitted':
        raise ValueError('Consumed or unresolved admissions cannot be rearmed by expiry')
    if not descriptor.created_at <= _producer_now(now) < descriptor.expires_at:
        raise ValueError('Admission is not inside its start deadline')
    return descriptor


def _producer_work_binding(descriptor, work_id, work_input_sha256, investigation_id):
    if descriptor.run.source == 'huginn_chat':
        if any(value is not None for value in (work_id, work_input_sha256, investigation_id)):
            raise ValueError('A chat invocation cannot adopt protected autonomous work')
        return
    if work_id is None or work_input_sha256 is None or investigation_id is None:
        raise ValueError('Autonomous admission requires facts from its actual protected work input')
    actual = (_producer_uuid(work_id), TypeAdapter(Hash64).validate_python(work_input_sha256),
        TypeAdapter(Hash64).validate_python(investigation_id))
    if actual != (descriptor.run.work_id, descriptor.work_input_sha256, descriptor.run.investigation_id):
        raise ValueError('Admission differs from the actual protected autonomous work')


def validate_claim_binding(claim, descriptor, *, owner_nonce):
    """Match the invocation-local nonce for reconciliation, without a TTL escape.

    This is not evidence of a successful claim PUT or pointer CAS. Another native
    entry cannot adopt this claim by reusing its session, input or checkpoint.
    """
    claim = AdmissionClaim.model_validate(claim)
    descriptor = AdmissionDescriptor.model_validate(descriptor)
    identity = (claim.admission_id, claim.scope, claim.run_id, claim.attempt_id, claim.owner_nonce)
    expected = (descriptor.admission_id, descriptor.scope, descriptor.run.run_id,
        descriptor.run.attempt_id, _producer_uuid(owner_nonce))
    if identity != expected:
        raise ValueError('Claim belongs to another admission or invocation owner')
    if not descriptor.created_at <= claim.claimed_at < descriptor.expires_at:
        raise ValueError('Claim was not created inside its admission start deadline')
    return claim


class ProducerEnvelope(Closed):
    event_id: UUID
    run_id: UUID
    attempt_id: UUID
    producer_stream_id: UUID
    producer_sequence: ProducerSequence
    span_id: UUID
    parent_span_id: UUID | None
    causation_id: UUID | None
    emitted_at: AwareDatetime

    @model_validator(mode='after')
    def producer_record_bytes(self):
        if len(producer_encoded(self.model_dump(mode='json'))) > 16384:
            raise ValueError('Producer record exceeds its 16 KiB byte bound')
        return self


class ProducerStage(ProducerEnvelope):
    type: Literal['stage']
    summary: _read.Summary
    effect: _read.Stage.model_fields['effect'].annotation
    status: _read.Stage.model_fields['status'].annotation = 'progress'

    @model_validator(mode='after')
    def observation_signal(self):
        if self.status != 'progress' and self.effect is not None:
            raise ValueError('Degradation or uncertainty signals cannot assert a business effect')
        return self


class ProducerToolStart(ProducerEnvelope):
    type: Literal['tool_start']
    tool_name: str = deepcopy(_read.ToolStart.model_fields['tool_name'])
    summary: _read.Summary
    domain: _read.ToolStart.model_fields['domain'].annotation


class ProducerToolFinish(ProducerEnvelope):
    type: Literal['tool_finish']
    summary: _read.Summary
    effect: _read.ToolFinish.model_fields['effect'].annotation


class ProducerToolError(ProducerEnvelope):
    type: Literal['tool_error']
    error_code: str = deepcopy(_read.ToolError.model_fields['error_code'])
    summary: _read.Summary


class ProducerTextDelta(ProducerEnvelope):
    type: Literal['text_delta']
    public_text_delta: str = deepcopy(_read.TextDelta.model_fields['public_text_delta'])


class NativeCompletionReceipt(ProducerEnvelope):
    """Private execution outcome candidate; the broker must validate its artifact."""
    kind: Literal['execution_completion']
    outcome: _read.Terminal.model_fields['outcome'].annotation
    summary: _read.Summary
    final_ref: ResultRef | None

    @model_validator(mode='after')
    def completion_binding(self):
        if self.outcome == 'completed' and self.final_ref is None:
            raise ValueError('Completed native execution requires an opaque final artifact reference')
        return self


ProducerEvent = Annotated[Union[ProducerStage, ProducerToolStart, ProducerToolFinish,
    ProducerToolError, ProducerTextDelta], Field(discriminator='type')]
ProducerRecord = Union[ProducerEvent, NativeCompletionReceipt]
PRODUCER_RECORDS = TypeAdapter(ProducerRecord)


class JournalBatch(Closed):
    schema_version: SchemaVersion
    batch_id: UUID
    admission_id: UUID
    scope: Scope
    run_id: UUID
    attempt_id: UUID
    producer_stream_id: UUID
    first_sequence: ProducerSequence
    last_sequence: ProducerSequence
    events: Annotated[list[ProducerRecord], Field(min_length=1, max_length=100)]

    @model_validator(mode='after')
    def contiguous_producer(self):
        expected = list(range(self.first_sequence, self.last_sequence + 1))
        if [event.producer_sequence for event in self.events] != expected:
            raise ValueError('Journal batch must contain one contiguous producer sequence')
        binding = (self.run_id, self.attempt_id, self.producer_stream_id)
        if any((event.run_id, event.attempt_id, event.producer_stream_id) != binding for event in self.events):
            raise ValueError('Journal batch contains another run, attempt or producer stream')
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError('Journal batch event identities must be distinct')
        if len(producer_encoded(self.model_dump(mode='json'))) > 1048576:
            raise ValueError('Journal batch exceeds its 1 MiB byte bound')
        return self


class BatchRef(Closed):
    batch_id: UUID
    sha256: Hash64
    byte_length: Annotated[int, Field(strict=True, ge=1, le=1048576)]


class JournalIndex(Closed):
    schema_version: SchemaVersion
    index_id: UUID
    admission_id: UUID
    scope: Scope
    run_id: UUID
    attempt_id: UUID
    producer_stream_id: UUID
    index_sequence: ProducerSequence
    first_sequence: ProducerSequence
    last_sequence: ProducerSequence
    batch: BatchRef
    previous_index_sha256: Hash64 | None

    @model_validator(mode='after')
    def bounded_index(self):
        if not 1 <= self.last_sequence - self.first_sequence + 1 <= 100:
            raise ValueError('Journal index must reference one bounded nonempty batch')
        if (self.index_sequence == 1) != (self.previous_index_sha256 is None):
            raise ValueError('Only the first journal index has no predecessor')
        if self.first_sequence < self.index_sequence or self.index_sequence == 1 and self.first_sequence != 1:
            raise ValueError('Journal index cannot skip the producer genesis')
        return self


class JournalHead(Closed):
    schema_version: SchemaVersion
    admission_id: UUID
    scope: Scope
    run_id: UUID
    attempt_id: UUID
    producer_stream_id: UUID
    index_sequence: StreamCount
    index_id: UUID | None
    index_sha256: Hash64 | None
    producer_sequence: StreamCount

    @model_validator(mode='after')
    def genesis(self):
        if self.index_sequence == 0:
            if self.index_id is not None or self.index_sha256 is not None or self.producer_sequence != 0:
                raise ValueError('Genesis head has zero sequences and null index references')
        elif self.index_id is None or self.index_sha256 is None or self.producer_sequence < self.index_sequence:
            raise ValueError('Published head requires its exact nonempty index reference')
        return self


ProducerDocument = Union[AdmissionDescriptor, CurrentPointer, AdmissionClaim, JournalBatch, JournalIndex, JournalHead]
PRODUCER_DOCUMENTS = TypeAdapter(ProducerDocument)


def producer_schema():
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$id': 'urn:sonda:activity:producer:v1:candidate:1', **PRODUCER_DOCUMENTS.json_schema()}


def _producer_unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate producer JSON key')
        value[key] = item
    return value


def parse_producer_document(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= 1048576:
        raise ValueError('Producer document exceeds its nonempty byte bound')
    value = json.loads(raw, object_pairs_hook=_producer_unique)
    document = PRODUCER_DOCUMENTS.validate_python(value)
    if not isinstance(document, JournalBatch) and len(raw) > 16384:
        raise ValueError('Producer control object exceeds its 16 KiB byte bound')
    if raw != producer_encoded(document.model_dump(mode='json')):
        raise ValueError('Producer document requires exact canonical bytes')
    return document


def parse_producer_record(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= 16384:
        raise ValueError('Producer record exceeds its nonempty 16 KiB byte bound')
    record = PRODUCER_RECORDS.validate_python(json.loads(raw, object_pairs_hook=_producer_unique))
    if raw != producer_encoded(record.model_dump(mode='json')):
        raise ValueError('Producer record requires exact canonical bytes')
    return record


def _producer_journal_identity(value):
    return (value.admission_id, value.scope, value.run_id, value.attempt_id, value.producer_stream_id)


def validate_batch_binding(batch, descriptor):
    batch = JournalBatch.model_validate(batch)
    descriptor = AdmissionDescriptor.model_validate(descriptor)
    if (batch.admission_id, batch.scope, batch.run_id, batch.attempt_id) != (
            descriptor.admission_id, descriptor.scope, descriptor.run.run_id, descriptor.run.attempt_id):
        raise ValueError('Producer batch differs from its admission binding')
    if batch.producer_stream_id != descriptor.run.attempt_id:
        raise ValueError('Native stream identity must match its admitted attempt')
    return batch


def validate_index_batch(index, batch_raw):
    index = JournalIndex.model_validate(index)
    batch = parse_producer_document(batch_raw)
    if not isinstance(batch, JournalBatch):
        raise ValueError('Journal index requires a batch document')
    actual = (batch.batch_id, producer_sha256(batch_raw), len(batch_raw))
    if actual != (index.batch.batch_id, index.batch.sha256, index.batch.byte_length):
        raise ValueError('Journal batch bytes differ from their immutable index reference')
    if _producer_journal_identity(index) != _producer_journal_identity(batch):
        raise ValueError('Journal batch belongs to another admission, run or producer')
    if (index.first_sequence, index.last_sequence) != (batch.first_sequence, batch.last_sequence):
        raise ValueError('Journal index sequence range differs from its batch')
    return index


def validate_head_successor(current, proposed, index_raw):
    """Validate chain facts only; caller still performs and reconciles the head CAS."""
    proposed = JournalHead.model_validate(proposed)
    index = parse_producer_document(index_raw)
    if not isinstance(index, JournalIndex):
        raise ValueError('Journal head requires an index document')
    reference = (index.index_id, producer_sha256(index_raw), index.index_sequence, index.last_sequence)
    if reference != (proposed.index_id, proposed.index_sha256, proposed.index_sequence, proposed.producer_sequence):
        raise ValueError('Journal head does not reference the exact index bytes and sequence')
    if _producer_journal_identity(proposed) != _producer_journal_identity(index):
        raise ValueError('Journal head and index bindings differ')
    if current is None:
        if index.index_sequence != 1:
            raise ValueError('A new journal head must start at its genesis index')
        return 'create'
    current = JournalHead.model_validate(current)
    if _producer_journal_identity(current) != _producer_journal_identity(proposed):
        raise ValueError('Journal head predecessor belongs to another producer')
    if current == proposed:
        return 'same'
    if (index.index_sequence, index.previous_index_sha256, index.first_sequence) != (
            current.index_sequence + 1, current.index_sha256, current.producer_sequence + 1):
        raise ValueError('Journal head must extend its exact contiguous predecessor')
    return 'replace'


def public_text_bytes_after(previous_bytes, batch):
    """Pure accumulator; broker must retain the aggregate across all run producers."""
    previous = TypeAdapter(Annotated[int, Field(strict=True, ge=0, le=65536)]).validate_python(previous_bytes)
    batch = JournalBatch.model_validate(batch)
    total = previous + sum(len(event.public_text_delta.encode('utf-8'))
        for event in batch.events if isinstance(event, ProducerTextDelta))
    if total > 65536:
        raise ValueError('Public text exceeds the run-wide 64 KiB byte bound')
    return total
