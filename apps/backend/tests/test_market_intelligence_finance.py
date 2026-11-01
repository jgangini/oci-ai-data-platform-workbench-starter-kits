"""Financial adapters validate synthetic samples of publisher-documented shapes."""
import copy
from datetime import datetime, timezone

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.finance import (
    ACTORS, compile_finance, normalize_finance,
)


CLOCK = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
SCOPE = {'tickers': ['AAPL', '^GSPC', 'MSFT'], 'countries': ['CO', 'COL', 'BR'],
         'indicator_codes': ['NY.GDP.MKTP.CD', 'FP.CPI.TOTL.ZG']}
BASE = {'tickers': [], 'countries': [], 'indicator_codes': [],
        'start_date': None, 'end_date': None, 'year_from': None, 'year_to': None}
YAHOO = {**BASE, 'tickers': ['AAPL'], 'start_date': '2026-10-01', 'end_date': '2026-10-31'}
WORLD = {**BASE, 'countries': ['CO'], 'indicator_codes': ['NY.GDP.MKTP.CD'], 'year_from': 2020, 'year_to': 2026}


def call_for(profile, args=None):
    return {'actor': ACTORS[profile], 'observed_at': CLOCK.isoformat(),
            'input': compile_finance(profile, args or (WORLD if profile == 'world_bank' else YAHOO), SCOPE, 10, CLOCK)}


def yahoo_row():
    return {'ticker': 'AAPL', 'stock_info': {'currency': 'USD'},
            'history': {'symbol': 'AAPL', 'data': [{'date': '2026-10-02', 'close': 0},
                                                {'date': '2026-10-01', 'close': None}]}}


def world_row():
    return {'recordType': 'series', 'countryCode': 'CO', 'countryIso3': 'COL', 'isAggregate': False,
            'indicatorCode': 'NY.GDP.MKTP.CD', 'unit': 'current US$',
            'values': {'2021': 0, '2020': None}, 'dataLastUpdated': '2026-07-13'}


def test_compile_yahoo_emits_only_reviewed_bounded_fields_without_mutating_inputs():
    args, scope = copy.deepcopy(YAHOO), copy.deepcopy(SCOPE)
    assert compile_finance('yahoo_finance', args, scope, 1, CLOCK) == {
        'tickers': ['AAPL'], 'start_date': '2026-10-01', 'end_date': '2026-10-31'}
    assert args == YAHOO and scope == SCOPE


def test_compile_world_bank_uses_exact_series_limit_and_no_expansion():
    result = compile_finance('world_bank', WORLD, SCOPE, 1, CLOCK)
    assert result == {'countries': ['CO'], 'indicatorCodes': ['NY.GDP.MKTP.CD'],
                      'yearFrom': 2020, 'yearTo': 2026, 'outputFormat': 'series', 'maxResults': 1,
                      'latestValueOnly': False, 'includeAggregates': False, 'skipEmptyValues': False,
                      'includeIndicatorDetails': True, 'includeCountryDetails': True}


@pytest.mark.parametrize('limit', [False, 0, 51, 1.5])
def test_financial_limits_are_positive_bounded_integers(limit):
    with pytest.raises(ValueError):
        compile_finance('world_bank', WORLD, SCOPE, limit, CLOCK)


