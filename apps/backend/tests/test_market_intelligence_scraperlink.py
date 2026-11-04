"""ScraperLink request, payload and durable organic-read authority; no live calls."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.catalog import Catalog
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import compile_call
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import PaidOperations, SourceScopeError, execute_selected
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Journal, Records
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.research import Research
from test_market_intelligence_localized_search import ARGS as LEGACY_ARGS, LOCALE, WINDOW, RECEIPT
from test_market_intelligence_payload import provider_for
from test_market_intelligence_repository import MemorySnapshot
from test_market_intelligence_workflow import TOPIC


ACTOR = 'scraperlink/google-search-results-serp-scraper'
ARGS = {**LEGACY_ARGS, 'max_items': 10}


def result_row(position=1, url='https://example.com/noticia'):
    return {'position': position, 'url': url, 'title': 'Una noticia sobre D1',
        'description': 'Fragmento sintético, no contenido leído.', 'date': 'hace 2 días',
        'sitelinks': [{'title': 'No seleccionado', 'url': 'https://unselected.example.com/'}]}


def publisher_item():
    return {'search_term': ARGS['query'], 'page_number': 1, 'next_page': 2, 'next_start': 10,
        'related_keywords': {'keywords': [], 'spelling_suggestion': None}, 'results': [result_row()]}


def compile_search(args=None, locale=None):
    return compile_call('web_search_serp', args or ARGS, WINDOW, 50000,
                        approved_search_context=LOCALE if locale is None else locale)


def normalize(item=None, call=None, rows=None):
    client, tool = provider_for(rows if rows is not None else [publisher_item() if item is None else item])
    return asyncio.run(client.observations(RECEIPT, call or compile_search())), tool


def test_single_page_has_distinct_dataset_and_organic_limits():
    call = compile_search({**ARGS, 'max_items': 10})
    assert call == {'actor': ACTOR, 'input': {'keyword': ARGS['query'], 'limit': '10', 'include_merged': False,
        'page': 1, 'gl': 'CO', 'hl': 'es', 'tbs': 'cdr:1,cd_min:10/25/2026,cd_max:11/01/2026'},
        'waitSecs': 0, 'callOptions': {'build': '0.0.92', 'memory': 128, 'timeout': 120,
                                     'maxItems': 1, 'maxTotalChargeUsd': 0.05}}
    navigation = compile_search({**ARGS, 'search_mode': 'navigation'})
    assert 'tbs' not in navigation['input']
    assert 'country' not in call['input'] and 'proxy_location' not in call['input']
    assert compile_search(locale={'country_code': 'BR', 'language_code': 'pt-BR'})['input']['hl'] == 'pt'


@pytest.mark.parametrize('query', ['D1\nsegunda consulta', 'D1 after:2020-01-01', 'https://example.com/', 'site:localhost D1'])
def test_model_cannot_expand_query_count_or_override_dates_or_public_scope(query):
    with pytest.raises(ValueError):
        compile_search({**ARGS, 'query': query})


@pytest.mark.parametrize('limit', [1, 3, 9])
def test_a_smaller_admitted_ceiling_is_not_silently_expanded_to_ten(limit):
    with pytest.raises(ValueError, match='one page of up to ten'):
        compile_search({**ARGS, 'max_items': limit})


def test_ten_nested_results_are_snippets_with_unknown_dates_and_unverified_effective_filters():
    item = publisher_item()
    item['results'] = [result_row(i, f'https://example.com/{i}') for i in range(1, 11)]
    before = copy.deepcopy(item)
    output, tool = normalize(item)
    assert len(output.items) == 10 and item == before
    assert output.cost == RECEIPT['spent_microusd']
    for row in output.items:
        assert row['published_at'] is None and 'sitelinks' not in row['provenance']
        proof = row['provenance']
        assert proof['effective_search'] is None and proof['filter_verification'] == 'not_reported_by_provider'
        assert proof['requested_search']['gl'] == 'CO' and proof['requested_search']['hl'] == 'es'
        assert proof['publication_date_raw'] == 'hace 2 días' and proof['publication_date_precision'] == 'unknown'
        assert proof['maximum_search_pages'] == 1 and proof['returned_results_count'] == 10
    assert len(tool.calls) == 1  # next_page is not followed.


@pytest.mark.parametrize('change', [{'search_term': 'Otra empresa'}, {'page_number': 'all'}, {'page_number': True},
    {'page_number': 2}, {'results': [result_row(i) for i in range(1, 12)]},
    {'isError': True, 'results': []}, {'error': 'provider error', 'results': []}, {'errorMessage': 'failed'},
    {'results': [result_row(1), result_row(1)]}])
def test_wrong_query_page_and_unbounded_or_unsafe_results_are_rejected(change):
    with pytest.raises(ValueError):
        normalize({**publisher_item(), **change})


@pytest.mark.parametrize('rows', [[], [publisher_item(), publisher_item()]])
def test_missing_or_merged_pages_cannot_be_counted_as_one_search(rows):
    with pytest.raises(ValueError):
        normalize(rows=rows)


@pytest.mark.parametrize('destination',['https://localhost/','https://127.0.0.1/a',
    'https://example.org:8443/a','https://example.org/ice cream/','javascript:alert(1)',''])
def test_unusable_destination_cannot_discard_or_expand_the_eligible_candidates(destination):
    item=publisher_item()
    item['results']=[result_row(1),result_row(2,destination),result_row(3,'https://example.org/other')]
    output,tool=normalize(item)
    assert [row['url'] for row in output.items]==['https://example.com/noticia','https://example.org/other']
    assert all(row['provenance']['excluded_unusable_positions']==[2] for row in output.items)
    assert all(row['provenance']['excluded_http_positions']==[] for row in output.items)
    assert len(tool.calls)==1


def test_provider_error_in_an_organic_row_still_rejects_the_page():
    item=publisher_item();item['results']=[result_row(1),{**result_row(2),'error':'Failed'}]
    with pytest.raises(ValueError,match='error envelope'):
        normalize(item)


def test_empty_page_keeps_known_cost_without_inventing_articles():
    output, _ = normalize({**publisher_item(), 'results': []})
    assert output.items == [] and output.cost == RECEIPT['spent_microusd']


def test_public_http_result_is_excluded_without_losing_https_results_or_upgrading_authority():
    item=publisher_item()
    item['results']=[result_row(1),result_row(2,'http://public.example.org/document.pdf'),
                     result_row(3,'https://example.org/other')]
    output,tool=normalize(item)
    assert [row['url'] for row in output.items]==['https://example.com/noticia','https://example.org/other']
    assert all(row['provenance']['excluded_http_positions']==[2] for row in output.items)
    assert all(row['provenance']['returned_results_count']==3 for row in output.items)
    assert output.cost==RECEIPT['spent_microusd'] and len(tool.calls)==1
    item['results']=[result_row(1,'http://public.example.org/document.pdf')]
    assert normalize(item)[0].items==[]


@pytest.mark.parametrize('url',['http://localhost/','http://127.0.0.1/','http://user:secret@example.org/',
                               'http://example.org:1234/','https://127.0.0.1/'])
def test_unsafe_search_destination_is_not_relabelled_as_an_omittable_http_result(url):
    output,tool=normalize({**publisher_item(),'results':[result_row(1),result_row(2,url)]})
    assert [row['url'] for row in output.items]==['https://example.com/noticia']
    assert output.items[0]['provenance']['excluded_unusable_positions']==[2]
    assert output.items[0]['provenance']['excluded_http_positions']==[]
    assert len(tool.calls)==1


def test_exact_provider_date_echo_is_bound_to_the_sent_filter_without_verifying_google_filters():
    item={**publisher_item(),'search_term':ARGS['query']+' after:2026-10-25'}
    output,_=normalize(item)
    proof=output.items[0]['provenance']
    assert proof['query']==ARGS['query'] and proof['effective_query']==item['search_term']
    assert proof['provider_date_suffix']==' after:2026-10-25' and proof['query_correction'] is False
    assert proof['effective_search'] is None and proof['filter_verification']=='not_reported_by_provider'
    with pytest.raises(ValueError,match='another query'):
        normalize(item,call=compile_search({**ARGS,'search_mode':'navigation'}))


@pytest.mark.parametrize('echo',[ARGS['query']+' after:2026-10-24',ARGS['query']+' after:2026-10-25 before:2099-01-01',
                               'Otra empresa after:2026-10-25'])
def test_changed_query_or_date_echo_cannot_be_admitted_as_provider_normalization(echo):
    with pytest.raises(ValueError,match='another query'):
        normalize({**publisher_item(),'search_term':echo})


def test_reported_bounded_spelling_correction_keeps_original_request_provenance():
    call=compile_search({**ARGS,'query':'leche Lati D1 fabricante Colombia'})
    item={**publisher_item(),'search_term':'leche Latti D1 fabricante Colombia',
          'related_keywords':{'spelling_suggestion':'lecheLattiD1 fabricante Colombia'}}
    output,_=normalize(item,call=call)
    proof=output.items[0]['provenance']
    assert proof['query']=='leche Lati D1 fabricante Colombia'
    assert proof['effective_query']==item['search_term'] and proof['query_correction'] is True
    item['related_keywords']['spelling_suggestion']='Otra consulta'
    with pytest.raises(ValueError,match='another query'):
        normalize(item,call=call)


def discovery_case(navigation=False):
    journal = Journal(MemorySnapshot(), 1000000)
    research, catalog, paid = Research(journal), Catalog(journal), PaidOperations(journal)
    question, topic = research.record_question('serp-question', 'Investiga en la web D1 Colombia.', TOPIC)
    run = research.prepare_run(question, WINDOW, 'Investigar D1', 100000, search_context=LOCALE)
    source, reader = catalog.ensure_public_web_search(), catalog.ensure_website_reader()
    call = compile_search({**ARGS, 'search_mode': 'navigation' if navigation else 'research'})
    grant = paid.authorize(run, source, None)
    tool = paid.reserve(run, source, None, call, 50000, 'Descubrir fuentes', authorization_hash=grant['authorization_hash'])
    async def start(arguments):
        return copy.deepcopy(RECEIPT)
    asyncio.run(execute_selected(paid, tool, call, start))
    output, _ = normalize(call=call)
    research.add_evidence(run, tool, output.items)
    return SimpleNamespace(journal=journal, research=research, catalog=catalog, paid=paid,
                           run=run, source=source, reader=reader, tool=tool, topic=topic)


@pytest.mark.parametrize('navigation', [False, True])
def test_organic_read_authority_uses_actual_request_and_never_sitelinks(navigation):
    case = discovery_case(navigation)
    grant = case.paid.authorize(case.run, case.reader, None, requested_urls=['https://example.com/noticia'])
    assert grant['urls'] == ['https://example.com/noticia']
    with pytest.raises(SourceScopeError):
        case.paid.authorize(case.run, case.reader, None, requested_urls=['https://unselected.example.com/'])


@pytest.mark.parametrize('change', ['query', 'filters', 'hash', 'run', 'effective', 'purpose', 'request_hash'])
def test_mutated_proof_cannot_authorize_a_page_read(change):
    case = discovery_case()
    def alter(rows, state):
        if change in {'purpose', 'request_hash'}:
            tool = rows.get('ai_mi_tool_runs', case.tool)
            if change == 'purpose':
                purpose = json.loads(tool['step_purpose_json'])
                purpose['search_call']['input']['keyword'] = 'Otra empresa'
                tool['step_purpose_json'] = json.dumps(purpose)
            else:
                tool['request_hash'] = 'wrong-hash'
            rows.upsert('ai_mi_tool_runs', tool)
            return
        for link in rows.rows('ai_mi_run_evidence'):
            span = json.loads(link['quoted_span_json'])
            proof = span['capture']['provenance']
            proof.update({'query': {'query': 'Otra empresa'}, 'filters': {'requested_search': {'gl': 'US'}},
                'hash': {'request_arguments_hash': 'wrong-hash'}, 'run': {'provider_run_id': 'wrong-run'},
                'effective': {'effective_search': {'gl': 'CO'}}}[change])
            rows.upsert('ai_mi_run_evidence', {**link, 'quoted_span_json': json.dumps(span)})
    case.journal.transact(alter)
    with pytest.raises(SourceScopeError):
        case.paid.authorize(case.run, case.reader, None, requested_urls=['https://example.com/noticia'])


def test_retired_managed_sources_keep_rows_and_explicit_operator_sources():
    case = discovery_case()
    old = case.catalog._ensure_public_search('web_search_localized')
    rows = Records(case.journal.scope, case.journal.read()[0]['tables'])
    assert rows.get('ai_mi_sources', old)['connector_revision'] == 'web_search_localized'
    assert old not in {row['source_key'] for row in case.catalog.visible_sources(rows)}
    assert case.source in {row['source_key'] for row in case.catalog.visible_sources(rows)}
