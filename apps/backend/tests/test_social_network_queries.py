"""Social chat regressions over generated SQL/evidence; no OCI calls or business data."""

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest


path = Path(__file__).parents[1] / 'app/labs/ai_social_network/notebooks/agent/social_agent.py'
spec = importlib.util.spec_from_file_location('social_query_contract', path)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

CONFIG = {'catalog': 'oci_medallion', 'gold_schema': 'oci_gold'}
SNAPSHOT = {'publication_id': 'fixture-receipt', 'published_at': '2026-10-29T10:00:00Z',
            'current_version': 11, 'history_version': 12, 'daily_version': 13,
            'comment_current_version':14, 'comment_history_version':15}
POST_KEY = 'a' * 64


def test_planner_output_schema_advertises_valid_filters_for_both_agents():
    legacy=agent.planning_schema('SocialQuery')
    native=agent.planning_schema('SondaQuery',('post','comment','page','indicator'))
    args=legacy['properties']['args']['properties']
    assert args['period']['enum']==['all','range']
    assert args['entity_scope']['enum']==['any','author','mentions']
    assert args['metric']['enum']==['posts','comments','likes','shares','views']
    assert args['platform']['enum']==['instagram','facebook','tiktok','x','linkedin','youtube']
    assert args['limit']=={'type':'integer','minimum':1,'maximum':50}
    assert 'document_type' not in args
    assert native['properties']['args']['properties']['document_type']['enum']==['post','comment','page','indicator']
    for period in args['period']['enum']:
        for metric in args['metric']['enum']:
            agent.resolved_args(SNAPSHOT,'summary',{'period':period,'metric':metric})
    for field,value in [('period','last_30_days'),('metric','count'),('entity_scope','official'),('platform','all')]:
        with pytest.raises(ValueError):agent.resolved_args(SNAPSHOT,'summary',{field:value})


@pytest.mark.parametrize('stage',['structured_output','request_normalization','plan_validation','query_validation'])
def test_planner_diagnostics_never_echo_untrusted_error_content(stage):
    private="credential=fixture-secret; SELECT private FROM anywhere; https://example.invalid/private"
    diagnostic=agent.planning_error(stage,ValueError(private))
    assert set(diagnostic)=={'stage','code','instruction'} and diagnostic['stage']==stage
    assert diagnostic['code']==('invalid_structured_output' if stage=='structured_output' else 'invalid_plan_constraints')
    assert not any(value in json.dumps(diagnostic) for value in ('fixture-secret','SELECT','example.invalid'))


def test_coverage_question_has_a_valid_grouped_count_plan_without_a_question_specific_fastpath():
    question='¿Qué redes tienen publicaciones Gold disponibles en los últimos 30 días? Indica cobertura.'
    assert agent.verified_plan(question) is None
    raw={'tool':'summary','args':{},'presentation':'table','continue_query':False}
    plan=agent.plan_for_question(SNAPSHOT,raw,question)
    agent.checked_plan(plan,[],SNAPSHOT)
    args=agent.resolved_args(SNAPSHOT,plan['tool'],plan['args'])
    assert args=={'period':'range','date_from':'2026-09-30','date_to':'2026-10-29','metric':'posts'}
    assert 'platform' not in args


def evidence(name, args, rows):
    return agent.query_evidence(SNAPSHOT, name, args, rows)


@pytest.mark.parametrize('value', ['youtube', 'YouTube', 'YOUTUBE', 'yotube', 'yt'])
def test_youtube_count_accepts_common_user_spelling_and_case(value):
    assert agent.canonical_platform(value) == 'youtube'
    sql = agent.tool_sql(CONFIG, SNAPSHOT, 'summary', {'platform': value, 'metric': 'posts'})
    assert 'ai_social_daily VERSION AS OF 13' in sql
    assert 'SUM(post_count)' in sql
    assert 'youtube'.encode().hex() in sql


@pytest.mark.parametrize('name', ['summary', 'search', 'ranking'])
def test_no_requested_dates_means_all_available_publications_including_undated(name):
    resolved = agent.resolved_args(SNAPSHOT, name, {})
    assert resolved['period'] == 'all'
    assert not resolved.get('date_from') and not resolved.get('date_to')
    sql = agent.tool_sql(CONFIG, SNAPSHOT, name, {})
    assert 'published_date BETWEEN' not in sql
    assert 'published_month BETWEEN' not in sql
    assert 'published_at IS NOT NULL' not in sql


