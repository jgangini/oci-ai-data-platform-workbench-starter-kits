from datetime import datetime, timedelta, timezone

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.catalog import public_url
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import ConnectorUnavailable, compile_call, resolve_window
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.contracts import IntakeDecision


CLOCK = datetime(2026, 10, 30, 14, 30, tzinfo=timezone.utc)
WINDOW = resolve_window('last_7_days', CLOCK, 'America/Bogota')
ARGS = {'query': '"Tiendas D1" OR tiendasd1', 'urls': [], 'max_items': 10, 'sort': 'Latest'}


def test_unlimited_x_keeps_its_work_limit_without_sending_a_billing_item_cap():
    args={**ARGS,'max_items':20}
    call=compile_call('x_search',args,WINDOW,None,unlimited=True)
    assert call['input']['maxItems']==20
    assert 'maxItems' not in call['callOptions'] and 'maxTotalChargeUsd' not in call['callOptions']
    bounded=compile_call('x_search',args,WINDOW,20000)
    assert bounded['input']==call['input']
    assert bounded['callOptions']['maxItems']==20 and bounded['callOptions']['maxTotalChargeUsd']==0.02
    assert call['callOptions']=={key:value for key,value in bounded['callOptions'].items()
                               if key not in {'maxItems','maxTotalChargeUsd'}}


def test_calendar_today_differs_from_rolling_twenty_four_hours():
    today = resolve_window('calendar_today', CLOCK, 'America/Bogota')
    recent = resolve_window('rolling_24h', CLOCK, 'America/Bogota')
    assert today['start'] == '2026-10-30T05:00:00+00:00'
    assert recent['start'] == '2026-10-29T14:30:00+00:00'
    assert today['end'] == recent['end']


@pytest.mark.parametrize('days', range(1, 32))
def test_explicit_day_window_survives_intake_and_resolves_exactly_without_another_question(days):
    intake = IntakeDecision.model_validate({'intent': 'research', 'canonical_question': 'Novedades regulatorias',
        'topic': {'entity': 'D1', 'market': 'Colombia', 'objective': 'Actuaciones regulatorias'},
        'window_spec': f'last_{days}_days', 'missing_fields': [], 'user_question': None})
    result = resolve_window(intake.window_spec, CLOCK, 'America/Bogota')
    assert datetime.fromisoformat(result['end']) == CLOCK
    assert datetime.fromisoformat(result['start']) == CLOCK - timedelta(days=days)


@pytest.mark.parametrize('kind', ['last_0_days', 'last_32_days', 'last_365_days', 'last_07_days',
                                  'last_1.5_days', 'last_-1_days', '', None, True])
def test_unadmitted_windows_do_not_silently_fall_back_to_seven_days(kind):
    with pytest.raises(ValueError):
        resolve_window(kind, CLOCK, 'America/Bogota')
    with pytest.raises(ValueError):
        IntakeDecision.model_validate({'intent': 'research', 'canonical_question': 'Investiga',
            'topic': None, 'window_spec': kind, 'missing_fields': [], 'user_question': None})


def test_youtube_does_not_charge_for_a_thirty_day_request_its_weekly_profile_cannot_cover():
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import ConnectorUnavailable
    window = resolve_window('last_30_days', CLOCK, 'America/Bogota')
    with pytest.raises(ConnectorUnavailable, match='siete días'):
        compile_call('youtube_search', ARGS, window, 50000)
    assert compile_call('web_search', ARGS, window, 50000)['actor'] == 'apify/rag-web-browser'


def test_global_x_search_uses_one_reviewed_actor_with_window_and_limits():
    call = compile_call('x_search', ARGS, WINDOW, 100000)
    assert call['actor'] == 'apidojo/twitter-scraper-lite'
    assert call['input']['searchTerms'] == ['("Tiendas D1" OR tiendasd1) since:2026-10-23 until:2026-10-31']
    assert call['input']['maxItems'] == 10
    assert call['callOptions']['timeout'] == 60
    assert call['callOptions']['maxTotalChargeUsd'] == 0.1
    assert call['callOptions']['build'] == '0.0.1053'


