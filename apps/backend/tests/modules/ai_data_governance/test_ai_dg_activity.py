from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.modules.ai_data_governance.activity_records import activity_agent, activity_intent, activity_typed_rows, validate_activity_batch
from app.modules.ai_data_governance.activity_commit import ActivityCommitter, ActivityPending, activity_receipt, activity_failure_receipt
from app.modules.ai_data_governance.journal import JournalConflict
from tests.support.ai_dg_fixtures import activity_fixture, MemoryJournal, FixedRunner


def test_activity_matches_the_approved_model_and_keeps_only_conversation_digests():
    config,intent,batch=activity_fixture()
    assert validate_activity_batch(batch)==batch
    typed=activity_typed_rows(batch)
    assert len(typed['AI_DG_RUN'])==1 and len(typed['AI_DG_EVENT'])==2
    assert typed['AI_DG_RUN'][0]['started_at'].utcoffset()==timedelta(0)
    assert typed['AI_DG_RUN'][0]['agent_id']==activity_agent(config)['agent_id']
    encoded=json.dumps(batch)
    assert all(value not in encoded for value in ('subject-a','session-a','Private question text'))
    assert all(row['asset_id'] is None for row in batch['events'])
    assert json.loads(batch['run']['manifest_json'])['result_state']=='answered'


def test_activity_has_a_distinct_identity_for_each_request_user_session_and_agent():
    config,_,_=activity_fixture(); at=datetime(2026,11,1,tzinfo=timezone.utc)
    kw={'subject':'a','session':'b','request_id':'44444444-4444-4444-8444-444444444444','question':'q','started_at':at}
    base=activity_intent(config,**kw)
    assert base==activity_intent(config,**kw)
    assert len({base['operation_id'],activity_intent(config,**{**kw,'subject':'other'})['operation_id'],
        activity_intent(config,**{**kw,'session':'other'})['operation_id'],
        activity_intent(config,**{**kw,'request_id':'55555555-5555-4555-8555-555555555555'})['operation_id']})==4


@pytest.mark.parametrize('change', ['scope','run_id','producer','prompt_text','attempt','event_id','event_count','extra_table','time'])
def test_malformed_or_model_authored_batches_fail_before_journal_or_write(change):
    _,_,batch=activity_fixture()
    if change=='scope': batch['scope_id']='foreign'
    if change=='run_id': batch['run']['run_id']='invented'
    if change=='producer': batch['agent']['native_agent_key']='other'
    if change=='prompt_text': batch['run']['manifest_json']=json.dumps({'prompt':'private text'})
    if change=='attempt': batch['run']['attempt_no']=True
    if change=='event_id': batch['events'][0]['event_id']='invented'
    if change=='event_count': batch['events'][0]['metrics_json']=json.dumps({'phase':'admission','asset_count':True,'evidence_count':0})
    if change=='extra_table': batch['tables']={'BUSINESS_TABLE':[{}]}
    if change=='time': batch['run']['finished_at']='2025-01-01T00:00:00+00:00'
    journal,runner=MemoryJournal(),FixedRunner()
    with pytest.raises((ValueError,KeyError)):
        ActivityCommitter(journal,runner,lambda:None).commit(batch)
    assert journal.values=={} and runner.append_calls==0


def test_once_confirmed_replay_only_verifies_the_same_rows():
    _,_,batch=activity_fixture(); journal,runner=MemoryJournal(),FixedRunner()
    commit=ActivityCommitter(journal,runner,lambda:None)
    assert commit.commit(batch)==activity_receipt(batch)
    assert commit.commit(batch)==activity_receipt(batch)
    assert runner.append_calls==1 and runner.verify_calls==1


def test_lost_write_acknowledgement_recovers_by_readback_without_second_append():
    _,_,batch=activity_fixture(); journal,runner=MemoryJournal(),FixedRunner(); runner.lose_ack=True
    commit=ActivityCommitter(journal,runner,lambda:None)
    with pytest.raises(ActivityPending): commit.commit(batch)
    assert commit.commit(batch)==activity_receipt(batch)
    assert runner.append_calls==1 and runner.verify_calls==1


