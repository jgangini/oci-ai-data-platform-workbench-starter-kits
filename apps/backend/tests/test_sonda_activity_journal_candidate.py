"""Offline adapter simulations, not proof of a live OCI conditional-write path."""
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[3]/'contracts/sonda-activity/candidate-v1'
spec = importlib.util.spec_from_file_location('activity_journal_candidate_tests', ROOT/'journal.py')
j = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = j
spec.loader.exec_module(j)
p = j.p


def uid(n):
    return str(UUID(int=n))


@dataclass
class Blob:
    data: bytes
    etag: str


class Objects:
    def __init__(self):
        self.data, self.puts = {}, []
        self.before = self.after = None

    def get(self, role, key):
        assert role == 'artifacts'
        return self.data.get(key)

    def put(self, role, key, raw, expected):
        assert role == 'artifacts'
        self.puts.append((key, raw, expected))
        if self.before:
            self.before(key)
        previous = self.data.get(key)
        if (previous.etag if previous else None) != expected:
            raise RuntimeError('private adapter detail')
        etag = str(len(self.puts))
        self.data[key] = Blob(raw, etag)
        if self.after:
            self.after(key)
        return etag


def seed():
    fixture = json.loads((ROOT/'fixtures/runs.json').read_bytes())
    run = fixture['items'][0]
    d = p.AdmissionDescriptor.model_validate({'schema_version':p.PRODUCER_VERSION,
        'admission_id':uid(20), 'scope':fixture['scope'], 'run':run,
        'native_session_sha256':'a'*64, 'input_sha256':'b'*64,
        'config_revision':21, 'tool_catalog_sha256':'c'*64,
        'created_at':run['created_at'], 'expires_at':'2026-10-30T00:30:00Z', 'work_input_sha256':None})
    c = p.CurrentPointer(schema_version=p.PRODUCER_VERSION, scope=d.scope,
        agent_id=d.run.agent_id, native_session_sha256=d.native_session_sha256,
        admission_id=d.admission_id, run_id=d.run.run_id, request_id=d.run.request_id,
        attempt_id=d.run.attempt_id, epoch=1, status='admitted', owner_nonce=None, expires_at=d.expires_at)
    args = dict(scope=d.scope.model_dump(mode='json'), agent_id=str(d.run.agent_id), session_sha256='a'*64,
        input_sha256='b'*64, admission_ref=uid(20), now=datetime(2026,10,30,0,5,tzinfo=timezone.utc))
    root, key = j.admission_keys(args['scope'], args['agent_id'], 'a'*64, uid(20))
    objects = Objects()
    objects.data[root+'descriptor.json'] = Blob(j._bytes(d), 'descriptor')
    objects.data[key] = Blob(j._bytes(c), 'current')
    return objects, args, root, key


def admitted():
    objects, args, root, key = seed()
    lease = j.admit_native(objects, **args)
    return objects, lease, root, key


def event(lease, sequence=1, **extra):
    d = lease.descriptor
    record = dict(event_id=uid(100+sequence), run_id=str(d.run.run_id), attempt_id=str(d.run.attempt_id),
        producer_stream_id=uid(5), producer_sequence=sequence, span_id=uid(70), parent_span_id=None,
        causation_id=None, emitted_at='2026-10-30T00:02:00Z', type='stage', summary='Consulta preparada.',
        effect='prepared', status='progress')
    record.update(extra)
    if record['type'] == 'text_delta':
        for field in ('summary','effect','status'):
            record.pop(field)
    return record


def test_admit_and_second_owner_denied():
    objects, args, root, key = seed()
    lease = j.admit_native(objects, **args)
    assert lease.claim.owner_nonce.version == 4
    assert len(objects.puts) == 2
    with pytest.raises(j.DeliveryError, match='activity_admission_denied'):
        j.admit_native(objects, **args)
    assert len(objects.puts) == 2


