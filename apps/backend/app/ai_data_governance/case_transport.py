"""Native caller binding for fixed case commands; no arbitrary Python or SQL tool."""

import asyncio
import time
from threading import Event

from .caller_transport import _caller_identity
from .case_changes import case_change_receipt
from .case_program import checked_case_command
from .central_sql import _run_central_command
from .inventory import canonical, identity
from .model_output import model_json


class CaseReconciliationPending(RuntimeError):
    def __init__(self, diagnostic):
        super().__init__('The frozen case remains unconfirmed; no new writer is admitted')
        self.diagnostic = diagnostic


class CaseObservationExpired(TimeoutError):
    """The observation bound expired; this does not prove a remote abort."""
    status_code = 504


def checked_case_pending_report(binding, change, report):
    fields = {'kind','verified','operation_id','input_digest','component_revision','checks'}
    expected = dict(kind='GOVERNANCE_CASE_PENDING', verified=False, operation_id=change['operation_id'],
        input_digest=identity('case-change',change), component_revision=binding['component_revision'])
    if (not isinstance(report,dict) or set(report)!=fields or report.get('verified') is not False
            or any(report.get(k)!=v for k,v in expected.items())):
        raise ValueError('Pending diagnostics differ from the exact operation')
    checks=report['checks']
    if not isinstance(checks,list) or len(checks)!=3:
        raise ValueError('Pending diagnostic coverage differs')
    for phase,row in zip(('FROZEN_START','CURRENT_REFERENCES','EXPECTED_PREVIOUS_STATE'), checks):
        checked_pending_case_check(phase,row)
    return report


def checked_pending_case_check(phase, row):
    if not isinstance(row,dict) or set(row)!={'phase','state','error_kind'} or row['phase']!=phase:
        raise ValueError('Pending diagnostic phase differs')
    if row['state']=='PASSED' and row['error_kind'] is None:
        return
    if (phase!='FROZEN_START' and row['state']=='REJECTED'
            and row['error_kind'] in ('PENDING','PERMISSION_ERROR','VALUE_ERROR','TYPE_ERROR','RUNTIME_ERROR','UNAVAILABLE')):
        return
    raise ValueError('Pending diagnostic state is invalid')


def case_command(binding, program, mode, change, caller_binding, grant):
    checked_case_command(binding, mode, change, caller_binding, grant)
    data = canonical(dict(binding=binding, mode=mode, change=change, caller_binding=caller_binding, grant=grant))
    # Quoted literal JSON is data even when treatment text contains Python punctuation.
    return program + f'\ncase_input = json.loads({data!r})\nprint(json.dumps(run_case_program(spark, **case_input, secret_get=aidputils.secrets.get), sort_keys=True))\n'


def run_native_case(binding, transport, program, mode, change, grant, *, get_context, cancelled=None):
    if transport.access_mode != "CENTRAL_METADATA":
        raise PermissionError("Cases require the installed central metadata identity")
    original = _caller_identity(get_context)
    _, subject, session = original
    caller_binding = dict(actor_ref=identity("activity-subject", subject), session_ref=identity("activity-session", session),
        operation_id=change["operation_id"], component_revision=binding["component_revision"])
    deadline=time.monotonic()+360
    def check_active():
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("Case invocation was cancelled; reconcile its frozen operation")
        transport.check_identity()
        if _caller_identity(get_context) != original:
            raise PermissionError("Case caller identity or conversation changed")
        if time.monotonic()>=deadline:
            raise CaseObservationExpired('The fixed case observation expired; reconcile its frozen operation')
    check_active()
    command = case_command(binding, program, mode, change, caller_binding, grant)
    result = _run_central_command(binding, transport.signer, command, check_active, language="PYTHON",max_poll_attempts=120)
    check_active()
    return checked_native_case_result(binding,mode,change,result)


def checked_native_case_result(binding, mode, change, result):
    if (not isinstance(result, dict) or result.get("status") != "SUCCEEDED" or result.get("error")
            or result.get("results_truncated", False) is not False):
        raise RuntimeError("The fixed case operation has no complete result; reconcile the same operation")
    outputs = result.get("outputs")
    if (not isinstance(outputs, list) or len(outputs) != 1 or not isinstance(outputs[0], dict)
            or outputs[0].get("type") != "TEXT_PLAIN" or outputs[0].get("is_base64", False) is not False):
        raise ValueError("Expected one complete fixed case receipt")
    receipt = model_json(outputs[0].get("value"), max_bytes=16384)
    if isinstance(receipt, dict) and receipt.get('kind')=='GOVERNANCE_CASE_PENDING':
        if mode!='RECONCILE_CASE':
            raise ValueError('Only a readback may return pending diagnostics')
        raise CaseReconciliationPending(checked_case_pending_report(binding,change,receipt))
    if canonical(receipt) != canonical(case_change_receipt(change)):
        raise ValueError("Case result does not confirm the exact frozen operation")
    return receipt


async def native_case_operation(binding, transport, program, mode, change, grant, *, get_context):
    """Drain the bounded SDK worker before the invocation closes its clients.

    An already submitted remote writer may complete despite local cancellation.
    The caller must retain its frozen operation for reconciliation, never replay
    COMMIT_CASE or manufacture a NOT_STARTED seal from this cancellation.
    """
    cancelled = Event()
    def execute():
        return run_native_case(binding, transport, program, mode, change, grant,
            get_context=get_context, cancelled=cancelled)
    task = asyncio.create_task(asyncio.to_thread(execute))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise
