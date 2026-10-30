"""Inactive public stream helper tests; controlled iterators, zero model calls."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('sonda_public_stream_candidate',
    ROOT/'contracts/sonda-activity/candidate-v1/public_stream.py')
public = importlib.util.module_from_spec(spec)
spec.loader.exec_module(public)


class Runnable:
    def __init__(self, snapshots, error=None):
        self.snapshots, self.error = snapshots, error
        self.calls, self.closed, self.completed, self.messages = 0, False, False, None

    async def astream(self, messages):
        self.calls += 1
        self.messages = messages
        try:
            for snapshot in self.snapshots:
                yield snapshot
            if self.error:
                raise self.error
            self.completed = True
        finally:
            self.closed = True

    async def ainvoke(self, *args, **kwargs):
        raise AssertionError('Streaming must not invoke a second model operation')


def consume(runnable, *, field='answer', on_delta=None, on_degraded=None, **limits):
    deltas, degradation = [], []
    async def delta(text):
        deltas.append(text)
    async def degraded(reason):
        degradation.append(reason)
    result = asyncio.run(public.stream_public_field(runnable, ['same messages'], field=field,
        on_delta=on_delta or delta, on_degraded=on_degraded or degraded, **limits))
    return result, deltas, degradation


def test_first_delta_arrives_while_same_model_iterator_is_blocked_before_final():
    async def scenario():
        first_delta, release_final = asyncio.Event(), asyncio.Event()
        deltas, degradation = [], []
        runnable = Runnable([])
        async def snapshots(messages):
            runnable.calls += 1
            assert messages == ['same messages']
            yield {'answer': 'Hola'}
            await release_final.wait()
            yield {'answer': 'Hola mundo', 'post_keys': ['verified-later']}
            runnable.completed = True
        runnable.astream = snapshots
        async def delta(text):
            deltas.append(text)
            first_delta.set()
        async def degraded(reason):
            degradation.append(reason)
        task = asyncio.create_task(public.stream_public_field(runnable, ['same messages'],
            field='answer', on_delta=delta, on_degraded=degraded))
        await asyncio.wait_for(first_delta.wait(), 1)
        assert deltas == ['Hola'] and not task.done() and not runnable.completed
        release_final.set()
        final = await task
        assert final == {'answer': 'Hola mundo', 'post_keys': ['verified-later']}
        assert deltas == ['Hola', ' mundo'] and degradation == [] and runnable.calls == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('field', ['answer', 'summary'])
def test_only_selected_public_field_is_emitted_never_private_fields_or_raw_json(field):
    private = {'private_reasoning': 'Do not expose this', 'sql': 'private SQL',
        'other': {'answer': 'Nested text is not the selected public field'}}
    final, deltas, degraded = consume(Runnable([
        private, {**private, field: 'Texto'}, {**private, field: 'Texto público'}]), field=field)
    assert deltas == ['Texto', ' público'] and degraded == []
    assert ''.join(deltas) == 'Texto público'
    assert final['private_reasoning'] == private['private_reasoning']
    assert all('Do not expose' not in delta and 'private SQL' not in delta and '{' not in delta for delta in deltas)


def test_unicode_and_json_escapes_are_already_decoded_text_and_chunks_bound_utf8():
    text = 'Café "uno" \\ruta\n😀终'
    parsed = json.loads(json.dumps({'answer': text}, ensure_ascii=True))
    final, deltas, degraded = consume(Runnable([{'answer': 'Café'}, parsed]), max_delta_bytes=4)
    assert final == parsed and ''.join(deltas) == text and degraded == []
    assert all(0 < len(delta.encode('utf-8')) <= 4 for delta in deltas)
    assert all('\\ud83d' not in delta for delta in deltas)


def test_empty_missing_and_duplicate_partial_snapshots_do_not_emit_duplicates():
    runnable = Runnable([{}, {'answer': ''}, {'answer': ''}, {'answer': 'A'},
        {'private': 'ignored'}, {'answer': 'A'}, {'answer': 'AB'}, {'answer': 'AB'}])
    final, deltas, degraded = consume(runnable)
    assert final == {'answer': 'AB'} and deltas == ['A', 'B'] and degraded == []
    assert runnable.calls == 1 and runnable.closed and runnable.completed


@pytest.mark.parametrize('snapshots,final', [
    ([{'answer': 'Hola'}, {'answer': 'Adiós'}, {'answer': 'Adiós final'}], {'answer': 'Adiós final'}),
    ([{'answer': 'Hola'}, {'answer': 'Ho'}, {'answer': 'Hola otra vez'}], {'answer': 'Hola otra vez'}),
    ([{'answer': 'Hola'}, {'answer': ''}, {'answer': 'Hola mundo'}], {'answer': 'Hola mundo'}),
])
def test_rewrite_or_truncation_stops_deltas_permanently_notifies_and_preserves_final(snapshots, final):
    runnable = Runnable(snapshots)
    result, deltas, degraded = consume(runnable)
    assert result == final
    assert deltas == ['Hola'] and degraded == ['public_text_rewritten']
    assert runnable.calls == 1 and runnable.completed


def test_callback_delivery_failure_does_not_retry_callback_or_model_and_still_returns_final():
    observed, degraded = [], []
    async def disconnected(text):
        observed.append(text)
        raise ConnectionError('Delivery response lost after possible commit')
    async def signal(reason):
        degraded.append(reason)
    runnable = Runnable([{'answer': 'Hola'}, {'answer': 'Hola mundo'}])
    final, _, _ = consume(runnable, on_delta=disconnected, on_degraded=signal)
    assert observed == ['Hola'] and degraded == ['public_text_delivery_failed']
    assert final == {'answer': 'Hola mundo'} and runnable.calls == 1 and runnable.completed


def test_degradation_delivery_failure_is_not_retried_or_a_new_generation():
    notices = []
    async def failed_signal(reason):
        notices.append(reason)
        raise ConnectionError('No observer connection')
    runnable = Runnable([{'answer': 'A'}, {'answer': 'B'}, {'answer': 'BC'}])
    final, deltas, _ = consume(runnable, on_degraded=failed_signal)
    assert final == {'answer': 'BC'} and deltas == ['A']
    assert notices == ['public_text_rewritten'] and runnable.calls == 1


def test_model_failure_after_partial_propagates_without_final_or_success():
    delivered = []
    async def delta(text):
        delivered.append(text)
    runnable = Runnable([{'answer': 'Parcial'}], RuntimeError('Model stream failed'))
    with pytest.raises(RuntimeError, match='Model stream failed'):
        consume(runnable, on_delta=delta)
    assert delivered == ['Parcial'] and runnable.calls == 1 and runnable.closed and not runnable.completed


def test_complete_answer_allows_incremental_citation_fields_without_repeating_text():
    answer='Respuesta pública basada en las fuentes.'
    snapshots=[{'answer':answer[:length]} for length in range(1,len(answer)+1)]
    keys=[]
    for number in range(8):
        for length in range(1,65):
            snapshots.append({'answer':answer,'post_keys':[*keys,format(number,'x')*length]})
        keys.append(format(number,'x')*64)
    runnable=Runnable(snapshots)
    final,deltas,degraded=consume(runnable)
    assert final=={'answer':answer,'post_keys':keys}
    assert ''.join(deltas)==answer and degraded==[]
    assert runnable.completed and runnable.calls==1


@pytest.mark.parametrize('snapshots', [[], [{}], [{'private': 'only'}], [{'answer': 'partial'}, {}],
    [None], [['not a dictionary']], [{'answer': 1}], [{'answer': None}], [{1: 'numeric key'}]])
def test_empty_iterator_or_invalid_final_data_never_becomes_success(snapshots):
    runnable = Runnable(snapshots)
    with pytest.raises(ValueError):
        consume(runnable)
    assert runnable.calls == 1 and runnable.closed


@pytest.mark.parametrize('snapshots,limits', [
    ([{'answer': 'abc'}], {'max_text_chars': 2}),
    ([{'answer': '😀😀'}], {'max_text_bytes': 7}),
    ([{'answer': 'a', 'private': 'x'*100}], {'max_snapshot_bytes': 50}),
    ([{'answer': 'a'}, {'answer': 'ab'}], {'max_snapshots': 1}),
    ([{'answer': '\ud800'}], {}),
    ([{'answer': 'a', 'private': float('nan')}], {}),
    ([{'answer': 'a', 'private': object()}], {}),
])
def test_bounds_and_invalid_unicode_or_json_fail_closed(snapshots, limits):
    runnable = Runnable(snapshots)
    with pytest.raises(ValueError):
        consume(runnable, **limits)
    assert runnable.calls == 1 and runnable.closed


@pytest.mark.parametrize('field', ['reasoning', 'private', 'sql', 'post_keys', '', None])
def test_unknown_public_fields_are_rejected_before_starting_model(field):
    runnable = Runnable([{'answer': 'a'}])
    with pytest.raises(ValueError):
        consume(runnable, field=field)
    assert runnable.calls == 0


@pytest.mark.parametrize('limits', [
    {'max_text_chars': False}, {'max_text_bytes': 0}, {'max_snapshot_bytes': -1},
    {'max_snapshots': '10'}, {'max_delta_bytes': 3}, {'max_delta_bytes': 4097},
])
def test_invalid_limit_configuration_is_rejected_before_starting_model(limits):
    runnable = Runnable([{'answer': 'a'}])
    with pytest.raises(ValueError):
        consume(runnable, **limits)
    assert runnable.calls == 0


def test_async_callbacks_required_before_model_start():
    runnable = Runnable([{'answer': 'a'}])
    with pytest.raises(TypeError):
        consume(runnable, on_delta=lambda _: None)
    assert runnable.calls == 0
    with pytest.raises(TypeError):
        consume(runnable, on_degraded=lambda _: None)
    assert runnable.calls == 0


def test_callback_cancellation_propagates_and_closes_the_same_iterator():
    async def cancelled(_):
        raise asyncio.CancelledError
    runnable = Runnable([{'answer': 'a'}, {'answer': 'ab'}])
    with pytest.raises(asyncio.CancelledError):
        consume(runnable, on_delta=cancelled)
    assert runnable.calls == 1 and runnable.closed and not runnable.completed


def test_final_dictionary_is_preserved_for_external_authoritative_validation():
    snapshot = {'answer': 'Texto final', 'post_keys': ['unchecked'], 'extra': {'result': 4}}
    runnable = Runnable([snapshot])
    final, deltas, degraded = consume(runnable)
    assert final == snapshot and final is not snapshot
    final['extra']['result'] = 9
    assert snapshot['extra']['result'] == 4
    assert deltas == ['Texto final'] and degraded == []
