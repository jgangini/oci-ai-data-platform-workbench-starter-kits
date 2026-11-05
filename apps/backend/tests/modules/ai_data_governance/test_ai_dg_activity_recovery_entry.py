import asyncio
import importlib.util
from pathlib import Path
from threading import Event
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.modules.ai_data_governance import activity_commit, activity_records, native_activity
from app.modules.ai_data_governance.inventory import identity
from app.modules.ai_data_governance.inventory import canonical
from app.modules.ai_data_governance.repository import _comparable
from tests.support.ai_dg_fixtures import MemoryJournal, activity_fixture


def fixture(monkeypatch, *, proven_terminal=True, proven_partial=True):
    binding, _, _ = activity_fixture()
    binding['storage_root'] = 'oci://artifacts@namespace/oci_artifacts'
    objects = SimpleNamespace(base_client=SimpleNamespace(session=SimpleNamespace(close=Mock())))
    journal = MemoryJournal()
    completed, calls = set(), []
    def execute(config, signer, program, mode, batch, check, *, diagnose=None):
        check(); calls.append((batch['operation_id'], mode))
        if "'kind':'TEST_PARTIAL_ACTIVITY'" in program:
            return ({'kind':'TEST_PARTIAL_ACTIVITY', 'run_count':1, 'event_count':1,
                'expected_event_count':2, 'verified':True} if proven_partial else {'verified':False})
        if mode == 'APPEND_ACTIVITY':
            if 'class PartialEventRepository' in program:
                return activity_commit.activity_failure_receipt(batch) if proven_terminal else {'verified':False}
            completed.add(batch['operation_id'])
        return activity_commit.activity_receipt(batch) if batch['operation_id'] in completed else {'verified':False}
    monkeypatch.setattr(native_activity, 'runtime_object_storage', lambda *a:objects)
    monkeypatch.setattr(native_activity, 'GovernanceJournal', lambda *a, **k:journal)
    monkeypatch.setattr(native_activity, 'run_native_activity', execute)
    runtime = SimpleNamespace(CONFIG=binding, ACTIVITY_PROGRAM='fixed-activity-program',
        NativeActivity=native_activity.NativeActivity, identity=identity,
        validate_activity_batch=activity_records.validate_activity_batch,
        activity_batch=activity_records.activity_batch,
        ActivityCommitter=activity_commit.ActivityCommitter, ActivityPending=activity_commit.ActivityPending,
        activity_receipt=activity_commit.activity_receipt,
        activity_failure_receipt=activity_commit.activity_failure_receipt,
        activity_abort_receipt=activity_commit.activity_abort_receipt)
    monkeypatch.setitem(sys.modules, 'governance_runtime', runtime)
    path = Path(__file__).parents[2] / 'fixtures' / 'ai_dg_activity_recovery_entry.py'
    spec = importlib.util.spec_from_file_location('activity_recovery_fixture', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    transport = SimpleNamespace(access_mode='CENTRAL_METADATA', signer=object(), check_identity=Mock(),
        original=('principal', 'native-subject', 'native-session'))
    return module, binding, transport, journal, completed, calls


def test_fault_harness_repairs_only_terminal_partial_or_proven_preexecutor_abort(monkeypatch):
    module, binding, transport, journal, complete, calls = fixture(monkeypatch)
    report = asyncio.run(module.run_cases(binding, transport, module.TEST_COMMAND, Mock()))
    assert report['fixture_kind'] == 'CONTROLLED_TEST_FAILURES'
    assert report['model_calls'] == report['catalog_writes'] == report['business_rows_written'] == 0
    assert len(complete) == 3 and len(report['cases']) == 3
    for case, expected_appends in zip(report['cases'], (2, 1, 1)):
        receipt = case['receipt']; operation = receipt['operation_id']
        assert receipt['verified'] and receipt['run_count'] == 1 and receipt['event_count'] == 2
        assert sum(op == operation and mode == 'APPEND_ACTIVITY' for op, mode in calls) == expected_appends
    cancelled = report['cases'][2]['receipt']['operation_id']
    batches = [value for (kind, _), value in journal.values.items()
        if kind == 'outcomes' and value.get('operation_id') == cancelled]
    assert len(batches) == 1 and batches[0]['run']['status'] == 'CANCELLED'
    assert all(value['run']['manifest_json'].find('native-subject') < 0 for value in batches)
    assert len([v for (kind, _), v in journal.values.items()
        if kind == 'materialized' and v.get('phase') == 'activity_recovery_acceptance']) == 1


def test_one_use_native_acceptance_cannot_repeat_a_started_test(monkeypatch):
    module, binding, transport, _, _, calls = fixture(monkeypatch)
    asyncio.run(module.run_cases(binding, transport, module.TEST_COMMAND, Mock()))
    before = len(calls)
    with pytest.raises(activity_commit.ActivityPending):
        asyncio.run(module.run_cases(binding, transport, module.TEST_COMMAND, Mock()))
    assert len(calls) == before


def test_one_use_claim_also_prevents_repeating_from_another_conversation(monkeypatch):
    module, binding, transport, _, _, calls = fixture(monkeypatch)
    asyncio.run(module.run_cases(binding, transport, module.TEST_COMMAND, Mock()))
    transport.original = ('another-principal', 'another-subject', 'another-session')
    before = len(calls)
    with pytest.raises(activity_commit.ActivityPending):
        asyncio.run(module.run_cases(binding, transport, module.TEST_COMMAND, Mock()))
    assert len(calls) == before


@pytest.mark.parametrize('change', ['production', 'ordinary_question'])
def test_harness_cannot_be_used_as_a_production_or_ordinary_agent(monkeypatch, change):
    module, binding, transport, journal, _, calls = fixture(monkeypatch)
    question = module.TEST_COMMAND
    if change == 'production': binding['deployment_stage'] = 'PROD'
    else: question = 'List the tables'
    with pytest.raises(PermissionError):
        asyncio.run(module.run_cases(binding, transport, question, Mock()))
    assert journal.values == {} and calls == []


def test_uncertain_partial_acknowledgement_never_authorizes_repair(monkeypatch):
    module, binding, transport, _, complete, calls = fixture(monkeypatch, proven_terminal=False)
    with pytest.raises(AssertionError, match='terminal failure proof'):
        asyncio.run(module.run_cases(binding, transport, module.TEST_COMMAND, Mock()))
    assert len(calls) == 1 and calls[0][1] == 'APPEND_ACTIVITY' and complete == set()


def test_terminal_failure_before_any_row_is_not_a_partial_write_acceptance(monkeypatch):
    module, binding, transport, _, complete, calls = fixture(monkeypatch, proven_partial=False)
    with pytest.raises(AssertionError, match='exact native partial state'):
        asyncio.run(module.run_cases(binding, transport, module.TEST_COMMAND, Mock()))
    assert [mode for _, mode in calls] == ['APPEND_ACTIVITY', 'VERIFY_ACTIVITY'] and not complete


def test_outer_cancellation_drains_the_child_before_closing_the_activity_client(monkeypatch):
    module, binding, transport, _, _, calls = fixture(monkeypatch)
    waiting, release, finished = Event(), Event(), Event()
    gate = module.JournalGate
    class DelayedGate(gate):
        def create(self, kind, key, value):
            if kind != 'outcomes': return super().create(kind, key, value)
            waiting.set()
            try:
                assert release.wait(2)
                return super().create(kind, key, value)
            finally:
                finished.set()
    monkeypatch.setattr(module, 'JournalGate', DelayedGate)
    activity = native_activity.NativeActivity(binding, transport, 'fixed', Mock())
    close = Mock(side_effect=lambda: finished.is_set() or pytest.fail('Client closed with a live freeze worker'))
    monkeypatch.setattr(activity, 'close', close)
    async def run():
        await activity.begin('native-subject', 'native-session', module.TEST_COMMAND)
        async def outer():
            try: await module.cancellation_case(activity, binding, transport, Mock())
            finally: activity.close()
        task = asyncio.create_task(outer())
        while not waiting.is_set(): await asyncio.sleep(0)
        task.cancel()
        while not activity.cancelled.is_set(): await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(run())
    close.assert_called_once()
    assert finished.is_set() and calls == []


@pytest.mark.parametrize('state', ['partial', 'no_run', 'no_event', 'complete', 'duplicate_run', 'foreign_event'])
def test_partial_spark_probe_requires_the_exact_committed_subset(monkeypatch, state):
    module, binding, _, _, _, _ = fixture(monkeypatch)
    run, first, second = {'run_id':'r', 'status':'FAILED'}, {'event_id':'one'}, {'event_id':'two'}
    rows = {'AI_DG_RUN':[run], 'AI_DG_EVENT':[first, second]}
    actual_run, actual_events = [run], [first]
    if state == 'no_run': actual_run = []
    elif state == 'no_event': actual_events = []
    elif state == 'complete': actual_events = [first, second]
    elif state == 'duplicate_run': actual_run = [run, run]
    elif state == 'foreign_event': actual_events = [second]
    class Repository:
        def read_by_keys(self, name, keys):
            assert keys == ([{'run_id':'r'}] if name == 'AI_DG_RUN' else [{'event_id':'one'}, {'event_id':'two'}])
            return actual_run if name == 'AI_DG_RUN' else actual_events
    namespace = {'DeltaRepository':lambda *a, **k:Repository(), 'canonical':canonical, '_comparable':_comparable,
        'check_activity_scope':Mock(), 'prepare_activity_rows':lambda *a, **k:rows}
    exec(compile(module.PARTIAL_READBACK_PROGRAM, 'partial_readback.py', 'exec'), namespace)
    spark = SimpleNamespace(conf=SimpleNamespace(set=Mock()))
    if state == 'partial':
        assert namespace['run_activity_program'](spark, binding, 'VERIFY_ACTIVITY', {'run':run})['verified'] is True
    else:
        with pytest.raises(RuntimeError, match='exact partial state'):
            namespace['run_activity_program'](spark, binding, 'VERIFY_ACTIVITY', {'run':run})
