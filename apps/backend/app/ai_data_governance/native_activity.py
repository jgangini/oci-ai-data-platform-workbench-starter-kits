"""Trusted consultation activity lifecycle, separate from model capabilities."""

import asyncio
from datetime import datetime,timezone
from threading import Event
from uuid import uuid4

from .activity_records import activity_intent,activity_event,activity_batch
from .activity_commit import ActivityCommitter,ActivityPending,activity_receipt,activity_receipt_matches
from .activity_transport import run_native_activity
from .inventory import identity
from .journal import GovernanceJournal
from .service_transport import runtime_object_storage


class NativeActivity:
    def __init__(self,binding,transport,program,secret_get):
        if transport.access_mode != "CENTRAL_METADATA":
            raise PermissionError("Activity requires the installed central identity")
        self.binding,self.transport,self.program=binding,transport,program
        self.cancelled=Event()
        self.objects=runtime_object_storage(binding,transport.signer,secret_get)
        self.journal=GovernanceJournal(self.objects,platform_key=binding['platform_id'],
            workspace_key=binding['workspace_key'],storage_root=binding['storage_root'])
        self.intent=None; self.events=[]
        self.diagnostics=[]

    def diagnose(self,stage,transition,error=None):
        stages={'freeze','finish','commit_identity','outcome_create','outcome_read','outcome_match','materialized_read','materialized_match',
            'claim_create','append_identity','append','verify','receipt_match','materialized_identity',
            'materialized_create','command_setup','command','command_identity','cleanup','session_close','receipt_decode','program_terminal','reconcile'}
        if stage not in stages or transition not in {'started','completed','failed'} or len(self.diagnostics)>=48:
            return
        row={'stage':stage,'transition':transition}
        if transition=='failed':
            allowed={'RuntimeError','ValueError','PermissionError','InterruptedError','TimeoutError',
                'ServiceError','ReadTimeout','ConnectTimeout','ConnectionError','JournalConflict','ActivityPending'}
            kind=type(error).__name__
            row['error_type']=kind if kind in allowed else 'other'
        self.diagnostics.append(row)

    def _persist_diagnostic(self,batch):
        self.check_active()
        observations=list(self.diagnostics)
        value={'schema_version':1,'kind':'ACTIVITY_PENDING_DIAGNOSTIC','operation_id':batch['operation_id'],
            'component_revision':self.binding['component_revision'],'input_digest':identity('activity-batch',batch),
            'observations':observations,'first_failure':next((r for r in observations if r['transition']=='failed'),None),
            'result_observed':{'stage':'command','transition':'completed'} in observations,
            'cleanup_completed':{'stage':'cleanup','transition':'completed'} in observations,
            'cleanup_failed':any(r['stage']=='cleanup' and r['transition']=='failed' for r in observations)}
        key=identity('consultation-activity-diagnostic-v1',batch['operation_id'])
        self.check_active()
        self.journal.create('responses',key,value)
        self.check_active()

    def check_active(self):
        if self.cancelled.is_set():
            raise InterruptedError("Activity invocation was cancelled")
        self.transport.check_identity()

    async def _worker(self,function,*args):
        task=asyncio.create_task(asyncio.to_thread(function,*args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self.cancelled.set()
            # The SDK has bounded polls/timeouts. Do not close its signer/session mid-write.
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.done():
                        break
                except Exception:
                    break
            raise

    async def begin(self,subject,session,question,request_id=None):
        self.check_active()
        self.intent=activity_intent(self.binding,subject=subject,session=session,question=question,
            request_id=str(uuid4()) if request_id is None else request_id,started_at=datetime.now(timezone.utc))
        key=identity("consultation-start",self.intent['operation_id'])
        if not await self._worker(self.journal.create,"executions",key,self.intent):
            raise ActivityPending("This request already started; recover it without another model invocation")
        self.observe('invocation','started',{})

    def observe(self,phase,transition,state):
        self.check_active()
        if self.intent is None or len(self.events)>=31:
            raise ValueError("Activity observations are not admitted")
        self.events.append(activity_event(phase,transition,datetime.now(timezone.utc),
            asset_count=len(state.get('assets',[])),evidence_count=len(state.get('evidence',[]))))

    def execute(self,mode,batch):
        first=len(self.diagnostics)
        try:
            return run_native_activity(self.binding,self.transport.signer,self.program,mode,batch,self.check_active,diagnose=self.diagnose)
        except Exception:
            observations=self.diagnostics[first:]
            failures=[row for row in observations if row['transition']=='failed']
            # A lost command result permits one read, never another append or a repair seal.
            if (mode!='APPEND_ACTIVITY' or not failures or failures[0]['stage']!='command'
                    or any(row['stage']!='command' for row in failures)
                    or {'stage':'cleanup','transition':'completed'} not in observations
                    or {'stage':'session_close','transition':'completed'} not in observations):
                raise
            self.check_active()
            self.diagnose('reconcile','started')
            try:
                observed=run_native_activity(self.binding,self.transport.signer,self.program,
                    'VERIFY_ACTIVITY',batch,self.check_active,diagnose=self.diagnose)
                self.check_active()
                if not activity_receipt_matches(observed,activity_receipt(batch)):
                    raise ActivityPending('The frozen activity rows are not confirmed')
            except (PermissionError,InterruptedError):
                raise
            except Exception as exc:
                self.diagnose('reconcile','failed',exc)
            else:
                self.diagnose('reconcile','completed')
                return observed
            raise

    async def finish(self,state,*,failed=False,cancelled=False,materialize=True):
        if type(materialize) is not bool:
            raise ValueError('Activity materialization requires an explicit boolean')
        if self.intent is None:
            raise ValueError("Activity did not start")
        result=state.get('response',{}).get('result_state','unavailable')
        self.observe('invocation','failed' if failed or cancelled else 'completed',state)
        status='CANCELLED' if cancelled else ('FAILED' if failed else ('PARTIAL' if result in {'partial','not_evaluable'} else 'SUCCEEDED'))
        batch=activity_batch(self.intent,self.events,finished_at=datetime.now(timezone.utc),status=status,result_state=result)
        # Freeze even when cancellation arrives before the executor worker gets scheduled.
        self.diagnose('freeze','started')
        try:
            try:
                await self._worker(self.journal.create,'outcomes',identity('consultation-activity-commit',batch['operation_id']),batch)
            except Exception as exc:
                self.diagnose('freeze','failed',exc)
                raise
            self.diagnose('freeze','completed')
            if not materialize:
                return {'schema_version':1,'kind':'CONSULTATION_ACTIVITY_RECORDED',
                    'operation_id':batch['operation_id'],'input_digest':identity('activity-batch',batch),
                    'recorded':True,'materialized':False}
            return await self._worker(ActivityCommitter(self.journal,self.execute,self.check_active,diagnose=self.diagnose).commit,batch)
        except Exception as exc:
            self.diagnose('finish','failed',exc)
            try:
                await self._worker(self._persist_diagnostic,batch)
            except Exception:
                pass
            raise

    def close(self):
        self.objects.base_client.session.close()
