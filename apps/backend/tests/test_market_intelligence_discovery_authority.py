"""Organic results authorize scoped reads, never permanent source registration."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.catalog import Catalog
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import PROFILES, compile_call
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import (
    PaidOperations, SourceScopeError, TargetAlreadyInvestigated, discovered_read_options,
    execute_selected, resolve_source_scope, schedule_scope_bindings)
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import (
    Journal, Records, Scope, digest, json_text)
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.research import Research
from test_market_intelligence_localized_search import LOCALE, WINDOW, RECEIPT, compile_search, normalize, publisher_item, result_row
from test_market_intelligence_repository import MemorySnapshot, SCOPE
from test_market_intelligence_workflow import TOPIC


URLS = ['https://example.com/noticia', 'https://example.com/segunda', 'https://example.com/tercera']


def update_row(case, table, key, **changes):
    return case.journal.transact(lambda records, state: records.upsert(table, {
        **records.get(table, key), **changes}))


def discovery_case():
    journal = Journal(MemorySnapshot(), 1000000)
    research, catalog, paid = Research(journal), Catalog(journal), PaidOperations(journal)
    question, topic = research.record_question('organic-question', 'Investiga en la web las novedades de D1 en Colombia.', TOPIC)
    run = research.prepare_run(question, WINDOW, 'Novedades de D1', 100000, search_context=LOCALE)
    source = catalog._ensure_public_search('web_search_localized')
    reader = catalog.ensure_website_reader()
    call = compile_search()
    grant = paid.authorize(run, source, None)
    tool = paid.reserve(run, source, None, call, 50000, 'Descubrir fuentes', authorization_hash=grant['authorization_hash'])

    async def start(arguments):
        assert arguments == call
        return copy.deepcopy(RECEIPT)

    asyncio.run(execute_selected(paid, tool, call, start))
    item = publisher_item()
    item['results'] = [result_row(number, url) for number, url in enumerate(URLS, 1)]
    observations, _ = normalize(item, call)
    research.add_evidence(run, tool, observations.items)
    return SimpleNamespace(journal=journal, research=research, catalog=catalog, paid=paid,
        source=source, reader=reader, run=run, topic=topic, tool=tool, call=call,
        evidence=observations.items)


def records(case):
    return Records(case.journal.scope, case.journal.read()[0]['tables'])


def read_grant(case, urls=None, run=None):
    return case.paid.authorize(run or case.run, case.reader, None, requested_urls=urls or [URLS[0]])


def reserve_read(case, urls=None, grant=None):
    urls = urls or [URLS[0]]
    grant = grant or read_grant(case, urls)
    call = compile_call('website', {'query': '', 'urls': urls, 'max_items': len(urls), 'sort': 'Latest'},
        WINDOW, 50000, approved_urls=grant['urls'], usage_cost_verified=True, require_all_urls=True)
    tool = case.paid.reserve(case.run, case.reader, None, call, 50000, 'Leer resultado orgánico',
        authorization_hash=grant['authorization_hash'])
    return tool, call


def test_organic_grant_is_bound_to_capture_and_does_not_register_pages():
    case = discovery_case()
    before = case.journal.read()[0]
    grant = read_grant(case, [URLS[0] + '?utm_source=other'])
    assert grant['discovered_urls'] == [URLS[0]]
    assert grant['urls'] == [URLS[0]]
    assert grant['connector_revision'] == 'website'
    options = discovered_read_options(records(case), case.topic, case.run)
    assert set(options['discovered_urls']) == set(URLS)
    assert options['targets'] == []
    assert case.journal.read()[0] == before
    assert records(case).rows('ai_mi_source_targets') == []
    assert records(case).rows('ai_mi_topic_sources') == []


@pytest.mark.parametrize('url', ['https://unselected.example.com/', 'https://example.com/in-the-snippet',
    'https://example.com/noticia/guessed-from-title', 'https://example.com/noticia-extra'])
def test_snippet_sitelink_or_guessed_url_is_not_organic_authority(url):
    case = discovery_case()
    before = case.journal.read()[0]
    with pytest.raises(SourceScopeError):
        read_grant(case, [url])
    assert case.journal.read()[0] == before


def test_metadata_from_another_research_cannot_authorize_the_new_run():
    case = discovery_case()
    question, _ = case.research.record_question('another-question', 'Otra investigación de D1', TOPIC)
    other = case.research.prepare_run(question, WINDOW, 'Otra investigación', 100000, search_context=LOCALE)
    with pytest.raises(SourceScopeError):
        read_grant(case, run=other)
    assert discovered_read_options(records(case), case.topic, other) is None


@pytest.mark.parametrize('scope', [Scope('tenant-a', 'other-owner', SCOPE.generation_id),
    Scope('other-tenant', 'owner-a', SCOPE.generation_id)])
def test_foreign_scope_cannot_reuse_owned_discovery(scope):
    case = discovery_case()
    with pytest.raises(ValueError):
        foreign = Records(scope, case.journal.read()[0]['tables'])
        resolve_source_scope(foreign, case.topic, case.reader, None,
            run_id=case.run, requested_urls=[URLS[0]])


@pytest.mark.parametrize('change', ['inactive', 'discovery_revoked', 'profile_changed', 'reader_policy'])
def test_revoked_source_or_changed_reader_cannot_use_old_metadata(change):
    case = discovery_case()
    source_changes = {
        'inactive': {'status': 'disabled'},
        'discovery_revoked': {'allowed_scope_json': json_text({'allow_discovery': False})},
        'profile_changed': {'connector_revision': 'web_search'},
    }
    if change == 'reader_policy':
        update_row(case, 'ai_mi_sources', case.reader, allowed_scope_json=json_text({'allow_discovery': True}))
    else:
        update_row(case, 'ai_mi_sources', case.source, **source_changes[change])
    before = case.journal.read()[0]
    with pytest.raises(SourceScopeError):
        read_grant(case)
    assert case.journal.read()[0] == before


@pytest.mark.parametrize('receipt', [{'runId': 'other-run', 'status': 'SUCCEEDED'},
    {'runId': RECEIPT['runId'], 'status': 'RUNNING'}, {'status': 'SUCCEEDED'}])
def test_receipt_must_bind_the_same_successful_provider_run(receipt):
    case = discovery_case()
    update_row(case, 'ai_mi_tool_runs', case.tool, provider_receipt_json=json.dumps(receipt))
    with pytest.raises(SourceScopeError):
        read_grant(case)


@pytest.mark.parametrize('field,value', [('actor_build', '0.0.20'), ('source', 'apify/rag-web-browser'),
    ('provider_run_id', 'other-run'), ('content_basis', 'page_text'),
    ('effective_search', {'provider': 'google', 'countryCode': 'US', 'languageCode': 'en'}),
    ('requested_search', {'provider': 'google', 'countryCode': 'CO', 'languageCode': 'es'})])
def test_provenance_must_match_frozen_search_framing(field, value):
    case = discovery_case()
    def alter(rows, state):
        for link in rows.rows('ai_mi_run_evidence'):
            span = json.loads(link['quoted_span_json'])
            span['capture']['provenance'][field] = value
            rows.upsert('ai_mi_run_evidence', {**link, 'quoted_span_json': json.dumps(span)})
    case.journal.transact(alter)
    with pytest.raises(SourceScopeError):
        read_grant(case)


@pytest.mark.parametrize('change', ['inactive', 'discovery_revoked', 'build', 'reader_policy'])
def test_claim_revalidates_discovery_after_reservation_without_spending_or_releasing_hold(monkeypatch, change):
    case = discovery_case()
    tool, _ = reserve_read(case)
    if change == 'build':
        monkeypatch.setitem(PROFILES, 'web_search_localized', {**PROFILES['web_search_localized'], 'build': '0.0.999'})
    elif change == 'reader_policy':
        update_row(case, 'ai_mi_sources', case.reader, allowed_scope_json=json_text({'allow_discovery': True}))
    elif change == 'inactive':
        update_row(case, 'ai_mi_sources', case.source, status='disabled')
    else:
        update_row(case, 'ai_mi_sources', case.source, allowed_scope_json=json_text({'allow_discovery': False}))
    before = case.journal.read()[0]
    with pytest.raises(SourceScopeError):
        case.paid.claim_start(tool)
    assert case.journal.read()[0] == before
    hold = before['operations'][tool]
    assert hold['state'] == 'reserved'
    assert hold.get('permit') is None and hold.get('spent_microusd') is None


def test_same_read_replays_without_a_second_start_and_overlap_cannot_double_debit():
    case = discovery_case()
    tool, call = reserve_read(case)
    starts = []
    async def start(arguments):
        starts.append(arguments)
        return {'runId': 'synthetic-page', 'status': 'SUCCEEDED', 'spent_microusd': 1000}
    asyncio.run(execute_selected(case.paid, tool, call, start))
    after = case.journal.read()[0]
    assert asyncio.run(execute_selected(case.paid, tool, call, start))['state'] == 'reconcile'
    assert len(starts) == 1
    replayed = case.journal.read()[0]
    assert replayed['revision'] >= after['revision']
    assert {key: value for key, value in replayed.items() if key != 'revision'} == {
        key: value for key, value in after.items() if key != 'revision'}
    assert discovered_read_options(records(case), case.topic, case.run)['discovered_urls'] == URLS[1:]
    with pytest.raises(TargetAlreadyInvestigated):
        reserve_read(case, [URLS[0], URLS[1]])
    rejected = case.journal.read()[0]
    assert {key: value for key, value in rejected.items() if key != 'revision'} == {
        key: value for key, value in replayed.items() if key != 'revision'}
    assert len(starts) == 1


def test_localized_discovery_is_not_silently_scheduled_and_legacy_sources_remain_explicit():
    case = discovery_case()
    with pytest.raises(ValueError):
        schedule_scope_bindings(records(case), case.topic, {'target_ids': [], 'source_keys': [case.source],
            'window_days': 7, 'cap_microusd': 100000})
    old = case.catalog._ensure_public_search('web_search')
    custom = SCOPE.key('custom-legacy-search')
    case.journal.transact(lambda rows, state: rows.upsert('ai_mi_sources', {
        'source_key': custom, 'name': 'Legacy operator search', 'connector_revision': 'web_search',
        'status': 'active', 'allowed_scope_json': json_text({'allow_discovery': True})}))
    visible = {row['source_key'] for row in case.catalog.visible_sources(records(case))}
    assert old not in visible and custom in visible and case.source not in visible
    assert records(case).get('ai_mi_sources', case.source)['status'] == 'active'
    legacy = case.catalog.approved_base(records(case), case.topic, set(), {old})
    assert [row['source_key'] for row in legacy] == [old]


def test_scheduled_run_cannot_borrow_an_unapproved_reader():
    case = discovery_case()
    schedule = SCOPE.key('synthetic-schedule')
    case.journal.transact(lambda rows, state: rows.upsert('ai_mi_schedules', {
        'schedule_id': schedule, 'topic_id': case.topic, 'status': 'active'}))
    update_row(case, 'ai_mi_research_runs', case.run, schedule_id=schedule)
    with pytest.raises(SourceScopeError):
        read_grant(case)
    assert discovered_read_options(records(case), case.topic, case.run) is None


def test_reused_metadata_without_the_original_tool_link_never_grants_a_page_read():
    case = discovery_case()
    def reuse(rows, state):
        for link in rows.rows('ai_mi_run_evidence'):
            span = json.loads(link['quoted_span_json'])
            span['reuse'] = {'original_run_id': case.run}
            rows.upsert('ai_mi_run_evidence', {**link, 'tool_run_id': None, 'quoted_span_json': json.dumps(span)})
    case.journal.transact(reuse)
    with pytest.raises(SourceScopeError):
        read_grant(case)
    assert discovered_read_options(records(case), case.topic, case.run) is None


def test_changed_research_dates_invalidate_discovery_and_context_cannot_be_rebound():
    case = discovery_case()
    before = case.journal.read()[0]
    with pytest.raises(ValueError):
        case.research.bind_search_context(case.run, {'country_code': 'US', 'language_code': 'en'})
    assert case.journal.read()[0] == before
    update_row(case, 'ai_mi_research_runs', case.run,
        plan_window_json=json_text({**WINDOW, 'start': '2026-10-24T05:00:00+00:00'}))
    with pytest.raises(SourceScopeError):
        read_grant(case)