def test_explicit_period_is_resolved_in_evidence_for_followup_questions():
    args = {'platform': 'YouTube', 'date_from': '2026-10-01', 'date_to': '2026-10-28'}
    result = evidence('summary', args, [{'platform': 'youtube', 'value': 4, 'post_count': 4}])
    assert result['args']['platform'] == 'youtube'
    assert result['args']['period'] == 'range'
    assert result['args']['date_from'] == '2026-10-01'
    assert result['args']['date_to'] == '2026-10-28'
    assert 'period' not in args, 'Resolving a query must not modify prior conversation evidence'
    sql = agent.tool_sql(CONFIG, SNAPSHOT, 'summary', args)
    assert "DATE '2026-10-01'" in sql and "DATE '2026-10-28'" in sql


def test_history_always_has_a_bounded_thirty_day_observation_window():
    args = agent.resolved_args(SNAPSHOT, 'history', {'post_key': POST_KEY})
    assert args['period'] == 'range'
    assert args['date_from'] == '2026-09-30'
    assert args['date_to'] == '2026-10-29'
    sql = agent.tool_sql(CONFIG, SNAPSHOT, 'history', {'post_key': POST_KEY})
    assert 'ai_social_history VERSION AS OF 12' in sql
    assert 'observation_month BETWEEN' in sql
    assert "observed_at < TIMESTAMP '2026-10-30 00:00:00'" in sql


def test_inclusive_range_accepts_366_dates_and_rejects_367():
    agent.tool_sql(CONFIG, SNAPSHOT, 'summary', {'date_from': '2024-01-01', 'date_to': '2024-12-31'})
    with pytest.raises(ValueError):
        agent.tool_sql(CONFIG, SNAPSHOT, 'summary', {'date_from': '2024-01-01', 'date_to': '2025-01-01'})


def test_most_commented_ranking_orders_the_gold_population_before_limiting():
    sql = agent.tool_sql(CONFIG, SNAPSHOT, 'ranking', {'metric': 'comments', 'limit': 7})
    assert 'ai_social_current VERSION AS OF 11' in sql
    assert 'comment_count' in sql and 'value' in sql
    assert re.search(r'ORDER BY\s+value\s+DESC\s+NULLS LAST\s*,\s*post_key', sql, re.I)
    assert sql.rfind('ORDER BY') < sql.rfind('LIMIT 7')
    assert 'LIMIT ' not in sql[:sql.index('ORDER BY')], 'Rank the population before limiting rows'
    assert re.search(r'(comment_count|value)\s+IS NOT NULL', sql, re.I)
    assert 'scope_posts' in sql and 'reported_count' in sql
    assert 'ORDER BY observed_at' not in sql


def test_oracle_comment_comparison_aggregates_filtered_current_posts_with_coverage():
    sql = agent.tool_sql(CONFIG, SNAPSHOT, 'summary',
                         {'entity': 'Oracle', 'entity_scope': 'any', 'metric': 'comments'})
    assert 'ai_social_current VERSION AS OF 11' in sql
    assert 'ai_social_daily' not in sql
    assert 'SUM(comment_count)' in sql
    assert 'COUNT(comment_count)' in sql and 'reported_count' in sql
    assert 'post_count' in sql and 'undated_count' in sql
    assert 'GROUP BY platform' in sql
    for field in ('author', 'text', 'url'):
        assert field in sql
    assert 'Oracle' not in sql and 'Oracle'.encode().hex() in sql


def test_author_and_mentions_are_distinct_entity_scopes():
    author = agent.tool_sql(CONFIG, SNAPSHOT, 'summary',
                            {'entity': '@oracle', 'entity_scope': 'author', 'metric': 'posts'})
    mentions = agent.tool_sql(CONFIG, SNAPSHOT, 'summary',
                              {'entity': 'oracle', 'entity_scope': 'mentions', 'metric': 'posts'})
    assert author != mentions
    assert 'author' in author
    assert 'text' in mentions
    assert 'instr(lower(author)' not in author, 'An author filter must not match an arbitrary substring'


@pytest.mark.parametrize('name,args', [
    ('ranking', {'limit': 0}), ('ranking', {'limit': 51}), ('ranking', {'limit': True}),
    ('ranking', {'metric': 'comment_count DESC; DROP TABLE x'}),
    ('summary', {'platform': "youtube'; DROP TABLE x"}),
    ('summary', {'entity_scope': 'all_credentials'}),
    ('summary', {'period': 'unbounded_history'}), ('summary', {'sql': 'SELECT secret'}),
    ('history', {}), ('details', {'post_key': 'unqueried-invalid-id'}),
])
def test_untrusted_plan_cannot_expand_identifiers_history_or_limits(name, args):
    with pytest.raises(ValueError):
        agent.tool_sql(CONFIG, SNAPSHOT, name, args)


def test_entity_and_text_values_are_sql_literals_and_not_executable_sql():
    value = "Oracle'); DROP TABLE ai_social_current; --"
    sql = agent.tool_sql(CONFIG, SNAPSHOT, 'summary',
                         {'entity': value, 'text': value, 'metric': 'comments'})
    assert sql.startswith('SELECT ') and ';' not in sql
    assert value not in sql and value.encode().hex() in sql
    assert 'VERSION AS OF 11' in sql


