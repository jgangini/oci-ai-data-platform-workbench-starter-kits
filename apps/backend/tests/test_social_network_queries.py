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
            'current_version': 11, 'history_version': 12, 'daily_version': 13}
POST_KEY = 'a' * 64


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
