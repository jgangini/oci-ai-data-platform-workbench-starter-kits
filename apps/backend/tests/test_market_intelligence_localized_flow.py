"""Real graph + ledger + provider normalization with synthetic search/page datasets."""
import asyncio
import copy
import json
from datetime import datetime, timezone
from functools import partial

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import execution, research
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.model import context_limitations, evidence_context
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Records, json_text, projection_checks
from test_market_intelligence_scraperlink import LOCALE, WINDOW, publisher_item, result_row
from test_market_intelligence_payload import provider_for
from test_market_intelligence_workflow import configured, turn


QUESTION = 'Investiga en la web las aperturas de Tiendas D1 en Colombia de los últimos siete días.'
TOPIC = {'entity': 'D1', 'market': 'Colombia', 'objective': 'Aperturas comprobadas'}
URLS = ['https://example.com/noticia', 'https://example.com/segunda', 'https://example.com/tercera']
PAGE = 'La tienda de la ciudad de prueba comenzó a operar el 30 de octubre de 2026. Fuente sintética.'
BASE_URLS = ['https://example.com/base-a', 'https://example.com/base-b']


def registered_base(flow, topic):
    reader = flow.catalog.ensure_website_reader()
    _, topic_id = flow.research.record_question('seed-base-question', 'Pregunta administrativa de la base', topic, 'administration')
    def seed(rows, state):
        for index, url in enumerate(BASE_URLS):
            target = flow.journal.scope.key('localized-base-' + str(index))
            rows.upsert('ai_mi_source_targets', {'target_id': target, 'source_key': reader,
                'canonical_url_or_id': url, 'status': 'active'})
            rows.upsert('ai_mi_topic_sources', {'topic_source_id': flow.journal.scope.key('localized-base-link-' + str(index)),
                'topic_id': topic_id, 'target_id': target, 'status': 'active', 'approved_scope_json': json_text({'urls': [url]})})
    flow.journal.transact(seed)
    return reader, topic_id


def synthetic_localized_intake(payload, base_targets):
    web = next(row['source_key'] for row in payload['sources'] if row['name'] == 'Búsqueda web')
    return {'intent': 'research', 'canonical_question': payload['question'], 'topic': TOPIC,
        'window_spec': 'last_7_days', 'search_context': LOCALE, 'missing_fields': [],
        'user_question': None, 'selected_source_key': None if base_targets else web, 'clarification_action': None}


def synthetic_localized_plan(payload, base_targets):
    base_first = base_targets and not payload['evidence']
    has_search = any(row['provenance']['source'] == 'scraperlink/google-search-results-serp-scraper' for row in payload['evidence'])
    profile = 'website' if base_first or has_search else 'web_search_serp'
    source = next(row for row in payload['catalog'] if row['profile_id'] == profile)
    url = BASE_URLS[0] if base_first else URLS[0]
    target = next(target['target_id'] for target in source['targets'] if target['canonical_url_or_id'] == url) if base_first else None
    arguments = {'query': '' if profile == 'website' else 'Tiendas D1 Colombia nuevas aperturas',
        'urls': [url] if profile == 'website' else [], 'max_items': 1 if profile == 'website' else 10,
        'sort': 'Latest'}
    return {'next_action': 'execute', 'source_key': source['source_key'], 'target_id': target,
        'arguments': arguments, 'purpose': 'Leer una noticia de apertura' if profile == 'website' else 'Descubrir noticias',
        'expected_contribution': 'Distinguir el contenido de la página de su extracto', 'user_question': None}


def synthetic_localized_assessment(payload):
    pages = [row for row in payload['evidence'] if row['provenance']['source'] == 'apify/website-content-crawler'
             and row['url'] == URLS[0]]
    return {'assessments': [{'evidence_id': row['evidence_id'],
        'stance': 'supports' if row in pages else 'context', 'reason': 'Página leída' if row in pages else 'Solo metadatos del buscador',
        'independence': 'known'} for row in payload['evidence']],
        'continue_research': not pages, 'reason': 'Leer antes de concluir' if not pages else 'Página disponible',
        'gap': 'Falta leer una página' if not pages else ''}