@pytest.mark.parametrize('change', ['expired','stale','input','scope','agent','malformed','consumed'])
def test_denial_has_zero_puts(change):
    objects, args, root, key = seed()
    if change == 'expired':
        args['now'] = datetime(2026,10,30,0,30,tzinfo=timezone.utc)
    elif change == 'stale':
        c = p.parse_producer_document(objects.data[key].data)
        objects.data[key] = Blob(j._bytes(c.model_copy(update={'admission_id':UUID(uid(22))})), 'new')
    elif change == 'malformed':
        objects.data[root+'descriptor.json'].data += b' '
    elif change == 'consumed':
        objects.data[root+'claim.json'] = Blob(b'{}\n', 'claim')
    else:
        args[{'input':'input_sha256','scope':'scope','agent':'agent_id'}[change]] = (
            {'registry_id':uid(98),'generation_id':uid(99)} if change == 'scope' else uid(99) if change == 'agent' else 'd'*64)
    with pytest.raises(j.DeliveryError):
        j.admit_native(objects, **args)
    assert objects.puts == []


@pytest.mark.parametrize('value', ['../x','UPPER','00000000000000000000000000000014'])
def test_key_traversal_or_noncanonical_uuid(value):
    objects, args, _, _ = seed()
    args['admission_ref'] = value
    with pytest.raises(j.DeliveryError):
        j.admit_native(objects, **args)
    assert not objects.puts


@pytest.mark.parametrize('phase', ['claim','current'])
def test_lost_admission_ack_reconciles_own_bytes(phase):
    objects, args, _, _ = seed()
    objects.after = lambda key: (_ for _ in ()).throw(TimeoutError()) if (phase in key) else None
    lease = j.admit_native(objects, **args)
    assert lease.current.owner_nonce == lease.claim.owner_nonce
    assert len(objects.puts) == 2


def test_crash_after_claim_burns_admission():
    objects, args, root, key = seed()
    objects.before = lambda target: (_ for _ in ()).throw(TimeoutError()) if target == key else None
    with pytest.raises(j.DeliveryError):
        j.admit_native(objects, **args)
    assert root+'claim.json' in objects.data
    objects.before = None
    before = len(objects.puts)
    with pytest.raises(j.DeliveryError):
        j.admit_native(objects, **args)
    assert len(objects.puts) == before


def test_pointer_race_cannot_start_old_owner():
    objects, args, root, key = seed()
    def replace(target):
        if target == root+'claim.json':
            c = p.parse_producer_document(objects.data[key].data)
            objects.data[key] = Blob(j._bytes(c.model_copy(update={'admission_id':UUID(uid(22)), 'epoch':2})), 'new')
    objects.after = replace
    with pytest.raises(j.DeliveryError):
        j.admit_native(objects, **args)
    assert root+'claim.json' in objects.data


def test_concurrent_claim_winner_never_adopts_foreign_nonce():
    objects, args, root, key = seed()
    def competing(target):
        if target == root+'claim.json':
            objects.before = None
            j.admit_native(objects, **args)
    objects.before = competing
    with pytest.raises(j.DeliveryError):
        j.admit_native(objects, **args)
    assert p.parse_producer_document(objects.data[key].data).status == 'running'


def test_immediate_flush_chain_and_duplicate_no_writes():
    objects, lease, _, _ = admitted()
    writer = j.JournalWriter(objects, lease, uid(5))
    first = event(lease)
    assert writer.append(first)
    assert writer.head.producer_sequence == 1
    count = len(objects.puts)
    assert not writer.append(first)
    assert not writer.flush()
    assert len(objects.puts) == count
    prior = writer.head.index_sha256
    writer.append(event(lease, 2))
    index = next(p.parse_producer_document(blob.data) for key, blob in objects.data.items()
        if '/indexes/' in key and p.parse_producer_document(blob.data).index_sequence == 2)
    assert index.previous_index_sha256 == prior
    assert writer.head.producer_sequence == 2


