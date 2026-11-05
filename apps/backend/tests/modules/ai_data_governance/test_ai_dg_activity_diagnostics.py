import asyncio
import json
from unittest.mock import Mock

import pytest

from app.modules.ai_data_governance import activity_transport, native_activity
from app.modules.ai_data_governance.activity_commit import ActivityCommitter, ActivityPending, activity_receipt
from app.modules.ai_data_governance.central_sql import _observe_central_command
from app.modules.ai_data_governance.inventory import identity
from tests.support.ai_dg_fixtures import MemoryJournal, FixedRunner, activity_fixture
from tests.modules.ai_data_governance.test_ai_dg_native_activity import activity_harness


def diagnostic(journal, activity):
    key = identity('consultation-activity-diagnostic-v1', activity.intent['operation_id'])
    rows = [value for (kind, found), value in journal.values.items() if kind == 'responses' and found == key]
    assert len(rows) == 1
    return rows[0]


async def start(activity):
    await activity.begin('private-subject', 'private-session', 'private-question')


def test_diagnostic_is_bounded_private_and_cannot_authorize_repair(monkeypatch):
    activity, journal, runner, _ = activity_harness(monkeypatch)
    runner.side_effect = RuntimeError('private-provider-payload')
    async def run():
        await start(activity)
        with pytest.raises(ActivityPending):
            await activity.finish({'response': {'result_state': 'answered'}})
    asyncio.run(run())
    value = diagnostic(journal, activity)
    assert set(value) == {'schema_version','kind','operation_id','component_revision','input_digest',
        'observations','first_failure','result_observed','cleanup_completed','cleanup_failed'}
    assert value['first_failure'] == {'stage':'append','transition':'failed','error_type':'RuntimeError'}
    assert value['result_observed'] is value['cleanup_completed'] is value['cleanup_failed'] is False
    assert 'private' not in json.dumps(value)
    key = identity('consultation-activity-commit', activity.intent['operation_id'])
    assert journal.read('responses', key) is None and journal.read('materialized', key) is None
    batch = journal.read('outcomes', key)
    assert value['input_digest'] == identity('activity-batch',batch)
    with pytest.raises(ActivityPending, match='No verified terminal failure'):
        ActivityCommitter(journal,runner,activity.check_active).repair(batch)
    runner.assert_called_once()


@pytest.mark.parametrize('failure_kind', ['freeze','claim','materialized'])
def test_journal_boundary_and_original_exception_are_preserved(monkeypatch, failure_kind):
    activity,journal,runner,_ = activity_harness(monkeypatch)
    original = journal.create
    failure = RuntimeError('private journal failure')
    def create(kind,key,value):
        if failure_kind == 'freeze' and kind == 'outcomes':
            raise failure
        if failure_kind == 'claim' and kind == 'claims':
            original(kind,key,value)
            raise failure
        if failure_kind == 'materialized' and kind == 'materialized':
            raise failure
        return original(kind,key,value)
    monkeypatch.setattr(journal,'create',create)
    async def run():
        await start(activity)
        with pytest.raises(RuntimeError) as caught:
            await activity.finish({'response':{'result_state':'answered'}})
        assert caught.value is failure
    asyncio.run(run())
    expected = {'freeze':'freeze','claim':'claim_create','materialized':'materialized_create'}[failure_kind]
    assert diagnostic(journal,activity)['first_failure']['stage'] == expected
    assert runner.call_count == (1 if failure_kind == 'materialized' else 0)
    assert not any(kind == 'materialized' for kind,_ in journal.values)


