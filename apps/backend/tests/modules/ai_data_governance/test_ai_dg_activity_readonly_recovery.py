import asyncio
import json

import pytest

from app.modules.ai_data_governance import activity_transport, native_activity
from app.modules.ai_data_governance.activity_commit import ActivityPending, activity_receipt
from app.modules.ai_data_governance.inventory import identity
from tests.modules.ai_data_governance.test_ai_dg_native_activity import activity_harness


def recovery_harness(monkeypatch, *, defect='lost_ack', verification='exact'):
    activity,journal,_,_=activity_harness(monkeypatch)
    monkeypatch.setattr(native_activity,'run_native_activity',activity_transport.run_native_activity)
    calls=[]
    persisted=[]

    def command(binding,signer,source,check,*,language,diagnose):
        assert language=='PYTHON' and signer is activity.transport.signer
        mode='VERIFY_ACTIVITY' if '"mode":"VERIFY_ACTIVITY"' in source else 'APPEND_ACTIVITY'
        calls.append(mode)
        key=identity('consultation-activity-commit',activity.intent['operation_id'])
        batch=journal.read('outcomes',key)
        check()
        diagnose('command','started')
        if mode=='APPEND_ACTIVITY':
            if defect!='missing_rows':
                persisted.append(batch)
            if defect!='decode':
                diagnose('command','failed',TimeoutError('private provider response'))
                if defect=='cleanup':
                    diagnose('cleanup','failed',RuntimeError('private cleanup'))
                elif defect!='no_cleanup':
                    diagnose('cleanup','completed')
                if defect=='close':
                    diagnose('session_close','failed',RuntimeError('private close'))
                elif defect!='no_close':
                    diagnose('session_close','completed')
                if defect=='revoked':
                    activity.transport.check_identity.side_effect=PermissionError('revoked')
                if defect=='cancelled':
                    activity.cancelled.set()
                raise TimeoutError('private provider response')
            diagnose('command','completed')
            diagnose('cleanup','completed')
            diagnose('session_close','completed')
            return {'status':'SUCCEEDED','outputs':[]}
        assert mode=='VERIFY_ACTIVITY'
        if verification=='error':
            diagnose('command','failed',RuntimeError('private read error'))
            raise RuntimeError('private read error')
        result=activity_receipt(batch) if persisted else {'verified':False}
        if verification=='wrong_digest':
            result={**result,'input_digest':'0'*64}
        if verification=='truthy':
            result={**result,'verified':1}
        diagnose('command','completed')
        diagnose('cleanup','completed')
        diagnose('session_close','completed')
        if verification=='revoked':
            activity.transport.check_identity.side_effect=PermissionError('revoked after read')
        return {'status':'SUCCEEDED','outputs':[{'type':'TEXT_PLAIN','value':json.dumps(result)}]}

    monkeypatch.setattr(activity_transport,'_run_central_command',command)
    return activity,journal,calls,persisted


async def finish(activity):
    await activity.begin('actor','session','question')
    return await activity.finish({'response':{'result_state':'answered'}})


def test_lost_ack_is_verified_and_materialized_without_another_append(monkeypatch):
    activity,journal,calls,persisted=recovery_harness(monkeypatch)
    receipt=asyncio.run(finish(activity))
    batch=persisted[0]
    key=identity('consultation-activity-commit',batch['operation_id'])
    assert receipt==activity_receipt(batch)==journal.read('materialized',key)
    assert calls==['APPEND_ACTIVITY','VERIFY_ACTIVITY'] and len(persisted)==1
    assert journal.read('responses',key) is None
    assert {'stage':'reconcile','transition':'completed'} in activity.diagnostics
    assert next(row for row in activity.diagnostics if row['transition']=='failed')['stage']=='command'


@pytest.mark.parametrize('defect,verification',[
    ('missing_rows','exact'),('lost_ack','wrong_digest'),('lost_ack','truthy'),('lost_ack','error'),
])
def test_partial_wrong_or_failed_read_remains_pending_without_repair(monkeypatch,defect,verification):
    activity,journal,calls,_=recovery_harness(monkeypatch,defect=defect,verification=verification)
    with pytest.raises(ActivityPending):
        asyncio.run(finish(activity))
    assert calls==['APPEND_ACTIVITY','VERIFY_ACTIVITY']
    assert not any(kind=='materialized' for kind,_ in journal.values)
    assert not any(kind=='responses' and value.get('terminal') for (kind,_),value in journal.values.items())


@pytest.mark.parametrize('defect',['cleanup','close','no_cleanup','no_close','decode'])
def test_other_failures_do_not_admit_automatic_verification(monkeypatch,defect):
    activity,journal,calls,_=recovery_harness(monkeypatch,defect=defect)
    with pytest.raises(ActivityPending):
        asyncio.run(finish(activity))
    assert calls==['APPEND_ACTIVITY']
    assert not any(kind=='materialized' for kind,_ in journal.values)


@pytest.mark.parametrize('defect,verification,error',[
    ('revoked','exact',PermissionError),('cancelled','exact',InterruptedError),
    ('lost_ack','revoked',PermissionError),
])
def test_identity_and_cancellation_are_rechecked_around_readback(monkeypatch,defect,verification,error):
    activity,journal,calls,_=recovery_harness(monkeypatch,defect=defect,verification=verification)
    # The committer wraps failures before returning a materialized receipt.
    with pytest.raises(ActivityPending):
        asyncio.run(finish(activity))
    assert calls==(['APPEND_ACTIVITY','VERIFY_ACTIVITY'] if verification=='revoked' else ['APPEND_ACTIVITY'])
    assert not any(kind=='materialized' for kind,_ in journal.values)
    assert not any(kind=='responses' for kind,_ in journal.values)
    with pytest.raises(error):
        activity.check_active()


def test_old_or_truncated_diagnostics_cannot_authorize_a_new_read(monkeypatch):
    activity,journal,calls,_=recovery_harness(monkeypatch)
    activity.diagnostics=[{'stage':'cleanup','transition':'completed'},
        {'stage':'session_close','transition':'completed'}]*24
    with pytest.raises(ActivityPending):
        asyncio.run(finish(activity))
    assert calls==['APPEND_ACTIVITY'] and len(activity.diagnostics)==48
    assert not any(kind=='materialized' for kind,_ in journal.values)
