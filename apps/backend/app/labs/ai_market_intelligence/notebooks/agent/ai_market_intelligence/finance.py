"""Bounded financial inputs and attributed observations from reviewed Actors."""
from datetime import date, datetime, timezone
import math
import re
from urllib.parse import quote


ACTORS = {'yahoo_finance': 'architjn/yahoo-finance',
          'world_bank': 'solidcode/world-bank-indicators-scraper'}
ARGUMENTS = {'tickers', 'countries', 'indicator_codes', 'start_date', 'end_date', 'year_from', 'year_to'}
PATTERNS = {'tickers': r'[A-Z0-9^][A-Z0-9^=.\-]{0,29}',
            'countries': r'[A-Z]{2,3}', 'indicator_codes': r'[A-Z0-9]+(?:\.[A-Z0-9]+)+'}


def _moment(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('A timezone-aware observation clock is required')
    return value.astimezone(timezone.utc)


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Explicit YYYY-MM-DD dates are required')
    return date.fromisoformat(value)


def _selection(values, allowed, name):
    if not isinstance(values, list) or not 1 <= len(values) <= 3 or not isinstance(allowed, list):
        raise ValueError('Select one to three explicitly approved financial identifiers')
    if any(not isinstance(value, str) for value in allowed):
        raise ValueError('Invalid financial approval list')
    if any(not isinstance(value, str) or not re.fullmatch(PATTERNS[name], value)
           or value == 'ALL' for value in values):
        raise ValueError('Invalid financial identifier')
    if len(set(values)) != len(values) or not set(values).issubset(allowed):
        raise ValueError('Financial selection exceeds the approved scope')
    return list(values)


def compile_finance(profile_id, args, approved_scope, limit, clock_end):
    """Compile only explicit, approved destinations; never infer a ticker or period."""
    if profile_id not in ACTORS or not isinstance(args, dict) or set(args) - ARGUMENTS:
        raise ValueError('Unsupported financial profile or arguments')
    if not isinstance(approved_scope, dict) or approved_scope.get('allow_discovery'):
        raise ValueError('Financial sources require explicit approved scope')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Financial result limit must be between one and fifty')
    if any(not isinstance(args[key], list) for key in ('tickers', 'countries', 'indicator_codes') if key in args):
        raise ValueError('Financial identifiers must be lists')
    end_clock = _moment(clock_end)
    if profile_id == 'yahoo_finance':
        return _yahoo_input(args, approved_scope, limit, end_clock)
    return _world_bank_input(args, approved_scope, limit, end_clock)


def _yahoo_input(args, approved_scope, limit, end_clock):
    if args.get('countries') or args.get('indicator_codes') or args.get('year_from') is not None or args.get('year_to') is not None:
        raise ValueError('World Bank arguments are not valid for Yahoo Finance')
    tickers = _selection(args.get('tickers'), approved_scope.get('tickers'), 'tickers')
    start, end = _date(args.get('start_date')), _date(args.get('end_date'))
    if not 0 <= (end - start).days <= 31 or end > end_clock.date() or len(tickers) > limit:
        raise ValueError('Yahoo history must fit the admitted date and result limits')
    return {'tickers': tickers, 'start_date': start.isoformat(), 'end_date': end.isoformat()}


def _world_bank_input(args, approved_scope, limit, end_clock):
    if args.get('tickers') or args.get('start_date') is not None or args.get('end_date') is not None:
        raise ValueError('Yahoo Finance arguments are not valid for World Bank')
    countries = _selection(args.get('countries'), approved_scope.get('countries'), 'countries')
    indicators = _selection(args.get('indicator_codes'), approved_scope.get('indicator_codes'), 'indicator_codes')
    start, end = args.get('year_from'), args.get('year_to')
    if type(start) is not int or type(end) is not int or not 1960 <= start <= end <= end_clock.year or end - start >= 10:
        raise ValueError('World Bank requires one to ten explicit historical years')
    if len(countries) * len(indicators) > limit:
        raise ValueError('Financial series exceed the admitted result limit')
    return {'countries': countries, 'indicatorCodes': indicators, 'yearFrom': start, 'yearTo': end,
            'outputFormat': 'series', 'maxResults': limit, 'latestValueOnly': False,
            'includeAggregates': False, 'skipEmptyValues': False,
            'includeIndicatorDetails': True, 'includeCountryDetails': True}


def _number(value):
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ValueError('Financial values must be finite numbers or explicit nulls')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError('Financial values must be finite numbers or explicit nulls')
    return value


def _text(value, limit=500):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or any(ord(char) < 32 for char in value):
        raise ValueError('Invalid financial metadata')
    return value


def _yahoo(row, inputs):
    ticker = _text(row.get('ticker'), 30)
    if ticker not in inputs.get('tickers', []):
        raise ValueError('Yahoo output ticker was not requested')
    history = row.get('history')
    if not isinstance(history, dict) or history.get('symbol', ticker) != ticker:
        raise ValueError('Unsupported Yahoo history shape or identity')
    start, end = _date(inputs.get('start_date')), _date(inputs.get('end_date'))
    points = _history_points(history.get('data'), start, end)
    stock = row.get('stock_info')
    if stock is None:
        stock = {}
    if not isinstance(stock, dict):
        raise ValueError('Unsupported Yahoo stock metadata shape')
    currency = stock.get('currency')
    if currency is not None and (not isinstance(currency, str) or not re.fullmatch(r'[A-Za-z]{3}', currency)):
        raise ValueError('Invalid Yahoo currency')
    metrics = {'metric': 'close', 'unit': currency or 'unknown', 'currency': currency,
               'entity': ticker, 'indicator': None, 'points': points}
    return 'https://finance.yahoo.com/quote/' + quote(ticker, safe='') + '/history/', metrics, {
        'temporal_basis': 'financial_period', 'requested_start': start.isoformat(), 'requested_end': end.isoformat()}


def _history_points(rows, start, end):
    if not isinstance(rows, list) or len(rows) > 31:
        raise ValueError('Yahoo history must contain at most thirty-one daily points')
    points = []
    for item in rows:
        if not isinstance(item, dict) or 'close' not in item:
            raise ValueError('Yahoo daily history lacks an explicit close value')
        period = _date(item.get('date'))
        if not start <= period <= end:
            raise ValueError('Yahoo output period exceeds the requested range')
        points.append({'period': period.isoformat(), 'value': _number(item['close'])})
    return points


def _world_bank(row, inputs):
    if row.get('recordType') != 'series' or row.get('isAggregate') is not False:
        raise ValueError('Only nonaggregate World Bank series are supported')
    country = _text(row.get('countryCode'), 3)
    iso3 = _text(row.get('countryIso3'), 3)
    if not re.fullmatch(r'[A-Z]{2}', country) or not re.fullmatch(r'[A-Z]{3}', iso3):
        raise ValueError('Invalid World Bank country identity')
    if not {country, iso3}.intersection(inputs.get('countries', [])):
        raise ValueError('World Bank output country was not requested')
    indicator = _text(row.get('indicatorCode'), 100)
    if indicator not in inputs.get('indicatorCodes', []):
        raise ValueError('World Bank output indicator was not requested')
    points = _annual_points(row.get('values'), inputs['yearFrom'], inputs['yearTo'])
    unit = row.get('unit')
    unit = 'unknown' if unit in (None, '') else _text(unit)
    metrics = {'metric': 'value', 'unit': unit, 'currency': None, 'entity': iso3,
               'indicator': indicator, 'points': points}
    provenance = {'temporal_basis': 'annual_period', 'country_code': country, 'country_iso3': iso3,
                  'indicator_code': indicator, 'requested_start': inputs['yearFrom'], 'requested_end': inputs['yearTo']}
    if row.get('dataLastUpdated') is not None:
        provenance['data_last_updated'] = _date(row['dataLastUpdated']).isoformat()
    return 'https://data.worldbank.org/indicator/' + quote(indicator, safe='.') + '?locations=' + country, metrics, provenance


def _annual_points(values, start, end):
    if not isinstance(values, dict) or len(values) > 10:
        raise ValueError('World Bank series must contain at most ten yearly points')
    points = []
    for period, value in values.items():
        if not isinstance(period, str) or not re.fullmatch(r'\d{4}', period) or not start <= int(period) <= end:
            raise ValueError('World Bank output period exceeds the requested range')
        points.append({'period': period, 'value': _number(value)})
    return points


def normalize_finance(row, actor, run_id, call):
    """Attribute structured figures to the Actor; the text is a derived summary."""
    if not isinstance(row, dict) or row.get('error') or not isinstance(call, dict) or call.get('actor') != actor:
        raise ValueError('Invalid financial provider result')
    _text(run_id, 128)
    observed = _moment(call.get('observed_at')).isoformat()
    inputs = call.get('input')
    if not isinstance(inputs, dict):
        raise ValueError('Financial input receipt is required')
    if actor == ACTORS['yahoo_finance']:
        url, metrics, provenance = _yahoo(row, inputs)
    elif actor == ACTORS['world_bank']:
        url, metrics, provenance = _world_bank(row, inputs)
    else:
        raise ValueError('Unsupported financial Actor')
    points = sorted(metrics['points'], key=lambda point: point['period'])
    if len({point['period'] for point in points}) != len(points):
        raise ValueError('Financial periods must be unique')
    if not points:
        return []
    metrics['points'] = points
    values = '; '.join(point['period'] + ': ' + ('desconocido' if point['value'] is None else str(point['value'])) for point in points)
    text = ('Resumen derivado de la salida del Actor ' + actor + ', no cita literal: ' + metrics['entity']
            + ('; ' + metrics['indicator'] if metrics['indicator'] else '') + '; ' + metrics['metric']
            + '; unidad: ' + metrics['unit'] + '; ' + values)
    return [{'url': url, 'text': text, 'published_at': None, 'observed_at': observed, 'metrics': metrics,
             'provenance': {**provenance, 'source': actor, 'provider_run_id': run_id,
                            'scope': 'bounded_financial_series', 'location': None,
                            'reference_kind': 'official_entity_page', 'summary_kind': 'derived_actor_output'}}]