def synthetic_localized_draft(payload):
    page = next((row for row in payload['evidence'] if row['provenance']['source'] == 'apify/website-content-crawler'
                 and PAGE in row['text']), None)
    if page is None:
        return {'answer_markdown': 'Sólo hay metadatos; no pude confirmar una apertura.',
            'cited_evidence_ids': [], 'findings': [], 'limitations': ['Falta contenido de la página.'],
            'chart_request': None, 'decision': {'status': 'unresolved', 'reason': 'Sin página válida.', 'material_gaps': ['Contenido de página.']}}
    return {'answer_markdown': PAGE + ' [Fuente](' + page['url'] + ')',
        'cited_evidence_ids': [page['evidence_id']], 'findings': [{'claim': PAGE, 'kind': 'observation',
            'supports': [page['evidence_id']], 'refutes': [], 'limitations': ['Fixture sintético.']}],
        'limitations': ['Una sola página, no cobertura exhaustiva.'], 'chart_request': None,
        'decision': {'status': 'answered', 'reason': 'Se comprobó la página sintética solicitada.', 'material_gaps': []}}


class LocalizedProvider:
    def __init__(self, malformed_page):
        self.malformed_page = malformed_page
        self.starts, self.read_flags = [], []

    async def start(self, call):
        self.starts.append(copy.deepcopy(call))
        return {'runId': 'synthetic-localized-' + str(len(self.starts)), 'status': 'SUCCEEDED',
            'datasetId': 'synthetic-dataset', 'spent_microusd': 5000 if call['actor'] == 'scraperlink/google-search-results-serp-scraper' else 1000}

    async def read(self, run_id):
        index = int(run_id.removeprefix('synthetic-localized-')) - 1
        call = self.starts[index]
        return {'runId': run_id, 'status': 'SUCCEEDED', 'datasetId': 'synthetic-dataset',
                'spent_microusd': 5000 if call['actor'] == 'scraperlink/google-search-results-serp-scraper' else 1000}

    async def observations(self, receipt, call, *, allow_partial_pages=False):
        self.read_flags.append((call['actor'], allow_partial_pages))
        if call['actor'] == 'scraperlink/google-search-results-serp-scraper':
            item = publisher_item()
            item['search_term'] = call['input']['keyword']
            item['results'] = [result_row(index, url) for index, url in enumerate(URLS, 1)]
            client, _ = provider_for([item])
            return await client.observations(receipt, call)
        assert call['actor'] == 'apify/website-content-crawler'
        url = call['input']['startUrls'][0]['url']
        assert allow_partial_pages is (url in URLS)
        row = {'url': url, 'crawl': {'loadedUrl': url, 'depth': 0, 'httpStatusCode': 200},
            'markdown': PAGE if url in URLS else 'Página base de contexto; no confirma la apertura investigada.'}
        if self.malformed_page:
            row['crawl']['loadedUrl'] = 'https://unselected.example.com/other'
        client, _ = provider_for([row])
        return await client.observations(receipt, call, allow_partial_pages=allow_partial_pages)


async def localized_readback(current, *, flow, malformed_page, delivered):
    saved, _ = flow.journal.read()
    reread = Records(flow.journal.scope, saved['tables'])
    assert current.tables == reread.tables
    reports = reread.rows('ai_mi_reports')
    if reports:
        report = reports[-1]
        assert flow.research.reference_urls(report['run_id'], json.loads(report['cited_evidence_ids_json'])) == ([URLS[0]] if not malformed_page else [])
        assert any('`oci_artifacts`.`ai_mi_reports`' in sql for sql, _ in projection_checks(reread, 'catalog', 'oci_artifacts'))
        delivered.append(report['report_id'])


