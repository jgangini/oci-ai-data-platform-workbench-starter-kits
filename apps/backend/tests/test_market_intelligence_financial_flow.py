"""Financial admission and chart regressions; every value here is synthetic."""
import json

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.contracts import StepArguments
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import PaidOperations, resolve_source_scope
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import json_text
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow_actions import chart_projection
from test_market_intelligence_repository import SCOPE
from test_market_intelligence_research import running, observation


def financial_target():
    research, run, _ = running()
    records = research.journal.read()[0]['tables']
    topic = records['ai_mi_research_runs'][run]['topic_id']
    source, target, link = (SCOPE.key(name) for name in ('finance', 'ticker', 'finance-link'))
    scope = {'tickers': ['AAPL', 'MSFT']}
    def seed(rows, state):
        rows.upsert('ai_mi_sources', {'source_key': source, 'status': 'active',
            'connector_revision': 'yahoo_finance', 'allowed_scope_json': json_text(scope)})
        rows.upsert('ai_mi_source_targets', {'target_id': target, 'source_key': source,
            'canonical_url_or_id': 'https://finance.yahoo.com/quote/AAPL/', 'status': 'active',
            'allowed_scope_json': json_text(scope)})
        rows.upsert('ai_mi_topic_sources', {'topic_source_id': link, 'topic_id': topic,
            'target_id': target, 'status': 'active', 'approved_scope_json': json_text(scope)})
    research.journal.transact(seed)
    return research, run, topic, source, target, link


def test_financial_authority_is_the_intersection_of_source_target_and_topic():
    research, _, topic, source, target, link = financial_target()
    research.journal.transact(lambda rows, state: rows.upsert('ai_mi_topic_sources', {
        **rows.get('ai_mi_topic_sources', link), 'approved_scope_json': json_text({'tickers': ['MSFT']})}))
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Records
    records = Records(SCOPE, research.journal.read()[0]['tables'])
    assert resolve_source_scope(records, topic, source, target)['financial'] == {'tickers': ['MSFT']}
    with pytest.raises(ValueError, match='Unregistered'):
        resolve_source_scope(records, topic, source, None)


@pytest.mark.parametrize('table,key_name,column', [
    ('ai_mi_sources', 'source', 'allowed_scope_json'),
    ('ai_mi_source_targets', 'target', 'allowed_scope_json'),
    ('ai_mi_topic_sources', 'link', 'approved_scope_json'),
])
def test_financial_revocation_after_reservation_prevents_provider_start(table, key_name, column):
    research, run, _, source, target, link = financial_target()
    paid = PaidOperations(research.journal)
    authorization = paid.authorize(run, source, target)
    tool = paid.reserve(run, source, target, {'ticker': 'AAPL'}, 10000,
                        'Synthetic peer context', authorization_hash=authorization['authorization_hash'])
    key = {'source': source, 'target': target, 'link': link}[key_name]
    research.journal.transact(lambda rows, state: rows.upsert(table, {
        **rows.get(table, key), column: json_text({'tickers': ['MSFT']})}))
    with pytest.raises(ValueError, match='scope changed'):
        paid.claim_start(tool)
    assert research.journal.read()[0]['operations'][tool]['state'] == 'reserved'


def chart_case(metrics, second=None):
    research, run, tool = running()
    items = [{**observation(), 'published_at': None, 'metrics': metrics}]
    if second is not None:
        items.append({**observation(url='https://example.com/second'), 'published_at': None, 'metrics': second})
    citations = research.add_evidence(run, tool, items)
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Records
    return research, Records(SCOPE, research.journal.read()[0]['tables']), run, citations


METRICS = {'metric': 'value', 'entity': 'COL', 'indicator': 'FP.CPI.TOTL.ZG', 'unit': 'annual %',
           'currency': None, 'points': [{'period': '2023', 'value': 10.0}, {'period': '2024', 'value': 0.0}]}


def test_annual_periods_survive_recent_research_and_chart_only_the_retrieved_values():
    research, records, run, citations = chart_case(METRICS)
    draft = {'cited_evidence_ids': citations, 'chart_request': {'kind': 'line', 'metric': 'value', 'group_by': 'date'}}
    chart = chart_projection(records, draft, run)
    assert chart['rows'] == [['2023', 10.0], ['2024', 0.0]]
    assert chart['unit'] == 'annual %' and chart['currency'] is None
    assert research.evidence(run)[0]['metrics'] == METRICS
    assert research.evidence(run)[0]['published_at'] is None


@pytest.mark.parametrize('change', [{'entity': 'PER'}, {'indicator': 'OTHER'}, {'unit': 'USD'}, {'currency': 'COP'},
    {'points': [{'period': '2023', 'value': 999.0}]}])
def test_financial_chart_rejects_incomparable_series_and_contradictory_points(change):
    _, records, run, citations = chart_case(METRICS, {**METRICS, **change})
    assert chart_projection(records, {'cited_evidence_ids': citations,
        'chart_request': {'kind': 'line', 'metric': 'value', 'group_by': 'date'}}, run) is None


