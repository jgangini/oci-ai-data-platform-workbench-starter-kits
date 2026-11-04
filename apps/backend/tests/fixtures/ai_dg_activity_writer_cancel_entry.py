"""One operator-pinned TEST of cancellation after a fixed writer is acknowledged.

This entry is temporary and never part of an installer. Its barrier wraps our
central SDK adapter, not vendor code. Acknowledgement does not prove commit.
"""
import asyncio
import json
from threading import Event

import governance_runtime as runtime

HARNESS_REVISION = 'OPERATOR_PINS_REVISION'
TEST_COMMAND = 'OPERATOR_PINS_ONE_USE_COMMAND'


async def drain_writer(task, release, original, cancelled):
    parent = asyncio.current_task()
    cancellations = parent.cancelling()
    try:
        if not task.done():
            task.cancel()
        # The SDK barrier is released only after the coroutine receives cancellation.
        deadline = asyncio.get_running_loop().time() + 5
        while not task.done() and not cancelled.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        release.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.result()
    finally:
        runtime.CentralSqlSdk = original
    if parent.cancelling() > cancellations:
        raise asyncio.CancelledError


async def cancel_acknowledged_writer(activity):
    acknowledged, release = Event(), Event()
    original = runtime.CentralSqlSdk
    observations = {'acknowledged_commands':0, 'cleanup_confirmed':False}
    cleanup_failure = {}

    class AcknowledgedWriter(original):
        def execute_command(self, *args, **kwargs):
            response = super().execute_command(*args, **kwargs)
            observations['acknowledged_commands'] += 1
            acknowledged.set()
            if not release.wait(10):
                raise TimeoutError('The TEST acknowledgement barrier was not released')
            return response

        def cleanup(self):
            try:
                super().cleanup()
            except Exception as error:
                cleanup_failure.update(cleanup_error(error, len(self.contexts)))
                raise
            observations['cleanup_confirmed'] = not self.contexts

    runtime.CentralSqlSdk = AcknowledgedWriter
    task = asyncio.create_task(activity.finish({}, failed=True))
    entered = False
    try:
        deadline = asyncio.get_running_loop().time() + 180
        while not acknowledged.is_set() and not task.done() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        entered = acknowledged.is_set()
    finally:
        await drain_writer(task, release, original, activity.cancelled)
    if (not entered or not task.cancelled() or not activity.cancelled.is_set()
            or observations != {'acknowledged_commands':1, 'cleanup_confirmed':True}):
        boundary = {**observations, 'barrier_acknowledged':entered, 'worker_done':task.done(),
            'worker_cancelled':task.cancelled(), 'activity_cancelled':activity.cancelled.is_set(),
            'sdk_restored':runtime.CentralSqlSdk is original}
        if cleanup_failure:
            boundary['cleanup_failure'] = cleanup_failure
        raise AssertionError('Writer cancellation boundary was not accepted: ' + json.dumps(boundary, sort_keys=True))
    key = runtime.identity('consultation-activity-commit', activity.intent['operation_id'])
    batch = activity.journal.read('outcomes', key)
    runtime.validate_activity_batch(batch)
    if (activity.journal.read('claims', key) is None or activity.journal.read('responses', key) is not None
            or activity.journal.read('materialized', key) is not None):
        raise AssertionError('Cancellation lost admission or manufactured a terminal/commit receipt')
    return key, batch, observations


def cleanup_error(error, remaining):
    names = {'ServiceError', 'TimeoutError', 'ConnectionError', 'InterruptedError',
        'RuntimeError', 'ValueError'}
    status = getattr(error, 'status', None)
    return {'error_type':type(error).__name__ if type(error).__name__ in names else 'OtherException',
        'http_status':status if type(status) is int and 100 <= status <= 599 else None,
        'remaining_contexts':remaining}


