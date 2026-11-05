import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.ai_data_governance import native_activity
from app.modules.ai_data_governance.activity_commit import ActivityPending, activity_receipt
from app.modules.ai_data_governance.activity_records import validate_activity_batch
from app.modules.ai_data_governance.inventory import identity
from tests.modules.ai_data_governance.test_ai_dg_native_activity import activity_harness


def writer_harness(monkeypatch, failure=None, cleanup_gate=None):
    activity, journal, _, objects = activity_harness(monkeypatch)
    calls = []

    class Adapter:
        def __init__(self):
            self.contexts = {'owned-context'}

        def execute_command(self):
            calls.append('submitted')
            if failure == 'lost_ack':
                raise TimeoutError('An acknowledgement is uncertain')
            return object()

        def cleanup(self):
            calls.append('cleanup')
            if cleanup_gate is not None:
                cleanup_gate[0].set()
                assert cleanup_gate[1].wait(2)
            if failure == 'cleanup':
                raise RuntimeError('Context cleanup failed')
            self.contexts.clear()

    runtime = SimpleNamespace(CentralSqlSdk=Adapter, identity=identity,
        validate_activity_batch=validate_activity_batch)
    monkeypatch.setitem(sys.modules, 'governance_runtime', runtime)
    path = Path(__file__).parents[2] / 'fixtures' / 'ai_dg_activity_writer_cancel_entry.py'
    spec = importlib.util.spec_from_file_location('writer_cancel_fixture', path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)

    def execute(binding, signer, program, mode, batch, check, *, diagnose=None):
        assert mode == 'APPEND_ACTIVITY'
        if failure == 'normal_completion':
            calls.append('completed_without_ack')
            return activity_receipt(batch)
        client = runtime.CentralSqlSdk()
        try:
            client.execute_command()
            check()
            raise AssertionError('Cancelled command continued')
        finally:
            client.cleanup()

    monkeypatch.setattr(native_activity, 'run_native_activity', execute)
    return activity, journal, calls, runtime, fixture, Adapter, objects


@pytest.mark.parametrize('failure', [None, 'lost_ack', 'cleanup'])
def test_writer_cancellation_requires_acknowledgement_cleanup_and_preserves_uncertainty(monkeypatch, failure):
    activity, journal, calls, runtime, fixture, original, _ = writer_harness(monkeypatch, failure)
    async def run():
        await activity.begin('subject', 'session', 'operator test')
        if failure is None:
            key, batch, observed = await fixture.cancel_acknowledged_writer(activity)
            assert observed == {'acknowledged_commands':1, 'cleanup_confirmed':True}
            assert journal.read('claims', key) is not None
            assert journal.read('responses', key) is None
            assert journal.read('materialized', key) is None
            assert batch['operation_id'] == activity.intent['operation_id']
        else:
            with pytest.raises(ActivityPending if failure == 'lost_ack' else AssertionError):
                await fixture.cancel_acknowledged_writer(activity)
        assert runtime.CentralSqlSdk is original
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]

    asyncio.run(run())
    assert calls == ['submitted', 'cleanup']
    assert sum(kind == 'outcomes' for kind, _ in journal.values) == 1
    assert not any(kind == 'materialized' for kind, _ in journal.values)
    for (kind,key),value in journal.values.items():
        if kind == 'responses':
            assert value['kind'] == 'ACTIVITY_PENDING_DIAGNOSTIC'
            assert key == identity('consultation-activity-diagnostic-v1',activity.intent['operation_id'])
            assert 'terminal' not in value and 'verified' not in value


@pytest.mark.parametrize('failure', ['cleanup', 'normal_completion'])
def test_unaccepted_cancellation_explains_the_failed_boundary_without_private_values(monkeypatch, failure):
    import json
    activity, _, _, _, fixture, _, _ = writer_harness(monkeypatch, failure)

    async def run():
        await activity.begin('private-subject', 'private-session', 'private-question')
        with pytest.raises(AssertionError) as error:
            await fixture.cancel_acknowledged_writer(activity)
        observed = json.loads(str(error.value).partition(': ')[2])
        expected = {'acknowledged_commands', 'cleanup_confirmed', 'barrier_acknowledged',
            'worker_done', 'worker_cancelled', 'activity_cancelled', 'sdk_restored'}
        if failure == 'cleanup':
            expected.add('cleanup_failure')
            assert observed['cleanup_failure'] == {'error_type':'RuntimeError',
                'http_status':None, 'remaining_contexts':1}
        assert set(observed) == expected
        assert observed['acknowledged_commands'] == (1 if failure == 'cleanup' else 0)
        assert observed['worker_done'] is observed['sdk_restored'] is True
        assert observed['cleanup_confirmed'] is False
        assert observed['barrier_acknowledged'] is (failure == 'cleanup')
        assert observed['worker_cancelled'] is observed['activity_cancelled'] is (failure == 'cleanup')
        assert all('private' not in str(value) for value in observed.values())
    asyncio.run(run())


@pytest.mark.parametrize('status', [409, None, 'private-status', True, 600])
def test_cleanup_diagnostic_reports_only_a_closed_error_type_status_and_count(monkeypatch, status):
    _, _, _, _, fixture, _, _ = writer_harness(monkeypatch)
    error = RuntimeError('private payload')
    error.status = status
    observed = fixture.cleanup_error(error, 1)
    assert observed == {'error_type':'RuntimeError', 'http_status':409 if status == 409 else None,
        'remaining_contexts':1}
    assert fixture.valid_cleanup_error(observed)
    assert 'private' not in json.dumps(observed)
    for field, invalid in [('error_type','private-value'), ('remaining_contexts',True),
            ('remaining_contexts',33), ('http_status','private-value')]:
        assert not fixture.valid_cleanup_error({**observed, field:invalid})
    assert not fixture.valid_cleanup_error({**observed, 'private_field':'private-value'})