def test_head_replays_three_indexes_with_get_only_and_no_cached_ids():
    objects, lease, _, _ = admitted()
    writer = j.JournalWriter(objects, lease, uid(5))
    for sequence in range(1, 4):
        writer.append(event(lease, sequence))
    head = p.parse_producer_document(objects.get('artifacts', writer.root+'head.json').data)
    sequence, digest, recovered = head.index_sequence, head.index_sha256, []
    while sequence:
        raw = objects.get('artifacts', writer.root+'indexes/'+j.index_object_name(sequence, digest)).data
        assert p.producer_sha256(raw) == digest
        index = p.parse_producer_document(raw)
        assert index.index_sequence == sequence
        assert index.run_id == head.run_id and index.attempt_id == head.attempt_id
        if sequence == head.index_sequence:
            assert index.index_id == head.index_id
        batch = objects.get('artifacts', writer.root+'batches/'+str(index.batch.batch_id)+'-'+index.batch.sha256+'.json')
        p.validate_index_batch(index, batch.data)
        value = p.parse_producer_document(batch.data)
        recovered.extend(row.producer_sequence for row in value.events)
        digest = index.previous_index_sha256
        sequence -= 1
    assert digest is None and recovered == [3, 2, 1]


def test_native_stream_is_known_from_admission_and_rejects_another_uuid_without_io():
    objects, lease, _, _ = admitted()
    count = len(objects.puts)
    with pytest.raises(ValueError, match='admitted attempt'):
        j.JournalWriter(objects, lease, uid(30))
    assert len(objects.puts) == count
    writer = j.JournalWriter(objects, lease, str(lease.descriptor.run.attempt_id))
    assert writer.root == lease.root+'streams/'+str(lease.descriptor.run.attempt_id)+'/'


@pytest.mark.parametrize('sequence,digest', [(True,'a'*64),(0,'a'*64),(10001,'a'*64),
    (1,'A'*64),(1,'../index'),(1,None)])
def test_index_object_name_rejects_noncanonical_inputs(sequence, digest):
    with pytest.raises(ValueError):
        j.index_object_name(sequence, digest)


@pytest.mark.parametrize('field,value', [('authority','native_agent'),('received_at','2026-10-30T00:02:00Z'),
    ('ingestion_sequence',1),('callback_url','https://example.com'),('producer_sequence',2)])
def test_closed_records_and_sequence_fail_without_writes(field, value):
    objects, lease, _, _ = admitted()
    writer = j.JournalWriter(objects, lease, uid(5))
    before = len(objects.puts)
    with pytest.raises(j.DeliveryError):
        writer.append(event(lease, **{field:value}))
    assert len(objects.puts) == before


def test_conflicting_duplicate_event_stops_delivery():
    objects, lease, _, _ = admitted()
    writer = j.JournalWriter(objects, lease, uid(5))
    writer.append(event(lease))
    before = len(objects.puts)
    with pytest.raises(j.DeliveryError):
        writer.append(event(lease, summary='Changed'))
    with pytest.raises(j.DeliveryError, match='stopped'):
        writer.append(event(lease, 2))
    assert len(objects.puts) == before


def test_text_buffers_and_explicit_flush():
    objects, lease, _, _ = admitted()
    writer = j.JournalWriter(objects, lease, uid(5), batch_events=2)
    before = len(objects.puts)
    writer.append(event(lease, type='text_delta', public_text_delta='Hola '))
    assert len(objects.puts) == before+3
    assert writer.head.producer_sequence == 1
    writer.append(event(lease, 2, type='text_delta', public_text_delta='🌍'))
    assert len(objects.puts) == before+3
    assert writer.text_bytes == len('Hola 🌍'.encode())
    writer.append(event(lease, 3, type='text_delta', public_text_delta='!'))
    assert writer.head.producer_sequence == 3
    writer.append(event(lease, 4, type='text_delta', public_text_delta=' Fin'))
    assert writer.flush()
    assert writer.head.producer_sequence == 4


