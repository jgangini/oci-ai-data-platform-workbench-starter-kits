"""Missing dataset rows disclose missing coverage without inventing HTTP outcomes."""
import asyncio
import copy
import json
import re

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import ObservationResult
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import json_text
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow_actions import (
    provider_limitations, retrieval_page_context)
from test_market_intelligence_localized_flow import localized_graph, QUESTION
from test_market_intelligence_workflow import configured, turn, SCOPE, TOPIC


URLS = ['https://example.com/one', 'https://example.com/two', 'https://example.com/three']
EMPTY = {'version': 1, 'total_rows': 0, 'accepted_rows': 0, 'failed_pages': []}


def tool_row(coverage=None, urls=None, *, run='synthetic-run'):
    purpose = {'version': 1, 'purpose': 'Leer páginas seleccionadas'}
    if urls is not None:
        purpose['requested_urls'] = list(urls)
    receipt = {'runId': 'existing-provider', 'status': 'SUCCEEDED', 'usageTotalUsd': 0.001}
    if coverage is not None:
        receipt['retrieval_coverage'] = copy.deepcopy(coverage)
    return {'run_id': run, 'source_key': SCOPE.key('web'), 'state': 'succeeded',
            'step_purpose_json': json_text(purpose), 'provider_receipt_json': json_text(receipt)}


def test_empty_dataset_exposes_exact_requested_urls_without_false_http_or_received_rows():
    row = tool_row(EMPTY, URLS[:2])
    before = copy.deepcopy(row)
    assert retrieval_page_context(row) == {'source_key': SCOPE.key('web'), 'total_rows': 0,
        'accepted_rows': 0, 'failed_reasons': {}, 'requested_pages': 2, 'unreturned_pages': 2,
        'unreturned_urls': URLS[:2]}
    assert row == before
    assert json.loads(row['provider_receipt_json'])['retrieval_coverage'] == EMPTY


def test_partial_batch_reports_only_missing_count_not_an_unproved_page_identity():
    coverage = {'version': 1, 'total_rows': 2, 'accepted_rows': 1, 'failed_pages': [
        {'url': URLS[1], 'reason': 'http_error', 'http_status': 404}]}
    row = tool_row(coverage, URLS)
    context = retrieval_page_context(row)
    assert context == {'source_key': SCOPE.key('web'), 'total_rows': 2, 'accepted_rows': 1,
        'failed_reasons': {'http_error': 1}, 'requested_pages': 3, 'unreturned_pages': 1}
    assert 'unreturned_urls' not in context
    assert context['failed_reasons'] == {'http_error': 1}
    assert json.loads(row['provider_receipt_json'])['retrieval_coverage'] == coverage


def test_all_returned_rows_never_create_a_negative_missing_count_or_missing_identities():
    coverage = {'version': 1, 'total_rows': 2, 'accepted_rows': 2, 'failed_pages': []}
    context = retrieval_page_context(tool_row(coverage, URLS[:2]))
    assert context['requested_pages'] == 2 and context['unreturned_pages'] == 0
    assert 'unreturned_urls' not in context and not context['failed_reasons']


def test_legacy_rag_coverage_without_requested_pages_does_not_claim_page_cardinality():
    row = tool_row(EMPTY)
    assert retrieval_page_context(row) == {'source_key': SCOPE.key('web'), 'total_rows': 0,
                                         'accepted_rows': 0, 'failed_reasons': {}}
    assert 'requested_urls' not in json.loads(row['step_purpose_json'])
    assert retrieval_page_context(tool_row()) is None


def test_empty_discovery_explains_how_to_continue_without_model_or_external_effect():
    flow, model, provider, _ = configured()
    question, topic = flow.research.record_question('empty-discovery', 'Qué se dice de D1 en Colombia', TOPIC)
    from test_market_intelligence_research import WINDOW
    run = flow.research.prepare_run(question, WINDOW, 'Comprobar menciones', 100000)
    before = flow.journal.read()[0]
    result = asyncio.run(flow.draft_report({'run_id': run, 'question_id': question,
        'topic_id': topic, 'reason': 'no_data', 'catalog': [], 'intake': {'topic': TOPIC}, 'window': WINDOW}))
    text = result['draft']['answer_markdown']
    assert 'No se obtuvo evidencia verificable' in text and 'no demuestra que el problema no exista' in text
    assert 'Puedes compartir una publicación o una URL pública' in text
    assert 'otro periodo para una nueva consulta' in text
    assert not provider.starts and not model.calls
    after = flow.journal.read()[0]
    assert after['operations'] == before['operations']
    assert {key: len(rows) for key, rows in after['tables'].items()} == {
        key: len(rows) for key, rows in before['tables'].items()}
    updated = after['tables']['ai_mi_research_runs'][run]
    assert updated['comparison_context_json'] is not None
    after['tables']['ai_mi_research_runs'][run]['comparison_context_json'] = before['tables']['ai_mi_research_runs'][run]['comparison_context_json']
    assert after['tables'] == before['tables']