@pytest.mark.parametrize(('profile', 'changes'), [
    ('yahoo_finance', {'tickers': []}), ('yahoo_finance', {'tickers': ['D1']}),
    ('yahoo_finance', {'tickers': ['AAPL', 'AAPL']}), ('yahoo_finance', {'tickers': ['AAPL'] * 4}),
    ('yahoo_finance', {'tickers': [True]}), ('yahoo_finance', {'countries': False}),
    ('yahoo_finance', {'countries': ['CO']}), ('yahoo_finance', {'market': 'IN'}),
    ('yahoo_finance', {'start_date': None}), ('yahoo_finance', {'start_date': '20261001'}),
    ('yahoo_finance', {'start_date': '2026-09-01'}), ('yahoo_finance', {'end_date': '2026-11-02'}),
    ('yahoo_finance', {'end_date': '2026-09-30'}), ('yahoo_finance', {'year_to': True}),
    ('world_bank', {'countries': ['ALL']}), ('world_bank', {'countries': ['MX']}),
    ('world_bank', {'indicator_codes': ['NY.UNAPPROVED']}), ('world_bank', {'indicator_codes': []}),
    ('world_bank', {'tickers': ['AAPL']}), ('world_bank', {'year_from': 1959}),
    ('world_bank', {'year_to': 2027}), ('world_bank', {'year_to': True}),
    ('world_bank', {'year_from': None}), ('world_bank', {'year_from': 2016}),
    ('world_bank', {'start_date': '2026-10-01'}), ('world_bank', {'indicatorSearch': 'GDP'}),
])
def test_compile_rejects_unbounded_unauthorized_or_cross_profile_arguments(profile, changes):
    args = YAHOO if profile == 'yahoo_finance' else WORLD
    with pytest.raises(ValueError):
        compile_finance(profile, {**args, **changes}, SCOPE, 10, CLOCK)


def test_financial_series_cannot_silently_exceed_requested_result_count():
    args = {**WORLD, 'countries': ['CO', 'BR'], 'indicator_codes': SCOPE['indicator_codes']}
    with pytest.raises(ValueError, match='result limit'):
        compile_finance('world_bank', args, SCOPE, 3, CLOCK)


def test_finance_discovery_and_missing_authenticated_clock_are_rejected():
    with pytest.raises(ValueError, match='approved scope'):
        compile_finance('world_bank', WORLD, {**SCOPE, 'allow_discovery': True}, 10, CLOCK)
    with pytest.raises(ValueError, match='timezone-aware'):
        compile_finance('world_bank', WORLD, SCOPE, 10, datetime(2026, 11, 1))


def test_yahoo_observation_preserves_zero_unknown_currency_period_and_attribution():
    row, call = yahoo_row(), call_for('yahoo_finance')
    originals = copy.deepcopy((row, call))
    result, = normalize_finance(row, call['actor'], 'synthetic-run', call)
    assert result['url'] == 'https://finance.yahoo.com/quote/AAPL/history/'
    assert result['published_at'] is None and result['observed_at'] == CLOCK.isoformat()
    assert result['metrics'] == {'metric': 'close', 'unit': 'USD', 'currency': 'USD', 'entity': 'AAPL',
                                 'indicator': None, 'points': [{'period': '2026-10-01', 'value': None},
                                                             {'period': '2026-10-02', 'value': 0}]}
    assert result['provenance']['temporal_basis'] == 'financial_period'
    assert result['provenance']['provider_run_id'] == 'synthetic-run'
    assert result['provenance']['source'] == call['actor']
    assert 'no cita literal' in result['text']
    assert (row, call) == originals


def test_missing_currency_is_unknown_and_never_assumed_usd():
    row = yahoo_row()
    del row['stock_info']
    call = call_for('yahoo_finance')
    result, = normalize_finance(row, call['actor'], 'synthetic-run', call)
    assert result['metrics']['currency'] is None and result['metrics']['unit'] == 'unknown'


def test_yahoo_official_reference_escapes_the_exact_approved_ticker():
    call = call_for('yahoo_finance', {**YAHOO, 'tickers': ['^GSPC']})
    row = yahoo_row()
    row['ticker'] = row['history']['symbol'] = '^GSPC'
    result, = normalize_finance(row, call['actor'], 'synthetic-run', call)
    assert result['url'] == 'https://finance.yahoo.com/quote/%5EGSPC/history/'


def test_world_bank_series_preserves_measurement_year_and_dataset_revision_separately():
    call = call_for('world_bank')
    result, = normalize_finance(world_row(), call['actor'], 'synthetic-run', call)
    assert result['url'] == 'https://data.worldbank.org/indicator/NY.GDP.MKTP.CD?locations=CO'
    assert result['published_at'] is None
    assert result['metrics'] == {'metric': 'value', 'unit': 'current US$', 'currency': None, 'entity': 'COL',
                                 'indicator': 'NY.GDP.MKTP.CD',
                                 'points': [{'period': '2020', 'value': None}, {'period': '2021', 'value': 0}]}
    assert result['provenance']['temporal_basis'] == 'annual_period'
    assert result['provenance']['data_last_updated'] == '2026-07-13'