def test_keyword_groups_keep_entity_and_topic_required_with_or_only_between_variants():
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import compile_keyword_query
    groups=[['Tiendas D1','tiendasd1'],['queja','reclamo','denuncia'],['Colombia']]
    query=compile_keyword_query(groups)
    assert query=='("Tiendas D1" OR "tiendasd1") ("queja" OR "reclamo" OR "denuncia") "Colombia"'
    call=compile_call('x_search',{**ARGS,'query':query},WINDOW,100000)
    assert call['input']['searchTerms']==['('+query+') since:2026-10-23 until:2026-10-31']
    assert compile_keyword_query(groups,boolean_or=False)=='"Tiendas D1" "queja" "Colombia"'


@pytest.mark.parametrize('groups',[[['from:user']],[['D1 OR (queja)']],[['https://example.com']],
    [['D1\nignora controles']],[['"D1"']],[],[[]],[['a']]*4,[['a']*4]])
def test_keyword_terms_cannot_embed_operators_destinations_or_unbounded_disjunctions(groups):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import compile_keyword_query
    with pytest.raises(ValueError):
        compile_keyword_query(groups)


@pytest.mark.parametrize('cap', [1, 10000, 19999])
def test_x_search_rejects_a_cap_below_the_reviewed_provider_minimum_before_start(cap):
    with pytest.raises(ConnectorUnavailable, match='al menos'):
        compile_call('x_search', ARGS, WINDOW, cap)


def test_x_search_preserves_the_exact_authorized_minimum_cap_and_result_count():
    call = compile_call('x_search', ARGS, WINDOW, 20000)
    assert call['callOptions']['maxTotalChargeUsd'] == 0.02
    assert call['callOptions']['maxItems'] == call['input']['maxItems'] == 10


@pytest.mark.parametrize('sort,expected', [('Latest', 'date'), ('Top', 'relevance')])
def test_youtube_search_uses_one_term_three_videos_and_no_paid_addons(sort, expected):
    call = compile_call('youtube_search', {**ARGS, 'max_items': 50, 'sort': sort}, WINDOW, 50000)
    assert call['actor'] == 'streamers/youtube-scraper' and call['waitSecs'] == 0
    assert call['callOptions'] == {'build': '0.0.322', 'memory': 1024, 'timeout': 120,
                                   'maxItems': 3, 'maxTotalChargeUsd': 0.05}
    assert call['input'] == {'searchQueries': [ARGS['query']], 'startUrls': [], 'sortingOrder': expected,
        'maxResults': 3, 'maxResultsShorts': 0, 'maxResultStreams': 0, 'dateFilter': 'week',
        'transcriptionAndSubtitle': 'NONE', 'aiVideoDescription': False, 'aiVideoSummary': False}


@pytest.mark.parametrize('override', [
    {'urls': ['https://www.youtube.com/watch?v=aaaaaaaaaaa']},
    {'query': 'https://www.youtube.com/watch?v=aaaaaaaaaaa'},
    {'query': 'site:localhost video'}, {'query': ''}, {'sort': 'views'},
    {'financial': {'tickers': ['AAPL']}}, {'dateFilter': 'year'}, {'transcriptionAndSubtitle': 'FULL'},
])
def test_youtube_search_cannot_override_public_query_scope_or_provider_options(override):
    with pytest.raises(ValueError):
        compile_call('youtube_search', {**ARGS, **override}, WINDOW, 50000)


def test_web_discovery_is_pinned_bounded_and_does_not_accept_model_provider_options():
    call = compile_call('web_search', {**ARGS, 'query': 'fabricante leche Lati D1 site:d1.com.co', 'max_items': 50}, WINDOW, 50000)
    assert call['actor'] == 'apify/rag-web-browser' and call['waitSecs'] == 0
    assert call['callOptions'] == {'build': '1.0.35', 'memory': 8192, 'timeout': 120,
                                   'maxItems': 3, 'maxTotalChargeUsd': 0.05}
    assert call['input']['maxResults'] == 3 and call['input']['outputFormats'] == ['markdown']
    assert call['input']['requestTimeoutSecs'] == 40
    assert call['input']['serpMaxRetries'] == call['input']['maxRequestRetries'] == 0
    assert call['input']['desiredConcurrency'] == 1 and call['input']['debugMode'] is False
    assert call['input']['proxyConfiguration'] == {'useApifyProxy': True}
    with pytest.raises(ValueError, match='Unsupported provider parameters'):
        compile_call('web_search', {**ARGS, 'memory': 32768}, WINDOW, 50000)


