"""Reviewed extraction compilers; model arguments never select an Actor."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import re

from .catalog import public_url
from .research import _window
from .finance import compile_finance


PROFILES = {
    'x_search': {'actor': 'apidojo/twitter-scraper-lite', 'actor_id': 'nfp1fpt5gUlBwPcor',
                 'build': '0.0.1048', 'platform': 'x', 'operation': 'topic_search', 'pricing': 'event'},
    'website': {'actor': 'apify/website-content-crawler', 'actor_id': 'aYG0l9s7dbB7j3gbS',
                'build': '0.3.102', 'platform': 'web', 'operation': 'read_urls', 'pricing': 'usage'},
    'yahoo_finance': {'actor': 'architjn/yahoo-finance', 'actor_id': 'lujI4mrby2M9OV868',
                      'build': '1.1.1', 'platform': 'finance', 'operation': 'ticker_history', 'pricing': 'event'},
    'world_bank': {'actor': 'solidcode/world-bank-indicators-scraper', 'actor_id': 'h9FnVs8QXP9lBK6Gp',
                   'build': '1.0.6', 'platform': 'economics', 'operation': 'annual_series', 'pricing': 'event'},
}


def resolve_window(kind, clock, timezone_name):
    if clock.tzinfo is None:
        raise ValueError('Research clock must be timezone-aware')
    end = clock.astimezone(timezone.utc)
    if kind == 'calendar_today':
        zone = ZoneInfo(timezone_name)
        start = datetime.combine(end.astimezone(zone).date(), time.min, zone).astimezone(timezone.utc)
    elif kind in {'rolling_24h', 'last_7_days'}:
        start = end - timedelta(days=1 if kind == 'rolling_24h' else 7)
    else:
        raise ValueError('Unsupported research window')
    return {'start': start.isoformat(), 'end': end.isoformat()}


def compile_call(profile_id, args, window, cap_microusd, *, approved_urls=(), approved_finance=None, usage_cost_verified=False):
    profile = PROFILES[profile_id]
    start, end = _window(window)
    if type(cap_microusd) is not int or not 0 < cap_microusd <= 5000000:
        raise ValueError('A bounded approved provider reservation is required')
    if set(args) - {'query', 'urls', 'max_items', 'sort', 'financial'} or not {'query', 'urls', 'max_items', 'sort'}.issubset(args):
        raise ValueError('Unsupported provider parameters')
    limit = args['max_items']
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Result limit must be between one and fifty')
    if profile_id in {'yahoo_finance', 'world_bank'}:
        if args['query'] or args['urls']:
            raise ValueError('Financial sources accept only approved structured selections')
        inputs = compile_finance(profile_id, args.get('financial'), approved_finance, limit, end)
    elif args.get('financial') is not None:
        raise ValueError('Financial parameters do not belong to this connector')
    elif profile_id == 'x_search':
        inputs = _x_input(args, start, end, limit)
    elif profile_id == 'website':
        if not usage_cost_verified:
            raise RuntimeError('Usage-priced crawling requires a verified cost envelope before admission')
        inputs = _web_input(args, approved_urls)
    # Return the run identity before the native MCP helper's shorter deadline.
    return {'actor': profile['actor'], 'input': inputs, 'waitSecs': 0,
            'callOptions': {'build': profile['build'], 'memory': 512 if profile_id == 'website' else 256,
                            'timeout': 120 if profile_id == 'website' else 60,
                            'maxItems': limit, 'maxTotalChargeUsd': cap_microusd / 1000000}}


def _x_input(args, start, end, limit):
    query = args['query']
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500 or any(ord(c) < 32 for c in query):
        raise ValueError('A bounded global topic search is required')
    if args['urls'] or args['sort'] not in {'Latest', 'Top'}:
        raise ValueError('Invalid X search arguments')
    # Dates supplied by the controller cannot be overridden by a model query.
    if re.search(r'(?<![a-z0-9_])(?:since|until|since_time|until_time|from):', query, re.I):
        raise ValueError('Global topic search cannot add author or time overrides')
    until = end.date() + timedelta(days=1)
    return {'searchTerms': ['(' + query.strip() + ') since:' + start.date().isoformat() + ' until:' + until.isoformat()],
            'sort': args['sort'], 'maxItems': limit}


def _web_input(args, approved_urls):
    if not isinstance(args['urls'], list) or not 1 <= len(args['urls']) <= 10:
        raise ValueError('Select between one and ten explicitly authorized URLs')
    allowed = {public_url(url) for url in approved_urls}
    urls = list(dict.fromkeys(public_url(url) for url in args['urls']))
    if not set(urls).issubset(allowed):
        raise ValueError('Crawler URLs exceed the approved targets')
    return {'startUrls': [{'url': url} for url in urls], 'crawlerType': 'cheerio',
            'maxCrawlDepth': 0, 'maxCrawlPages': len(urls), 'maxResults': len(urls),
            'proxyConfiguration': {'useApifyProxy': True},
            'maxConcurrency': 1, 'requestTimeoutSecs': 60,
            'maxRequestRetries': 0, 'maxSessionRotations': 0,
            'useSitemaps': False, 'useLlmsTxt': False, 'respectRobotsTxtFile': True,
            'saveMarkdown': True, 'saveHtml': False, 'saveFiles': False,
            'saveScreenshots': False, 'summarize': False}