def localized_graph(monkeypatch, *, malformed_page=False, base_targets=False):
    flow, model, _, _ = configured()
    if base_targets:
        registered_base(flow, TOPIC)
    now = datetime.fromisoformat(WINDOW['end'])
    flow.clock = lambda: now
    flow.public_discovery = True
    monkeypatch.setattr(research, 'utc_now', lambda: now.isoformat())
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    monkeypatch.setattr(execution, 'datetime', Clock)
    original = model.ask
    responses = {'understand': lambda payload: synthetic_localized_intake(payload, base_targets),
        'plan_research': lambda payload: synthetic_localized_plan(payload, base_targets),
        'assess_evidence': synthetic_localized_assessment, 'draft_report': synthetic_localized_draft}
    async def ask(name, schema, payload):
        if name in responses:
            model.calls.append((name, copy.deepcopy(payload)))
            return responses[name](payload)
        return await original(name, schema, payload)
    model.ask = ask

    provider = LocalizedProvider(malformed_page)
    flow.provider = provider
    delivered = []
    flow.project = partial(localized_readback, flow=flow, malformed_page=malformed_page, delivered=delivered)
    return flow, model, provider, flow.build(InMemorySaver()), delivered


def test_search_then_selected_page_publishes_body_references_and_replays_without_another_capture(monkeypatch):
    flow, model, provider, graph, delivered = localized_graph(monkeypatch)
    result = turn(graph, 'localized-end-to-end', QUESTION)
    assert [call['actor'] for call in provider.starts] == ['scraperlink/google-search-results-serp-scraper', 'apify/website-content-crawler']
    assert provider.starts[0]['input']['gl'] == 'CO'
    assert provider.starts[0]['input']['hl'] == 'es'
    assert provider.starts[1]['input']['startUrls'] == [{'url': URLS[0]}]
    assert provider.read_flags == [('scraperlink/google-search-results-serp-scraper', False), ('apify/website-content-crawler', True)]
    assert PAGE in result['response']['markdown']
    assert result['response']['references'] == [URLS[0]]
    assert delivered == [result['response']['report_id']]
    records = flow.records()
    evidence = flow.research.evidence(result['run_id'])
    assert len(evidence) == 4
    metadata = [row for row in evidence if row['provenance']['source'] == 'scraperlink/google-search-results-serp-scraper']
    assert len(metadata) == 3 and all(row['provenance']['content_basis'] == 'search_title_and_snippet' for row in metadata)
    assert all(row['text'] != PAGE for row in metadata)
    assert len(records.rows('ai_mi_questions')) == len(records.rows('ai_mi_reports')) == 1
    assert len(records.rows('ai_mi_source_targets')) == 1
    assert len(records.rows('ai_mi_topic_sources')) == 1
    assert all(row['canonical_url_or_id'] not in URLS for row in records.rows('ai_mi_source_targets'))
    before = flow.journal.read()[0]
    calls = len(model.calls)
    replay = turn(graph, 'localized-end-to-end', QUESTION)
    assert replay['response'] == result['response']
    assert flow.journal.read()[0] == before
    assert len(model.calls) == calls and len(provider.starts) == 2


def test_unrequested_page_cannot_turn_search_metadata_into_confirmed_page_body(monkeypatch):
    flow, _, provider, graph, _ = localized_graph(monkeypatch, malformed_page=True)
    result = turn(graph, 'localized-page-mismatch', QUESTION)
    assert len(provider.starts) == 2
    assert result['response']['references'] == []
    assert PAGE not in result['response']['markdown']
    assert len(flow.research.evidence(result['run_id'])) == 3
    report = flow.records().get('ai_mi_reports', result['response']['report_id'])
    assert json.loads(report['cited_evidence_ids_json']) == []
    assert json.loads(report['coverage_json'])['conclusion']['status'] == 'unresolved'


def test_localized_metadata_requires_the_search_vs_reading_limitation():
    from test_market_intelligence_localized_search import normalize
    observation = normalize()[0].items[0]
    observation['evidence_id'] = 'synthetic-evidence'
    context = evidence_context({'question': QUESTION}, [observation])
    limits = '\n'.join(context_limitations(context)).lower()
    assert 'título' in limits and 'metadatos' in limits
    assert 'no equivalen a leer' in limits and 'página' in limits


