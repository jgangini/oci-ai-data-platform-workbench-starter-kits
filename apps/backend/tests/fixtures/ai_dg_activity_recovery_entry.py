"""Temporary original-agent DEV entry for controlled monitoring acceptance only.

The operator pins this file and the existing runtime before installation, then
restores the original entry. It is never included in a production installer.
"""
import asyncio
from datetime import datetime, timezone
import json
from threading import Event

import governance_runtime as runtime

HARNESS_REVISION = 'OPERATOR_PINS_REVISION'
TEST_COMMAND = 'OPERATOR_PINS_ONE_USE_COMMAND'

PARTIAL_PROGRAM = '''
class PartialEventRepository(DeltaRepository):
    def insert_immutable(self, name, rows):
        if name == 'AI_DG_EVENT':
            super().insert_immutable(name, rows[:1])
            raise RuntimeError('Intentional TEST failure after first EVENT commit')
        return super().insert_immutable(name, rows)
DeltaRepository = PartialEventRepository
'''

PARTIAL_READBACK_PROGRAM = '''
def run_activity_program(spark, binding, mode, payload, expected_version=None):
    if mode != 'VERIFY_ACTIVITY' or expected_version is not None:
        raise ValueError('This TEST probe is read-only')
    spark.conf.set('spark.sql.session.timeZone', 'UTC')
    repository = DeltaRepository(spark, binding['storage_root'], max_batch_rows=32)
    check_activity_scope(repository, binding)
    rows = prepare_activity_rows(repository, binding, payload, historical=True)
    if len(rows['AI_DG_EVENT']) != 2:
        raise ValueError('The partial TEST requires exactly two expected events')
    actual_run = repository.read_by_keys('AI_DG_RUN', [{'run_id':payload['run']['run_id']}])
    actual_events = repository.read_by_keys('AI_DG_EVENT',
        [{'event_id':row['event_id']} for row in rows['AI_DG_EVENT']])
    expected_events = rows['AI_DG_EVENT'][:1]
    ordered = lambda values, key: sorted((_comparable(row) for row in values), key=lambda row:row[key])
    if (canonical(ordered(actual_run, 'run_id')) != canonical(ordered(rows['AI_DG_RUN'], 'run_id'))
            or canonical(ordered(actual_events, 'event_id')) != canonical(ordered(expected_events, 'event_id'))):
        raise RuntimeError('RUN and the first EVENT are not the exact partial state')
    return {'kind':'TEST_PARTIAL_ACTIVITY', 'run_count':1, 'event_count':1,
        'expected_event_count':2, 'verified':True}
'''


class BoundTransport:
    access_mode = 'CENTRAL_METADATA'

    def __init__(self, binding, get_context, secret_get):
        self.get_context = get_context
        self.original = runtime._caller_identity(get_context)
        self.signer = runtime.runtime_signer(binding, secret_get)

    def check_identity(self):
        if runtime._caller_identity(self.get_context) != self.original:
            raise PermissionError('Native TEST caller or session changed')


class JournalGate:
    def __init__(self, journal, *, freeze=False, abort=False):
        self.journal, self.freeze, self.abort = journal, freeze, abort
        self.entered, self.release = Event(), Event()
        self.abort_pending = False

    def read(self, kind, key):
        return self.journal.read(kind, key)

    def create(self, kind, key, value):
        if self.freeze and kind == 'outcomes':
            self.entered.set()
            if not self.release.wait(10):
                raise TimeoutError('TEST freeze gate was not released')
        admitted = self.journal.create(kind, key, value)
        if self.abort and kind == 'claims' and admitted:
            self.abort_pending = True
        return admitted

    def check(self):
        if self.abort_pending:
            self.abort_pending = False
            raise InterruptedError('Intentional TEST abort before the executor')


def frozen_batch(activity):
    key = runtime.identity('consultation-activity-commit', activity.intent['operation_id'])
    value = activity.journal.read('outcomes', key)
    runtime.validate_activity_batch(value)
    return key, value


def controller(activity, *, check=None):
    return runtime.ActivityCommitter(activity.journal, activity.execute,
        activity.check_active if check is None else check)


async def partial_case(activity):
    normal = activity.program
    activity.program = normal + PARTIAL_PROGRAM
    try:
        await activity.finish({}, failed=True)
    except runtime.ActivityPending:
        pass
    else:
        raise AssertionError('The partial write was incorrectly confirmed')
    finally:
        activity.program = normal
    key, batch = frozen_batch(activity)
    if activity.journal.read('responses', key) != runtime.activity_failure_receipt(batch):
        raise AssertionError('No native terminal failure proof for repair')
    activity.program = normal + PARTIAL_READBACK_PROGRAM
    try:
        pending = await activity._worker(activity.execute, 'VERIFY_ACTIVITY', batch)
    finally:
        activity.program = normal
    if pending != {'kind':'TEST_PARTIAL_ACTIVITY', 'run_count':1, 'event_count':1,
            'expected_event_count':2, 'verified':True}:
        raise AssertionError('No exact native partial state was observed')
    receipt = await activity._worker(controller(activity).repair, batch)
    if await activity._worker(controller(activity).commit, batch) != receipt:
        raise AssertionError('Read-only replay differs after repair')
    return receipt