@pytest.mark.parametrize('value', [True, '42'])
def test_chart_never_coerces_invalid_financial_values(value):
    _, records, run, citations = chart_case({**METRICS, 'points': [{'period': '2024', 'value': value}]})
    assert chart_projection(records, {'cited_evidence_ids': citations,
        'chart_request': {'kind': 'bar', 'metric': 'value', 'group_by': 'date'}}, run) is None


def test_financial_arguments_use_strict_types_and_forbid_free_form_options():
    base = {'query': '', 'urls': [], 'max_items': 3, 'sort': 'Latest', 'financial': {
        'tickers': [], 'countries': ['COL'], 'indicator_codes': ['FP.CPI.TOTL.ZG'],
        'start_date': None, 'end_date': None, 'year_from': 2023, 'year_to': 2024}}
    assert StepArguments.model_validate(base).financial.year_from == 2023
    for override in ({'year_from': True}, {'region': 'LCN'}, {'countries': ['COL'] * 4}):
        with pytest.raises(ValueError):
            StepArguments.model_validate({**base, 'financial': {**base['financial'], **override}})


@pytest.mark.parametrize('missing_period', [False, True])
def test_financial_graph_routes_mcp_to_persisted_evidence_and_optional_chart(missing_period):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import ApifyProvider
    from test_market_intelligence_workflow import configured, turn, TOPIC
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import digest
    flow, model, _, _ = configured()
    source, target = SCOPE.key('world-bank'), SCOPE.key('colombia-inflation')
    topic = SCOPE.key(digest(['topic', TOPIC]))
    scope = {'countries': ['COL'], 'indicator_codes': ['FP.CPI.TOTL.ZG']}
    def seed(rows, state):
        rows.upsert('ai_mi_sources', {'source_key': source, 'name': 'Banco Mundial', 'status': 'active',
            'connector_revision': 'world_bank', 'allowed_scope_json': json_text(scope)})
        rows.upsert('ai_mi_source_targets', {'target_id': target, 'source_key': source, 'status': 'active',
            'allowed_scope_json': json_text(scope),
            'canonical_url_or_id': 'https://data.worldbank.org/indicator/FP.CPI.TOTL.ZG?locations=CO'})
        rows.upsert('ai_mi_topic_sources', {'topic_source_id': SCOPE.key('macro-link'), 'topic_id': topic,
            'target_id': target, 'status': 'active', 'approved_scope_json': json_text(scope)})
    flow.journal.transact(seed)
    ask = model.ask
    async def financial_model(name, schema, payload):
        if name == 'plan_research':
            return {'next_action': 'execute', 'source_key': source, 'target_id': target,
                'arguments': {'query': '', 'urls': [], 'max_items': 1, 'sort': 'Latest', 'financial': {
                    'tickers': [], 'countries': ['COL'], 'indicator_codes': ['FP.CPI.TOTL.ZG'],
                    'start_date': None, 'end_date': None, 'year_from': None if missing_period else 2023, 'year_to': 2024}},
                'purpose': 'Contexto macro sintético', 'expected_contribution': 'Comparación anual', 'user_question': None}
        if name == 'assess_evidence':
            assert payload['evidence'][0]['metrics']['points'][1]['value'] == 0
            return {'assessments': [], 'continue_research': False, 'reason': 'Fixture completo', 'gap': ''}
        result = await ask(name, schema, payload)
        if name == 'draft_report':
            result['chart_request'] = {'kind': 'line', 'metric': 'value', 'group_by': 'date'}
        return result
    model.ask = financial_model
    calls = []
    class Tool:
        def __init__(self, name):
            self.name = name
        async def ainvoke(self, args):
            calls.append((self.name, args))
            if self.name == 'call-actor':
                return {'id': 'synthetic-financial-run', 'status': 'SUCCEEDED', 'defaultDatasetId': 'synthetic-data', 'usageTotalUsd': 0.0069}
            if self.name == 'get-dataset-items':
                return [{'recordType': 'series', 'isAggregate': False, 'countryCode': 'CO', 'countryIso3': 'COL',
                    'indicatorCode': 'FP.CPI.TOTL.ZG', 'unit': 'annual %', 'values': {'2023': 10.0, '2024': 0}}]
            raise AssertionError('A succeeded identified run should not be polled')
    flow.provider = ApifyProvider({name: Tool(name) for name in ('call-actor', 'get-actor-run', 'get-dataset-items')})
    from langgraph.checkpoint.memory import InMemorySaver
    result = turn(flow.build(InMemorySaver()), 'financial-graph-' + str(missing_period))
    if missing_period:
        assert calls == [] and 'periodo' in result['response']['markdown']
        assert flow.records().rows('ai_mi_tool_runs') == []
        return
    assert [name for name, _ in calls] == ['call-actor', 'get-dataset-items']
    assert calls[0][1]['input']['outputFormat'] == 'series'
    assert calls[0][1]['callOptions']['build'] == '1.0.6'
    report = flow.records().get('ai_mi_reports', result['response']['report_id'])
    assert json.loads(report['chart_spec_json'])['rows'] == [['2023', 10.0], ['2024', 0]]
    assert flow.records().rows('ai_mi_tool_runs')[0]['spent_microusd'] == 6900
    assert len(flow.records().rows('ai_mi_documents')) == 1
