"""Inactive producer codec checks; no OCI, CAS, model or execution is simulated."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[3]/'contracts/sonda-activity/candidate-v1'
spec = importlib.util.spec_from_file_location('activity_producer_candidate_tests', ROOT/'producer.py')
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


def uid(number):
    return str(UUID(int=number))


def test_chat_admission_fixture_reuses_core_model_and_exact_canonical_bytes():
    from app.sonda_contract import ChatInput, encoded, no_duplicate_keys, sha256
    fixture = json.loads((ROOT/'fixtures/chat-admission-input.json').read_bytes())
    request = ChatInput.model_validate(json.loads(fixture['raw_input'], object_pairs_hook=no_duplicate_keys))
    canonical = encoded(request.model_dump(mode='json'))
    assert canonical.decode('utf-8') == fixture['canonical_utf8']
    assert sha256(canonical) == fixture['input_sha256']
    assert sha256(fixture['raw_input'].encode('utf-8')) != fixture['input_sha256']
    assert request.search_context.source_ids == [UUID(uid(32)), UUID(uid(31))]
    assert 'Cafe\u0301' in request.question and canonical.endswith(b'\n')


def test_native_batch_stream_is_derived_from_its_admitted_attempt():
    batch = json.loads((ROOT/'fixtures/producer-batch.json').read_bytes())
    admission = producer.parse_producer_document((ROOT/'fixtures/producer-descriptor.json').read_bytes())
    assert producer.validate_batch_binding(batch, admission).producer_stream_id == admission.run.attempt_id
    batch['producer_stream_id'] = uid(99)
    for event in batch['events']:
        event['producer_stream_id'] = uid(99)
    with pytest.raises(ValueError, match='admitted attempt'):
        producer.validate_batch_binding(batch, admission)


def descriptor(autonomous=False):
    fixture = json.loads((ROOT/'fixtures/runs.json').read_bytes())
    run = fixture['items'][0]
    value = {'schema_version': producer.PRODUCER_VERSION, 'admission_id': uid(20),
        'scope': fixture['scope'], 'run': run, 'native_session_sha256': 'a'*64,
        'input_sha256': 'b'*64, 'config_revision': 21, 'tool_catalog_sha256': 'c'*64,
        'created_at': run['created_at'], 'expires_at': '2026-10-30T00:30:00Z', 'work_input_sha256': None}
    if autonomous:
        run.update(source='autonomous_study', work_id=uid(90), investigation_id='d'*64)
        value['work_input_sha256'] = 'e'*64
    return value


def pointer(value=None):
    value = value or descriptor()
    return {'schema_version': producer.PRODUCER_VERSION, 'scope': value['scope'],
        'agent_id': value['run']['agent_id'], 'native_session_sha256': value['native_session_sha256'],
        'admission_id': value['admission_id'], 'run_id': value['run']['run_id'],
        'request_id': value['run']['request_id'], 'attempt_id': value['run']['attempt_id'],
        'epoch': 1, 'status': 'admitted', 'owner_nonce': None, 'expires_at': value['expires_at']}


def claim(value=None):
    value = value or descriptor()
    return {'schema_version': producer.PRODUCER_VERSION, 'scope': value['scope'],
        'admission_id': value['admission_id'], 'run_id': value['run']['run_id'],
        'attempt_id': value['run']['attempt_id'], 'owner_nonce': uid(80),
        'claimed_at': '2026-10-30T00:01:00Z'}


def admission_args(value=None):
    value = value or descriptor()
    return {'scope': value['scope'], 'agent_id': value['run']['agent_id'],
        'admission_id': value['admission_id'], 'native_session_sha256': value['native_session_sha256'],
        'input_sha256': value['input_sha256'], 'now': datetime(2026,10,30,0,5,tzinfo=timezone.utc)}


def event(sequence=1, **changes):
    value = descriptor()
    result = {'event_id': uid(100+sequence), 'run_id': value['run']['run_id'],
        'attempt_id': value['run']['attempt_id'], 'producer_stream_id': uid(30),
        'producer_sequence': sequence, 'span_id': uid(70), 'parent_span_id': None,
        'causation_id': None, 'emitted_at': '2026-10-30T00:02:00Z',
        'type': 'stage', 'summary': 'Consulta preparada.', 'effect': 'prepared', 'status': 'progress'}
    result.update(changes)
    return result


def completion(sequence=1):
    value = event(sequence)
    for key in ('type', 'effect', 'status'):
        value.pop(key)
    value.update(kind='execution_completion', outcome='completed',
        final_ref={'result_id': uid(60), 'sha256': 'f'*64, 'byte_length': 1024})
    return value


def batch(events=None, identity=40):
    events = events or [event()]
    value = descriptor()
    return {'schema_version': producer.PRODUCER_VERSION, 'batch_id': uid(identity),
        'scope': value['scope'], 'admission_id': value['admission_id'],
        'run_id': value['run']['run_id'], 'attempt_id': value['run']['attempt_id'],
        'producer_stream_id': uid(30), 'first_sequence': events[0]['producer_sequence'],
        'last_sequence': events[-1]['producer_sequence'], 'events': events}


def encoded_model(model, value):
    return producer.producer_encoded(model.model_validate(value).model_dump(mode='json'))


def index(value=None, *, sequence=1, previous=None):
    value = value or batch()
    raw = encoded_model(producer.JournalBatch, value)
    fields = ('scope', 'admission_id', 'run_id', 'attempt_id', 'producer_stream_id', 'first_sequence', 'last_sequence')
    return {'schema_version': producer.PRODUCER_VERSION, **{key:value[key] for key in fields},
        'index_id': uid(50+sequence), 'index_sequence': sequence,
        'batch': {'batch_id': value['batch_id'], 'sha256': producer.producer_sha256(raw), 'byte_length': len(raw)},
        'previous_index_sha256': previous}


def head(entry):
    raw = encoded_model(producer.JournalIndex, entry)
    fields = ('scope', 'admission_id', 'run_id', 'attempt_id', 'producer_stream_id', 'index_id', 'index_sequence')
    return {'schema_version': producer.PRODUCER_VERSION, **{key:entry[key] for key in fields},
        'index_sha256': producer.producer_sha256(raw), 'producer_sequence': entry['last_sequence']}


@pytest.mark.parametrize('model,factory', [
    (producer.AdmissionDescriptor, descriptor), (producer.CurrentPointer, pointer), (producer.AdmissionClaim, claim),
    (producer.JournalBatch, batch), (producer.JournalIndex, index),
    (producer.JournalHead, lambda: head(index())),
])
def test_closed_control_and_journal_canonical_round_trip(model, factory):
    raw = encoded_model(model, factory())
    value = producer.parse_producer_document(raw)
    assert isinstance(value, model)
    assert producer.producer_encoded(value.model_dump(mode='json')) == raw


def test_admission_binds_real_scope_agent_session_input_and_deadline():
    value = descriptor()
    assert producer.validate_admission(value, pointer(value), **admission_args(value)).run.source == 'huginn_chat'
    args = admission_args(value)
    args['now'] = datetime(2026,10,29,19,5,tzinfo=timezone(timedelta(hours=-5)))
    assert producer.validate_admission(value, pointer(value), **args)


def test_actual_metadata_reference_requires_its_canonical_uuid_representation():
    args = admission_args()
    args['admission_id'] = UUID(args['admission_id']).hex
    with pytest.raises(ValueError,match='canonical UUIDs'):
        producer.validate_admission(descriptor(),pointer(),**args)


@pytest.mark.parametrize('field,bad', [('agent_id',uid(999)), ('admission_id',uid(999)),
    ('scope',{'registry_id':uid(999),'generation_id':uid(2)}),
    ('native_session_sha256','0'*64), ('input_sha256','0'*64)])
def test_self_consistent_descriptor_cannot_replace_actual_invocation_facts(field,bad):
    args = admission_args()
    args[field] = bad
    with pytest.raises(ValueError, match='authenticated invocation'):
        producer.validate_admission(descriptor(), pointer(), **args)


@pytest.mark.parametrize('field,bad', [('admission_id',uid(999)), ('run_id',uid(999)),
    ('request_id',uid(999)), ('attempt_id',uid(999)), ('expires_at','2026-10-30T01:00:00Z')])
def test_retained_reference_does_not_match_a_new_current_pointer(field,bad):
    current = pointer()
    current[field] = bad
    with pytest.raises(ValueError, match='Current pointer'):
        producer.validate_admission(descriptor(), current, **admission_args())


def test_same_session_identical_input_still_requires_new_admission_and_request():
    first = descriptor()
    later = copy.deepcopy(first)
    later['admission_id'] = uid(21)
    later['run'].update(run_id=uid(31), request_id=uid(41), attempt_id=uid(51))
    current = pointer(later)
    current['epoch'] = 2
    assert first['input_sha256'] == later['input_sha256']
    assert first['native_session_sha256'] == later['native_session_sha256']
    with pytest.raises(ValueError, match='Current pointer'):
        producer.validate_admission(first, current, **admission_args(first))
    assert producer.validate_admission(later, current, **admission_args(later))


@pytest.mark.parametrize('status', ['running','uncertain','terminal'])
def test_running_or_uncertain_admission_never_rearms_on_expiry(status):
    current = pointer()
    current.update(status=status, owner_nonce=uid(80))
    args = admission_args()
    args['now'] += timedelta(days=1)
    with pytest.raises(ValueError, match='cannot be rearmed'):
        producer.validate_admission(descriptor(), current, **args)


def test_immutable_claim_burns_even_an_admitted_pointer_and_other_owner_cannot_adopt_it():
    with pytest.raises(ValueError, match='cannot be rearmed'):
        producer.validate_admission(descriptor(), pointer(), existing_claim=claim(), **admission_args())
    assert producer.validate_claim_binding(claim(), descriptor(), owner_nonce=uid(80))
    with pytest.raises(ValueError, match='another admission or invocation owner'):
        producer.validate_claim_binding(claim(), descriptor(), owner_nonce=uid(81))
    # Reconciliation has no expiry-based replacement or fresh admission result.
    assert not hasattr(producer.AdmissionClaim.model_validate(claim()), 'expires_at')


@pytest.mark.parametrize('when', [datetime(2026,10,29,23,59,tzinfo=timezone.utc),
    datetime(2026,10,30,0,30,tzinfo=timezone.utc), datetime(2026,10,30,0,5)])
def test_not_yet_valid_expired_or_naive_clock_is_denied(when):
    args = admission_args()
    args['now'] = when
    with pytest.raises(ValueError):
        producer.validate_admission(descriptor(), pointer(), **args)


@pytest.mark.parametrize('field,value', [('config_revision',True), ('config_revision',0),
    ('created_at','2026-10-30T00:00:00'), ('expires_at','2026-10-30T00:00:00Z'),
    ('expires_at','2026-10-30T01:00:01Z'), ('tool_catalog_sha256','bad')])
def test_descriptor_rejects_invalid_bounds_and_timestamps(field,value):
    data = descriptor()
    data[field] = value
    with pytest.raises(ValueError):
        producer.AdmissionDescriptor.model_validate(data)


def test_run_creation_time_and_autonomous_identity_are_not_optional_claims():
    value = descriptor()
    value['run']['created_at'] = '2026-10-30T00:00:01Z'
    with pytest.raises(ValueError):
        producer.AdmissionDescriptor.model_validate(value)


def test_autonomous_admission_must_match_already_loaded_protected_work_without_other_case_lookup():
    value = descriptor(True)
    actual = {'work_id': value['run']['work_id'], 'work_input_sha256': value['work_input_sha256'],
        'investigation_id': value['run']['investigation_id']}
    assert producer.validate_admission(value,pointer(value),**admission_args(value),**actual)
    with pytest.raises(ValueError,match='actual protected work input'):
        producer.validate_admission(value,pointer(value),**admission_args(value))
    for field,bad in [('work_id',uid(999)),('work_input_sha256','0'*64),('investigation_id','0'*64)]:
        changed = {**actual,field:bad}
        with pytest.raises(ValueError,match='actual protected autonomous work'):
            producer.validate_admission(value,pointer(value),**admission_args(value),**changed)
    with pytest.raises(ValueError,match='cannot adopt'):
        producer.validate_admission(descriptor(),pointer(),**admission_args(),**actual)


def test_provider_or_gold_run_does_not_borrow_native_admission():
    for kind,component in [('provider_execution','huginn_provider_broker'),('gold_publication','aidp_gold_runner')]:
        value = descriptor()
        value['run'].update(execution_kind=kind,component=component,agent_id=None,agent_name=None)
        with pytest.raises(ValueError,match='Only native invocations'):
            producer.AdmissionDescriptor.model_validate(value)


def test_known_initial_execution_and_causal_refs_are_immutable_descriptor_content_only():
    value = descriptor(True)
    value['run'].update(execution_ref='known-work-execution',core_request_id=uid(1000),core_message_ids=[uid(1001)],
        causal_parent={'run_id':uid(1002),'attempt_id':uid(1003),'span_id':uid(1004),'event_id':uid(1005)})
    parsed = producer.AdmissionDescriptor.model_validate(value)
    assert parsed.run.core_request_id == UUID(int=1000)
    assert parsed.run.causal_parent.event_id == UUID(int=1005)
    autonomous = descriptor(True)
    assert producer.AdmissionDescriptor.model_validate(autonomous).work_input_sha256
    autonomous['work_input_sha256'] = None
    with pytest.raises(ValueError):
        producer.AdmissionDescriptor.model_validate(autonomous)
    value = descriptor()
    value['run']['work_id'] = uid(90)
    with pytest.raises(ValueError):
        producer.AdmissionDescriptor.model_validate(value)


@pytest.mark.parametrize('value', [True, 0, '1'])
def test_pointer_epoch_is_a_strict_positive_integer(value):
    current = pointer()
    current['epoch'] = value
    with pytest.raises(ValueError):
        producer.CurrentPointer.model_validate(current)


def test_owner_nonce_is_null_only_before_claim():
    current = pointer()
    current['owner_nonce'] = uid(80)
    with pytest.raises(ValueError):
        producer.CurrentPointer.model_validate(current)
    current.update(status='running',owner_nonce=None)
    with pytest.raises(ValueError):
        producer.CurrentPointer.model_validate(current)


@pytest.mark.parametrize('field,value', [('authority','native_agent'), ('ingestion_sequence',1),
    ('received_at','2026-10-30T00:02:00Z'), ('resume_cursor','cursor'), ('sql','SELECT 1'),
    ('arguments',{}), ('prompt','private'), ('reasoning','private')])
def test_producer_cannot_assign_broker_fields_or_upload_private_payloads(field,value):
    item = event()
    item[field] = value
    with pytest.raises(ValueError):
        producer.parse_producer_record(producer.producer_encoded(item))


def test_explicit_public_payloads_reuse_read_bounds_but_not_authority():
    common = event()
    for key in ('type','summary','effect','status'):
        common.pop(key)
    values = [dict(common,type='tool_start',tool_name='sonda_query',summary='Leyendo Gold.',domain='query_read'),
        dict(common,type='tool_finish',summary='Lectura terminada.',effect='executed'),
        dict(common,type='tool_error',summary='Lectura no disponible.',error_code='query_unavailable'),
        dict(common,type='text_delta',public_text_delta='Respuesta parcial.')]
    for value in values:
        parsed = producer.parse_producer_record(producer.producer_encoded(value))
        assert not {'authority','received_at','ingestion_sequence'} & set(parsed.model_dump())
    for domain in ('research_preparation','provider_capture','publication'):
        values[0]['domain'] = domain
        assert producer.parse_producer_record(producer.producer_encoded(values[0]))
    values[0]['tool_name'] = 'x'*201
    with pytest.raises(ValueError):
        producer.parse_producer_record(producer.producer_encoded(values[0]))
    values[2]['error_code'] = 'arbitrary exception text'
    with pytest.raises(ValueError):
        producer.parse_producer_record(producer.producer_encoded(values[2]))


def test_native_completion_is_private_not_a_canonical_public_terminal():
    value = completion()
    parsed = producer.parse_producer_record(producer.producer_encoded(value))
    assert isinstance(parsed, producer.NativeCompletionReceipt)
    assert parsed.kind == 'execution_completion'
    with pytest.raises(ValueError):
        producer._read.Terminal.model_validate(value)
    value['type'] = 'run_terminal'
    with pytest.raises(ValueError):
        producer.parse_producer_record(producer.producer_encoded(value))
    value.pop('type')
    value['final_ref'] = None
    with pytest.raises(ValueError):
        producer.parse_producer_record(producer.producer_encoded(value))
    value['outcome'] = 'failed'
    assert producer.parse_producer_record(producer.producer_encoded(value)).final_ref is None


def test_degradation_signal_has_no_business_effect():
    for status in ('degraded','uncertain'):
        assert producer.ProducerStage.model_validate(event(status=status,effect=None))
        with pytest.raises(ValueError):
            producer.ProducerStage.model_validate(event(status=status,effect='committed'))


def test_event_utf8_bytes_and_batch_count_bounds_are_independent():
    item = event()
    for key in ('summary','effect','status'):
        item.pop(key)
    item.update(type='text_delta',public_text_delta='😀'*4096)
    with pytest.raises(ValueError, match='16 KiB'):
        producer.ProducerTextDelta.model_validate(item)
    with pytest.raises(ValueError):
        producer.JournalBatch.model_validate(batch([event(index) for index in range(1,102)]))
    with pytest.raises(ValueError):
        producer.JournalBatch.model_validate(batch([event(10001)]))
    oversized = []
    for sequence in range(1,101):
        value = event(sequence)
        for key in ('summary','effect','status'):
            value.pop(key)
        value.update(type='text_delta',public_text_delta='文'*4000)
        oversized.append(value)
    with pytest.raises(ValueError, match='1 MiB'):
        producer.JournalBatch.model_validate(batch(oversized))


@pytest.mark.parametrize('failure', ['sequence_gap','run','attempt','producer','duplicate_event','bool_sequence'])
def test_batch_contiguity_identity_and_strict_counts(failure):
    value = batch([event(1),event(2)])
    second = value['events'][1]
    if failure == 'sequence_gap':
        second['producer_sequence'] = 3
        value['last_sequence'] = 3
    elif failure in ('run','attempt'):
        second[failure+'_id'] = uid(999)
    elif failure == 'producer':
        second['producer_stream_id'] = uid(999)
    elif failure == 'duplicate_event':
        second['event_id'] = value['events'][0]['event_id']
    else:
        second['producer_sequence'] = True
    with pytest.raises(ValueError):
        producer.JournalBatch.model_validate(value)


def test_private_completion_can_share_producer_sequence_without_claiming_public_terminal():
    value = batch([event(1),completion(2)])
    value['producer_stream_id'] = descriptor()['run']['attempt_id']
    for record in value['events']:
        record['producer_stream_id'] = value['producer_stream_id']
    parsed = producer.parse_producer_document(encoded_model(producer.JournalBatch,value))
    assert isinstance(parsed.events[-1],producer.NativeCompletionReceipt)
    assert producer.validate_batch_binding(parsed,descriptor())
    other = descriptor()
    other['admission_id'] = uid(999)
    with pytest.raises(ValueError,match='admission binding'):
        producer.validate_batch_binding(parsed,other)


def test_index_references_exact_batch_bytes_without_bucket_or_object_keys():
    value = batch()
    raw = encoded_model(producer.JournalBatch,value)
    entry = index(value)
    assert producer.validate_index_batch(entry,raw)
    for field,bad in [('sha256','0'*64),('byte_length',len(raw)+1),('batch_id',uid(999))]:
        wrong = copy.deepcopy(entry)
        wrong['batch'][field] = bad
        with pytest.raises(ValueError,match='immutable index reference'):
            producer.validate_index_batch(wrong,raw)
    entry['batch']['object_key'] = 'outside/admission.json'
    with pytest.raises(ValueError):
        producer.JournalIndex.model_validate(entry)


def test_head_is_hash_linked_contiguous_and_exact_replay_is_a_fact_only():
    first = index()
    first_raw = encoded_model(producer.JournalIndex,first)
    first_head = head(first)
    assert producer.validate_head_successor(None,first_head,first_raw) == 'create'
    assert producer.validate_head_successor(first_head,first_head,first_raw) == 'same'
    second = index(batch([event(2),event(3)],identity=41),sequence=2,previous=producer.producer_sha256(first_raw))
    second_raw = encoded_model(producer.JournalIndex,second)
    assert producer.validate_head_successor(first_head,head(second),second_raw) == 'replace'
    second['previous_index_sha256'] = '0'*64
    with pytest.raises(ValueError,match='exact contiguous predecessor'):
        producer.validate_head_successor(first_head,head(second),encoded_model(producer.JournalIndex,second))


def test_genesis_and_head_reference_bounds():
    value = head(index())
    value.update(index_sequence=0,index_id=None,index_sha256=None,producer_sequence=0)
    assert producer.JournalHead.model_validate(value)
    value['producer_sequence'] = 1
    with pytest.raises(ValueError):
        producer.JournalHead.model_validate(value)
    value = head(index())
    value['index_sequence'] = True
    with pytest.raises(ValueError):
        producer.JournalHead.model_validate(value)
    value = index()
    value['previous_index_sha256'] = '0'*64
    with pytest.raises(ValueError):
        producer.JournalIndex.model_validate(value)


def test_cross_admission_index_and_head_are_not_adopted():
    value = batch()
    entry = index(value)
    entry['admission_id'] = uid(999)
    with pytest.raises(ValueError,match='another admission'):
        producer.validate_index_batch(entry,encoded_model(producer.JournalBatch,value))
    entry = index(value)
    proposed = head(entry)
    proposed['attempt_id'] = uid(999)
    with pytest.raises(ValueError,match='bindings differ'):
        producer.validate_head_successor(None,proposed,encoded_model(producer.JournalIndex,entry))


def test_duplicate_json_noncanonical_bytes_and_byte_bounds_are_rejected():
    for raw in (b'{"scope":{},"scope":{}}', b'', b' '*1048577):
        with pytest.raises(ValueError):
            producer.parse_producer_document(raw)
    raw = encoded_model(producer.AdmissionDescriptor,descriptor())
    with pytest.raises(ValueError,match='canonical'):
        producer.parse_producer_document(b' '+raw)
    raw = producer.producer_encoded(event())
    with pytest.raises(ValueError,match='Duplicate producer'):
        producer.parse_producer_record(raw.replace(b'"prepared"',b'"prepared","effect":"queued"'))


def test_run_text_budget_accumulates_utf8_bytes_without_reset_or_authority():
    item = event()
    for key in ('summary','effect','status'):
        item.pop(key)
    item.update(type='text_delta',public_text_delta='é'*100)
    assert producer.public_text_bytes_after(0,batch([item])) == 200
    assert producer.public_text_bytes_after(65336,batch([item])) == 65536
    with pytest.raises(ValueError,match='run-wide'):
        producer.public_text_bytes_after(65337,batch([item]))
    with pytest.raises(ValueError):
        producer.public_text_bytes_after(True,batch([item]))


def test_export_schema_is_closed_and_separates_completion_from_public_terminal():
    schema = producer.producer_schema()
    assert schema['$schema'] == 'https://json-schema.org/draft/2020-12/schema'
    assert schema['$defs']['AdmissionDescriptor']['additionalProperties'] is False
    for name in ('ProducerStage','ProducerToolStart','ProducerToolFinish','ProducerToolError','ProducerTextDelta'):
        assert schema['$defs'][name]['additionalProperties'] is False
        fields = schema['$defs'][name]['properties']
        assert not {'authority','received_at','ingestion_sequence','resume_cursor'} & set(fields)
    completion_schema = schema['$defs']['NativeCompletionReceipt']['properties']
    assert completion_schema['kind']['const'] == 'execution_completion' and 'type' not in completion_schema
