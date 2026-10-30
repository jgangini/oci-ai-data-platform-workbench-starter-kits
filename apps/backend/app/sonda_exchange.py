"""Pinned Sonda Object Storage streams; callers own the durable outbox and writer lease.

Persist PreparedPublication before publish, and retry those exact bytes after uncertainty.
Consumption returns validated messages; acknowledge only after their Gold/rejection journal
and receipt outbox have committed. No provider exceptions or message bodies are logged.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from uuid import UUID

from . import sonda_contract as contract


class StreamConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class Blob:
    data: bytes
    etag: str


class OciObjects:
    """Conditional object IO using the already configured OCI SDK client."""

    def __init__(self, client, namespace, buckets):
        self.client, self.namespace, self.buckets = client, namespace, dict(buckets)

    def get(self, role, key):
        try:
            response = self.client.get_object(self.namespace, self.buckets[role], key)
        except Exception as exc:
            if getattr(exc, 'status', None) == 404:
                return None
            raise
        try:
            raw = response.data.raw.read(contract.MAX_BYTES + 1)
        finally:
            response.data.close()
        if len(raw) > contract.MAX_BYTES:
            raise StreamConflict('Stream object exceeds its byte limit')
        return Blob(raw, response.headers['etag'])

    def put(self, role, key, data, expected):
        condition = {'if_none_match': '*'} if expected is None else {'if_match': expected}
        try:
            response = self.client.put_object(self.namespace, self.buckets[role], key,
                data, content_type='application/json', **condition)
        except Exception as exc:
            if getattr(exc, 'status', None) == 412:
                raise StreamConflict('Conditional stream write conflicted') from None
            raise
        return response.headers['etag']


@dataclass(frozen=True)
class PreparedPublication:
    message: bytes
    entry: bytes
    head: bytes
    previous_head_etag: str | None


@dataclass(frozen=True)
class RejectedMessage:
    """Safe identity for a durable rejection; never retain an invalid secret-bearing DTO."""
    message_id: UUID
    kind: str
    code: str = 'invalid_business_document'


class Exchange:
    def __init__(self, objects, *, registry_id, generation_id, contract_sha256,
                 direction, source_prefix):
        self.objects = objects
        self.scope = {'registry_id': UUID(str(registry_id)),
            'generation_id': UUID(str(generation_id)), 'contract_sha256': contract_sha256}
        self.direction, self.source_prefix = direction, source_prefix
        self.root = contract.stream_prefix(registry_id, generation_id, direction)

    def _parse(self, raw, kind=None):
        value = contract.parse(raw, **self.scope)
        if value.direction != self.direction or (kind and value.kind != kind):
            raise StreamConflict('Stream kind or direction does not match')
        return value

    def _body(self, kind, message_id):
        return contract.message_target(kind, message_id, self.scope['registry_id'],
            self.scope['generation_id'], self.direction, self.source_prefix)

    def _received(self, raw, entry):
        # Decode identity separately: a business-invalid document with valid transport
        # can be rejected durably; malformed/scope-invalid envelopes stop the stream.
        value = json.loads(raw, object_pairs_hook=contract.no_duplicate_keys)
        if not isinstance(value, dict):
            raise StreamConflict('Message envelope must be an object')
        identity = contract.Message.model_validate({key:value[key] for key in (
            'contract_version','contract_sha256','registry_id','generation_id',
            'message_id','created_at')})
        if (identity.registry_id != self.scope['registry_id']
            or identity.generation_id != self.scope['generation_id']
            or identity.contract_sha256 != self.scope['contract_sha256']
            or value.get('direction') != self.direction
            or identity.message_id != entry.message_id or value.get('kind') != entry.message_kind):
            raise StreamConflict('Message envelope identity does not match its committed entry')
        try:
            return self._parse(raw)
        except ValueError:
            return RejectedMessage(identity.message_id, entry.message_kind)

    def _cursor(self, name, kind):
        blob = self.objects.get('artifacts', self.root + name)
        return (self._parse(blob.data, kind), blob.etag) if blob else (None, None)

    def _record(self, kind, **fields):
        return contract.encoded({'contract_version': contract.VERSION,
            **{k:str(v) for k,v in self.scope.items()}, 'kind':kind,
            'direction':self.direction, **fields})

    def _entry(self, sequence):
        return self.root + 'entries/' + f'{sequence:020d}.json'

    def verified_hash(self, sequence):
        """Read the immutable raw-body hash for a message already returned by pending."""
        if type(sequence) is not int or sequence<1:
            raise ValueError('Invalid committed sequence')
        blob=self.objects.get('artifacts',self._entry(sequence))
        if blob is None:
            raise StreamConflict('Committed entry disappeared')
        entry=self._parse(blob.data,'stream_entry')
        if entry.sequence!=sequence:
            raise StreamConflict('Entry identity does not match its path')
        return entry.message_sha256

    def prepare(self, raw, *, now=None):
        """Under the generation writer lease, reserve then durably save this result."""
        message = self._parse(raw)
        self._body(message.kind, message.message_id)  # Reject control documents.
        previous, etag = self._cursor('head.json', 'stream_head')
        sequence = previous.last_sequence + 1 if previous else 1
        entry = self._record('stream_entry', sequence=sequence,
            message_id=str(message.message_id), message_kind=message.kind,
            message_sha256=contract.sha256(raw), message_bytes=len(raw),
            previous_entry_sha256=previous.last_entry_sha256 if previous else None)
        instant = (now or datetime.now(timezone.utc)).isoformat()
        head = self._record('stream_head', last_sequence=sequence,
            last_entry_sha256=contract.sha256(entry), updated_at=instant)
        self._parse(entry, 'stream_entry')
        self._parse(head, 'stream_head')
        return PreparedPublication(raw, entry, head, etag)

    def _immutable(self, role, key, raw):
        try:
            self.objects.put(role, key, raw, None)
        except StreamConflict:
            existing = self.objects.get(role, key)
            if existing is None or existing.data != raw:
                raise StreamConflict('Immutable stream object has different bytes') from None

    def publish(self, prepared):
        """Retry the persisted reservation, never prepare a fresh UUID after a timeout."""
        message = self._parse(prepared.message)
        entry = self._parse(prepared.entry, 'stream_entry')
        head = self._parse(prepared.head, 'stream_head')
        if (entry.message_id != message.message_id or entry.message_kind != message.kind
            or entry.message_sha256 != contract.sha256(prepared.message)
            or entry.message_bytes != len(prepared.message)
            or head.last_sequence != entry.sequence
            or head.last_entry_sha256 != contract.sha256(prepared.entry)):
            raise StreamConflict('Prepared publication is internally inconsistent')
        target = self._body(message.kind, message.message_id)
        self._immutable(target['bucket_role'], target['key'], prepared.message)
        self._immutable('artifacts', self._entry(entry.sequence), prepared.entry)
        current, etag = self._cursor('head.json', 'stream_head')
        if current and current.last_sequence >= entry.sequence:
            # A committed immutable entry plus a later valid head is an idempotent retry.
            self.read_entries(entry.sequence, current)
            return entry.sequence
        if etag != prepared.previous_head_etag:
            raise StreamConflict('Publication lost its reserved head')
        if (current.last_entry_sha256 if current else None) != entry.previous_entry_sha256:
            raise StreamConflict('Publication predecessor changed')
        self.objects.put('artifacts', self.root+'head.json', prepared.head, etag)
        return entry.sequence

    def _verify_head_tip(self, head):
        if head.last_sequence:
            tip = self.objects.get('artifacts', self._entry(head.last_sequence))
            if (tip is None or contract.sha256(tip.data) != head.last_entry_sha256
                or self._parse(tip.data,'stream_entry').sequence != head.last_sequence):
                raise StreamConflict('Committed head does not match its entry hash')

    def read_entries(self, start, head, *, previous_hash=None, maximum=100):
        """Verify a bounded contiguous window and the head tip, without rescanning history.

        The consumer validates every predecessor as it advances. An unseen gap stops
        at that gap; it does not prevent draining earlier valid committed messages.
        """
        if type(maximum) is not int or not 1 <= maximum <= 1000:
            raise ValueError('A stream read window must contain one to 1000 entries')
        self._verify_head_tip(head)
        rows = []
        for sequence in range(start, min(head.last_sequence+1,start+maximum)):
            blob = self.objects.get('artifacts', self._entry(sequence))
            if blob is None:
                raise StreamConflict('Committed stream has an entry gap')
            entry = self._parse(blob.data, 'stream_entry')
            if entry.sequence != sequence or (sequence == start and start == 1 and entry.previous_entry_sha256 is not None):
                raise StreamConflict('Stream sequence does not match its object path')
            if (sequence > start or previous_hash is not None) and entry.previous_entry_sha256 != previous_hash:
                raise StreamConflict('Stream predecessor hash does not match')
            previous_hash = contract.sha256(blob.data)
            rows.append(entry)
        if rows and rows[-1].sequence == head.last_sequence and previous_hash != head.last_entry_sha256:
            raise StreamConflict('Committed head does not match its entry hash')
        return rows

    def pending(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('A pending batch must contain one to 1000 entries')
        head, _ = self._cursor('head.json', 'stream_head')
        ack, _ = self._cursor('ack.json', 'consumer_ack')
        if head is None:
            if ack and ack.last_processed_sequence:
                raise StreamConflict('Acknowledged stream head is missing')
            return []
        sequence = ack.last_processed_sequence if ack else 0
        if sequence > head.last_sequence:
            raise StreamConflict('Acknowledgement is ahead of its stream')
        if sequence:
            blob = self.objects.get('artifacts', self._entry(sequence))
            if blob is None or contract.sha256(blob.data) != ack.last_entry_sha256:
                raise StreamConflict('Acknowledgement no longer matches its entry')
        entries = self.read_entries(sequence+1, head,
            previous_hash=ack.last_entry_sha256 if ack else None, maximum=limit)
        output = []
        for entry in entries:
            target = self._body(entry.message_kind, entry.message_id)
            body = self.objects.get(target['bucket_role'], target['key'])
            if (body is None or len(body.data) != entry.message_bytes
                or contract.sha256(body.data) != entry.message_sha256):
                raise StreamConflict('Committed message bytes do not match')
            message = self._received(body.data, entry)
            output.append((entry.sequence, message))
        return output

    def acknowledge(self, sequence, *, now=None):
        """Call only after durable business receipt/outbox commit, including rejection."""
        ack, etag = self._cursor('ack.json', 'consumer_ack')
        old = ack.last_processed_sequence if ack else 0
        if sequence <= old:
            return
        pending = self.pending(limit=1)
        if not pending or sequence != old+1 or pending[0][0] != sequence:
            raise StreamConflict('Acknowledgements must advance one committed message')
        blob = self.objects.get('artifacts', self._entry(sequence))
        raw = self._record('consumer_ack', last_processed_sequence=sequence,
            last_entry_sha256=contract.sha256(blob.data),
            updated_at=(now or datetime.now(timezone.utc)).isoformat())
        self.objects.put('artifacts', self.root+'ack.json', raw, etag)
