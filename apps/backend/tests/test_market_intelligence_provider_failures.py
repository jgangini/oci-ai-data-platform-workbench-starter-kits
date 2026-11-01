"""Provider failures must remain visible without paying to retry known defects."""
import asyncio

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import (
    ConnectorUnavailable, PROFILES, compile_call,
)
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import PaidOperations
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import json_text
from test_market_intelligence_financial_flow import financial_target
from test_market_intelligence_workflow import configured, turn


def test_known_incompatible_yahoo_build_cannot_compile_or_reserve():
    research, run, _, source, target, _ = financial_target()
    with pytest.raises(ConnectorUnavailable, match='origen MCP'):
        compile_call('yahoo_finance', {}, {}, 50000)
    paid = PaidOperations(research.journal)
    scope = paid.authorize(run, source, target)
    before = research.journal.read()[0]
    with pytest.raises(ConnectorUnavailable, match='origen MCP'):
        paid.reserve(run, source, target, {}, 10000, 'Historical prices',
                     authorization_hash=scope['authorization_hash'])
    assert research.journal.read()[0] == before


def test_connector_disabled_after_reservation_cannot_start(monkeypatch):
    research, run, _, source, target, _ = financial_target()
    with monkeypatch.context() as compatible:
        compatible.delitem(PROFILES['yahoo_finance'], 'unavailable_reason')
        paid = PaidOperations(research.journal)
        scope = paid.authorize(run, source, target)
        tool = paid.reserve(run, source, target, {}, 10000, 'Synthetic compatible build',
                            authorization_hash=scope['authorization_hash'])
    with pytest.raises(ConnectorUnavailable):
        paid.claim_start(tool)
    value, _ = research.journal.read()
    assert value['operations'][tool]['state'] == 'reserved'
    assert not value['operations'][tool].get('permit')


def test_resumed_reserved_step_explains_disabled_connector(monkeypatch):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow import MarketWorkflow
    research, run, _, source, target, _ = financial_target()
    with monkeypatch.context() as compatible:
        compatible.delitem(PROFILES['yahoo_finance'], 'unavailable_reason')
        paid = PaidOperations(research.journal)
        scope = paid.authorize(run, source, target)
        tool = paid.reserve(run, source, target, {}, 10000, 'Synthetic compatible build',
                            authorization_hash=scope['authorization_hash'])
    class NoCallProvider:
        async def start(self, call):
            pytest.fail('A disabled connector must not start')
    flow = MarketWorkflow(research.journal, None, NoCallProvider())
    result = asyncio.run(flow.execute_step({'tool_id': tool, 'call': {}}))
    assert result['reason'] == 'unresolved'
    assert 'origen MCP' in result['connector_issue']
    assert research.journal.read()[0]['operations'][tool]['state'] == 'reserved'


def test_graph_closes_disabled_financial_source_with_no_provider_call():
    flow, model, provider, graph = configured()
    def seed(rows, state):
        for row in rows.rows('ai_mi_sources'):
            if row['name'] == 'x':
                rows.upsert('ai_mi_sources', {**row, 'status': 'inactive'})
            else:
                rows.upsert('ai_mi_sources', {**row, 'connector_revision': 'yahoo_finance',
                    'allowed_scope_json': json_text({'tickers': ['AAPL']})})
        for row in rows.rows('ai_mi_source_targets'):
            rows.upsert('ai_mi_source_targets', {**row, 'allowed_scope_json': json_text({'tickers': ['AAPL']})})
        for row in rows.rows('ai_mi_topic_sources'):
            rows.upsert('ai_mi_topic_sources', {**row, 'approved_scope_json': json_text({'tickers': ['AAPL']})})
    flow.journal.transact(seed)
    original = model.ask
    async def select_disabled(name, schema, payload):
        if name == 'plan_research':
            source = payload['catalog'][0]
            return {'next_action': 'execute', 'source_key': source['source_key'],
                'target_id': source['targets'][0]['target_id'], 'arguments': {
                    'query': '', 'urls': [], 'max_items': 1, 'sort': 'Latest'},
                'purpose': 'Synthetic financial research', 'expected_contribution': 'Series', 'user_question': None}
        return await original(name, schema, payload)
    model.ask = select_disabled
    result = turn(graph, 'disabled-finance')
    assert 'Yahoo Finance está suspendido' in result['response']['markdown']
    assert not provider.starts
    assert flow.records().rows('ai_mi_tool_runs') == []
    assert flow.records().rows('ai_mi_research_runs')[0]['termination_reason'] == 'unresolved'