def saved_tools():
    flow, _, _, _ = configured()
    original, _ = flow.research.record_question('first-research', 'Novedades de D1', TOPIC)
    second, _ = flow.research.record_question('second-research', 'Otra investigación de D1', TOPIC)
    from test_market_intelligence_research import WINDOW
    run = flow.research.prepare_run(original, WINDOW, 'Primera investigación', 100000)
    other = flow.research.prepare_run(second, WINDOW, 'Segunda investigación', 100000)
    def seed(records, state):
        for index, current in enumerate((run, other)):
            row = tool_row(EMPTY, URLS[:2] if current == run else URLS, run=current)
            receipt = json.loads(row['provider_receipt_json'])
            receipt['runId'] = 'existing-provider-' + str(index)
            records.upsert('ai_mi_tool_runs', {**row, 'tool_run_id': SCOPE.key('unreturned-' + str(index)),
                'provider_run_id': receipt['runId'], 'provider_receipt_json': json_text(receipt)})
    flow.journal.transact(seed)
    return flow, run, other


def test_model_context_and_notices_are_scoped_and_do_not_mutate_durable_receipts():
    flow, run, other = saved_tools()
    before = flow.journal.read()[0]
    first = flow.retrieval_context(run)
    second = flow.retrieval_context(other)
    assert len(first) == len(second) == 1
    assert first[0]['requested_pages'] == first[0]['unreturned_pages'] == 2
    assert second[0]['requested_pages'] == second[0]['unreturned_pages'] == 3
    assert first[0]['unreturned_urls'] == URLS[:2] and second[0]['unreturned_urls'] == URLS
    assert provider_limitations(flow.records(), SCOPE.key('unrelated-run')) == []
    notices = provider_limitations(flow.records(), run)
    assert notices and '2' in '\n'.join(notices)
    text = '\n'.join(notices).lower()
    assert not re.search(r'\b(?:403|404|500)\b|http|robots|captcha|bloquead', text)
    assert re.search(r'(?:aporta|facilita|compart|elige|revisa|usa|consulta|lee|prueba)\w*[^.]*'
                     r'(?:contenido|texto|url|enlace|página|fuente)', text)
    assert flow.journal.read()[0] == before


def test_two_equal_empty_batches_are_counted_without_inventing_unique_missing_pages():
    flow, run, _ = saved_tools()
    def duplicate(records, state):
        row = next(row for row in records.rows('ai_mi_tool_runs') if row['run_id'] == run)
        records.upsert('ai_mi_tool_runs', {**row, 'tool_run_id': SCOPE.key('second-empty-batch')})
    flow.journal.transact(duplicate)
    contexts = flow.retrieval_context(run)
    assert len(contexts) == 2 and sum(row['unreturned_pages'] for row in contexts) == 4
    assert all(row['failed_reasons'] == {} for row in contexts)
    # Four attempted page slots may repeat the same two URLs; no unique-site claim.
    assert len({url for row in contexts for url in row['unreturned_urls']}) == 2


def test_real_graph_carries_unreturned_pages_into_plan_assessment_report_and_replay(monkeypatch):
    flow, model, provider, graph, _ = localized_graph(monkeypatch)
    async def verify_saved(current):
        assert current.tables == flow.records().tables
    flow.project = verify_saved
    original_ask = model.ask
    async def close_after_unreturned(name, schema, payload):
        if name == 'plan_research' and any(row.get('unreturned_pages') for row in payload.get('retrieval_coverage', [])):
            model.calls.append((name, copy.deepcopy(payload)))
            return {'next_action': 'close', 'source_key': None, 'target_id': None, 'arguments': None,
                    'purpose': 'Entregar el parcial con la cobertura faltante',
                    'expected_contribution': '', 'user_question': None}
        return await original_ask(name, schema, payload)
    model.ask = close_after_unreturned
    original = provider.observations
    async def empty_reader(receipt, call, *, allow_partial_pages=False):
        if call['actor'] == 'apify/website-content-crawler':
            assert allow_partial_pages is True
            provider.read_flags.append((call['actor'], allow_partial_pages))
            return ObservationResult([], receipt['spent_microusd'], copy.deepcopy(EMPTY))
        return await original(receipt, call, allow_partial_pages=allow_partial_pages)
    provider.observations = empty_reader
    result = turn(graph, 'zero-row-page-coverage', QUESTION)
    records = flow.records()
    assert [call['actor'] for call in provider.starts] == ['scraperlink/google-search-results-serp-scraper', 'apify/website-content-crawler']
    assert len(flow.research.evidence(result['run_id'])) == 3
    report = records.get('ai_mi_reports', result['response']['report_id'])
    assert json.loads(report['coverage_json'])['conclusion']['status'] == 'unresolved'
    saved = json.loads(report['coverage_json'])['retrieval_coverage']
    assert len(saved) == 1 and saved[0]['requested_pages'] == saved[0]['unreturned_pages'] == 1
    assert saved[0]['total_rows'] == saved[0]['accepted_rows'] == 0 and saved[0]['failed_reasons'] == {}
    for name in ('plan_research', 'assess_evidence', 'draft_report'):
        relevant = [payload['retrieval_coverage'] for called, payload in model.calls
                    if called == name and payload.get('retrieval_coverage')]
        assert relevant and any(rows[0]['unreturned_pages'] == 1 for rows in relevant)
    assert result['response']['references'] == []
    assert not re.search(r'\b(?:403|404|500)\b|HTTP', report['markdown'])
    before = flow.journal.read()[0]
    starts, model_calls = len(provider.starts), len(model.calls)
    again = turn(graph, 'zero-row-page-coverage', QUESTION)
    assert again['response'] == result['response']
    assert flow.journal.read()[0] == before
    assert len(provider.starts) == starts and len(model.calls) == model_calls
