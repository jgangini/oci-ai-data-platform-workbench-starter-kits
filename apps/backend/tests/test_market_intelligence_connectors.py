from datetime import datetime, timezone

import pytest

from app.labs.ai_market_intelligence.catalog import public_url
from app.labs.ai_market_intelligence.connectors import compile_call, resolve_window


CLOCK = datetime(2026, 10, 30, 14, 30, tzinfo=timezone.utc)
WINDOW = resolve_window('last_7_days', CLOCK, 'America/Bogota')
ARGS = {'query': '"Tiendas D1" OR tiendasd1', 'urls': [], 'max_items': 10, 'sort': 'Latest'}


def test_calendar_today_differs_from_rolling_twenty_four_hours():
    today = resolve_window('calendar_today', CLOCK, 'America/Bogota')
    recent = resolve_window('rolling_24h', CLOCK, 'America/Bogota')
    assert today['start'] == '2026-10-30T05:00:00+00:00'
    assert recent['start'] == '2026-10-29T14:30:00+00:00'
    assert today['end'] == recent['end']


def test_global_x_search_uses_one_reviewed_actor_with_window_and_limits():
    call = compile_call('x_search', ARGS, WINDOW, 100000)
    assert call['actor'] == 'apidojo/twitter-scraper-lite'
    assert call['input']['searchTerms'] == ['("Tiendas D1" OR tiendasd1) since:2026-10-23 until:2026-10-31']
    assert call['input']['maxItems'] == 10
    assert call['callOptions']['timeout'] == 60
    assert call['callOptions']['maxTotalChargeUsd'] == 0.1


@pytest.mark.parametrize('query', ['(from:someone) D1', 'D1 OR until:2020-01-01', 'D1 since_time:1'])
def test_model_query_cannot_override_global_search_and_time_policy(query):
    with pytest.raises(ValueError, match='overrides'):
        compile_call('x_search', {**ARGS, 'query': query}, WINDOW, 100000)


def test_web_reader_requires_cost_envelope_and_explicit_approved_urls():
    args = {**ARGS, 'query': '', 'urls': ['https://example.com/noticias']}
    with pytest.raises(RuntimeError, match='cost envelope'):
        compile_call('website', args, WINDOW, 100000, approved_urls=args['urls'])
    with pytest.raises(ValueError, match='approved targets'):
        compile_call('website', args, WINDOW, 100000, usage_cost_verified=True)
    call = compile_call('website', args, WINDOW, 100000, approved_urls=args['urls'], usage_cost_verified=True)
    assert call['input']['maxCrawlPages'] == 1 and call['input']['maxCrawlDepth'] == 0
    assert not call['input']['saveScreenshots'] and not call['input']['saveFiles']


@pytest.mark.parametrize('url', ['http://example.com', 'https://user:pass@example.com', 'https://127.0.0.1',
                               'https://localhost', 'https://example.internal', 'https://example.com:8443'])
def test_nonpublic_or_credential_urls_are_rejected(url):
    with pytest.raises(ValueError):
        public_url(url)


def test_tracking_parameters_and_x_aliases_do_not_duplicate_identity():
    assert public_url('https://twitter.com/user/status/1?utm_source=feed#reply') == 'https://x.com/user/status/1'
