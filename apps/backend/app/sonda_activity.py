"""Invocation-local Activity delivery; activation and final-result validation belong to callers.

Admission errors block entry. After admission, journal errors disable observability
without retrying business work or replacing its result. Private completion requires
the canonical result codec and the caller's existing contextual semantic validator.
"""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import uuid4

from app.sonda_activity_runtime import (
    ActivityJournal_admit_native,
    ActivityJournal_JournalWriter,
    ActivityJournal__put,
    ActivityProducer_PRODUCER_RECORDS,
    ActivityProducer_producer_sha256,
    ActivityResult_RESULT_VERSION,
    ActivityResult_result_bytes,
    ActivityResult_parse_result,
    ActivityPublic_stream_public_field,
)


_ACTIVE_ACTIVITY = ContextVar('sonda_activity_invocation', default=None)
_ACTIVITY_SPAN = ContextVar('sonda_activity_span', default=None)


def current_activity():
    """Return only this async invocation's adapter, never checkpoint state."""
    return _ACTIVE_ACTIVITY.get()


async def _activity_io(operation, *args):
    """Keep the serial lock until a cancelled thread's actual IO has stopped."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
            if task.done():
                raise cancellation
        except Exception:
            if cancellation is not None:
                raise cancellation
            raise
        else:
            if cancellation is not None:
                raise cancellation
            return result



class InvocationActivity:
    def __init__(self, lease):
        self.lease = lease
        self.writer = None
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.closed = False
        self.completed = False
        self.observability_failed = False

    async def start(self, objects):
        self.objects = objects
        try:
            self.writer = await _activity_io(ActivityJournal_JournalWriter, objects,
                self.lease, str(self.lease.descriptor.run.attempt_id))
        except Exception:
            self.observability_failed = True

    async def _write(self, operation, *args):
        if self.observability_failed or self.writer is None:
            return False
        try:
            await _activity_io(operation, *args)
            return True
        except Exception:
            self.observability_failed = True
            return False

    async def _emit_record(self, payload, *, span=None, parent=None):
        if self.closed or self.completed or self.observability_failed or self.writer is None:
            return False
        self.sequence += 1
        run = self.lease.descriptor.run
        active = _ACTIVITY_SPAN.get()
        span = span or active or run.attempt_id
        event = dict(event_id=uuid4(), run_id=run.run_id, attempt_id=run.attempt_id,
            producer_stream_id=run.attempt_id, producer_sequence=self.sequence,
            span_id=span, parent_span_id=parent, causation_id=None,
            emitted_at=datetime.now(timezone.utc), **payload)
        try:
            record = ActivityProducer_PRODUCER_RECORDS.validate_python(event).model_dump(mode='json')
        except Exception:
            self.observability_failed = True
            return False
        return await self._write(self.writer.append, record)

    async def _emit(self, payload, *, span=None, parent=None):
        async with self.lock:
            return await self._emit_record(payload, span=span, parent=parent)

    def _validated_result(self, kind, payload, chat_validator, gold_report_validator):
        descriptor = self.lease.descriptor
        run = descriptor.run
        value = dict(schema_version=ActivityResult_RESULT_VERSION, scope=descriptor.scope.model_dump(mode='json'),
            result_id=str(descriptor.admission_id), run_id=str(run.run_id), attempt_id=str(run.attempt_id),
            kind=kind, payload=payload)
        raw = ActivityResult_result_bytes(value)
        ref = dict(result_id=str(descriptor.admission_id), sha256=ActivityProducer_producer_sha256(raw), byte_length=len(raw))
        ActivityResult_parse_result(raw, expected_scope=descriptor.scope.model_dump(mode='json'),
            expected_run=run.model_dump(mode='json'), expected_ref=ref, chat_validator=chat_validator,
            gold_report_validator=gold_report_validator, expected_work_input_sha256=descriptor.work_input_sha256)
        return raw, ref

    async def complete_result(self, kind, payload, *, chat_validator=None, gold_report_validator=None):
        """Validate unchanged output, persist its fixed artifact, then privately signal completion.

        An uncertain write may leave an orphan artifact; without exact readback no
        completion is emitted. Semantic rejection never writes an artifact. False
        never authorizes another model/tool execution or an automatic retry.
        """
        async with self.lock:
            if self.closed or self.completed or self.observability_failed or self.writer is None:
                return False
            try:
                raw, ref = self._validated_result(kind, payload, chat_validator, gold_report_validator)
            except Exception:
                self.observability_failed = True
                return False
            key = self.lease.root+'results/'+str(self.lease.descriptor.admission_id)+'.json'
            if not await self._write(ActivityJournal__put, self.objects, key, raw, None):
                return False
            emitted = await self._emit_record(dict(kind='execution_completion', outcome='completed',
                summary='Resultado validado y guardado.', final_ref=ref))
            self.completed = emitted
            return emitted

    async def stage(self, summary, effect=None):
        if effect is not None:
            raise ValueError('Native progress stages cannot assert business effects')
        return await self._emit(dict(type='stage', summary=summary, effect=effect, status='progress'))

    @asynccontextmanager
    async def tool(self, domain, name):
        """Caller validates the operation's result before leaving this context."""
        if domain not in ('query_read', 'research_preparation'):
            raise ValueError('Native Activity tools may only read or prepare research')
        span, parent = uuid4(), _ACTIVITY_SPAN.get()
        await self._emit(dict(type='tool_start', domain=domain, tool_name=name,
            summary='Consultando datos aprobados.' if domain == 'query_read' else 'Preparando investigaciÃ³n aprobada.'),
            span=span, parent=parent)
        token = _ACTIVITY_SPAN.set(span)
        try:
            yield
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._emit(dict(type='tool_error', error_code='tool_failed', summary='La operaciÃ³n de la herramienta fallÃ³.'),
                span=span, parent=parent)
            raise
        else:
            await self._emit(dict(type='tool_finish', summary='Consulta de datos aprobados completada.' if domain == 'query_read'
                else 'Solicitud de investigaciÃ³n preparada.', effect='executed' if domain == 'query_read' else 'prepared'),
                span=span, parent=parent)
        finally:
            _ACTIVITY_SPAN.reset(token)

    async def stream_answer(self, runnable, messages, *, field='answer', max_text_chars=1800,
            max_text_bytes=7200, max_snapshot_bytes=98304, max_snapshots=4096, max_delta_bytes=4096):
        async def delta(text):
            await self._emit(dict(type='text_delta', public_text_delta=text))

        async def degraded(reason):
            await self.stage('La respuesta parcial no estÃ¡ disponible.')

        return await ActivityPublic_stream_public_field(runnable, messages, field=field,
            on_delta=delta, on_degraded=degraded, max_text_chars=max_text_chars,
            max_text_bytes=max_text_bytes, max_snapshot_bytes=max_snapshot_bytes,
            max_snapshots=max_snapshots, max_delta_bytes=max_delta_bytes)

    async def flush(self):
        async with self.lock:
            if self.writer is not None:
                await self._write(self.writer.flush)

    async def _timer(self):
        while True:
            await asyncio.sleep(0.1)
            async with self.lock:
                if self.closed or self.completed or self.observability_failed or self.writer is None:
                    return
                await self._write(self.writer.flush_due)


@asynccontextmanager
async def activity_invocation(objects, scope, agent_id, native_session_sha, input_sha, ref, now,
        protected_work=None):
    """Admit once before model/tool work; caller decides whether Activity is enabled."""
    lease = await _activity_io(ActivityJournal_admit_native, objects, scope, agent_id,
        native_session_sha, input_sha, ref, now, protected_work)
    activity = InvocationActivity(lease)
    await activity.start(objects)
    token = _ACTIVE_ACTIVITY.set(activity)
    span_token = _ACTIVITY_SPAN.set(None)
    timer = asyncio.create_task(activity._timer())
    try:
        yield activity
    finally:
        activity.closed = True
        timer.cancel()
        try:
            await timer
        except asyncio.CancelledError:
            pass
        try:
            await activity.flush()
        finally:
            _ACTIVITY_SPAN.reset(span_token)
            _ACTIVE_ACTIVITY.reset(token)
