"""Reviewed extraction compilers; model arguments never select an Actor."""
from datetime import datetime, time, timedelta, timezone
from .actor_profiles import PROFILES
from zoneinfo import ZoneInfo
import re

from .runtime_core import ConnectorUnavailable, ProviderQueryRejected, public_url, research_window
from .finance import compile_finance
from .contracts import ROLLING_DAYS_PATTERN
from .serp import compile_serp_input
from .social import compile_social_input






def require_available(profile_id):
    reason = PROFILES[profile_id].get('unavailable_reason')
    if reason:
        raise ConnectorUnavailable(reason)


def compile_keyword_query(groups,*,boolean_or=True,quote_terms=True):
    if not isinstance(groups,list) or not 1<=len(groups)<=3:
        raise ProviderQueryRejected('Select one to three keyword concepts')
    concepts=[]
    for variants in groups:
        if not isinstance(variants,list) or not 1<=len(variants)<=3:
            raise ProviderQueryRejected('Select one to three alternatives per concept')
        if any(not isinstance(term,str) or not 1<=len(term)<=40 or term!=term.strip()
               or not re.fullmatch(r"[\w][\w .’'&+\-]*",term) for term in variants):
            raise ProviderQueryRejected('Keywords must be literal terms without query operators or destinations')
        if boolean_or and not quote_terms and any(re.search(r'(?:^|\s)(?:OR|AND|NOT)(?=\s|$)|(?:^|\s)[+\-]|\.\.',term) for term in variants):
            raise ProviderQueryRejected('Unquoted keywords cannot contain active search syntax')
        terms=list(dict.fromkeys('"'+term+'"' if quote_terms else term for term in variants))
        concepts.append('('+' OR '.join(terms)+')' if boolean_or and len(terms)>1 else terms[0])
    return ' '.join(concepts)


def resolve_window(kind, clock, timezone_name):
    if clock.tzinfo is None:
        raise ValueError('Research clock must be timezone-aware')
    end = clock.astimezone(timezone.utc)
    days = re.fullmatch(ROLLING_DAYS_PATTERN, kind) if isinstance(kind, str) else None
    if kind == 'calendar_today':
        zone = ZoneInfo(timezone_name)
        start = datetime.combine(end.astimezone(zone).date(), time.min, zone).astimezone(timezone.utc)
    elif kind == 'rolling_24h' or days:
        start = end - timedelta(days=int(days[1]) if days else 1)
    else:
        raise ValueError('Unsupported research window')
    return {'start': start.isoformat(), 'end': end.isoformat()}


def _validate_parameters(args, cap_microusd, *, unlimited=False):
    if not (unlimited is True and cap_microusd is None) and (type(cap_microusd) is not int or not 0 < cap_microusd <= 5000000):
        raise ValueError('A bounded approved provider reservation is required')
    if set(args) - {'query', 'urls', 'max_items', 'sort', 'financial', 'search_mode'} or not {'query', 'urls', 'max_items', 'sort'}.issubset(args):
        raise ValueError('Unsupported provider parameters')
    if args.get('search_mode', 'research') not in ('research', 'navigation'):
        raise ValueError('Unsupported public search mode')
    limit = args['max_items']
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Result limit must be between one and fifty')
    return limit


def compile_call(profile_id, args, window, cap_microusd, *, approved_urls=(), approved_finance=None, usage_cost_verified=False, require_all_urls=False, approved_search_context=None, unlimited=False, reference_search=False, render_javascript=False):
    require_available(profile_id)
    profile = PROFILES[profile_id]
    start, end = research_window(window)
    limit = min(_validate_parameters(args, cap_microusd, unlimited=unlimited), profile.get('max_items', 50))
    if args.get('search_mode') == 'navigation' and profile_id not in {'web_search', 'web_search_localized', 'web_search_serp'}:
        raise ValueError('Source navigation requires a public web search')
    if cap_microusd is not None and cap_microusd < profile.get('minimum_cap_microusd', 0):
        raise ConnectorUnavailable('La búsqueda de X necesita un límite de al menos US$0,02 por consulta. '
                                   'El límite actual es menor; no se inició esa búsqueda.')
    inputs=_source_input(profile_id,args,start,end,limit,approved_urls=approved_urls,
        approved_finance=approved_finance,usage_cost_verified=usage_cost_verified,
        require_all_urls=require_all_urls,approved_search_context=approved_search_context)
    options=_call_options(profile_id,limit,cap_microusd)
    inputs,options=_runtime_read_policy(profile_id,args,inputs,options,reference_search,render_javascript)
    if profile_id in {'x_search','instagram_posts','world_bank','facebook_search','instagram_search'} and cap_microusd is None:
        options.pop('maxItems')
    # Return the run identity before the native MCP helper's shorter deadline.
    return {'actor': profile['actor'], 'input': inputs, 'waitSecs': 0,
            'callOptions': options}


