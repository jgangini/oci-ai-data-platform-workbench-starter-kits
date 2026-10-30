"""Inactive candidate: stream one public field from one structured model run.

Only ``answer`` or ``summary`` can become provisional text deltas. The returned
last dictionary still requires the caller's authoritative schema, citation and
business validation; this helper never emits success or publishes charts.

A rewritten/truncated snapshot stops deltas permanently and notifies
``on_degraded('public_text_rewritten')``. The same iterator continues to its final
dictionary. After external validation, the consumer must replace provisional text
with the final answer rather than append it. Callback delivery failure similarly
stops deltas and attempts one degradation notice, without retrying the callback
or model. If that notice also fails, the final response remains authoritative.

Callers own run/event identities, authenticated delivery, persistence and replay.
Cancellation propagates normally; telemetry delivery errors are best effort.
"""
from contextlib import aclosing
import copy
import inspect
import json


PUBLIC_STREAM_FIELDS = frozenset(('answer', 'summary'))


def _public_stream_options(field, on_delta, on_degraded, limits):
    if field not in PUBLIC_STREAM_FIELDS:
        raise ValueError('Only answer or summary may be streamed publicly')
    for callback in (on_delta, on_degraded):
        method = callback if inspect.iscoroutinefunction(callback) else getattr(callback, '__call__', None)
        if not inspect.iscoroutinefunction(method):
            raise TypeError('Public stream callbacks must be asynchronous')
    if any(type(value) is not int or value <= 0 for value in limits.values()):
        raise ValueError('Public stream limits must be positive integers')
    if not 4 <= limits['max_delta_bytes'] <= 4096:
        raise ValueError('Public delta byte limit must be between 4 and 4096')


def _public_stream_snapshot(snapshot, field, max_snapshot_bytes, max_text_chars, max_text_bytes):
    if not isinstance(snapshot, dict) or any(not isinstance(key, str) for key in snapshot):
        raise ValueError('Structured stream must yield dictionaries with string keys')
    try:
        raw = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError):
        raise ValueError('Structured stream snapshot must be valid bounded JSON') from None
    if len(raw) > max_snapshot_bytes:
        raise ValueError('Structured stream snapshot exceeds its byte limit')
    if field not in snapshot:
        return None
    text = snapshot[field]
    if not isinstance(text, str):
        raise ValueError('Public stream field must contain a string')
    if len(text) > max_text_chars or len(text.encode('utf-8')) > max_text_bytes:
        raise ValueError('Public stream text exceeds its character or byte limit')
    return text


def _public_stream_chunks(text, max_delta_bytes):
    chunk, size = [], 0
    for char in text:
        width = len(char.encode('utf-8'))
        if size + width > max_delta_bytes:
            yield ''.join(chunk)
            chunk, size = [], 0
        chunk.append(char)
        size += width
    if chunk:
        yield ''.join(chunk)


async def _public_stream_degraded(callback, reason):
    try:
        await callback(reason)
    except Exception:
        # A lost telemetry response must not cause a second model execution.
        pass


async def _public_stream_deliver(text, previous, on_delta, on_degraded, max_delta_bytes):
    if not text.startswith(previous):
        await _public_stream_degraded(on_degraded, 'public_text_rewritten')
        return False
    for delta in _public_stream_chunks(text[len(previous):], max_delta_bytes):
        try:
            await on_delta(delta)
        except Exception:
            await _public_stream_degraded(on_degraded, 'public_text_delivery_failed')
            return False
    return True


async def stream_public_field(runnable, messages, *, field, on_delta, on_degraded,
        max_text_chars=1800, max_text_bytes=7200, max_snapshot_bytes=98304,
        max_snapshots=4096, max_delta_bytes=4096):
    """Consume ``runnable.astream(messages)`` once; return its last dictionary.

    ``runnable`` is the caller's existing ``with_structured_output(schema_dict)``
    runnable. This does not call ainvoke, bind another model, retry generation or
    synthesize token chunks from a completed answer. Empty dictionaries/missing
    public fields during generation and duplicate snapshots emit no deltas. An
    empty iterator or final dictionary without the selected public field fails.
    """
    limits = {'max_text_chars': max_text_chars, 'max_text_bytes': max_text_bytes,
        'max_snapshot_bytes': max_snapshot_bytes, 'max_snapshots': max_snapshots,
        'max_delta_bytes': max_delta_bytes}
    _public_stream_options(field, on_delta, on_degraded, limits)
    previous, final, active, count = '', None, True, 0
    async with aclosing(runnable.astream(messages)) as snapshots:
        async for snapshot in snapshots:
            count += 1
            if count > max_snapshots:
                raise ValueError('Structured stream exceeded its snapshot limit')
            text = _public_stream_snapshot(snapshot, field, max_snapshot_bytes, max_text_chars, max_text_bytes)
            final = copy.deepcopy(snapshot)
            if text is not None and active:
                active = await _public_stream_deliver(text, previous, on_delta, on_degraded, max_delta_bytes)
                previous = text
    if final is None or field not in final:
        raise ValueError('Structured stream ended without a final public dictionary')
    return final