async def abort_case(activity):
    activity.observe('invocation', 'failed', {})
    batch = runtime.activity_batch(activity.intent, activity.events,
        finished_at=datetime.now(timezone.utc), status='FAILED', result_state='unavailable')
    gate = JournalGate(activity.journal, abort=True)
    activity.journal = gate
    def check():
        activity.check_active()
        gate.check()
    try:
        await activity._worker(controller(activity, check=check).commit, batch)
    except InterruptedError:
        pass
    else:
        raise AssertionError('The pre-executor abort was not observed')
    key, observed = frozen_batch(activity)
    if observed != batch or gate.read('responses', key) != runtime.activity_abort_receipt(batch):
        raise AssertionError('The pre-executor abort has no exact NOT_STARTED seal')
    if await activity._worker(activity.execute, 'VERIFY_ACTIVITY', batch) == runtime.activity_receipt(batch):
        raise AssertionError('An aborted writer unexpectedly materialized rows')
    return await activity._worker(controller(activity).repair, batch)


async def cancellation_case(activity, binding, transport, secret_get):
    gate = JournalGate(activity.journal, freeze=True)
    activity.journal = gate
    task = asyncio.create_task(activity.finish({}, cancelled=True))
    entered = False
    try:
        entered = await asyncio.to_thread(gate.entered.wait, 10)
    finally:
        # Own the child even if the outer native invoke is itself cancelled.
        if not task.done() and not task.cancelling():
            task.cancel()
        gate.release.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.result()
    if not task.cancelled():
        raise AssertionError('The native asyncio cancellation was not propagated')
    if not entered:
        raise TimeoutError('The cancellation did not reach the freeze boundary')
    key, batch = frozen_batch(activity)
    if (not activity.cancelled.is_set() or gate.read('claims', key) is not None
            or gate.read('materialized', key) is not None or batch['run']['status'] != 'CANCELLED'):
        raise AssertionError('Cancellation started a writer or lost its frozen result')
    recovery = runtime.NativeActivity(binding, transport, runtime.ACTIVITY_PROGRAM, secret_get)
    try:
        # A fresh active adapter explicitly resumes the durable batch. No new begin/model.
        return await recovery._worker(controller(recovery).commit, batch)
    finally:
        recovery.close()


async def run_cases(binding, transport, question, secret_get):
    if binding.get('deployment_stage') != 'TEST' or question != TEST_COMMAND:
        raise PermissionError('Only the operator-pinned TEST invocation is admitted')
    results, once = [], None
    for name, operation in (('partial_write', partial_case), ('pre_executor_abort', abort_case),
                            ('cancel_during_freeze', cancellation_case)):
        transport.check_identity()
        activity = runtime.NativeActivity(binding, transport, runtime.ACTIVITY_PROGRAM, secret_get)
        try:
            _, subject, session = transport.original
            if not results:
                once = runtime.identity('activity-fault-acceptance', TEST_COMMAND,
                    binding['component_revision'], binding['platform_id'], binding['workspace_key'])
                if activity.journal.read('claims', once) is not None:
                    raise runtime.ActivityPending('This TEST invocation already started; reconcile it')
                if not await activity._worker(activity.journal.create, 'claims', once,
                        {'component_revision':binding['component_revision'], 'kind':'TEST_ACTIVITY_RECOVERY',
                         'actor_hash':runtime.identity('activity-subject', subject),
                         'session_hash':runtime.identity('activity-session', session)}):
                    raise runtime.ActivityPending('This TEST invocation already started; reconcile it')
            await activity.begin(subject, session, question + ' / ' + name)
            if name == 'cancel_during_freeze':
                receipt = await operation(activity, binding, transport, secret_get)
            else:
                receipt = await operation(activity)
            if receipt.get('verified') is not True or receipt.get('event_count') != 2:
                raise AssertionError('The recovered TEST operation has no exact receipt')
            result = {'case':name, 'receipt':receipt}
            results.append(result)
            await activity._worker(activity.journal.create, 'outcomes',
                runtime.identity('activity-fault-case', once, name), result)
            if len(results) == 3:
                report = {'phase':'activity_recovery_acceptance', 'component_revision':binding['component_revision'],
                    'fixture_kind':'CONTROLLED_TEST_FAILURES', 'cases':results,
                    'model_calls':0, 'catalog_writes':0, 'business_rows_written':0}
                await activity._worker(activity.journal.create, 'materialized', once, report)
                return report
        finally:
            activity.close()


class DataGovernanceAgent:
    def setup(self):
        pass

    async def invoke(self, user_query, **kwargs):
        from aidputils.agents.toolkit.agent_helper import pre_invoke_setup, flow_setup
        from aidputils.agents.auth.util import auth_utils
        from langchain_core.messages import AIMessage
        import aidputils

        if runtime.CONFIG.get('deployment_stage') != 'TEST' or user_query != TEST_COMMAND:
            raise PermissionError('No ordinary or production invocation is allowed in this TEST harness')
        pre_invoke_setup(**kwargs)
        with flow_setup(**kwargs):
            binding = {**runtime.CONFIG, 'component_revision':HARNESS_REVISION}
            transport = BoundTransport(binding, auth_utils.get_auth_context, aidputils.secrets.get)
            result = await run_cases(binding, transport, user_query, aidputils.secrets.get)
            return {'messages':[AIMessage(content=json.dumps(result, sort_keys=True))]}