@pytest.mark.parametrize('provider_status, phrase', [
    ('FAILED', 'El proveedor falló'), ('ABORTED', 'La extracción se interrumpió'),
    ('TIMED-OUT', 'La extracción agotó su tiempo'),
])
def test_failed_provider_is_not_reported_as_successful_empty_search(provider_status, phrase):
    flow, _, provider, graph = configured()
    async def failed_read(run_id):
        return {'runId': run_id, 'status': provider_status, 'spent_microusd': 50}
    async def failed_start(call):
        provider.starts.append(call)
        return {'runId': 'synthetic-failed', 'status': provider_status}
    provider.start, provider.read = failed_start, failed_read
    result = turn(graph, 'failed-source-' + provider_status)
    report = flow.records().rows('ai_mi_reports')[0]
    assert phrase in result['response']['markdown']
    assert 'no equivale a una búsqueda sin resultados' in result['response']['markdown']
    assert phrase in report['limitations_json']
    assert result['reason'] == 'unresolved'
    assert len(provider.starts) == 1
    assert flow.records().rows('ai_mi_documents') == []


def test_partial_report_keeps_failed_source_limit_even_if_model_omits_it():
    flow, _, _, graph = configured()
    result = turn(graph, 'partial-technical-failure')
    records = flow.records()
    tool = records.rows('ai_mi_tool_runs')[0]
    flow.journal.transact(lambda rows, state: rows.upsert('ai_mi_tool_runs', {**tool, 'state': 'failed'}))
    # Use the existing verified evidence to exercise partial report composition.
    draft = asyncio.run(flow.draft_report({**result, 'assessment': {'gap': ''}}))['draft']
    assert 'El proveedor falló' in draft['answer_markdown']
    assert any('El proveedor falló' in value for value in draft['limitations'])
    assert draft['cited_evidence_ids']


def test_financial_provider_rejects_more_rows_than_the_admitted_call():
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import ApifyProvider
    class Tool:
        async def ainvoke(self, args):
            return [{'type': 'candle'}, {'type': 'candle'}]
    provider = ApifyProvider({name: Tool() for name in ('call-actor', 'get-actor-run', 'get-dataset-items')})
    with pytest.raises(ValueError, match='admitted row limit'):
        asyncio.run(provider.observations({'runId': 'synthetic', 'status': 'SUCCEEDED', 'datasetId': 'synthetic'},
            {'actor': 'memo23/yahoo-finance-scraper', 'callOptions': {'maxItems': 1}}))


@pytest.mark.parametrize('error', [ValueError('untrusted period'), RuntimeError('untrusted transport body')])
@pytest.mark.parametrize('retain_verified', [False, True])
def test_invalid_dataset_closes_with_coverage_limit_without_repeating_or_discarding_verified_evidence(error, retain_verified):
    flow, model, provider, graph = configured()
    original = provider.observations
    async def invalid_result(receipt, call):
        if retain_verified and len(provider.starts) == 1:
            return await original(receipt, call)
        raise error
    provider.observations = invalid_result
    result = turn(graph, 'invalid-provider-' + str(retain_verified) + type(error).__name__)
    assert 'No se pudo validar la salida del proveedor' in result['response']['markdown']
    assert 'untrusted' not in result['response']['markdown']
    assert len(provider.starts) == (2 if retain_verified else 1)
    assert len(flow.records().rows('ai_mi_documents')) == int(retain_verified)
    assert len(flow.records().rows('ai_mi_reports')) == 1
    assert result['reason'] == 'unresolved'