@pytest.mark.parametrize('suffix', ['claim.json','head.json'])
def test_foreign_readback_never_reconciles(suffix):
    objects, args, root, key = seed()
    if suffix == 'claim.json':
        def mutate(target):
            if target.endswith(suffix):
                value = p.parse_producer_document(objects.data[target].data)
                objects.data[target] = Blob(j._bytes(value.model_copy(update={'owner_nonce':UUID(uid(77))})), 'foreign')
                raise TimeoutError()
        objects.after = mutate
        with pytest.raises(j.DeliveryError):
            j.admit_native(objects, **args)
    else:
        lease = j.admit_native(objects, **args)
        writer = j.JournalWriter(objects, lease, uid(5))
        def mutate(target):
            if target.endswith(suffix):
                value = p.parse_producer_document(objects.data[target].data)
                objects.data[target] = Blob(j._bytes(value.model_copy(update={'index_id':UUID(uid(77))})), 'foreign')
                raise TimeoutError()
        objects.after = mutate
        with pytest.raises(j.DeliveryError):
            writer.append(event(lease))


def test_lost_head_ack_exact_bytes_once():
    objects, lease, _, _ = admitted()
    writer = j.JournalWriter(objects, lease, uid(5))
    objects.after = lambda key: (_ for _ in ()).throw(TimeoutError()) if key.endswith('head.json') else None
    writer.append(event(lease))
    assert writer.head.producer_sequence == 1
    assert len(objects.puts) == 5


def test_orphan_batch_and_index_not_visible_without_head():
    objects, lease, _, _ = admitted()
    writer = j.JournalWriter(objects, lease, uid(5))
    objects.before = lambda key: (_ for _ in ()).throw(TimeoutError()) if key.endswith('head.json') else None
    with pytest.raises(j.DeliveryError):
        writer.append(event(lease))
    assert any('/batches/' in key for key in objects.data)
    assert any('/indexes/' in key for key in objects.data)
    assert writer.root+'head.json' not in objects.data
    assert writer.failed


def test_ownership_change_stops_before_batch_put():
    objects, lease, root, key = admitted()
    writer = j.JournalWriter(objects, lease, uid(5))
    objects.data[key] = Blob(j._bytes(lease.current.model_copy(update={'owner_nonce':UUID(uid(80))})), 'other')
    before = len(objects.puts)
    with pytest.raises(j.DeliveryError):
        writer.append(event(lease))
    assert len(objects.puts) == before


def test_text_total_and_record_byte_bounds():
    objects, lease, _, _ = admitted()
    writer = j.JournalWriter(objects, lease, uid(5))
    for number in range(1, 17):
        writer.append(event(lease, number, type='text_delta', public_text_delta='a'*4096))
    with pytest.raises(j.DeliveryError):
        writer.append(event(lease, 17, type='text_delta', public_text_delta='x'))
    assert writer.text_bytes == 65536


@pytest.mark.parametrize('options', [{'batch_events':True},{'batch_events':101},{'batch_bytes':False},{'batch_bytes':1048577}])
def test_strict_batch_options(options):
    objects, lease, _, _ = admitted()
    with pytest.raises(ValueError):
        j.JournalWriter(objects, lease, uid(5), **options)


def test_ten_thousand_event_limit():
    objects, lease, _, _ = admitted()
    writer = j.JournalWriter(objects, lease, uid(5))
    for number in range(1, 10001):
        writer.append(event(lease, number, type='text_delta', public_text_delta='a'))
    writer.flush()
    assert writer.head.producer_sequence == 10000
    before = len(objects.puts)
    with pytest.raises(j.DeliveryError):
        writer.append(event(lease, 10001, type='text_delta', public_text_delta='a'))
    assert len(objects.puts) == before


def test_batch_byte_threshold_flushes_before_overflow():
    objects, lease, _, _ = admitted()
    writer = j.JournalWriter(objects, lease, uid(5), batch_bytes=1800)
    writer.append(event(lease, type='text_delta', public_text_delta='a'*600))
    writer.append(event(lease, 2, type='text_delta', public_text_delta='b'*600))
    writer.append(event(lease, 3, type='text_delta', public_text_delta='c'*600))
    assert writer.head.producer_sequence == 2
    assert len(writer.pending) == 1
    writer.flush()
    assert writer.head.producer_sequence == 3
    assert all(len(blob.data) <= 1800 for key, blob in objects.data.items() if '/batches/' in key)


