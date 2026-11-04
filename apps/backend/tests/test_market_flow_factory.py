import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from market_flow_factory import FrozenSnapshot, NoExternalEffects, ReplayFirstDraft, StructuredBridge, checked_json
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Conflict


def test_local_snapshot_copies_data_and_rejects_stale_writes():
    value = {'scope': {'tenant_id': 't', 'owner_id': 'o', 'generation_id': '00000000-0000-4000-8000-000000000001'},
             'tables': {}}
    snapshot = FrozenSnapshot(value)
    first, etag = snapshot.read()
    first['tables']['local'] = {}
    assert snapshot.read()[0]['tables'] == {}
    snapshot.write(first, etag)
    with pytest.raises(Conflict):
        snapshot.write(value, etag)


def test_frozen_provider_cannot_start_or_read_external_operations():
    for name in ('start', 'read', 'observations', 'credentials', 'project'):
        with pytest.raises(RuntimeError, match='cannot execute'):
            getattr(NoExternalEffects(), name)


def test_first_draft_replay_requires_exact_context_and_only_skips_the_seed_call():
    from unittest.mock import AsyncMock
    actual = SimpleNamespace(ask=AsyncMock(return_value={'accepted': True, 'issues': []}))
    payload = {'evidence': ['frozen']}
    replay = {'input_sha256': hashlib.sha256(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
              'response': {'answer_markdown': 'A captured draft'}}
    model = ReplayFirstDraft(actual, replay)
    with pytest.raises(ValueError, match='factual context'):
        asyncio.run(model.ask('draft_report', {}, {'evidence': ['different']}))
    assert not model.consumed and actual.ask.await_count == 0
    result = asyncio.run(model.ask('draft_report', {}, payload))
    result['answer_markdown'] = 'Local edit'
    assert replay['response']['answer_markdown'] == 'A captured draft'
    asyncio.run(model.ask('review_factual_support', {}, payload))
    asyncio.run(model.ask('draft_report', {}, payload))
    assert actual.ask.await_count == 2


class TrackedModel:
    def __init__(self):
        self.calls, self.received, self.finalized = 0, [], []
        self.settings = {'max_tokens': 6000, 'reasoning_effort': 'LOW',
                         'compartment_id': 'test-compartment', 'model_id': 'test-model'}
        self.client = SimpleNamespace(chat=self.chat)

    def _complete(self, system, question):
        raise AssertionError('The generic request must be replaced by the structured bridge')

    def chat(self, details, **kwargs):
        self.received.append(details)
        return SimpleNamespace(data=SimpleNamespace(chat_response={'answer': 'recorded'}))

    def _final_text(self, response):
        self.finalized.append(response)
        return json.dumps(response)

    async def complete(self, system, question):
        if self.calls >= 1:
            raise ValueError('Call limit')
        self.calls += 1
        return await asyncio.to_thread(self._complete, system, question)


def test_structured_bridge_uses_counted_call_real_request_and_final_validator():
    tracked = TrackedModel()
    bridge = StructuredBridge(tracked, {})
    schema = {'type': 'object', 'properties': {'answer': {'type': 'string'}}}
    bound = bridge.with_structured_output(schema, 'json_schema')
    messages = [SimpleNamespace(type='system', content='instruction'),
                SimpleNamespace(type='human', content='frozen content')]
    assert asyncio.run(bound.ainvoke(messages)) == {'answer': 'recorded'}
    request = tracked.received[0].chat_request
    assert request.response_format.json_schema.schema == schema
    assert request.response_format.json_schema.is_strict is True
    assert request.reasoning_effort == 'LOW' and request.max_tokens == 6000
    assert request.temperature == 0
    assert tracked.calls == 1 and tracked.finalized == [{'answer': 'recorded'}]
    with pytest.raises(ValueError, match='Call limit'):
        asyncio.run(bound.ainvoke(messages))
    assert len(tracked.received) == 1


def test_bridge_rejects_incompatible_messages_before_model_call():
    tracked = TrackedModel()
    bound = StructuredBridge(tracked, {}).with_structured_output({}, 'json_schema')
    with pytest.raises(ValueError, match='message contract'):
        asyncio.run(bound.ainvoke([SimpleNamespace(type='system', content='one')]))
    assert tracked.calls == 0


def test_bridge_retries_only_local_receipt_replace_after_model_response(tmp_path, monkeypatch):
    from pathlib import Path
    import market_model_bridge
    original = Path.replace
    failures = []
    def transient_lock(path, target):
        record = json.loads(path.read_text(encoding='utf-8'))
        if record['state'] == 'COMPLETE_RESPONSE' and len(failures) < 2:
            failures.append(record['state'])
            raise PermissionError('Concurrent Windows reader')
        return original(path, target)
    monkeypatch.setattr(Path, 'replace', transient_lock)
    monkeypatch.setattr(market_model_bridge.time, 'sleep', lambda _: None)
    tracked = TrackedModel()
    bridge = StructuredBridge(tracked, {}, observations_dir=tmp_path)
    messages = [SimpleNamespace(type='system', content='JSON only'),
                SimpleNamespace(type='human', content='frozen content')]
    result = asyncio.run(bridge.with_structured_output({}, 'json_schema').ainvoke(messages))
    saved = json.loads(next(tmp_path.glob('*.json')).read_text(encoding='utf-8'))
    assert result == {'answer': 'recorded'} and tracked.calls == 1
    assert len(failures) == 2 and saved['state'] == 'COMPLETE_RESPONSE'
    assert sum(row['state'] == 'COMPLETE_RESPONSE' for row in saved['history']) == 1


def test_bridge_persistent_receipt_lock_stops_before_model_call(tmp_path, monkeypatch):
    from pathlib import Path
    import market_model_bridge
    attempts = []
    def locked(path, target):
        attempts.append(path)
        raise PermissionError('Persistent receipt lock')
    monkeypatch.setattr(Path, 'replace', locked)
    monkeypatch.setattr(market_model_bridge.time, 'sleep', lambda _: None)
    tracked = TrackedModel()
    bridge = StructuredBridge(tracked, {}, observations_dir=tmp_path)
    messages = [SimpleNamespace(type='system', content='JSON only'),
                SimpleNamespace(type='human', content='frozen content')]
    with pytest.raises(PermissionError):
        asyncio.run(bridge.with_structured_output({}, 'json_schema').ainvoke(messages))
    assert tracked.calls == 0 and len(attempts) == 10


def test_local_bridge_preserves_complete_output_before_downstream_validation(tmp_path):
    tracked = TrackedModel()
    bound = StructuredBridge(tracked, {}, observations_dir=tmp_path).with_structured_output({}, 'json_schema')
    messages = [SimpleNamespace(type='system', content='instructions are not recorded'),
                SimpleNamespace(type='human', content='frozen evidence is hashed')]
    assert asyncio.run(bound.ainvoke(messages)) == {'answer': 'recorded'}
    files = list(tmp_path.glob('*.json'))
    assert len(files) == 1
    receipt = json.loads(files[0].read_text(encoding='utf-8'))
    assert receipt['input_sha256'] == hashlib.sha256(messages[1].content.encode()).hexdigest()
    assert receipt['schema_sha256'] == hashlib.sha256(b'{}').hexdigest()
    assert receipt['response'] == '{"answer": "recorded"}'
    assert receipt['state'] == 'COMPLETE_RESPONSE'
    assert [item['state'] for item in receipt['history']] == [
        'CALL_ADMITTED', 'HTTP_SUBMISSION_INTENT', 'HTTP_RESPONSE_RECEIVED', 'COMPLETE_RESPONSE']
    assert len(receipt['client_request_id']) == 32 and receipt['worker_pid'] > 0
    assert set(receipt) == {'input_sha256', 'schema_sha256', 'response', 'state', 'history',
                            'client_request_id', 'worker_pid', 'provider_request_id'}
    assert tracked.calls == 1


def test_wrapped_sdk_failure_keeps_safe_cause_metadata_without_error_text_or_retry(tmp_path):
    tracked = TrackedModel()
    class ProviderFailure(Exception):
        status = 400
    async def wrapped(system, question):
        tracked.calls += 1
        try:
            raise ProviderFailure('synthetic secret token and private evidence')
        except ProviderFailure:
            raise RuntimeError('Generic wrapper hides the cause') from None
    tracked.complete = wrapped
    bound = StructuredBridge(tracked, {}, observations_dir=tmp_path).with_structured_output({}, 'json_schema')
    messages = [SimpleNamespace(type='system', content='instructions'),
                SimpleNamespace(type='human', content='frozen content')]
    with pytest.raises(RuntimeError):
        asyncio.run(bound.ainvoke(messages))
    raw = next(tmp_path.glob('*.json')).read_text(encoding='utf-8')
    receipt = json.loads(raw)
    assert receipt['error_chain'] == ['RuntimeError', 'ProviderFailure'] and receipt['provider_status'] == 400
    assert receipt['state'] == 'FAILED_UNKNOWN_USAGE' and tracked.calls == 1
    assert 'synthetic secret' not in raw and 'Generic wrapper' not in raw


def test_submission_intent_survives_local_termination_without_a_false_completion(tmp_path):
    class SimulatedTermination(BaseException):
        pass
    tracked = TrackedModel()
    submitted = []

    def terminate(details, **kwargs):
        submitted.append(kwargs['opc_request_id'])
        raise SimulatedTermination()

    tracked.client.chat = terminate
    bound = StructuredBridge(tracked, {}, observations_dir=tmp_path).with_structured_output({}, 'json_schema')
    messages = [SimpleNamespace(type='system', content='instructions'),
                SimpleNamespace(type='human', content='frozen content')]
    with pytest.raises(SimulatedTermination):
        asyncio.run(bound.ainvoke(messages))
    receipt = json.loads(next(tmp_path.glob('*.json')).read_text(encoding='utf-8'))
    assert receipt['state'] == 'HTTP_SUBMISSION_INTENT' and 'response' not in receipt
    assert submitted == [receipt['client_request_id']] and tracked.calls == 1


def test_local_bridge_keeps_invalid_json_for_offline_diagnosis_without_retry(tmp_path):
    tracked = TrackedModel()
    tracked._final_text = lambda response: 'not JSON'
    bound = StructuredBridge(tracked, {}, observations_dir=tmp_path).with_structured_output({}, 'json_schema')
    messages = [SimpleNamespace(type='system', content='instructions'),
                SimpleNamespace(type='human', content='frozen content')]
    with pytest.raises(json.JSONDecodeError):
        asyncio.run(bound.ainvoke(messages))
    receipt = json.loads(next(tmp_path.glob('*.json')).read_text(encoding='utf-8'))
    assert receipt['response'] == 'not JSON'
    assert tracked.calls == len(tracked.received) == 1


def test_changed_binding_is_rejected_before_it_can_select_a_model_or_identity(tmp_path):
    path = tmp_path / 'binding.json'
    path.write_text('{"model_endpoint": "https://example.invalid/original"}', encoding='utf-8')
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert checked_json(path, expected)['model_endpoint'].endswith('/original')
    path.write_text('{"model_endpoint": "https://example.invalid/changed"}', encoding='utf-8')
    with pytest.raises(ValueError, match='Frozen evaluation input changed'):
        checked_json(path, expected)
