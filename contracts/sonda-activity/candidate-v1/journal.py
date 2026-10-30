"""Inactive, synchronous Activity journal candidate; no SDK or business execution.

The adapter must authenticate reads and implement real conditional PUT. This
module cannot prove that contract. A failed delivery never retries business work.
Claims remain burned after a lost current-pointer CAS; expiry cannot rearm them.
Immutable orphans are invisible until their exact index is published by head CAS.
"""
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import time
import math
from uuid import uuid4

_spec = importlib.util.spec_from_file_location('_sonda_activity_journal_producer', Path(__file__).with_name('producer.py'))
p = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = p
_spec.loader.exec_module(p)


class DeliveryError(RuntimeError):
    """Bounded public operational code, never the underlying adapter exception."""


def _uuid(value):
    return str(p._producer_uuid(value))


def activity_prefix(scope):
    value = p.Scope.model_validate(scope)
    # Model UUID parsing alone accepts alternate spellings; external keys do not.
    if isinstance(scope, dict):
        _uuid(scope['registry_id'])
        _uuid(scope['generation_id'])
    return f'ai_social_network/registries/{value.registry_id}/generations/{value.generation_id}/control/activity/v1/'


def _hash(value):
    return p.TypeAdapter(p.Hash64).validate_python(value)


def admission_keys(scope, agent_id, session_sha256, admission_ref):
    prefix = activity_prefix(scope)
    root = prefix + 'admissions/' + _uuid(admission_ref) + '/'
    current = prefix + 'current/' + _uuid(agent_id) + '/' + _hash(session_sha256) + '.json'
    return root, current


def index_object_name(sequence, digest):
    """A predecessor is GET-addressable using its sequence and committed hash."""
    if type(sequence) is not int or not 1 <= sequence <= 10000:
        raise ValueError('Invalid activity index sequence')
    return f'{sequence:020d}-{_hash(digest)}.json'


def _read(objects, key, expected_type, *, optional=False):
    try:
        blob = objects.get('artifacts', key)
        if blob is None:
            if optional:
                return None, None
            raise DeliveryError('activity_object_missing')
        value = p.parse_producer_document(blob.data)
        if not isinstance(value, expected_type) or not isinstance(blob.etag, str) or not blob.etag:
            raise DeliveryError('activity_object_invalid')
        return value, blob.etag
    except Exception:
        raise DeliveryError('activity_read_failed') from None


def _bytes(model):
    return p.producer_encoded(model.model_dump(mode='json'))


def _put(objects, key, raw, etag):
    try:
        result = objects.put('artifacts', key, raw, etag)
        if not isinstance(result, str) or not result:
            raise DeliveryError('activity_write_unconfirmed')
        return result
    except Exception:
        try:
            saved = objects.get('artifacts', key)
            if saved is not None and saved.data == raw and isinstance(saved.etag, str) and saved.etag:
                return saved.etag
        except Exception:
            pass
        raise DeliveryError('activity_write_conflict_or_uncertain') from None


@dataclass(frozen=True)
class AdmissionLease:
    descriptor: object
    current: object
    claim: object
    root: str
    current_key: str


def admit_native(objects, scope, agent_id, session_sha256, input_sha256,
        admission_ref, now, protected_work_facts=None):
    """One invocation only. Readers/reconnects must never call this function."""
    try:
        root, key = admission_keys(scope, agent_id, session_sha256, admission_ref)
        descriptor, _ = _read(objects, root+'descriptor.json', p.AdmissionDescriptor)
        current, etag = _read(objects, key, p.CurrentPointer)
        claim, _ = _read(objects, root+'claim.json', p.AdmissionClaim, optional=True)
        descriptor = p.validate_admission(descriptor, current, admission_id=admission_ref,
            scope=scope, agent_id=agent_id, native_session_sha256=session_sha256,
            input_sha256=input_sha256, now=now, existing_claim=claim,
            **(protected_work_facts or {}))
        nonce = uuid4()
        claim = p.AdmissionClaim(schema_version=p.PRODUCER_VERSION,
            admission_id=descriptor.admission_id, scope=descriptor.scope,
            run_id=descriptor.run.run_id, attempt_id=descriptor.run.attempt_id,
            owner_nonce=nonce, claimed_at=p._producer_now(now))
        _put(objects, root+'claim.json', _bytes(claim), None)
        running = current.model_copy(update={'status':'running', 'owner_nonce':nonce})
        _put(objects, key, _bytes(running), etag)
        return AdmissionLease(descriptor, running, claim, root, key)
    except Exception:
        raise DeliveryError('activity_admission_denied') from None