@pytest.mark.parametrize('segment', ['/batches/','/indexes/'])
def test_lost_immutable_ack_exact_readback(segment):
    objects, lease, _, _ = admitted()
    writer = j.JournalWriter(objects, lease, uid(5))
    objects.after = lambda key: (_ for _ in ()).throw(TimeoutError()) if segment in key else None
    writer.append(event(lease))
    assert writer.head.producer_sequence == 1
    assert len(objects.puts) == 5


def test_adapter_error_never_escapes_constructor():
    objects, lease, _, _ = admitted()
    def fail(*args):
        raise RuntimeError('SECRET adapter input')
    objects.get = fail
    with pytest.raises(j.DeliveryError, match='^activity_read_failed$'):
        j.JournalWriter(objects, lease, uid(5))


def test_nontext_flushes_immediately_after_first_text():
    objects, lease, _, _ = admitted()
    writer = j.JournalWriter(objects, lease, uid(5))
    writer.append(event(lease, type='text_delta', public_text_delta='Hola'))
    writer.append(event(lease, 2))
    batches = [p.parse_producer_document(blob.data) for key, blob in objects.data.items() if '/batches/' in key]
    assert [[record.producer_sequence for record in batch.events] for batch in batches] == [[1], [2]]
    assert writer.head.producer_sequence == 2


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def test_first_delta_head_is_persisted_before_any_completion():
    objects, lease, _, _ = admitted()
    clock = Clock()
    writer = j.JournalWriter(objects, lease, uid(5), monotonic=clock)
    delta = event(lease, type='text_delta', public_text_delta='Hola')
    writer.append(delta)
    assert writer.head.producer_sequence == 1
    saved = p.parse_producer_document(objects.data[writer.root+'head.json'].data)
    assert saved.producer_sequence == 1
    assert not writer.pending
    count = len(objects.puts)
    assert not writer.append(delta)
    assert not writer.flush_due()
    assert len(objects.puts) == count


def test_flush_due_publishes_pending_during_sdk_pause():
    objects, lease, _, _ = admitted()
    clock = Clock()
    writer = j.JournalWriter(objects, lease, uid(5), monotonic=clock)
    writer.append(event(lease, type='text_delta', public_text_delta='a'))
    writer.append(event(lease, 2, type='text_delta', public_text_delta='b'))
    assert writer.head.producer_sequence == 1
    clock.value = 0.099
    assert not writer.flush_due()
    clock.value = 0.1
    assert writer.flush_due()
    assert writer.head.producer_sequence == 2
    count = len(objects.puts)
    assert not writer.flush_due()
    assert len(objects.puts) == count


def test_slow_io_resets_cadence_after_completion_not_before():
    objects, lease, _, _ = admitted()
    clock = Clock()
    writer = j.JournalWriter(objects, lease, uid(5), monotonic=clock)
    def slow_io(key):
        clock.value += 2.0
    objects.after = slow_io
    writer.append(event(lease, type='text_delta', public_text_delta='a'))
    assert writer.last_flush == 6.0
    writer.append(event(lease, 2, type='text_delta', public_text_delta='b'))
    assert writer.head.producer_sequence == 1
    assert not writer.flush_due()
    clock.value = 6.101
    assert writer.flush_due()
    assert writer.last_flush == clock.value


def test_due_failure_latches_without_retry_or_business_callback():
    objects, lease, _, _ = admitted()
    clock = Clock()
    writer = j.JournalWriter(objects, lease, uid(5), monotonic=clock)
    writer.append(event(lease, type='text_delta', public_text_delta='a'))
    writer.append(event(lease, 2, type='text_delta', public_text_delta='b'))
    objects.before = lambda key: (_ for _ in ()).throw(TimeoutError())
    clock.value = 0.1
    with pytest.raises(j.DeliveryError):
        writer.flush_due()
    count = len(objects.puts)
    for operation in (writer.flush_due, writer.flush):
        with pytest.raises(j.DeliveryError, match='stopped'):
            operation()
    assert len(objects.puts) == count


@pytest.mark.parametrize('interval', [True,0,0.001,2,float('nan'),float('inf')])
def test_invalid_flush_interval(interval):
    objects, lease, _, _ = admitted()
    with pytest.raises(ValueError):
        j.JournalWriter(objects, lease, uid(5), flush_interval=interval)