def test_remaining_registered_target_does_not_hide_the_discovered_read_on_the_same_source(monkeypatch):
    flow, model, provider, graph, _ = localized_graph(monkeypatch, base_targets=True)
    result = turn(graph, 'base-then-discovery-then-page', QUESTION)
    assert [call['actor'] for call in provider.starts] == [
        'apify/website-content-crawler', 'scraperlink/google-search-results-serp-scraper', 'apify/website-content-crawler']
    assert provider.starts[0]['input']['startUrls'] == [{'url': BASE_URLS[0]}]
    assert provider.starts[2]['input']['startUrls'] == [{'url': URLS[0]}]
    assert not any({'url': BASE_URLS[1]} in call['input'].get('startUrls', []) for call in provider.starts)
    final_plan = [payload for name, payload in model.calls if name == 'plan_research'][-1]
    reader = [source for source in final_plan['catalog'] if source['profile_id'] == 'website']
    assert len(reader) == 1
    assert {target['canonical_url_or_id'] for target in reader[0]['targets']} == {BASE_URLS[1]}
    assert set(reader[0]['discovered_urls']) == set(URLS)
    assert result['response']['references'] == [URLS[0]]
    assert PAGE in result['response']['markdown']
    assert len(flow.records().rows('ai_mi_source_targets')) == 3


def combined_reader_case():
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow import MarketWorkflow
    from test_market_intelligence_discovery_authority import discovery_case, records
    from test_market_intelligence_workflow import TOPIC as registered_topic
    case = discovery_case()
    flow = MarketWorkflow(case.journal, None, None, usage_cost_verified=True)
    _, topic_id = registered_base(flow, registered_topic)
    assert topic_id == case.topic
    state = {'run_id': case.run, 'topic_id': case.topic, 'question_id': flow.records().get('ai_mi_research_runs', case.run)['question_id'],
        'catalog': case.catalog.approved_base(records(case), case.topic), 'window': WINDOW,
        'intake': {'intent': 'research', 'topic': registered_topic}}
    available = flow.available_research_sources(state)
    reader = next(row for row in available if row['source_key'] == case.reader)
    assert reader['targets'] and reader['discovered_urls']
    return case, flow, state, available


@pytest.mark.parametrize('target,url', [('invented-target', URLS[0]), (None, 'https://example.com/never-discovered')])
def test_combined_source_does_not_authorize_invented_target_or_unselected_url(target, url):
    case, flow, state, available = combined_reader_case()
    plan = {'next_action': 'execute', 'source_key': case.reader,
        'target_id': case.journal.scope.key(target) if target else None,
        'arguments': {'query': '', 'urls': [url], 'max_items': 1, 'sort': 'Latest'},
        'purpose': 'Intento no autorizado', 'expected_contribution': '', 'user_question': None}
    before = case.journal.read()[0]
    assert flow.catalog.bind_plan_target(plan, available) is None
    rejected = flow.admit_step({**state, 'plan': plan, 'plan_sources': available})
    assert rejected['route'] == 'draft_report' and rejected['reason'] == 'unresolved'
    assert case.journal.read()[0] == before


def test_combined_source_keeps_registered_target_binding_separate_from_discovery():
    case, flow, _, available = combined_reader_case()
    reader = next(row for row in available if row['source_key'] == case.reader)
    target = next(row for row in reader['targets'] if row['canonical_url_or_id'] == BASE_URLS[1])
    plan = {'source_key': case.reader, 'target_id': target['target_id'],
        'arguments': {'query': '', 'urls': [BASE_URLS[1]], 'max_items': 1, 'sort': 'Latest'}}
    assert flow.catalog.bind_plan_target(plan, available) == plan
    grant = case.paid.authorize(case.run, case.reader, target['target_id'], requested_urls=[BASE_URLS[1]])
    assert grant['urls'] == [BASE_URLS[1]] and 'discovered_urls' not in grant