@pytest.mark.parametrize(('profile', 'changes'), [('yahoo_finance', {'stock_info': False}),
                                               ('world_bank', {'unit': False})])
def test_false_metadata_is_not_silently_treated_as_unknown(profile, changes):
    call = call_for(profile)
    row = yahoo_row() if profile == 'yahoo_finance' else world_row()
    with pytest.raises(ValueError):
        normalize_finance({**row, **changes}, call['actor'], 'synthetic-run', call)


@pytest.mark.parametrize('profile', ['yahoo_finance', 'world_bank'])
@pytest.mark.parametrize('bad_value', [True, '5.1', float('nan'), float('inf'), {}, 10 ** 400])
def test_normalization_rejects_non_numeric_or_non_finite_values(profile, bad_value):
    row = yahoo_row() if profile == 'yahoo_finance' else world_row()
    if profile == 'yahoo_finance':
        row['history']['data'][0]['close'] = bad_value
    else:
        row['values']['2020'] = bad_value
    call = call_for(profile)
    with pytest.raises(ValueError, match='finite numbers'):
        normalize_finance(row, call['actor'], 'synthetic-run', call)


@pytest.mark.parametrize('change', ['ticker', 'symbol', 'date', 'duplicate', 'oversized', 'unsupported'])
def test_yahoo_rejects_wrong_identity_period_or_undocumented_output(change):
    row = yahoo_row()
    if change == 'ticker':
        row['ticker'] = 'MSFT'
    elif change == 'symbol':
        row['history']['symbol'] = 'MSFT'
    elif change == 'date':
        row['history']['data'][0]['date'] = '2026-09-30'
    elif change == 'duplicate':
        row['history']['data'][0]['date'] = '2026-10-01'
    elif change == 'oversized':
        row['history']['data'] *= 16
    else:
        row['history']['data'] = [{'Date': '2026-10-01', 'Close': 1}]
    call = call_for('yahoo_finance')
    with pytest.raises(ValueError):
        normalize_finance(row, call['actor'], 'synthetic-run', call)


@pytest.mark.parametrize('changes', [
    {'countryCode': 'BR', 'countryIso3': 'BRA'}, {'indicatorCode': 'FP.CPI.TOTL.ZG'},
    {'isAggregate': True}, {'isAggregate': None}, {'recordType': 'observation'},
    {'values': {'2019': 1}}, {'values': {'2027': 1}},
    {'values': {str(year): 1 for year in range(2016, 2027)}},
])
def test_world_bank_rejects_unrequested_or_unbounded_output(changes):
    call = call_for('world_bank')
    with pytest.raises(ValueError):
        normalize_finance({**world_row(), **changes}, call['actor'], 'synthetic-run', call)


def test_empty_provider_series_does_not_fabricate_an_observation():
    call = call_for('world_bank')
    assert normalize_finance({**world_row(), 'values': {}}, call['actor'], 'synthetic-run', call) == []


def test_provider_errors_and_missing_observation_clock_are_not_financial_evidence():
    call = call_for('world_bank')
    with pytest.raises(ValueError):
        normalize_finance({**world_row(), 'error': 'provider failure'}, call['actor'], 'synthetic-run', call)
    del call['observed_at']
    with pytest.raises(ValueError, match='timezone-aware'):
        normalize_finance(world_row(), call['actor'], 'synthetic-run', call)


def candle_row():
    return {'symbol': 'AAPL', 'type': 'candle', 'date': '2026-10-02T13:30:00.000Z',
            'close': 298.01, 'currency': 'USD', 'interval': '1d'}


def test_compile_yahoo_history_caps_all_rows_and_disables_other_products():
    args, scope = copy.deepcopy(YAHOO), copy.deepcopy(SCOPE)
    assert compile_finance('yahoo_history', args, scope, 10, CLOCK) == {
        'operation': 'historical', 'operations': [], 'tickers': ['AAPL'],
        'startDate': '2026-10-01', 'endDate': '2026-10-31', 'interval': '1d',
        'includeEvents': False, 'includePrePost': False, 'maxItems': 10}
    assert args == YAHOO and scope == SCOPE