@pytest.mark.parametrize('invalid', ['envelope','json','exact_receipt'])
def test_receipt_decode_is_distinct_from_exact_batch_comparison(monkeypatch,invalid):
    activity,journal,_,_ = activity_harness(monkeypatch)
    monkeypatch.setattr(native_activity,'run_native_activity',activity_transport.run_native_activity)
    def command(binding,signer,program,check,*,language,diagnose):
        diagnose('command','completed')
        diagnose('cleanup','completed')
        key = identity('consultation-activity-commit',activity.intent['operation_id'])
        receipt = {**activity_receipt(journal.read('outcomes',key)), 'input_digest':'f'*64}
        if invalid == 'envelope':
            return {'status':'FAILED'}
        return {'status':'SUCCEEDED','outputs':[{'type':'TEXT_PLAIN',
            'value':'not-json-private' if invalid == 'json' else json.dumps(receipt)}]}
    monkeypatch.setattr(activity_transport,'_run_central_command',command)
    async def run():
        await start(activity)
        with pytest.raises(ActivityPending):
            await activity.finish({'response':{'result_state':'answered'}})
    asyncio.run(run())
    value = diagnostic(journal,activity)
    assert value['first_failure']['stage'] == ('receipt_match' if invalid == 'exact_receipt' else 'receipt_decode')
    assert value['result_observed'] is value['cleanup_completed'] is True
    assert 'private' not in json.dumps(value)


def test_identity_revoked_after_command_prevents_receipt_and_diagnostic_writes(monkeypatch):
    activity,journal,runner,_ = activity_harness(monkeypatch)
    def execute(mode,batch):
        activity.transport.check_identity.side_effect = PermissionError('revoked')
        return activity_receipt(batch)
    runner.side_effect = execute
    async def run():
        await start(activity)
        with pytest.raises(PermissionError):
            await activity.finish({'response':{'result_state':'answered'}})
    asyncio.run(run())
    assert activity.diagnostics[-2]['stage'] == 'materialized_identity'
    assert not any(kind in {'responses','materialized'} for kind,_ in journal.values)
    runner.assert_called_once()


def test_diagnostic_write_failure_does_not_replace_original_failure(monkeypatch):
    activity,journal,runner,_ = activity_harness(monkeypatch)
    original = journal.create
    failure = RuntimeError('private-original')
    def create(kind,key,value):
        if kind == 'materialized':
            raise failure
        if kind == 'responses':
            raise ValueError('private-diagnostic-failure')
        return original(kind,key,value)
    monkeypatch.setattr(journal,'create',create)
    async def run():
        await start(activity)
        with pytest.raises(RuntimeError) as caught:
            await activity.finish({'response':{'result_state':'answered'}})
        assert caught.value is failure
    asyncio.run(run())
    assert not any(kind in {'responses','materialized'} for kind,_ in journal.values)
    runner.assert_called_once()


def test_cancelled_diagnostic_does_not_write_and_instances_do_not_share_state(monkeypatch):
    one,journal,_,_ = activity_harness(monkeypatch)
    two,_,_,_ = activity_harness(monkeypatch)
    one.diagnose('command','failed',RuntimeError('private'))
    assert two.diagnostics == []
    one.cancelled.set()
    with pytest.raises(InterruptedError):
        one._persist_diagnostic(activity_fixture()[2])
    assert journal.values == {}


def test_diagnostic_limit_and_error_allowlist_exclude_untrusted_values(monkeypatch):
    activity,_,_,_ = activity_harness(monkeypatch)
    class PrivateProviderError(Exception):
        pass
    activity.diagnose('private-stage','failed',RuntimeError('private'))
    activity.diagnose('command','private-transition',RuntimeError('private'))
    assert activity.diagnostics == []
    for _ in range(60):
        activity.diagnose('command','failed',PrivateProviderError('private-provider-response'))
    assert len(activity.diagnostics) == 48
    assert {row['error_type'] for row in activity.diagnostics} == {'other'}
    assert 'private' not in json.dumps(activity.diagnostics).lower()


def test_observer_failure_never_replaces_success_or_changes_append_count():
    def broken(*args):
        raise RuntimeError('private-observer')
    journal,runner = MemoryJournal(),FixedRunner()
    batch = activity_fixture()[2]
    assert ActivityCommitter(journal,runner,lambda:None,diagnose=broken).commit(batch) == activity_receipt(batch)
    _observe_central_command(broken,'command','completed')
    assert runner.append_calls == 1 and runner.verify_calls == 0