def test_single_network_count_does_not_add_an_unsolicited_chart():
    queries = [evidence('summary', {'platform': 'youtube', 'metric': 'posts'},
                        [{'platform': 'youtube', 'value': 7, 'post_count': 7}])]
    assert agent.build_visualization('none', queries) is None
    assert agent.build_visualization('auto', queries) is None


@pytest.mark.parametrize('kind', ['table', 'bar', 'pie'])
def test_requested_visualization_uses_only_queried_labels_and_values(kind):
    rows = [{'platform': 'instagram', 'value': 23, 'post_count': 23},
            {'platform': 'facebook', 'value': 17, 'post_count': 17}]
    queries = [evidence('summary', {'metric': 'posts'}, rows)]
    visual = agent.build_visualization(kind, queries)
    assert visual['version'] == 1 and visual['type'] == kind
    assert visual['rows'] == [['instagram', 23], ['facebook', 17]]
    assert len(visual['columns']) == 2
    assert isinstance(visual['title'], str) and isinstance(visual['caption'], str)
    assert set(visual) == {'version', 'type', 'title', 'columns', 'rows', 'caption'}
    assert rows == queries[0]['rows'], 'Rendering must preserve the original query evidence'


def test_followup_visualization_uses_latest_query_not_stale_previous_values():
    queries = [evidence('summary', {}, [{'platform': 'facebook', 'value': 99999}]),
               evidence('summary', {'metric': 'posts'},
                        [{'platform': 'instagram', 'value': 3}, {'platform': 'facebook', 'value': 2}])]
    visual = agent.build_visualization('bar', queries)
    assert visual['rows'] == [['instagram', 3], ['facebook', 2]]
    assert '99999' not in json.dumps(visual)


def test_partial_metric_coverage_keeps_unknown_values_distinct_from_zero():
    rows = [{'platform': 'instagram', 'value': None, 'reported_count': 0, 'post_count': 8},
            {'platform': 'facebook', 'value': 0, 'reported_count': 2, 'post_count': 3}]
    visual = agent.build_visualization('table', [evidence('summary', {'metric': 'comments'}, rows)])
    assert visual['rows'] == [['instagram', None], ['facebook', 0]]
    assert rows[0]['value'] is None


@pytest.mark.parametrize('rows', [[], [{'platform': 'facebook', 'value': 0}],
                                [{'platform': 'facebook', 'value': None}],
                                [{'platform': 'facebook', 'value': -1}]])
def test_invalid_pie_denominator_never_fabricates_percentages(rows):
    visual = agent.build_visualization('pie', [evidence('summary', {'metric': 'posts'}, rows)])
    assert visual is None or visual['type'] == 'table'


def test_ranking_automatically_presents_a_table_and_preserves_null_text():
    rows = [{'post_key': POST_KEY, 'platform': 'youtube', 'text': None, 'value': 42,
             'url': 'https://www.youtube.com/watch?v=fixture'}]
    visual = agent.build_visualization('auto', [evidence('ranking', {'metric': 'comments'}, rows)])
    assert visual['type'] == 'table'
    assert len(visual['columns']) <= 6 and len(visual['rows']) == 1
    assert 42 in visual['rows'][0]
    assert 'null' not in str(visual['rows'][0]), 'Missing text must not become the literal word null'


def test_chart_output_is_bounded_for_large_query_results():
    rows = [{'platform': 'network-' + str(index), 'value': index + 1} for index in range(50)]
    queries = [evidence('summary', {'metric': 'posts'}, rows)]
    for kind in ('bar', 'pie'):
        visual = agent.build_visualization(kind, queries)
        if visual is not None:
            assert len(visual['rows']) <= (50 if visual['type'] == 'table' else 20)
            assert len(visual['columns']) <= (6 if visual['type'] == 'table' else 2)


def test_model_cannot_supply_a_chart_as_executable_or_arbitrary_content():
    queries = [evidence('summary', {'metric': 'posts'}, [{'platform': 'youtube', 'value': 7}])]
    with pytest.raises(ValueError):
        agent.build_visualization({'type': 'bar', 'rows': [['invented', 999]]}, queries)


def test_broadening_a_previous_query_is_not_mistaken_for_a_repeat():
    queries = [evidence('summary', {'platform': 'youtube'}, [{'platform': 'youtube', 'value': 7}])]
    plan = {'tool': 'summary', 'args': {'metric': 'posts'}}
    assert agent.checked_plan(plan, queries, SNAPSHOT) == plan
    repeated = {'tool': 'summary', 'args': {'platform': 'youtube'}}
    assert agent.checked_plan(repeated, queries, SNAPSHOT)['tool'] == 'finish'


