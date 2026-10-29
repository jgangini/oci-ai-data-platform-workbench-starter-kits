"""Read-only serving contract; these tests never contact OCI or generate business data."""
import importlib.util
from pathlib import Path
import pytest

path = Path(__file__).parents[1] / 'app/labs/ai_social_network/notebooks/agent/social_agent.py'
spec = importlib.util.spec_from_file_location('social_agent', path)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
CONFIG = {'catalog': 'oci_medallion', 'gold_schema': 'oci_gold'}
SNAPSHOT = {'publication_id': 'v1', 'published_at':'2026-10-29T10:00:00Z', 'current_version':1, 'history_version':2, 'daily_version':3}


def test_every_tool_uses_exact_committed_gold_versions_and_fixed_bounded_sql():
    for name in agent.TOOLS:
        args = {'platform':'instagram', 'post_key':'a'*64} if name in ('history','details') else {'platform':'instagram'}
        sql = agent.tool_sql(CONFIG, SNAPSHOT, name, args)
        assert sql.startswith('SELECT ') and ('LIMIT 10' if name == 'ranking' else 'LIMIT 50') in sql and ';' not in sql
        version = 2 if name == 'history' else 3 if name == 'summary' else 1
        assert f'VERSION AS OF {version}' in sql
        assert 'oci_medallion.oci_gold.ai_social_' in sql
        assert 'ai_social_files' not in sql and 'oci_landing' not in sql
        if name == 'history':
            assert "observation_month BETWEEN '2026-09' AND '2026-10'" in sql
        if name == 'summary':
            assert 'SUM(post_count)' in sql and 'published_month BETWEEN' not in sql
            ranged = agent.tool_sql(CONFIG, SNAPSHOT, name, {**args, 'date_from':'2026-10-01', 'date_to':'2026-10-28'})
            assert "published_month BETWEEN '2026-10' AND '2026-10'" in ranged
            assert "published_date BETWEEN DATE '2026-10-01' AND DATE '2026-10-28'" in ranged


@pytest.mark.parametrize('name,args', [('sql',{}), ('search',{'sql':'DROP TABLE x'}), ('search',{'platform':'unknown'}),
    ('history',{}), ('details',{'post_key':"'; DROP"}), ('summary',{'post_key':'a'*64}),
    ('search',{'date_from':'2020-01-01','date_to':'2026-01-01'}), ('search',{'date_from':'2026-10-30'})])
def test_unbounded_history_unknown_tools_and_invalid_filters_fail_closed(name,args):
    with pytest.raises(ValueError):
        agent.tool_sql(CONFIG,SNAPSHOT,name,args)


def test_text_filters_are_encoded_literals_and_cannot_change_sql_structure():
    value = "x'); DELETE FROM posts; --"
    sql = agent.tool_sql(CONFIG,SNAPSHOT,'search',{'text':value})
    assert value not in sql and ';' not in sql
    assert value.encode().hex() in sql


def test_missing_publication_is_not_reported_as_no_data():
    for rows in ([], [{**SNAPSHOT,'current_version':-1}], [SNAPSHOT,SNAPSHOT]):
        with pytest.raises(RuntimeError):
            agent.checked_snapshot(rows)
    assert agent.checked_snapshot([SNAPSHOT]) == SNAPSHOT


def test_repeated_or_exhausted_plan_finishes_with_current_turn_evidence():
    plan = {'tool': 'summary', 'args': {'date_from': '2026-10-01', 'date_to': '2026-10-28'}}
    queries = [{**plan, 'rows': [{'platform': 'facebook', 'views': 26566}]}]
    assert agent.checked_plan(plan, queries) == {'tool': 'finish', 'args': {}}
    assert agent.checked_plan({'tool': 'search', 'args': {}}, queries * 4) == {'tool': 'finish', 'args': {}}
    assert agent.checked_plan(plan, []) == plan
    with pytest.raises(ValueError, match='current-turn Gold query'):
        agent.checked_plan({'tool': 'finish', 'args': {}}, [])
