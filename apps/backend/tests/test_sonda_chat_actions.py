"""Draft capability conformance; no provider, broker, SQL or model execution."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
import sys
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.sonda_chat_actions import (ACTION_ADAPTER, ActionChatInput, CaptureDecision,
    CaptureIntent, CaptureQuote, action_fingerprint, check_capture_decision,
    check_capture_intent, check_capture_quote, parse_action, capture_receipt_path,
    check_capture_publication, parse_capture_receipt)
from app.sonda_contract import ExecutionMessage, ReceiptMessage, StreamEntry, encoded, sha256

ROOT=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location('export_actions',ROOT/'scripts/export_sonda_chat_actions.py')
exporter=importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)
EXAMPLES=exporter.action_fixtures('a'*64,'b'*64)
NOW=datetime(2026,1,1,0,1,tzinfo=timezone.utc)


def parsed(name,**changes):
    return ACTION_ADAPTER.validate_python({**copy.deepcopy(EXAMPLES[name]),**changes})


@pytest.mark.parametrize('name',list(EXAMPLES))
def test_synthetic_capability_documents_conform(name):
    result=parse_action(encoded(EXAMPLES[name]),capability_sha256='a'*64,
        core_contract_sha256='b'*64,registry_id=UUID(int=1),generation_id=UUID(int=2))
    assert result.kind==EXAMPLES[name]['kind']


def test_intent_quote_and_operator_decision_correlate_without_authorizing_execution():
    turn=parsed('chat-input.json');intent=parsed('intent.json');quote=parsed('quote.json');decision=parsed('decision-approve.json')
    assert check_capture_intent(turn,intent,now=NOW)==intent
    assert check_capture_quote(intent,quote,now=NOW)==quote
    assert check_capture_decision(quote,decision,operation_revision=3,now=NOW)==decision
    assert not hasattr(intent,'approval') and not hasattr(intent,'budget_round_id')


@pytest.mark.parametrize('changes',[
    {'actor_id':'arbitrary'}, {'approval':{'status':'approved'}}, {'max_items':True},
    {'max_cost_microusd':-1}, {'purpose':'   '}, {'intent_id':str(UUID(int=999))},
    {'target_override':'file:///private'}, {'target_override':'https://user:password@example.com/'}])
def test_model_intent_cannot_add_authority_or_invalid_limits(changes):
    with pytest.raises(ValidationError):parsed('intent.json',**changes)


@pytest.mark.parametrize('changes',[
    {'turn_id':str(UUID(int=99))}, {'config_revision':2}, {'capability_sha256':'c'*64},
    {'source_id':str(UUID(int=99))}, {'query_id':str(UUID(int=99))},
    {'expires_at':'2026-01-02T00:00:00Z'}, {'expires_at':'2026-01-01T00:00:30Z'}])
def test_intent_must_match_original_live_server_turn(changes):
    value={**EXAMPLES['intent.json'],**changes}
    if 'turn_id' in changes:
        from app.sonda_chat_actions import capture_intent_id
        value['intent_id']=str(capture_intent_id(value['registry_id'],value['generation_id'],value['turn_id']))
    with pytest.raises(ValueError):
        check_capture_intent(parsed('chat-input.json'),CaptureIntent.model_validate(value),now=NOW)


def test_same_turn_changed_body_is_same_id_but_different_immutable_fingerprint():
    one=parsed('intent.json');other=parsed('intent.json',max_items=1)
    assert one.intent_id==other.intent_id
    assert action_fingerprint(one)!=action_fingerprint(other)
    # The operation owner must reject this conflict rather than overwrite it.


@pytest.mark.parametrize('changes',[
    {'max_items':3}, {'max_cost_microusd':250001}, {'intent_sha256':'c'*64},
    {'config_revision':2}, {'expires_at':'2026-01-01T00:16:00Z'},
    {'expires_at':'2026-01-01T00:00:30Z'}, {'capability_sha256':'c'*64}])
def test_quote_cannot_expand_or_reinterpret_intent(changes):
    with pytest.raises(ValueError):
        check_capture_quote(parsed('intent.json'),parsed('quote.json',**changes),now=NOW)


@pytest.mark.parametrize('changes',[
    {'expected_operation_revision':2}, {'quote_sha256':'c'*64}, {'quote_id':str(UUID(int=99))},
    {'generation_id':str(UUID(int=99))}, {'core_contract_sha256':'c'*64}])
def test_operator_decision_is_bound_to_exact_quote_and_revision(changes):
    with pytest.raises(ValueError):
        check_capture_decision(parsed('quote.json'),parsed('decision-approve.json',**changes),
            operation_revision=3,now=NOW)


def test_actor_success_and_unscoped_status_are_not_ready():
    with pytest.raises(ValidationError):parsed('status-captured.json',state='ready')
    wrong=copy.deepcopy(EXAMPLES['status-ready.json']);wrong['publication']['execution_id']=str(UUID(int=99))
    with pytest.raises(ValidationError):ACTION_ADAPTER.validate_python(wrong)
    with pytest.raises(ValidationError):parsed('status-running.json',execution_id=None)
    with pytest.raises(ValidationError):parsed('status-uncertain.json',failure_code=None)


def test_parse_rejects_cross_scope_wrong_pins_duplicates_and_large_body():
    raw=encoded(EXAMPLES['intent.json'])
    for changes in ({'registry_id':UUID(int=99)},{'generation_id':UUID(int=99)},
            {'capability_sha256':'c'*64},{'core_contract_sha256':'c'*64}):
        with pytest.raises(ValueError):parse_action(raw,**{'capability_sha256':'a'*64,
            'core_contract_sha256':'b'*64,'registry_id':UUID(int=1),'generation_id':UUID(int=2),**changes})
    for invalid in (b'{"kind":"capture_intent","kind":"capture_quote"}',b' '*1048577):
        with pytest.raises(ValueError):parse_action(invalid,capability_sha256='a'*64,
            core_contract_sha256='b'*64,registry_id=UUID(int=1),generation_id=UUID(int=2))


def test_one_action_per_turn_and_nested_capability_must_match():
    value=copy.deepcopy(EXAMPLES['chat-response-action.json'])
    value['actions']*=2
    with pytest.raises(ValidationError):ACTION_ADAPTER.validate_python(value)
    value=copy.deepcopy(EXAMPLES['chat-response-action.json']);value['actions'][0]['capability_sha256']='c'*64
    with pytest.raises(ValidationError):ACTION_ADAPTER.validate_python(value)


def test_turn_expiry_is_bounded_and_time_fingerprints_normalize_to_utc():
    with pytest.raises(ValidationError):parsed('chat-input.json',expires_at='2026-01-01T00:16:00Z')
    utc=parsed('intent.json')
    offset=parsed('intent.json',expires_at='2025-12-31T19:15:00-05:00')
    assert action_fingerprint(utc)==action_fingerprint(offset)


def test_draft_export_is_portable_and_preserves_core_pin(tmp_path,monkeypatch):
    files=exporter.artifacts()
    for name,raw in files.items():
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    (tmp_path/'sonda_core_1_0_2.py').write_bytes((ROOT/'contracts/sonda/v1/reference.py').read_bytes())
    monkeypatch.syspath_prepend(str(tmp_path))
    reference_spec=importlib.util.spec_from_file_location('portable_chat_actions',tmp_path/'reference.py')
    module=importlib.util.module_from_spec(reference_spec)
    monkeypatch.setitem(sys.modules,'portable_chat_actions',module)
    reference_spec.loader.exec_module(module)
    manifest=json.loads(files['manifest.json'])
    assert manifest['core_contract_sha256']=='6e2613ac0c28e863fdf2fc02d69f92b92dd56003edcc242c6b41bbe218924483'
    for name in manifest['fixtures']:
        module.parse_action(files[name],capability_sha256=manifest['capability_sha256'],
            core_contract_sha256=manifest['core_contract_sha256'],registry_id=UUID(int=1),generation_id=UUID(int=2))


def gold_arguments(*,zero=False,split=False):
    evidence=exporter.gold_evidence('b'*64,zero=zero,split=split)
    proof=ACTION_ADAPTER.validate_python({**EXAMPLES['capture-publication.json'],**evidence['receipt_fields']})
    row={'registry_id':str(proof.registry_id),'generation_id':str(proof.generation_id),
        'contract_version':'1.0.2','contract_sha256':'b'*64,'publication_id':proof.publication_id,
        'config_revision':proof.publication_config_revision,'versions_json':json.dumps(proof.serving_versions),
        'message_ids_json':json.dumps(proof.publication_message_ids),'published_at':proof.published_at}
    return [proof,encoded(evidence['execution']),StreamEntry.model_validate(evidence['entry']),
        [ReceiptMessage.model_validate(value) for value in evidence['receipts']],row]


def gold_check(args,*,zero=False,split=False,observation_raw=None,snapshot=None):
    from app.sonda_chat_actions import CaptureSnapshot
    evidence=exporter.gold_evidence('b'*64,zero=zero,split=split)
    return check_capture_publication(*args,
        observation_raw=[encoded(row) for row in evidence['observations']] if observation_raw is None else observation_raw,
        snapshot=CaptureSnapshot.model_validate(evidence['snapshot']) if snapshot is None else snapshot)


def test_writer_correlates_real_input_ids_applied_receipts_and_exact_publication():
    args=gold_arguments()
    assert gold_check(args)==args[0]


@pytest.mark.parametrize('failure',['rejected','foreign_receipt','case_request','failed_capture','count',
    'publication_inputs','publication_versions','publication_id','entry_predecessor','receipt_sequence','untyped'])
def test_gold_readiness_rejects_fabricated_or_uncommitted_evidence(failure):
    args=gold_arguments()
    if failure=='rejected':args[3][0].payload.status='rejected'
    if failure=='foreign_receipt':args[3][0].generation_id=UUID(int=99)
    if failure in ('case_request','failed_capture','count'):
        value=json.loads(args[1])
        if failure=='case_request':value['payload']['request_id']=str(UUID(int=99))
        if failure=='failed_capture':value['payload']['status']='failed'
        if failure=='count':value['payload']['record_count']=3
        args[1]=encoded(value)
    if failure=='publication_inputs':args[4]['message_ids_json']='[]'
    if failure=='publication_versions':args[4]['versions_json']='{}'
    if failure=='publication_id':args[4]['publication_id']='f'*64
    if failure=='entry_predecessor':args[2].previous_entry_sha256='f'*64
    if failure=='receipt_sequence':args[3][0].payload.source_sequence=99
    if failure=='untyped':args[3]=[{}]
    with pytest.raises(ValueError):gold_check(args)


def test_capture_receipt_object_path_and_read_are_bound_to_exact_execution():
    proof=parsed('capture-publication.json');raw=encoded(proof.model_dump(mode='json'))
    path=capture_receipt_path(proof.registry_id,proof.generation_id,proof.execution_id)
    assert path.endswith('/control/chat_actions/v1/capture_receipts/'+str(proof.execution_id)+'.json')
    scope={'capability_sha256':'a'*64,'core_contract_sha256':'b'*64,
        'registry_id':proof.registry_id,'generation_id':proof.generation_id}
    assert parse_capture_receipt(raw,execution_id=proof.execution_id,previous_sha256=sha256(raw),**scope)==proof
    for invalid in ('../escape','https://example.com/','00000000-0000-0000-0000-000000000009/extra'):
        with pytest.raises(ValueError):capture_receipt_path(proof.registry_id,proof.generation_id,invalid)
    with pytest.raises(ValueError):parse_capture_receipt(raw,execution_id=UUID(int=99),**scope)
    with pytest.raises(ValueError):parse_capture_receipt(raw,execution_id=proof.execution_id,previous_sha256='f'*64,**scope)


def test_receipt_cannot_forge_publication_hash_versions_or_capture_membership():
    original=EXAMPLES['capture-publication.json']
    for change in ({'publication_id':'f'*64},{'serving_versions':{'ai_social_document_current':0}},
            {'publication_message_ids':[str(UUID(int=99))]},
            {'capture_config_revision':2,'publication_config_revision':1}):
        with pytest.raises(ValidationError):ACTION_ADAPTER.validate_python({**original,**change})


def test_empty_capture_catalog_denies_actions_even_when_read_scope_means_all():
    turn=parsed('chat-input-no-capture-options.json')
    turn.search_context.source_ids=[];turn.search_context.query_ids=[]
    with pytest.raises(ValueError,match='No capture option'):
        check_capture_intent(turn,parsed('intent.json'),now=NOW)
    from app.sonda_chat_actions import check_chat_response
    assert check_chat_response(turn,parsed('chat-response-no-gold.json'),now=NOW)


@pytest.mark.parametrize('changes',[{'target':'https://other.example/path'},{'target':'http://127.0.0.1/'},
    {'target':'https://user:password@example.com/'},{'max_items':3},{'source_revision':2}])
def test_changed_option_cannot_retain_its_fingerprint(changes):
    value=copy.deepcopy(EXAMPLES['chat-input.json']);value['capture_options'][0].update(changes)
    with pytest.raises(ValidationError):ACTION_ADAPTER.validate_python(value)


@pytest.mark.parametrize('override',['http://127.0.0.1/','https://other.example/path',
    'https://example.com:8443/path','https://example.com/path#fragment'])
def test_capture_override_remains_inside_reviewed_public_target_rules(override):
    with pytest.raises(ValueError):
        check_capture_intent(parsed('chat-input.json'),parsed('intent.json',target_override=override),now=NOW)


def test_catalog_is_complete_bounded_and_bound_to_the_server_turn():
    from app.sonda_chat_actions import CaptureOption
    original=EXAMPLES['chat-input.json']
    for changes in ({'capture_options_total':2},{'capture_options':original['capture_options']*21,'capture_options_total':21}):
        with pytest.raises(ValidationError):ACTION_ADAPTER.validate_python({**original,**changes})
    value=copy.deepcopy(original);option=value['capture_options'][0]
    option['turn_id']=str(UUID(int=99));option.pop('option_id')
    option['option_id']=sha256(encoded(option))
    CaptureOption.model_validate(option)  # Valid metadata, but for another server turn.
    with pytest.raises(ValidationError):ACTION_ADAPTER.validate_python(value)


def test_capture_current_revision_can_differ_from_historical_gold_read_revision():
    from app.sonda_chat_actions import check_chat_response
    turn=copy.deepcopy(EXAMPLES['chat-input.json']);turn['capture_config_revision']=2
    option=turn['capture_options'][0];option['capture_config_revision']=2;option.pop('option_id')
    option['option_id']=sha256(encoded(option))
    request=ACTION_ADAPTER.validate_python(turn)
    intent=parsed('intent.json',config_revision=2,capture_option_id=option['option_id'])
    assert check_capture_intent(request,intent,now=NOW)
    response=parsed('chat-response-gold.json',capture_config_revision=2)
    assert response.gold.publication.config_revision==1
    assert check_chat_response(request,response,now=NOW)


def test_response_envelope_accepts_action_without_gold_and_gold_without_actions():
    from app.sonda_chat_actions import check_chat_response
    turn=parsed('chat-input.json')
    for name in ('chat-response-action.json','chat-response-gold.json','chat-response-no-gold.json'):
        assert check_chat_response(turn,parsed(name),now=NOW)
    for changes in ({'turn_id':str(UUID(int=99))},{'capture_config_revision':2}):
        with pytest.raises(ValueError):check_chat_response(turn,parsed('chat-response-gold.json',**changes),now=NOW)
    with pytest.raises(ValidationError):parsed('chat-response-action.json',gold_unavailable_reason=None)


def test_receipt_hash_matches_existing_native_publication_metadata_algorithm():
    from app.labs.ai_social_network.notebooks.agent.sonda_chat import publication_metadata
    receipt=parsed('capture-publication.json')
    binding={'registry_id':str(receipt.registry_id),'generation_id':str(receipt.generation_id)}
    row={'publication_id':receipt.publication_id,'published_at':receipt.published_at}
    metadata=publication_metadata(binding,row,receipt.publication_config_revision,receipt.serving_versions)
    assert metadata['publication_id']==receipt.publication_id


def test_nonfinite_payload_and_unreviewed_query_fields_are_rejected():
    value=copy.deepcopy(EXAMPLES['chat-response-gold.json'])
    value['gold']['query_context'][0]['args']['sql']='SELECT private FROM elsewhere'
    with pytest.raises(ValidationError):ACTION_ADAPTER.validate_python(value)
    raw=encoded(EXAMPLES['chat-response-gold.json']).replace(b'"rows":[["youtube",1]]',b'"rows":[["youtube",NaN]]')
    assert b'NaN' in raw
    with pytest.raises(ValueError):parse_action(raw,capability_sha256='a'*64,core_contract_sha256='b'*64,
        registry_id=UUID(int=1),generation_id=UUID(int=2))


def test_read_scope_a_and_server_capture_option_b_remain_independent():
    from app.sonda_chat_actions import check_chat_response
    value=copy.deepcopy(EXAMPLES['chat-input.json'])
    value['search_context']['source_ids']=[str(UUID(int=40))]
    value['search_context']['query_ids']=[str(UUID(int=50))]
    turn=ACTION_ADAPTER.validate_python(value)
    before=turn.model_dump(mode='json')
    assert check_capture_intent(turn,parsed('intent.json'),now=NOW)
    response=copy.deepcopy(EXAMPLES['chat-response-gold.json'])
    response['gold']['search_context']=value['search_context']
    assert check_chat_response(turn,ACTION_ADAPTER.validate_python(response),now=NOW)
    assert turn.model_dump(mode='json')==before
    turn.capture_options=[];turn.capture_options_total=0
    with pytest.raises(ValueError,match='No capture option'):
        check_capture_intent(turn,parsed('intent.json'),now=NOW)


def test_observation_25_then_execution_26_can_commit_in_separate_publications():
    args=gold_arguments(split=True)
    receipt=args[0]
    assert str(receipt.observation_message_ids[0]) not in receipt.publication_message_ids
    assert receipt.execution_source_sequence==26
    assert args[3][1].payload.source_sequence==25
    assert gold_check(args,split=True)==receipt


@pytest.mark.parametrize('failure',['missing','other_version','other_execution','other_generation'])
def test_split_capture_needs_every_content_version_in_exact_final_native_snapshot(failure):
    from app.sonda_chat_actions import CaptureSnapshot
    args=gold_arguments(split=True)
    value=exporter.gold_evidence('b'*64,split=True)['snapshot']
    if failure=='missing':value['observations'].pop()
    if failure=='other_version':
        row=value['observations'][0];row['content_sha256']='f'*64
        row['observation_key']=sha256(encoded([1,row['document_key'],row['content_sha256']]))
    if failure=='other_execution':value['execution_id']=str(UUID(int=99))
    if failure=='other_generation':value['generation_id']=str(UUID(int=99))
    with pytest.raises(ValueError):gold_check(args,split=True,snapshot=CaptureSnapshot.model_validate(value))


def test_split_capture_rejects_observation_body_from_another_execution():
    args=gold_arguments(split=True)
    observations=exporter.gold_evidence('b'*64,split=True)['observations']
    observations[0]['payload']['execution_id']=str(UUID(int=99))
    with pytest.raises(ValueError):gold_check(args,split=True,observation_raw=[encoded(row) for row in observations])


def test_completed_zero_result_has_no_invented_observations_and_receipt_read_is_idempotent():
    args=gold_arguments(zero=True)
    receipt=args[0]
    assert receipt.record_count==0 and not receipt.members and not receipt.observation_message_ids
    assert gold_check(args,zero=True)==receipt
    raw=encoded(receipt.model_dump(mode='json'))
    scope={'capability_sha256':'a'*64,'core_contract_sha256':'b'*64,
        'registry_id':receipt.registry_id,'generation_id':receipt.generation_id}
    assert parse_capture_receipt(raw,execution_id=receipt.execution_id,previous_sha256=sha256(raw),**scope)==receipt
    assert parse_capture_receipt(raw,execution_id=receipt.execution_id,previous_sha256=sha256(raw),**scope)==receipt