def test_details_evidence_does_not_claim_a_period_ignored_by_sql():
    args = {'post_key': POST_KEY, 'date_from': '2026-10-01', 'date_to': '2026-10-28'}
    sql = agent.tool_sql(CONFIG, SNAPSHOT, 'details', args)
    assert "published_date BETWEEN DATE '2026-10-01' AND DATE '2026-10-28'" in sql


def context_envelope(networks=None, **changes):
    value = {'version': 1, 'question': 'cuantos post hay en yotube', 'search_context': {'networks': networks or []}}
    value.update(changes)
    return json.dumps(value)


def test_saved_search_context_preserves_explicit_question_even_for_an_unselected_network():
    networks = [{'platform': 'youtube', 'selected': False, 'filters': ['https://www.youtube.com/user/Oracle/videos']},
                {'platform': 'instagram', 'selected': True, 'filters': ['@oracle']}]
    question, context = agent.input_context(context_envelope(networks))
    assert question == 'cuantos post hay en yotube'
    assert context == {'networks': networks}
    assert agent.input_context('  Pregunta sin configuración  ') == ('Pregunta sin configuración', {})


@pytest.mark.parametrize('value', [
    context_envelope(question=' '), context_envelope(question='x' * 501),
    context_envelope(version=2), context_envelope(credential='not-allowed'),
    context_envelope(search_context={'networks': [], 'sql': 'not-allowed'}),
    context_envelope([{'platform': 'youtube', 'selected': 1, 'filters': []}]),
    context_envelope([{'platform': 'youtube', 'selected': False, 'filters': [{}]}]),
    context_envelope([{'platform': 'youtube', 'selected': False, 'filters': ['x'] * 6}]),
    context_envelope([{'platform': 'youtube', 'selected': False, 'filters': []},
                      {'platform': 'yt', 'selected': True, 'filters': []}]),
    '{not-json', '', 'x' * 20001,
])
def test_invalid_context_is_rejected_before_native_invocation(value):
    with pytest.raises(ValueError):
        agent.input_context(value)


def test_memory_retains_resolved_query_context_without_replaying_chart_rows_and_receipts():
    previous = {'answer': 'Resumen', 'query_context': [{'tool': 'summary', 'args': {
        'entity': 'Oracle', 'metric': 'comments', 'period': 'range', 'date_from': '2026-10-01', 'date_to': '2026-10-28'}}],
        'visualization': {'rows': [['DO_NOT_REPLAY_ROWS', 99]]}, 'publication': {'marker': 'DO_NOT_REPLAY_RECEIPT'}}
    messages = [SimpleNamespace(type='human', content='anterior')] * 9
    messages += [SimpleNamespace(type='ai', content=json.dumps(previous)), SimpleNamespace(type='human', content='En pastel')]
    context = agent.conversation_context(messages)
    assert len(context) == 8
    parsed = json.loads(context[-2]['content'])
    assert parsed == {key: previous[key] for key in ('answer', 'query_context')}
    assert context[-1] == {'role': 'human', 'content': 'En pastel'}
    assert 'DO_NOT_REPLAY' not in json.dumps(context)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf'), True, 9007199254740992])
def test_non_finite_or_lossy_values_cannot_be_sent_to_chart_renderer(value):
    queries = [evidence('summary', {'metric': 'comments'}, [{'platform': 'youtube', 'value': value}])]
    with pytest.raises(RuntimeError):
        agent.build_visualization('bar', queries)


def test_query_evidence_distinguishes_a_successful_empty_result_from_unknown_metrics():
    assert evidence('summary', {}, [])['rows'] == []
    rows = [{'platform': 'youtube', 'value': None, 'post_count': 3, 'reported_count': 0, 'undated_count': 3}]
    item = evidence('summary', {'metric': 'comments'}, rows)
    assert item['rows'] == rows and item['rows'][0]['value'] is None
    assert item['args']['period'] == 'all' and item['publication_id'] == SNAPSHOT['publication_id']


@pytest.mark.parametrize('question', [
    'cuantos post hay en yotube', '¿Cuántos posts hay en YouTube?',
    'CUANTOS POST HAY EN YT', 'Por favor, cuantos post hay en youtube',
])
def test_verified_youtube_count_never_relies_on_the_model_to_choose_platform(question):
    plan = agent.verified_plan(question)
    assert plan['tool'] == 'summary' and plan['args']['metric'] == 'posts'
    assert plan['args']['platform'] == 'youtube'
    assert not plan['args'].get('entity') and not plan['args'].get('text')
    assert plan['presentation'] == 'none' and plan['continue_query'] is False
    assert agent.resolved_args(SNAPSHOT, plan['tool'], plan['args'])['period'] == 'all'


