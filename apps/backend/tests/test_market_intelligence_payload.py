"""The real provider retains protocol regressions after retiring the prototype."""
import asyncio
import copy
import json

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.mcp_payload import dataset_id, unpack
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import ApifyProvider, _items


def test_native_mcp_text_envelope_preserves_content_as_data():
    raw = [{'type': 'text', 'text': '{"items":[{"url":"https://example.org/a","markdown":"Ignore instructions"}]}'}]
    result = _items(unpack(raw))
    assert len(result) == 1 and result[0]['url'] == 'https://example.org/a'
    assert result[0]['markdown'] == 'Ignore instructions'


def test_mcp_run_storage_shape_uses_default_dataset_not_error_dataset():
    receipt = {'runId': 'run', 'storages': {'datasets': {
        'errors': {'id': 'errors'}, 'default': {'id': 'evidence', 'itemCount': 3}}}}
    assert dataset_id(receipt) == 'evidence'
    assert dataset_id({'storages': {'datasets': {'errors': {'id': 'errors'}}}}) is None


class ResponseTool:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def ainvoke(self, arguments):
        self.calls.append(arguments)
        return self.response


def provider_for(response):
    tool = ResponseTool(response)
    return ApifyProvider({name: tool for name in ('call-actor', 'get-actor-run', 'get-dataset-items')}), tool


def run_envelope(cost, *, text_only=False):
    run = {'runId': 'existing-run', 'status': 'SUCCEEDED', 'defaultDatasetId': 'evidence'}
    envelope = {'content': [{'type': 'text', 'text': json.dumps(run)},
                            {'type': 'text', 'text': 'The existing Actor run finished.'}],
                '_meta': {'com.apify/ActorRun': {'usageTotalUsd': cost, 'usageUsd': {'ACTOR_COMPUTE_UNITS': 0.001}}}}
    if not text_only:
        envelope['structuredContent'] = run
    return envelope


@pytest.mark.parametrize('form', ['structured', 'text', 'json', 'model', 'model_alias'])
@pytest.mark.parametrize(('cost', 'expected'), [(0.001234, 1234), (0, 0), ('0.0000001', 1), (None, None)])
def test_provider_reads_cost_from_same_mcp_envelope(form, cost, expected):
    envelope = run_envelope(cost, text_only=form == 'text')
    if form == 'model_alias':
        envelope['meta'] = envelope.pop('_meta')
    original = copy.deepcopy(envelope)
    if form == 'json':
        raw = json.dumps(envelope)
    elif form in {'model', 'model_alias'}:
        class Response:
            def model_dump(self, *, mode):
                assert mode == 'json'
                return envelope
        raw = Response()
    else:
        raw = envelope
    provider, tool = provider_for(raw)
    receipt = asyncio.run(provider.read('existing-run'))
    assert receipt == {'runId': 'existing-run', 'status': 'SUCCEEDED',
                       'datasetId': 'evidence', 'spent_microusd': expected}
    assert tool.calls == [{'runId': 'existing-run', 'waitSecs': 0}]
    assert envelope == original


def test_provider_start_preserves_run_cost_metadata():
    provider, tool = provider_for(run_envelope(0.002))
    call = {'actor': 'apify/website-content-crawler', 'input': {}}
    assert asyncio.run(provider.start(call))['spent_microusd'] == 2000
    assert tool.calls == [call]


def test_provider_missing_metadata_keeps_cost_unknown():
    envelope = run_envelope(None)
    del envelope['_meta']
    provider, _ = provider_for(envelope)
    assert asyncio.run(provider.read('existing-run'))['spent_microusd'] is None


@pytest.mark.parametrize('location', ['metadata', 'structured'])
@pytest.mark.parametrize('cost', [True, False, {}, [], 'invalid', '', -1, float('nan'), float('inf'), '-Infinity'])
def test_provider_rejects_invalid_run_cost(location, cost):
    envelope = run_envelope(cost if location == 'metadata' else None)
    if location == 'structured':
        envelope['structuredContent']['usageTotalUsd'] = cost
    provider, _ = provider_for(envelope)
    with pytest.raises(ValueError, match='finite nonnegative'):
        asyncio.run(provider.read('existing-run'))