async def verify_writer(binding, transport, activity, once, secret_get):
    key, batch, observations = await cancel_acknowledged_writer(activity)
    recovery = runtime.NativeActivity(binding, transport, runtime.ACTIVITY_PROGRAM, secret_get)
    try:
        # Verification cannot repair or append an uncertain cancelled operation.
        observed = await recovery._worker(recovery.execute, 'VERIFY_ACTIVITY', batch)
        expected = runtime.activity_receipt(batch)
        if observed not in (expected, {'verified':False}):
            raise AssertionError('The fixed readback returned an unexpected result')
        if activity.journal.read('responses', key) is not None:
            raise AssertionError('A cancellation was incorrectly sealed as a pre-start abort')
        report = {'phase':'activity_writer_cancellation_acceptance', 'component_revision':HARNESS_REVISION,
            'operation_id':batch['operation_id'], **observations, 'write_result':
                'verified_complete' if observed == expected else 'unconfirmed_preserved',
            'verification_receipt':observed, 'new_appends':0, 'model_calls':0, 'catalog_writes':0}
        await recovery._worker(recovery.journal.create, 'materialized', once, report)
        return report
    finally:
        recovery.close()


def rejection_report(error):
    report = {'phase':'activity_writer_cancellation_rejected', 'accepted':False,
        'component_revision':HARNESS_REVISION, 'error_type':type(error).__name__,
        'model_calls':0, 'catalog_writes':0, 'repair_attempted':False}
    prefix = 'Writer cancellation boundary was not accepted: '
    if isinstance(error, AssertionError) and str(error).startswith(prefix):
        try:
            boundary = json.loads(str(error)[len(prefix):])
            if valid_boundary(boundary):
                report['boundary'] = boundary
        except (ValueError, TypeError):
            pass
    return report


def valid_boundary(value):
    flags = {'cleanup_confirmed', 'barrier_acknowledged', 'worker_done',
        'worker_cancelled', 'activity_cancelled', 'sdk_restored'}
    expected = flags | {'acknowledged_commands'}
    return (isinstance(value, dict) and set(value) in (expected, expected | {'cleanup_failure'})
        and type(value['acknowledged_commands']) is int and 0 <= value['acknowledged_commands'] <= 32
        and all(type(value[key]) is bool for key in flags)
        and ('cleanup_failure' not in value or valid_cleanup_error(value['cleanup_failure'])))


def valid_cleanup_error(value):
    return (isinstance(value, dict) and set(value) == {'error_type','http_status','remaining_contexts'}
        and value['error_type'] in {'ServiceError','TimeoutError','ConnectionError','InterruptedError',
            'RuntimeError','ValueError','OtherException'}
        and (value['http_status'] is None or type(value['http_status']) is int and 100 <= value['http_status'] <= 599)
        and type(value['remaining_contexts']) is int and 0 <= value['remaining_contexts'] <= 32)


async def run_owned_probe(binding, transport, secret_get, get_context):
    activity, owns_probe = None, False
    once = runtime.identity('activity-writer-cancel-acceptance', TEST_COMMAND, HARNESS_REVISION,
        binding['platform_id'], binding['workspace_key'])
    try:
        activity = runtime.NativeActivity(binding, transport, runtime.ACTIVITY_PROGRAM, secret_get)
        if not await activity._worker(activity.journal.create, 'claims', once,
                {'kind':'TEST_WRITER_CANCELLATION', 'component_revision':HARNESS_REVISION}):
            raise runtime.ActivityPending('Reconcile this existing TEST; never start it again')
        owns_probe = True
        _, subject, session = runtime._caller_identity(get_context)
        await activity.begin(subject, session, TEST_COMMAND)
        return await verify_writer(binding, transport, activity, once, secret_get)
    except Exception as error:
        report = rejection_report(error)
        if owns_probe:
            transport.check_identity()
            await activity._worker(activity.journal.create, 'outcomes', once, report)
        return report
    finally:
        if activity is not None:
            activity.close()


class DataGovernanceAgent:
    def setup(self):
        pass

    async def invoke(self, user_query, **kwargs):
        from aidputils.agents.toolkit.agent_helper import pre_invoke_setup, flow_setup
        from aidputils.agents.auth.util import auth_utils
        from langchain_core.messages import AIMessage
        import aidputils

        pre_invoke_setup(**kwargs)
        if runtime.CONFIG.get('deployment_stage') != 'TEST' or user_query != TEST_COMMAND:
            raise PermissionError('Only the one-use writer-cancellation TEST is admitted')
        with flow_setup(**kwargs):
            binding = {**runtime.CONFIG, 'component_revision':HARNESS_REVISION}
            transport = runtime.native_central_transport(binding)
            try:
                report = await run_owned_probe(binding, transport, aidputils.secrets.get, auth_utils.get_auth_context)
                return {'messages':[AIMessage(content=json.dumps(report, sort_keys=True))]}
            finally:
                transport.session.close()