def test_partial_or_uncertain_write_remains_pending_and_never_restarts_itself():
    _,_,batch=activity_fixture(); journal,runner=MemoryJournal(),FixedRunner()
    runner.lose_ack=True; runner.partial=True
    commit=ActivityCommitter(journal,runner,lambda:None)
    with pytest.raises(ActivityPending): commit.commit(batch)
    with pytest.raises(ActivityPending): commit.commit(batch)
    assert runner.append_calls==1 and runner.verify_calls==1
    assert not any(kind=='materialized' for kind,key in journal.values)


def test_changed_timestamps_cannot_redefine_the_frozen_batch_on_retry():
    _,_,batch=activity_fixture(); journal,runner=MemoryJournal(),FixedRunner()
    commit=ActivityCommitter(journal,runner,lambda:None); commit.commit(batch)
    changed=deepcopy(batch)
    changed['run']['finished_at']='2026-11-01T00:00:03.000000+00:00'
    for event in changed['events']: event['ingested_at']=changed['run']['finished_at']
    validate_activity_batch(changed)
    with pytest.raises(JournalConflict): commit.commit(changed)
    assert runner.append_calls==1


def test_confirmed_receipt_does_not_hide_changed_or_missing_delta_rows():
    _,_,batch=activity_fixture(); journal,runner=MemoryJournal(),FixedRunner()
    commit=ActivityCommitter(journal,runner,lambda:None); commit.commit(batch)
    runner.persisted=None
    with pytest.raises(ActivityPending): commit.commit(batch)
    assert runner.append_calls==1


def test_truthy_numeric_values_are_not_boolean_confirmation_or_a_valid_agent_flag():
    _,_,batch=activity_fixture(); journal=MemoryJournal()
    def bad_receipt(mode,value): return {**activity_receipt(value),'verified':1}
    with pytest.raises(ActivityPending): ActivityCommitter(journal,bad_receipt,lambda:None).commit(batch)
    assert not any(kind=='materialized' for kind,key in journal.values)
    batch['agent']['enabled']=1
    with pytest.raises(ValueError): validate_activity_batch(batch)


def test_partial_write_can_be_repaired_once_only_after_fixed_program_terminal_failure():
    _,_,batch=activity_fixture(); journal=MemoryJournal(); calls=[]
    persisted=False
    def execute(mode,value):
        nonlocal persisted
        calls.append(mode)
        if mode=='APPEND_ACTIVITY' and calls.count(mode)==1:
            return activity_failure_receipt(value)
        if mode=='APPEND_ACTIVITY': persisted=True
        return activity_receipt(value) if persisted else {'verified':False}
    commit=ActivityCommitter(journal,execute,lambda:None)
    with pytest.raises(ActivityPending): commit.commit(batch)
    assert commit.repair(batch)==activity_receipt(batch)
    assert calls==['APPEND_ACTIVITY','VERIFY_ACTIVITY','APPEND_ACTIVITY']
    assert commit.commit(batch)==activity_receipt(batch)
    assert calls.count('APPEND_ACTIVITY')==2


def test_uncertain_acknowledgement_does_not_authorize_repair_even_if_rows_are_partial():
    _,_,batch=activity_fixture(); journal,runner=MemoryJournal(),FixedRunner()
    runner.partial=True; runner.lose_ack=True
    commit=ActivityCommitter(journal,runner,lambda:None)
    with pytest.raises(ActivityPending): commit.commit(batch)
    with pytest.raises(ActivityPending): commit.repair(batch)
    assert runner.append_calls==1


def test_acknowledged_claim_aborted_before_execute_has_a_durable_repair_path():
    _,_,batch=activity_fixture();journal,runner=MemoryJournal(),FixedRunner()
    admitted=False;revoked=False
    create=journal.create
    def record(kind,key,value):
        nonlocal admitted
        result=create(kind,key,value)
        if kind=='claims': admitted=True
        return result
    journal.create=record
    def check():
        if admitted and not revoked: raise InterruptedError('cancelled before execute')
    commit=ActivityCommitter(journal,runner,check)
    with pytest.raises(InterruptedError): commit.commit(batch)
    assert runner.append_calls==0
    assert any(kind=='responses' and value['status']=='NOT_STARTED' for (kind,key),value in journal.values.items())
    revoked=True
    assert commit.repair(batch)==activity_receipt(batch)
    assert runner.verify_calls==1 and runner.append_calls==1