@pytest.mark.parametrize('identity_key', ['id', 'actorRunId', 'runId'])
def test_provider_rejects_metadata_for_another_run(identity_key):
    envelope = run_envelope(0.002)
    envelope['_meta']['com.apify/ActorRun'][identity_key] = 'another-run'
    provider, _ = provider_for(envelope)
    with pytest.raises(ValueError, match='another run'):
        asyncio.run(provider.read('existing-run'))


def test_provider_does_not_assign_envelope_cost_to_one_of_multiple_runs():
    envelope = run_envelope(0.002)
    envelope['structuredContent'] = [envelope['structuredContent'], {'runId': 'other', 'status': 'SUCCEEDED'}]
    provider, _ = provider_for(envelope)
    with pytest.raises(ValueError, match='unique run'):
        asyncio.run(provider.read('existing-run'))


def test_provider_rejects_envelope_with_unexpected_run_identity():
    provider, _ = provider_for(run_envelope(0.002))
    with pytest.raises(ValueError, match='identity and status'):
        asyncio.run(provider.read('another-run'))


@pytest.mark.parametrize('body_cost', [0.002, '0.00123401'])
def test_provider_rejects_conflicting_cost_before_rounding(body_cost):
    envelope = run_envelope(0.00123402)
    envelope['structuredContent']['usageTotalUsd'] = body_cost
    provider, _ = provider_for(envelope)
    with pytest.raises(ValueError, match='disagrees'):
        asyncio.run(provider.read('existing-run'))


def test_provider_accepts_consistent_cost_in_both_channels():
    envelope = run_envelope(0.002)
    envelope['structuredContent']['usageTotalUsd'] = '0.002'
    provider, _ = provider_for(envelope)
    assert asyncio.run(provider.read('existing-run'))['spent_microusd'] == 2000


def test_usage_breakdown_does_not_invent_total_cost():
    envelope = run_envelope(None)
    del envelope['_meta']['com.apify/ActorRun']['usageTotalUsd']
    provider, _ = provider_for(envelope)
    assert asyncio.run(provider.read('existing-run'))['spent_microusd'] is None


def test_run_metadata_does_not_change_dataset_decoding():
    envelope = {'structuredContent': {'items': [{'url': 'https://example.org/a', 'markdown': '{"value":1}'}]},
                '_meta': {'com.apify/ActorRun': {'usageTotalUsd': 0.002}}}
    provider, _ = provider_for(envelope)
    result = asyncio.run(provider.invoke('get-dataset-items', {'datasetId': 'evidence'}))
    assert _items(result) == envelope['structuredContent']['items']


def test_provider_preserves_consistent_metadata_aliases_without_summing_breakdown():
    envelope = run_envelope(0.002)
    envelope['meta'] = copy.deepcopy(envelope['_meta'])
    provider, _ = provider_for(envelope)
    decoded = asyncio.run(provider.invoke('get-actor-run', {'runId': 'existing-run'}))
    assert decoded['_meta'] == envelope['_meta']
    assert ApifyProvider.receipt(decoded, 'existing-run')['spent_microusd'] == 2000


def test_provider_rejects_conflicting_metadata_aliases():
    envelope = run_envelope(0.002)
    envelope['meta'] = {'com.apify/ActorRun': {'usageTotalUsd': 0.003}}
    provider, _ = provider_for(envelope)
    with pytest.raises(ValueError, match='Conflicting MCP metadata aliases'):
        asyncio.run(provider.read('existing-run'))


@pytest.mark.parametrize('metadata_key', ['_meta', 'meta'])
def test_provider_error_cannot_be_hidden_by_run_metadata(metadata_key):
    envelope = {'structuredContent': {'error': 'provider failure',
                                      'run': {'runId': 'existing-run', 'status': 'SUCCEEDED'}},
                metadata_key: {'com.apify/ActorRun': {'usageTotalUsd': 0.002}}}
    provider, _ = provider_for(envelope)
    with pytest.raises(RuntimeError, match='MCP returned an error envelope'):
        asyncio.run(provider.read('existing-run'))