@pytest.mark.parametrize('cancel_count', [1, 2])
def test_parent_cancellation_drains_the_writer_and_restores_adapter_before_close(monkeypatch, cancel_count):
    from threading import Event
    entered, release = Event(), Event()
    activity, _, calls, runtime, fixture, original, objects = writer_harness(monkeypatch,
        cleanup_gate=(entered, release))
    restored_when_closed = []
    objects.base_client.session.close.side_effect = lambda: restored_when_closed.append(
        runtime.CentralSqlSdk is original and release.is_set())

    async def run():
        await activity.begin('subject', 'session', 'operator test')
        async def invocation():
            try:
                return await fixture.cancel_acknowledged_writer(activity)
            finally:
                activity.close()
        parent = asyncio.create_task(invocation())
        while not entered.is_set():
            await asyncio.sleep(0)
        try:
            assert activity.cancelled.is_set()
            for _ in range(cancel_count):
                parent.cancel()
                for _ in range(3):
                    await asyncio.sleep(0)
            assert not restored_when_closed
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await parent
        assert restored_when_closed == [True]
        assert runtime.CentralSqlSdk is original
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]
    asyncio.run(run())
    assert calls == ['submitted', 'cleanup']


@pytest.mark.parametrize('change', ['valid', 'private_field', 'wrong_type', 'large_count', 'malformed'])
def test_rejected_test_report_remains_explicit_and_never_exports_arbitrary_error_values(monkeypatch, change):
    _, _, _, _, fixture, _, _ = writer_harness(monkeypatch)
    boundary = dict(acknowledged_commands=1, cleanup_confirmed=False, barrier_acknowledged=True,
        worker_done=True, worker_cancelled=True, activity_cancelled=True, sdk_restored=True)
    if change == 'private_field':
        boundary['private_token'] = 'private-value'
    elif change == 'wrong_type':
        boundary['worker_done'] = 'private-value'
    elif change == 'large_count':
        boundary['acknowledged_commands'] = 33
    payload = json.dumps(boundary) if change != 'malformed' else 'private-value'
    report = fixture.rejection_report(AssertionError('Writer cancellation boundary was not accepted: ' + payload))
    assert report['accepted'] is False and report['phase'] == 'activity_writer_cancellation_rejected'
    assert report['error_type'] == 'AssertionError' and report['repair_attempted'] is False
    assert ('boundary' in report) is (change == 'valid')
    assert 'private' not in json.dumps(report)


def owned_probe_harness(monkeypatch):
    activity, journal, calls, runtime, fixture, _, objects = writer_harness(monkeypatch)
    runtime.NativeActivity = lambda *args: native_activity.NativeActivity(*args)
    runtime.ACTIVITY_PROGRAM = 'fixed-program'
    runtime.ActivityPending = ActivityPending
    runtime._caller_identity = lambda get_context: ('principal', 'subject', 'session')
    return activity, journal, calls, runtime, fixture, objects


def test_native_probe_reports_rejection_durably_without_repeating_or_overwriting_the_owner(monkeypatch):
    activity, journal, calls, runtime, fixture, objects = owned_probe_harness(monkeypatch)
    boundary = dict(acknowledged_commands=0, cleanup_confirmed=False, barrier_acknowledged=False,
        worker_done=True, worker_cancelled=False, activity_cancelled=False, sdk_restored=True)
    operation = AsyncMock(side_effect=AssertionError('Writer cancellation boundary was not accepted: ' + json.dumps(boundary)))
    monkeypatch.setattr(fixture, 'verify_writer', operation)

    async def run():
        first = await fixture.run_owned_probe(activity.binding, activity.transport, Mock(), Mock())
        assert first['accepted'] is False and first['boundary'] == boundary
        before = dict(journal.values)
        second = await fixture.run_owned_probe(activity.binding, activity.transport, Mock(), Mock())
        assert second['accepted'] is False and second['error_type'] == 'ActivityPending'
        assert journal.values == before
        outcomes = [v for (kind, _), v in journal.values.items() if kind == 'outcomes']
        assert outcomes == [first]
        operation.assert_awaited_once()
    asyncio.run(run())
    assert calls == [] and objects.base_client.session.close.call_count == 2
    assert not any(kind == 'materialized' for kind, _ in journal.values)


def test_native_probe_cancellation_propagates_without_manufacturing_a_rejection_or_success(monkeypatch):
    activity, journal, calls, _, fixture, objects = owned_probe_harness(monkeypatch)
    monkeypatch.setattr(fixture, 'verify_writer', AsyncMock(side_effect=asyncio.CancelledError()))

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await fixture.run_owned_probe(activity.binding, activity.transport, Mock(), Mock())
    asyncio.run(run())
    assert not any(kind in {'outcomes', 'materialized'} for kind, _ in journal.values)
    assert calls == []
    objects.base_client.session.close.assert_called_once()


def test_native_probe_unexpected_error_exports_only_its_type(monkeypatch):
    activity, journal, calls, _, fixture, _ = owned_probe_harness(monkeypatch)
    monkeypatch.setattr(fixture, 'verify_writer', AsyncMock(side_effect=RuntimeError('private credential payload')))
    report = asyncio.run(fixture.run_owned_probe(activity.binding, activity.transport, Mock(), Mock()))
    assert report['accepted'] is False and report['error_type'] == 'RuntimeError'
    assert 'private' not in json.dumps(report)
    assert not any(kind == 'materialized' for kind, _ in journal.values) and calls == []