@pytest.mark.parametrize('question', [
    'realiza un ranking de los post mas comentados',
    'Realiza un ranking de los posts más comentados.',
    'Por favor, realiza un ranking de los posts más comentados',
])
def test_verified_most_commented_ranking_is_global_and_has_a_table(question):
    plan = agent.verified_plan(question)
    assert plan['tool'] == 'ranking' and plan['args']['metric'] == 'comments'
    assert not plan['args'].get('entity') and not plan['args'].get('platform')
    assert plan['presentation'] == 'table' and plan['continue_query'] is False
    assert agent.resolved_args(SNAPSHOT, plan['tool'], plan['args'])['period'] == 'all'


@pytest.mark.parametrize('question,entity', [
    ('en que red social hay mas comentarios de oracle', 'oracle'),
    ('¿En qué red social hay más comentarios sobre Oracle?', 'Oracle'),
    ('Por favor, en qué red social hay más comentarios de Café Andino', 'Café Andino'),
    ("en que red social hay mas comentarios de L'Oréal", "L'Oréal"),
])
def test_verified_entity_comparison_cannot_drop_the_requested_entity(question, entity):
    plan = agent.verified_plan(question)
    assert plan['tool'] == 'summary' and plan['args']['metric'] == 'comments'
    assert plan['args']['entity'].casefold() == entity.casefold()
    assert plan['args']['entity_scope'] == 'any'
    assert plan['presentation'] == 'bar' and plan['continue_query'] is False
    sql = agent.tool_sql(CONFIG, SNAPSHOT, plan['tool'], plan['args'])
    assert plan['args']['entity'].encode().hex() in sql
    assert plan['args']['entity'] not in sql and ';' not in sql


def test_verified_entity_is_a_literal_even_if_its_text_looks_like_sql():
    question = "en que red social hay mas comentarios de Café'); DROP TABLE posts; --"
    plan = agent.verified_plan(question)
    if plan is not None:
        assert plan['args']['entity']
        sql = agent.tool_sql(CONFIG, SNAPSHOT, plan['tool'], plan['args'])
        assert ';' not in sql and 'DROP TABLE' not in sql
        assert plan['args']['entity'].encode().hex() in sql


@pytest.mark.parametrize('question', [
    'cuantos post hay en youtube hoy', 'cuantos post hay en youtube en los últimos 30 días',
    'cuantos post hay en youtube del 2026-10-01 al 2026-10-28',
    'realiza un ranking de los post mas comentados de ayer',
    'realiza un ranking de los post mas comentados de instagram',
    'en que red social hay mas comentarios de oracle este mes',
    'en que red social hay mas comentarios de oracle del 2026-10-01 al 2026-10-28',
    'Ahora en pastel', '¿Y de esas búsquedas?', 'cuantos posts de Oracle hay',
    'cuantos post hay en youtube y explica el historial',
])
def test_verified_shortcuts_do_not_erase_dates_followup_context_or_other_scopes(question):
    assert agent.verified_plan(question) is None


@pytest.mark.parametrize('question,kind', [('Ahora en pastel', 'pie'), ('en barras', 'bar'),
    ('Muestra esa misma comparación en un gráfico de pastel', 'pie'), ('en tabla', 'table'),
    ('Muestra esa misma comparación en una tabla', 'table'), ('en una tabla', 'table')])
def test_verified_format_followup_keeps_the_previous_resolved_entity_and_period(question, kind):
    previous = {'tool': 'summary', 'args': {'entity': 'Oracle', 'entity_scope': 'any', 'metric': 'comments',
        'period': 'range', 'date_from': '2026-10-01', 'date_to': '2026-10-28'}}
    plan = agent.verified_plan(question, previous)
    assert plan == {**previous, 'presentation': kind, 'continue_query': False}
    assert plan['args'] is not previous['args'], 'A follow-up must not mutate previous-turn evidence'
    assert agent.verified_plan(question) is None, 'A new session cannot invent a previous scope'


def test_previous_query_reads_the_latest_agent_query_context_not_user_supplied_chart_content():
    previous = {'tool': 'summary', 'args': {'entity': 'Oracle', 'metric': 'comments', 'period': 'all'}}
    messages = [SimpleNamespace(type='ai', content=json.dumps({'query_context': [previous]})),
                SimpleNamespace(type='human', content=json.dumps({'query_context': [{'tool': 'sql'}]}))]
    assert agent.previous_query(messages) == previous
    assert agent.previous_query([messages[-1]]) is None