class JournalWriter:
    """Serial single-owner writer. On DeliveryError, preserve the external result.

    There is no background worker or automatic retry. Text buffers flush at the
    configured bounds, flush_due or explicit flush. The first text and every
    nontext record flush immediately. Callers schedule flush_due during SDK pauses.
    """

    def __init__(self, objects, lease, producer_stream_id, *, batch_events=100, batch_bytes=1048576,
            flush_interval=0.1, monotonic=time.monotonic):
        if type(batch_events) is not int or not 1 <= batch_events <= 100:
            raise ValueError('Invalid activity batch count')
        if type(batch_bytes) is not int or not 1024 <= batch_bytes <= 1048576:
            raise ValueError('Invalid activity batch bytes')
        if type(flush_interval) not in (int, float) or not math.isfinite(flush_interval) or not 0.01 <= flush_interval <= 1:
            raise ValueError('Invalid activity flush interval')
        if not callable(monotonic):
            raise ValueError('Activity requires a monotonic clock')
        if not isinstance(lease, AdmissionLease):
            raise ValueError('Activity requires its invocation-local admission lease')
        self.objects, self.lease = objects, lease
        self.stream = _uuid(producer_stream_id)
        if self.stream != str(lease.descriptor.run.attempt_id):
            raise ValueError('Native stream identity must match its admitted attempt')
        self.root = lease.root+'streams/'+self.stream+'/'
        self.batch_events, self.batch_bytes = batch_events, batch_bytes
        self.clock, self.flush_interval = monotonic, flush_interval
        self.first_text_flushed = False
        self.pending, self.seen = [], {}
        self.sequence = self.text_bytes = 0
        self.head = self.head_etag = None
        self.failed = False
        self._ownership()
        existing, _ = _read(objects, self.root+'head.json', p.JournalHead, optional=True)
        if existing is not None:
            raise DeliveryError('activity_stream_already_exists')
        self.last_flush = self.clock()

    def _ownership(self):
        current, _ = _read(self.objects, self.lease.current_key, p.CurrentPointer)
        claim, _ = _read(self.objects, self.lease.root+'claim.json', p.AdmissionClaim)
        if current != self.lease.current or claim != self.lease.claim:
            raise DeliveryError('activity_ownership_changed')
        p.validate_claim_binding(claim, self.lease.descriptor, owner_nonce=self.lease.claim.owner_nonce)

    def _batch(self, records):
        descriptor = self.lease.descriptor
        return p.JournalBatch(schema_version=p.PRODUCER_VERSION, batch_id=uuid4(),
            admission_id=descriptor.admission_id, scope=descriptor.scope,
            run_id=descriptor.run.run_id, attempt_id=descriptor.run.attempt_id,
            producer_stream_id=self.stream, first_sequence=records[0].producer_sequence,
            last_sequence=records[-1].producer_sequence, events=records)

    def append(self, record):
        if self.failed:
            raise DeliveryError('activity_delivery_stopped')
        try:
            return self._append(record)
        except Exception:
            self.failed = True
            raise DeliveryError('activity_append_failed') from None

    def _append(self, record):
        raw = record if isinstance(record, bytes) else p.producer_encoded(record)
        event = p.parse_producer_record(raw)
        prior = self.seen.get(str(event.event_id))
        if prior is not None:
            if prior != raw:
                raise DeliveryError('activity_event_conflict')
            return False
        descriptor = self.lease.descriptor
        actual = (event.run_id, event.attempt_id, str(event.producer_stream_id), event.producer_sequence)
        expected = (descriptor.run.run_id, descriptor.run.attempt_id, self.stream, self.sequence+1)
        if actual != expected:
            raise DeliveryError('activity_sequence_or_binding_conflict')
        added = len(event.public_text_delta.encode('utf-8')) if isinstance(event, p.ProducerTextDelta) else 0
        if self.text_bytes+added > 65536:
            raise DeliveryError('activity_text_limit')
        candidate = self.pending+[event]
        if self.pending and len(_bytes(self._batch(candidate))) > self.batch_bytes:
            self.flush()
            candidate = [event]
        if len(_bytes(self._batch(candidate))) > self.batch_bytes:
            raise DeliveryError('activity_batch_limit')
        self.pending = candidate
        self.seen[str(event.event_id)] = raw
        self.sequence += 1
        self.text_bytes += added
        if (len(self.pending) >= self.batch_events or not isinstance(event, p.ProducerTextDelta)
                or not self.first_text_flushed or self.clock()-self.last_flush >= self.flush_interval):
            self.flush()
        return True

    def flush_due(self):
        """The future async adapter calls this timer while the SDK emits no chunk."""
        if self.failed:
            raise DeliveryError('activity_delivery_stopped')
        try:
            if self.pending and self.clock()-self.last_flush >= self.flush_interval:
                return self.flush()
            return False
        except Exception:
            self.failed = True
            raise DeliveryError('activity_flush_failed') from None

    def flush(self):
        if self.failed:
            raise DeliveryError('activity_delivery_stopped')
        if not self.pending:
            return False
        try:
            self._flush()
            # Timestamp only after all object IO; slow IO must not expire a fresh batch.
            self.last_flush = self.clock()
            return True
        except Exception:
            self.failed = True
            raise DeliveryError('activity_flush_failed') from None

    def _flush(self):
        self._ownership()
        batch = self._batch(self.pending)
        raw = _bytes(batch)
        common = {field:getattr(batch, field) for field in ('admission_id','scope','run_id','attempt_id','producer_stream_id')}
        index = p.JournalIndex(schema_version=p.PRODUCER_VERSION, index_id=uuid4(), **common,
            index_sequence=1 if self.head is None else self.head.index_sequence+1,
            first_sequence=batch.first_sequence, last_sequence=batch.last_sequence,
            batch=p.BatchRef(batch_id=batch.batch_id, sha256=p.producer_sha256(raw), byte_length=len(raw)),
            previous_index_sha256=None if self.head is None else self.head.index_sha256)
        index_raw = _bytes(index)
        proposed = p.JournalHead(schema_version=p.PRODUCER_VERSION, **common,
            index_sequence=index.index_sequence, index_id=index.index_id,
            index_sha256=p.producer_sha256(index_raw), producer_sequence=index.last_sequence)
        p.validate_index_batch(index, raw)
        p.validate_head_successor(self.head, proposed, index_raw)
        _put(self.objects, self.root+'batches/'+str(batch.batch_id)+'-'+p.producer_sha256(raw)+'.json', raw, None)
        _put(self.objects, self.root+'indexes/'+index_object_name(index.index_sequence,
            p.producer_sha256(index_raw)), index_raw, None)
        self._ownership()
        self.head_etag = _put(self.objects, self.root+'head.json', _bytes(proposed), self.head_etag)
        self.head = proposed
        self.first_text_flushed |= any(isinstance(event, p.ProducerTextDelta) for event in self.pending)
        self.pending = []