def _runtime_read_policy(profile_id,args,inputs,options,reference_search,render_javascript):
    if type(reference_search) is not bool or type(render_javascript) is not bool:
        raise ValueError('Runtime read policies must be explicit booleans')
    if reference_search:
        if profile_id!='x_search':
            raise ValueError('Reference post search belongs to the X adapter')
        inputs={**inputs,'searchTerms':['('+args['query'].strip()+')'],'sort':'Top','maxItems':min(inputs['maxItems'],20)}
        options={**options,'maxItems':min(options['maxItems'],20)}
    if render_javascript:
        if profile_id!='website':
            raise ValueError('Browser rendering belongs to the website adapter')
        inputs={**inputs,'crawlerType':'playwright:adaptive'}
        options={**options,'memory':4096,'timeout':180}
    return inputs,options


def _source_input(profile_id,args,start,end,limit,*,approved_urls,approved_finance,
                  usage_cost_verified,require_all_urls,approved_search_context):
    if profile_id in {'yahoo_finance', 'yahoo_history', 'world_bank'}:
        if args['query'] or args['urls']:
            raise ValueError('Financial sources accept only approved structured selections')
        inputs = compile_finance(profile_id, args.get('financial'), approved_finance, limit, end)
    elif args.get('financial') is not None:
        raise ValueError('Financial parameters do not belong to this connector')
    elif profile_id == 'x_search':
        inputs = _x_input(args, start, end, limit)
    elif profile_id == 'web_search':
        inputs = _web_search_input(args, limit)
    elif profile_id in {'web_search_localized', 'web_search_serp'}:
        inputs = _localized_search_input(args, limit, start, end, approved_search_context)
        if profile_id == 'web_search_serp':
            inputs = compile_serp_input(inputs)
    elif profile_id == 'youtube_search':
        inputs = _youtube_search_input(args, limit, end - start)
    elif profile_id in {'facebook_posts','instagram_posts'}:
        inputs = compile_social_input(profile_id,args,approved_urls,start,end,limit)
    elif profile_id in {'facebook_search','instagram_search'}:
        inputs = _social_search_input(profile_id,args,limit)
    elif profile_id == 'website':
        if not usage_cost_verified:
            raise ConnectorUnavailable('La lectura de páginas necesita un límite de coste verificado antes de continuar. No se inició esa lectura.')
        inputs = _web_input(args, approved_urls, require_all=require_all_urls)
    return inputs


def _social_search_input(profile_id,args,limit):
    query=_public_search_query(args)
    if ',' in query or re.search(r'(?<!\w)(?:OR|AND|NOT)(?!\w)',query):
        raise ProviderQueryRejected('This source accepts one keyword phrase without Boolean operators')
    if profile_id=='facebook_search':
        return {'categories':[query],'locations':[],'searchType':'posts','resultsLimit':limit}
    return {'search':query,'searchType':'popular','searchLimit':limit,'liveSearch':True,
            'enhanceUserSearchWithFacebookPage':False}


def _call_options(profile_id, limit, cap_microusd):
    profile=PROFILES[profile_id]
    return {'build':profile['build'],
            'memory':profile.get('memory',512 if profile_id in {'website','yahoo_history'} else 256),
            'timeout':profile.get('timeout',120 if profile_id=='website' else 60),
            'maxItems':profile.get('max_dataset_items',limit),
            **({'maxTotalChargeUsd':cap_microusd/1000000} if cap_microusd is not None else {})}


def _x_input(args, start, end, limit):
    query = args['query']
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500 or any(ord(c) < 32 for c in query):
        raise ProviderQueryRejected('A bounded global topic search is required')
    if args['urls'] or args['sort'] not in {'Latest', 'Top'}:
        raise ValueError('Invalid X search arguments')
    # Dates supplied by the controller cannot be overridden by a model query.
    if re.search(r'(?<![a-z0-9_])(?:since|until|since_time|until_time|from):', query, re.I):
        raise ProviderQueryRejected('Global topic search cannot add author or time overrides')
    until = end.date() + timedelta(days=1)
    return {'searchTerms': ['(' + query.strip() + ') since:' + start.date().isoformat() + ' until:' + until.isoformat()],
            'sort': args['sort'], 'maxItems': limit}