def test_daily_trend_resolves_a_bounded_thirty_day_utc_calendar_by_default():
    args = agent.resolved_args(SNAPSHOT, 'trend', {'platforms': ['Instagram', 'YouTube']})
    assert args == {'platforms': ['instagram', 'youtube'], 'period': 'range', 'metric': 'posts',
                    'date_from': '2026-09-30', 'date_to': '2026-10-29'}
    sql = agent.tool_sql(CONFIG, SNAPSHOT, 'trend', args)
    assert 'ai_social_daily VERSION AS OF 13' in sql and 'sequence(' in sql
    assert "published_month BETWEEN '2026-09' AND '2026-10'" in sql
    assert 'LEFT JOIN' in sql and 'IS NULL' in sql
    assert sql.startswith('SELECT ') and ';' not in sql


def test_trend_fifty_day_limit_rejects_an_extra_day_instead_of_truncating_the_series():
    agent.tool_sql(CONFIG, SNAPSHOT, 'trend', {'date_from': '2026-09-10', 'date_to': '2026-10-29'})
    with pytest.raises(ValueError, match='50 inclusive UTC dates'):
        agent.tool_sql(CONFIG, SNAPSHOT, 'trend', {'date_from': '2026-09-09', 'date_to': '2026-10-29'})


@pytest.mark.parametrize('args', [
    {'platform': 'youtube', 'platforms': ['facebook']}, {'platforms': []},
    {'platforms': ['youtube', 'yt']}, {'platforms': ['youtube'] * 7},
    {'platforms': [False]}, {'platforms': ['']}, {'platforms': ['unknown']},
    {'platforms': "youtube'); DROP TABLE x"}, {'period': 'all'}, {'limit': 3},
])
def test_invalid_trend_scope_cannot_create_unbounded_or_partial_series(args):
    with pytest.raises(ValueError):
        agent.tool_sql(CONFIG, SNAPSHOT, 'trend', args)


def trend_evidence(platforms=None, **changes):
    args = {'date_from': '2026-10-27', 'date_to': '2026-10-29', 'metric': 'comments'}
    if platforms is not None:
        args['platforms'] = platforms
    args.update(changes)
    rows = [{'period': day, **{network: 2 for network in agent.NETWORKS}}
            for day in ('2026-10-27', '2026-10-28', '2026-10-29')]
    rows[1]['facebook'] = None
    rows[2]['instagram'] = 0
    return evidence('trend', args, rows)


def test_line_chart_uses_real_dates_multiple_series_and_nullable_gaps():
    item = trend_evidence(['instagram', 'facebook'])
    visual = agent.build_visualization('line', [item])
    assert visual['type'] == 'line' and visual['columns'] == ['Fecha', 'Instagram', 'Facebook']
    assert visual['rows'] == [['2026-10-27', 2, 2], ['2026-10-28', 2, None], ['2026-10-29', 0, 2]]
    assert agent.build_visualization('auto', [item]) == visual
    assert 'UTC' in visual['caption'] and 'sin fecha' in visual['caption']
    assert item['rows'][1]['facebook'] is None


def test_six_network_line_or_table_retains_all_seven_columns():
    item = trend_evidence()
    for kind in ('line', 'table'):
        visual = agent.build_visualization(kind, [item])
        assert len(visual['columns']) == 7 and all(len(row) == 7 for row in visual['rows'])
        assert len(visual['rows']) == 3


@pytest.mark.parametrize('replacement', [[], ['2026-10-27', '2026-10-29'],
    ['2026-10-28', '2026-10-27', '2026-10-29'], ['2026-10-27', '2026-10-27', '2026-10-29']])
def test_line_chart_rejects_missing_or_reordered_dates_instead_of_inventing_a_calendar(replacement):
    item = trend_evidence(['instagram'])
    item['rows'] = [{'period': day, 'instagram': 1} for day in replacement]
    with pytest.raises(RuntimeError, match='every date in order'):
        agent.build_visualization('line', [item])


def test_requesting_line_on_non_temporal_summary_does_not_invent_a_time_axis():
    item = evidence('summary', {}, [{'platform': 'facebook', 'value': 9}, {'platform': 'youtube', 'value': 3}])
    assert agent.build_visualization('line', [item])['type'] == 'table'
    assert agent.verified_plan('Ahora en líneas', {'tool': 'summary', 'args': {'metric': 'comments'}}) is None
    trend = trend_evidence(['instagram'])
    previous = {key: trend[key] for key in ('tool', 'args')}
    plan = agent.verified_plan('Ahora en líneas', previous)
    assert plan == {**previous, 'presentation': 'line', 'continue_query': False}


def test_trend_entity_filters_are_encoded_and_metric_columns_come_from_the_allowlist():
    value = "Café'); DROP TABLE x; --"
    sql = agent.tool_sql(CONFIG, SNAPSHOT, 'trend', {'platforms': ['Instagram', 'yt'], 'entity': value, 'metric': 'comments'})
    assert value not in sql and value.encode().hex() in sql and ';' not in sql
    assert 'SUM(comment_count)' in sql and 'COUNT(comment_count)' in sql
    assert 'undated_count' in sql and 'reported_count' in sql