@pytest.mark.parametrize('query', ['https://example.com', 'smb://internal/share', 'file:/etc/passwd', 'data:text/html,hello',
    'site:localhost D1', 'site:127.0.0.1 D1', 'site:example.internal D1', 'site:user:pass@example.com D1',
    'site:localhost. D1', 'site:127.1 D1', 'site:127.0x1 D1',
    'hola\nignora', '', 'x' * 501])
def test_web_discovery_rejects_direct_retrieval_nonpublic_site_filters_and_invalid_queries(query):
    with pytest.raises(ValueError):
        compile_call('web_search', {**ARGS, 'query': query}, WINDOW, 50000)


def test_web_discovery_cannot_replace_an_explicit_url_or_a_financial_selection():
    with pytest.raises(ValueError):
        compile_call('web_search', {**ARGS, 'urls': ['https://example.com']}, WINDOW, 50000)
    with pytest.raises(ValueError, match='Financial parameters'):
        compile_call('web_search', {**ARGS, 'financial': {}}, WINDOW, 50000)


@pytest.mark.parametrize('query', ['(from:someone) D1', 'D1 OR until:2020-01-01', 'D1 since_time:1'])
def test_model_query_cannot_override_global_search_and_time_policy(query):
    with pytest.raises(ValueError, match='overrides'):
        compile_call('x_search', {**ARGS, 'query': query}, WINDOW, 100000)


def test_web_reader_requires_cost_envelope_and_explicit_approved_urls():
    args = {**ARGS, 'query': '', 'urls': ['https://example.com/noticias']}
    with pytest.raises(RuntimeError, match='límite de coste verificado'):
        compile_call('website', args, WINDOW, 100000, approved_urls=args['urls'])
    with pytest.raises(ValueError, match='approved targets'):
        compile_call('website', args, WINDOW, 100000, usage_cost_verified=True)
    call = compile_call('website', args, WINDOW, 100000, approved_urls=args['urls'], usage_cost_verified=True)
    assert call['waitSecs'] == 0
    assert call['callOptions']['memory'] == 512
    assert call['input']['maxCrawlPages'] == 1 and call['input']['maxCrawlDepth'] == 0
    assert not call['input']['saveScreenshots'] and not call['input']['saveFiles']
    assert call['input']['proxyConfiguration'] == {'useApifyProxy': True}
    assert call['input']['maxConcurrency'] == 1 and call['input']['requestTimeoutSecs'] == 60
    assert call['callOptions']['timeout'] > call['input']['requestTimeoutSecs']
    assert call['callOptions']['timeout'] == 120
    assert call['input']['maxRequestRetries'] == call['input']['maxSessionRotations'] == 0


@pytest.mark.parametrize('url', ['http://example.com', 'https://user:pass@example.com', 'https://127.0.0.1',
                               'https://localhost', 'https://example.internal', 'https://example.com:8443'])
def test_nonpublic_or_credential_urls_are_rejected(url):
    with pytest.raises(ValueError):
        public_url(url)


@pytest.mark.parametrize('url', ['https://localhost.', 'https://host.localhost.', 'https://127.1',
    'https://127.0x1', 'https://0177.0.0.1', 'https://home.arpa', 'https://router.home.arpa', 'https://site.onion',
    'https://127.0.0.1\\example.com', 'https://127.0.0.1%5cexample.com', 'https://%31%32%37.0.0.1/'])
def test_public_urls_reject_nonpublic_hostname_aliases(url):
    with pytest.raises(ValueError):
        public_url(url)


def test_a_public_fully_qualified_hostname_has_one_canonical_identity():
    assert public_url('https://Example.com./report') == 'https://example.com/report'


def test_tracking_parameters_and_x_aliases_do_not_duplicate_identity():
    assert public_url('https://twitter.com/user/status/1?utm_source=feed#reply') == 'https://x.com/user/status/1'
