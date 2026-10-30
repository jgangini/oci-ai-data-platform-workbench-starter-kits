"""Crash/replay, conditional writes and stream integrity with an in-memory object store."""
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from app.sonda_contract import encoded, sha256
from app.sonda_exchange import Blob, Exchange, RejectedMessage, StreamConflict

ROOT = Path(__file__).resolve().parents[3]/'contracts/sonda/v1'


class Objects:
    def __init__(self):
        self.values, self.counter, self.fail_key, self.reads = {}, 0, None, 0

    def get(self, role, key):
        self.reads += 1
        return self.values.get((role,key))

    def put(self, role, key, data, expected):
        old = self.get(role,key)
        if (old.etag if old else None) != expected:
            raise StreamConflict('CAS')
        if key == self.fail_key:
            self.fail_key = None
            raise TimeoutError('unknown write result')
        self.counter += 1
        self.values[role,key] = Blob(data,str(self.counter))
        return str(self.counter)


def exchange():
    return Exchange(Objects(), registry_id=UUID(int=1), generation_id=UUID(int=2),
        contract_sha256=json.loads((ROOT/'manifest.json').read_text())['contract_sha256'],
        direction='huginn_to_aidp',source_prefix='aidp-social/')


def fixture(name='01-configuration.json'):
    return (ROOT/'fixtures'/name).read_bytes()


def test_publish_consume_then_acknowledge_only_once():
    stream = exchange()
    prepared = stream.prepare(fixture())
    assert stream.publish(prepared) == 1
    assert stream.publish(prepared) == 1
    rows = stream.pending()
    assert [(seq,row.kind) for seq,row in rows] == [(1,'configuration')]
    stream.acknowledge(1)
    stream.acknowledge(1)
    assert stream.pending() == []


def test_orphan_body_is_invisible_and_reserved_retry_recovers():
    stream = exchange()
    prepared = stream.prepare(fixture())
    stream.objects.fail_key = stream._entry(1)
    with pytest.raises(TimeoutError):
        stream.publish(prepared)
    assert stream.pending() == []
    assert stream.publish(prepared) == 1


def test_head_timeout_retries_original_immutable_reservation():
    stream = exchange()
    prepared = stream.prepare(fixture())
    stream.objects.fail_key = stream.root+'head.json'
    with pytest.raises(TimeoutError):
        stream.publish(prepared)
    assert stream.pending() == []
    stream.publish(prepared)
    assert len(stream.pending()) == 1


def test_new_bytes_cannot_reuse_uuid_even_with_new_reservation():
    stream = exchange()
    stream.publish(stream.prepare(fixture()))
    body = json.loads(fixture())
    body['created_at'] = '2026-10-30T00:00:00Z'
    with pytest.raises(StreamConflict,match='different bytes'):
        stream.publish(stream.prepare(encoded(body)))


def test_prepared_reservation_cannot_be_tampered():
    stream = exchange()
    prepared = stream.prepare(fixture())
    wrong = json.loads(prepared.entry)
    wrong['message_sha256'] = '0'*64
    with pytest.raises(StreamConflict,match='inconsistent'):
        stream.publish(replace(prepared,entry=encoded(wrong)))


@pytest.mark.parametrize('corrupt', ['entry_gap','body_gap','body_hash','head_hash','predecessor'])
def test_corruption_never_advances_cursor(corrupt):
    stream = exchange()
    first = stream.prepare(fixture())
    stream.publish(first)
    second_body = json.loads(fixture())
    second_body['message_id'] = str(UUID(int=500))
    stream.publish(stream.prepare(encoded(second_body)))
    if corrupt == 'entry_gap':
        del stream.objects.values['artifacts',stream._entry(1)]
    elif corrupt in ('body_gap','body_hash'):
        key = stream._body('configuration',json.loads(fixture())['message_id'])['key']
        if corrupt == 'body_gap':
            del stream.objects.values['artifacts',key]
        else:
            stream.objects.values['artifacts',key] = Blob(b'{}','bad')
    else:
        key = stream.root+'head.json' if corrupt == 'head_hash' else stream._entry(2)
        body = json.loads(stream.objects.get('artifacts',key).data)
        body['last_entry_sha256' if corrupt == 'head_hash' else 'previous_entry_sha256'] = '0'*64
        stream.objects.values['artifacts',key] = Blob(encoded(body),'bad')
    with pytest.raises(StreamConflict):
        stream.pending()
    assert stream.objects.get('artifacts',stream.root+'ack.json') is None


def test_no_ack_skipping_or_generation_reuse():
    stream = exchange()
    stream.publish(stream.prepare(fixture()))
    with pytest.raises(StreamConflict):
        stream.acknowledge(2)
    body = json.loads(fixture())
    body['generation_id'] = str(UUID(int=777))
    with pytest.raises(ValueError,match='scope'):
        stream.prepare(encoded(body))


def test_observations_use_source_bucket_and_entries_remain_protected():
    stream = exchange()
    path = next(path for path in (ROOT/'fixtures').glob('*.json')
        if json.loads(path.read_text()).get('kind') == 'observations')
    stream.publish(stream.prepare(path.read_bytes()))
    assert any(role == 'source' and '/messages/' in key for role,key in stream.objects.values)
    assert ('artifacts',stream._entry(1)) in stream.objects.values
    assert stream.pending()[0][1].kind == 'observations'


def test_business_invalid_message_can_be_rejected_without_saving_its_payload():
    stream = exchange()
    body = json.loads(fixture())
    body['payload']['queries'][0]['max_items'] = -1
    raw = encoded(body)
    entry = stream._record('stream_entry',sequence=1,message_id=body['message_id'],
        message_kind=body['kind'],message_sha256=sha256(raw),message_bytes=len(raw),previous_entry_sha256=None)
    head = stream._record('stream_head',last_sequence=1,last_entry_sha256=sha256(entry),updated_at=body['created_at'])
    target = stream._body(body['kind'],body['message_id'])
    for role,key,data in [('artifacts',target['key'],raw),('artifacts',stream._entry(1),entry),
                          ('artifacts',stream.root+'head.json',head)]:
        stream.objects.put(role,key,data,None)
    rejected = stream.pending()[0][1]
    assert isinstance(rejected,RejectedMessage)
    assert not hasattr(rejected,'payload')
    stream.acknowledge(1)  # The runner must commit its rejection receipt before this call.
    assert stream.pending() == []


def test_large_backlog_drains_in_bounded_reads_without_rechecking_whole_suffix():
    stream = exchange()
    previous=None
    first=None
    for sequence in range(1,10002):
        body=json.loads(fixture())
        body['message_id']=str(UUID(int=sequence+1000))
        raw=encoded(body)
        entry=stream._record('stream_entry',sequence=sequence,message_id=body['message_id'],
            message_kind=body['kind'],message_sha256=sha256(raw),message_bytes=len(raw),
            previous_entry_sha256=previous)
        previous=sha256(entry)
        stream.objects.put('artifacts',stream._entry(sequence),entry,None)
        if sequence<=2:
            stream.objects.put('artifacts',stream._body(body['kind'],body['message_id'])['key'],raw,None)
    head=stream._record('stream_head',last_sequence=sequence,last_entry_sha256=previous,updated_at=body['created_at'])
    stream.objects.put('artifacts',stream.root+'head.json',head,None)
    stream.objects.reads=0
    assert [row[0] for row in stream.pending(limit=1)]==[1]
    stream.acknowledge(1)
    assert [row[0] for row in stream.pending(limit=1)]==[2]
    assert stream.objects.reads<25