def test_comment_text_search_reads_committed_comment_gold_and_not_the_parent_post_body():
    sql = agent.tool_sql(CONFIG, SNAPSHOT, 'comments', {'entity': 'Oracle', 'platform': 'facebook'})
    assert 'ai_social_comment_current VERSION AS OF 14' in sql
    assert 'comment_key' in sql and 'capture_scope' in sql
    assert "concat_ws(' ',author,text)" in sql and "concat_ws(' ',author,text,url)" not in sql
    assert 'commented_date BETWEEN' not in sql
    assert 'ORDER BY observed_at DESC, comment_key LIMIT 50' in sql
    assert 'ai_social_current ' not in sql


def test_comment_range_filters_comment_date_and_keeps_partial_capture_metadata():
    args = {'text': 'Oracle', 'date_from': '2026-10-01', 'date_to': '2026-10-29', 'limit': 8}
    sql = agent.tool_sql(CONFIG, SNAPSHOT, 'comments', args)
    assert "commented_date BETWEEN DATE '2026-10-01' AND DATE '2026-10-29'" in sql
    assert 'published_date' not in sql and 'LIMIT 8' in sql
    assert evidence('comments', args, [])['period_basis'] == 'comment'


def test_unpublished_comment_snapshot_does_not_masquerade_as_an_empty_comment_search():
    legacy = {key: value for key, value in SNAPSHOT.items() if not key.startswith('comment_')}
    assert agent.checked_snapshot([legacy]) == legacy
    agent.tool_sql(CONFIG, legacy, 'summary', {})
    with pytest.raises(RuntimeError, match='not available in this committed Gold publication'):
        agent.tool_sql(CONFIG, legacy, 'comments', {'text': 'Oracle'})
    with pytest.raises(ValueError, match='not complete comment metric aggregation'):
        agent.tool_sql(CONFIG, SNAPSHOT, 'comments', {'metric': 'likes'})


def test_comment_search_encodes_entity_text_and_parent_reference_filters():
    value = "Oracle'); DELETE FROM comments; --"
    sql = agent.tool_sql(CONFIG, SNAPSHOT, 'comments', {'entity': value, 'post_key': POST_KEY})
    assert value not in sql and value.encode().hex() in sql and ';' not in sql
    assert POST_KEY.encode().hex() in sql


def test_comment_table_labels_partial_captured_text_and_preserves_unknown_comment_likes_and_dates():
    rows = [{'comment_key': 'c' * 64, 'post_key': POST_KEY, 'platform': 'facebook', 'text': 'Oracle discussion',
             'author': None, 'like_count': None, 'commented_at': None, 'capture_scope': 'top_comments'}]
    visual = agent.build_visualization('auto', [evidence('comments', {'text': 'Oracle'}, rows)])
    assert visual['type'] == 'table' and visual['title'] == 'Comentarios capturados'
    assert visual['rows'] == [['Oracle discussion', 'facebook', None, None, None]]
    assert 'parcial' in visual['caption'] and '50 coincidencias' in visual['caption']
    assert visual['columns'][-1] == 'Fecha del comentario'


@pytest.mark.parametrize('question,word', [
    ('Busca la palabra Oracle en los comentarios capturados', 'Oracle'),
    ('BUSCA LA PALABRA ORACLE EN LOS COMENTARIOS CAPTURADOS', 'ORACLE'),
    ('Por favor, busca la palabra Café en los comentarios capturados', 'Café'),
])
def test_verified_captured_comment_word_search_cannot_be_routed_to_post_search(question, word):
    assert agent.verified_plan(question) == {'tool': 'comments', 'args': {'text': word},
                                            'presentation': 'table', 'continue_query': False}


def test_verified_comment_word_with_punctuation_remains_an_encoded_search_literal():
    word = "Café'); DROP TABLE posts; --"
    plan = agent.verified_plan('Busca la palabra ' + word + ' en los comentarios capturados')
    assert plan['tool'] == 'comments' and plan['args']['text'] == word
    sql = agent.tool_sql(CONFIG, SNAPSHOT, plan['tool'], plan['args'])
    assert word.encode().hex() in sql and word not in sql and ';' not in sql


@pytest.mark.parametrize('question', [
    'Busca la palabra Oracle en los comentarios capturados de ayer',
    'Busca la palabra Oracle en los comentarios capturados del 2026-10-01 al 2026-10-28',
    'Busca la palabra Oracle en los comentarios capturados de Instagram',
    'Busca esa misma palabra en los comentarios capturados',
    'Busca la palabra Oracle en las publicaciones',
])
def test_verified_comment_word_shortcut_does_not_erase_other_periods_or_scopes(question):
    assert agent.verified_plan(question) is None