def _web_input(args, approved_urls, *, require_all=False):
    if not isinstance(args['urls'], list) or not 1 <= len(args['urls']) <= 10:
        raise ValueError('Select between one and ten explicitly authorized URLs')
    allowed = {public_url(url) for url in approved_urls}
    urls = list(dict.fromkeys(public_url(url) for url in args['urls']))
    if not set(urls).issubset(allowed):
        raise ValueError('Crawler URLs exceed the approved targets')
    if require_all and set(urls) != allowed:
        raise ValueError('A pointed read must include exactly its explicitly requested pages')
    return {'startUrls': [{'url': url} for url in urls], 'crawlerType': 'cheerio',
            'maxCrawlDepth': 0, 'maxCrawlPages': len(urls), 'maxResults': len(urls),
            'proxyConfiguration': {'useApifyProxy': True},
            'maxConcurrency': 1, 'requestTimeoutSecs': 60,
            'maxRequestRetries': 0, 'maxSessionRotations': 0,
            'useSitemaps': False, 'useLlmsTxt': False, 'respectRobotsTxtFile': True,
            'saveMarkdown': True, 'saveHtml': False, 'saveFiles': False,
            'saveScreenshots': False, 'summarize': False}


def _public_search_query(args):
    query = args['query']
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500 or any(ord(c) < 32 for c in query):
        raise ProviderQueryRejected('A bounded public web search is required')
    if args['urls'] or args['sort'] not in {'Latest', 'Top'} or re.search(r'[a-z][a-z0-9+.-]*://|(?:file|data|javascript|ftp|mailto|tel):', query, re.I):
        raise ValueError('Web discovery accepts search terms, not direct URL retrieval')
    for host in re.findall(r'(?<!\w)site:([^\s()]+)', query, re.I):
        public_url('https://' + host.rstrip('/'))
    return query.strip()


def _web_search_input(args, limit):
    return {'query': _public_search_query(args), 'maxResults': limit, 'outputFormats': ['markdown'],
            'requestTimeoutSecs': 40, 'scrapingTool': 'raw-http', 'desiredConcurrency': 1,
            'proxyConfiguration': {'useApifyProxy': True}, 'serpMaxRetries': 0,
            'maxRequestRetries': 0, 'debugMode': False}


def _localized_search_input(args, limit, start, end, context):
    if not isinstance(context, dict) or set(context) != {'country_code', 'language_code'}:
        raise ValueError('Localized search needs its exact approved country and language')
    country, language = context['country_code'], context['language_code']
    if (not isinstance(country, str) or not re.fullmatch(r'[A-Z]{2}', country)
            or not isinstance(language, str) or not re.fullmatch(r'[a-z]{2}(?:-[A-Z]{2})?', language)):
        raise ValueError('Invalid approved search country or language')
    query = _public_search_query(args)
    if re.search(r'(?<![a-z0-9_])(?:before|after|since|until|daterange):', query, re.I):
        raise ProviderQueryRejected('Localized search cannot override its approved date filters')
    dates = {} if args.get('search_mode') == 'navigation' else {
        'dateFrom': start.date().isoformat(), 'dateTo': end.date().isoformat()}
    return {'query': query, 'provider': 'google', 'maxResults': limit,
            'countryCode': country, 'languageCode': language, **dates}


def _youtube_search_input(args, limit, duration):
    if duration > timedelta(days=7):
        raise ConnectorUnavailable('La búsqueda de YouTube configurada cubre como máximo siete días. Puedes acotar el periodo o elegir otra fuente; no se inició esa búsqueda.')
    return {'searchQueries': [_public_search_query(args)], 'startUrls': [],
            'sortingOrder': 'date' if args['sort'] == 'Latest' else 'relevance',
            'maxResults': limit, 'maxResultsShorts': 0, 'maxResultStreams': 0,
            'dateFilter': 'week', 'transcriptionAndSubtitle': 'NONE',
            'aiVideoDescription': False, 'aiVideoSummary': False}