@pytest.mark.parametrize('changes', [
    {'tickers': ['D1']}, {'start_date': None}, {'start_date': '2026-09-01'},
    {'end_date': '2026-11-02'}, {'countries': ['CO']}, {'operation': 'quotes'},
    {'operations': ['quotes']}, {'maxItems': 0}, {'tickers': [True]},
])
def test_yahoo_history_reuses_scope_and_date_validation_without_accepting_raw_actor_options(changes):
    with pytest.raises(ValueError):
        compile_finance('yahoo_history', {**YAHOO, **changes}, SCOPE, 10, CLOCK)


@pytest.mark.parametrize('value', [298.01, 0, None])
def test_daily_candle_keeps_explicit_currency_period_and_unknown_value(value):
    row, call = {**candle_row(), 'close': value}, call_for('yahoo_history')
    originals = copy.deepcopy((row, call))
    result, = normalize_finance(row, call['actor'], 'synthetic-run', call)
    assert result['metrics'] == {'metric': 'close', 'unit': 'USD', 'currency': 'USD',
                                 'entity': 'AAPL', 'indicator': None,
                                 'points': [{'period': '2026-10-02', 'value': value}]}
    assert result['published_at'] is None and result['observed_at'] == CLOCK.isoformat()
    assert result['url'] == 'https://finance.yahoo.com/quote/AAPL/history/'
    assert result['provenance']['source_timestamp'] == row['date']
    assert result['provenance']['provider_run_id'] == 'synthetic-run'
    assert result['provenance']['source'] == ACTORS['yahoo_history']
    assert result['provenance']['temporal_basis'] == 'financial_period'
    assert 'no cita literal' in result['text']
    assert (row, call) == originals


@pytest.mark.parametrize('changes', [
    {'symbol': 'MSFT'}, {'type': 'dividend'}, {'type': 'quote'}, {'interval': '1h'},
    {'date': '2026-10-02'}, {'date': '2026-10-02T13:30:00'},
    {'date': '2026-09-30T13:30:00Z'}, {'date': '2026-11-01T13:30:00Z'},
    {'date': '2026-10-99T13:30:00Z'}, {'currency': False}, {'currency': '$'},
    {'close': True}, {'close': '298.01'}, {'close': float('nan')}, {'close': float('inf')},
    {'error': 'provider failure'},
])
def test_yahoo_history_rejects_unrequested_or_untrustworthy_candles(changes):
    call = call_for('yahoo_history')
    with pytest.raises(ValueError):
        normalize_finance({**candle_row(), **changes}, call['actor'], 'synthetic-run', call)


@pytest.mark.parametrize('timestamp,period', [
    ('2026-10-01T00:30:00+02:00', '2026-10-01'),
    ('2026-10-31T23:30:00-03:00', '2026-10-31'),
])
def test_daily_candle_preserves_provider_calendar_date_across_utc_boundaries(timestamp, period):
    call = call_for('yahoo_history')
    row, = normalize_finance({**candle_row(), 'date': timestamp}, call['actor'], 'synthetic-run', call)
    assert row['metrics']['points'][0]['period'] == period
    assert row['provenance']['source_timestamp'] == timestamp
    assert row['provenance']['period_basis'] == 'provider_timestamp_calendar_date'


@pytest.mark.parametrize('changes', [{'operation': 'quotes'}, {'operations': ['quotes']},
                                     {'operations': None}, {'interval': '1h'}])
def test_yahoo_history_requires_the_exact_admitted_operation(changes):
    call = call_for('yahoo_history')
    call['input'].update(changes)
    with pytest.raises(ValueError, match='operation'):
        normalize_finance(candle_row(), call['actor'], 'synthetic-run', call)


def test_yahoo_history_missing_currency_stays_unknown_and_missing_close_is_rejected():
    row, call = candle_row(), call_for('yahoo_history')
    del row['currency']
    result, = normalize_finance(row, call['actor'], 'synthetic-run', call)
    assert result['metrics']['currency'] is None and result['metrics']['unit'] == 'unknown'
    del row['close']
    with pytest.raises(ValueError, match='shape'):
        normalize_finance(row, call['actor'], 'synthetic-run', call)