LINE_QUESTION = 'Compara las publicaciones de Instagram y Facebook del 2026-10-23 al 2026-10-29 con un gráfico de líneas.'


def test_captured_native_line_plan_binds_requested_dates_and_drops_only_generic_row_limit():
    captured = {'presentation': 'line', 'continue_query': False, 'tool': 'trend',
                'args': {'platforms': ['instagram', 'facebook'], 'limit': 50}}
    before = json.dumps(captured, sort_keys=True)
    plan = agent.plan_for_question(SNAPSHOT, captured, LINE_QUESTION)
    assert plan == {'presentation': 'line', 'continue_query': False, 'tool': 'trend', 'args': {
        'platforms': ['instagram', 'facebook'], 'period': 'range', 'date_from': '2026-10-23',
        'date_to': '2026-10-29', 'metric': 'posts'}}
    assert json.dumps(captured, sort_keys=True) == before
    agent.checked_plan(plan, [], SNAPSHOT)
    sql = agent.tool_sql(CONFIG, SNAPSHOT, plan['tool'], plan['args'])
    assert "sequence(DATE '2026-10-23',DATE '2026-10-29'" in sql


def test_original_iso_range_overrides_wrong_model_dates_and_empty_unused_arguments():
    raw = {'tool': 'summary', 'presentation': 'line', 'args': {'platform': '', 'platforms': [],
        'entity': '', 'date_from': '2026-01-01', 'date_to': '2026-01-02', 'period': 'all', 'limit': 50}}
    plan = agent.plan_for_question(SNAPSHOT, raw, LINE_QUESTION)
    assert plan['tool'] == 'trend' and plan['args'] == {'period': 'range', 'date_from': '2026-10-23',
                                                     'date_to': '2026-10-29', 'metric': 'posts'}
    assert 'platforms' in raw['args'], 'Normalization must not mutate a model response held as evidence'


def test_normalization_does_not_remove_a_real_limit_or_enable_free_sql():
    search = {'tool': 'search', 'args': {'limit': 50}}
    assert agent.plan_for_question(SNAPSHOT, search, 'Busca Oracle')['args']['limit'] == 50
    trend = agent.plan_for_question(SNAPSHOT, {'tool': 'trend', 'args': {'limit': 7}}, LINE_QUESTION)
    assert trend['args']['limit'] == 7
    with pytest.raises(ValueError):
        agent.tool_sql(CONFIG, SNAPSHOT, trend['tool'], trend['args'])
    injected = agent.plan_for_question(SNAPSHOT, {'tool': 'trend', 'args': {'sql': 'DROP TABLE x'}}, LINE_QUESTION)
    with pytest.raises(ValueError):
        agent.checked_plan(injected, [], SNAPSHOT)


def test_relative_days_are_inclusive_and_use_the_receipt_utc_date_not_local_wall_clock():
    snapshot = {**SNAPSHOT, 'published_at': '2026-10-29T00:30:00+02:00'}
    plan = agent.plan_for_question(snapshot, {'tool': 'trend', 'presentation': 'line', 'args': {}},
                                   'Compara publicaciones de los últimos 7 días')
    assert plan['args']['date_from'] == '2026-10-22' and plan['args']['date_to'] == '2026-10-28'
    assert plan['args']['metric'] == 'posts'


@pytest.mark.parametrize('question', ['Compara los posts con más comentarios', 'Compara las publicaciones y sus likes',
                                      'Compara las publicaciones y su engagement', 'Compara las publicaciones y sus interacciones',
                                      'Compara Instagram y Facebook'])
def test_question_binding_preserves_engagement_metric_when_posts_are_not_the_requested_count(question):
    raw = {'tool': 'trend', 'presentation': 'line', 'args': {'metric': 'likes'}}
    assert agent.plan_for_question(SNAPSHOT, raw, question)['args']['metric'] == 'likes'


@pytest.mark.parametrize('question', ['Compara publicaciones de los últimos 0 días',
    'Compara publicaciones de los últimos 367 días',
    'Compara publicaciones del 2026-02-30 al 2026-03-01',
    'Compara 2026-01-01, 2026-01-02 y 2026-01-03'])
def test_question_binding_rejects_invalid_or_ambiguous_ranges_before_sql(question):
    with pytest.raises(ValueError):
        agent.plan_for_question(SNAPSHOT, {'tool': 'trend', 'args': {}}, question)


def test_explicit_fifty_one_day_request_remains_an_error_and_is_never_silently_truncated():
    plan = agent.plan_for_question(SNAPSHOT, {'tool': 'trend', 'presentation': 'line', 'args': {'limit': 50}},
                                   'Compara publicaciones del 2026-09-09 al 2026-10-29')
    with pytest.raises(ValueError, match='50 inclusive UTC dates'):
        agent.tool_sql(CONFIG, SNAPSHOT, plan['tool'], plan['args'])
