"""A failed MCP status read cannot become a provider receipt or a new start."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import (
    ProviderPending, ProviderReceiptUnverified, ProviderStartRejected, ProviderStatusUnavailable)
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import ApifyProvider, ProviderMCPError


RUN = {'runId': 'existing-run', 'status': 'SUCCEEDED', 'defaultDatasetId': 'existing-dataset', 'usageTotalUsd': 0.002,
       'finishedAt': '2026-01-01T00:00:00Z'}


class Tool:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def ainvoke(self, arguments):
        self.calls.append(arguments)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def provider(result):
    tools = {name: Tool(result) for name in ('call-actor', 'get-actor-run', 'get-dataset-items')}
    return ApifyProvider(tools), tools


def envelope(payload, kind):
    if kind == 'raw':
        return payload
    if kind == 'json':
        return json.dumps(payload)
    if kind == 'text':
        return {'content': [{'type': 'text', 'text': json.dumps(payload)}], 'isError': False}
    if kind == 'structured':
        return {'structuredContent': payload, '_meta': {'com.apify/ActorRun': {'runId': 'existing-run', 'usageTotalUsd': 0.002}}}
    if kind == 'model':
        class MCPResult:
            isError = False
            def model_dump(self, **kwargs):
                return {'structuredContent': payload, 'meta': {}}
        return MCPResult()
    raise AssertionError(kind)


@pytest.mark.parametrize('error', ['', {}, False, 0, 'secret provider diagnostic'])
@pytest.mark.parametrize('kind', ['raw', 'json', 'text', 'structured', 'model'])
def test_error_envelope_is_specific_pending_status_without_inventing_receipt_or_cost(error, kind):
    client, tools = provider(envelope({'error': error}, kind))
    with pytest.raises(ProviderStatusUnavailable) as raised:
        asyncio.run(client.read('existing-run'))
    assert isinstance(raised.value, ProviderPending)
    assert 'secret provider diagnostic' not in str(raised.value)
    assert tools['get-actor-run'].calls == [{'runId': 'existing-run', 'waitSecs': 0}]
    assert not tools['call-actor'].calls and not tools['get-dataset-items'].calls


@pytest.mark.parametrize('kind', ['raw', 'json', 'text', 'structured', 'model'])
def test_none_error_preserves_verified_receipt_and_cost(kind):
    client, _ = provider(envelope({'error': None, 'run': RUN}, kind))
    assert asyncio.run(client.read('existing-run')) == {
        'runId': 'existing-run', 'status': 'SUCCEEDED', 'datasetId': 'existing-dataset', 'spent_microusd': 2000}


@pytest.mark.parametrize('error', ['', {}, False, 'failure'])
def test_error_wins_even_with_an_apparently_valid_run_and_usage_metadata(error):
    client, _ = provider(envelope({'error': error, 'run': RUN}, 'structured'))
    with pytest.raises(ProviderStatusUnavailable):
        asyncio.run(client.read('existing-run'))


@pytest.mark.parametrize('kind', ['raw', 'structured', 'text'])
@pytest.mark.parametrize('payload', [{'runId': 'another-run', 'status': 'SUCCEEDED'},
    {'runId': 'existing-run'}, {'runId': 'existing-run', 'status': 'UNKNOWN'},
    {'runId': 'existing-run', 'status': {}}, {'runId': 'existing-run', 'status': []},
    {'runId': 'existing-run', 'status': False},
    [{'runId': 'existing-run'}, {'status': 'SUCCEEDED'}]])
def test_unverified_identity_or_status_remains_strict_validation_error(payload, kind):
    client, tools = provider(envelope(payload, kind))
    with pytest.raises(ValueError):
        asyncio.run(client.read('existing-run'))
    assert tools['get-actor-run'].calls == [{'runId': 'existing-run', 'waitSecs': 0}]
    assert not tools['call-actor'].calls and not tools['get-dataset-items'].calls


@pytest.mark.parametrize('error', [ValueError('programming failure'), RuntimeError('transport failure'), PermissionError('authorization failure')])
def test_tool_exceptions_are_not_reclassified_as_mcp_envelope_status(error):
    client, _ = provider(error)
    with pytest.raises(type(error)) as raised:
        asyncio.run(client.read('existing-run'))
    assert raised.value is error
    assert not isinstance(raised.value, ProviderStatusUnavailable)


@pytest.mark.parametrize('raw', [{'isError': True}, SimpleNamespace(isError=True)])
def test_existing_is_error_contract_is_preserved(raw):
    client, _ = provider(raw)
    with pytest.raises(ProviderStatusUnavailable):
        asyncio.run(client.read('existing-run'))


def test_blank_start_error_is_not_confirmed_build_rejection():
    client, tools = provider({'error': ''})
    with pytest.raises(ProviderMCPError) as raised:
        asyncio.run(client.start({'actor': 'reviewed/actor', 'callOptions': {'build': '1.0.0'}}))
    assert not isinstance(raised.value, ProviderStartRejected)
    assert len(tools['call-actor'].calls) == 1
    assert not tools['get-actor-run'].calls


def test_blank_dataset_error_cannot_produce_observations_or_cost():
    client, tools = provider({'content': [{'type': 'text', 'text': '{"error":""}'}]})
    with pytest.raises(ProviderMCPError):
        asyncio.run(client.observations({'runId': 'existing-run', 'status': 'SUCCEEDED', 'datasetId': 'existing-dataset'},
            {'actor': 'apidojo/twitter-scraper-lite', 'callOptions': {'maxItems': 3}}))
    assert len(tools['get-dataset-items'].calls) == 1
    assert not tools['call-actor'].calls and not tools['get-actor-run'].calls


def test_receipt_unverified_exception_is_pending_without_masking_provider_read_validation():
    assert issubclass(ProviderReceiptUnverified, ProviderPending)
    assert not issubclass(ProviderReceiptUnverified, ProviderStatusUnavailable)


@pytest.mark.parametrize('error', ['', {}, False])
@pytest.mark.parametrize('wrapped', [False, True])
def test_multipart_protocol_error_cannot_hide_next_to_a_valid_receipt(error, wrapped):
    blocks = [{'type': 'text', 'text': json.dumps({'error': error})},
              {'type': 'text', 'text': json.dumps(RUN)}]
    raw = {'content': blocks, 'isError': False} if wrapped else blocks
    client, tools = provider(raw)
    with pytest.raises(ProviderStatusUnavailable):
        asyncio.run(client.read('existing-run'))
    assert not tools['call-actor'].calls


def test_json_encoded_nested_protocol_containers_preserve_error_detection():
    raw = json.dumps({'content': [{'type': 'text', 'text': json.dumps({'content': [
        {'type': 'text', 'text': '{"error":""}'}, {'type': 'text', 'text': json.dumps(RUN)}]})}]})
    client, _ = provider(raw)
    with pytest.raises(ProviderStatusUnavailable):
        asyncio.run(client.read('existing-run'))


@pytest.mark.parametrize('kind', ['items', 'structured', 'text', 'raw_list'])
def test_dataset_row_error_fields_are_data_not_protocol_envelopes(kind):
    rows = [{'url': 'https://example.com/page', 'error': False, 'markdown': 'Contenido'}]
    payload = {'items': rows}
    raw = (payload if kind == 'items' else rows if kind == 'raw_list' else {'structuredContent': rows} if kind == 'structured'
           else {'content': [{'type': 'text', 'text': json.dumps(payload)}]})
    client, _ = provider(raw)
    expected = rows if kind in ('structured', 'raw_list') else payload
    assert asyncio.run(client.invoke('get-dataset-items', {})) == expected


def test_multipart_text_error_is_detected_even_with_another_content_type():
    client, _ = provider({'content': [{'type': 'image', 'data': 'fixture', 'mimeType': 'image/png'},
        {'type': 'text', 'text': '{"error":""}'}, {'type': 'text', 'text': json.dumps(RUN)}]})
    with pytest.raises(ProviderStatusUnavailable):
        asyncio.run(client.read('existing-run'))


@pytest.mark.parametrize('error', ['', 'failure', {}, False])
@pytest.mark.parametrize('kind', ['siblings', 'nested', 'model'])
def test_structured_receipt_cannot_hide_an_error_in_sibling_or_nested_protocol_content(error, kind):
    blocks = [{'type': 'text', 'text': json.dumps({'error': error})}]
    raw = ({'structuredContent': {'content': blocks, 'run': RUN}} if kind == 'nested' else
           {'structuredContent': RUN, 'content': blocks, 'isError': False})
    if kind == 'model':
        class Result:
            def model_dump(self, **kwargs):
                return raw
        value = Result()
    else:
        value = raw
    client, _ = provider(value)
    with pytest.raises(ProviderStatusUnavailable):
        asyncio.run(client.read('existing-run'))
