from datetime import datetime, timezone

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.proposals import Proposals, next_occurrence
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Journal
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.research import Research
from test_market_intelligence_repository import MemorySnapshot, SCOPE
from test_market_intelligence_research import TOPIC


NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
WEEKLY = {'weekdays': [1], 'hour': 9, 'minute': 0, 'timezone': 'America/Bogota'}


def base():
    journal = Journal(MemorySnapshot(), 100000)
    research = Research(journal)
    _, topic = research.record_question('q1', 'Pregunta uno', TOPIC, now=NOW.isoformat())
    return journal, research, Proposals(journal), topic


def test_recurrence_counts_independent_questions_not_retries_or_followups():
    journal, research, proposals, topic = base()
    research.record_question('q1', 'Pregunta uno', TOPIC, now=NOW.isoformat())
    research.record_question('followup', 'Explícalo', TOPIC, 'followup', NOW.isoformat())
    assert proposals.propose_schedule(topic, now=NOW) is None
    research.record_question('q2', 'Pregunta dos', TOPIC, now=NOW.isoformat())
    research.record_question('q3', 'Pregunta tres', TOPIC, now=NOW.isoformat())
    assert len(proposals.recurring_questions(topic, NOW)) == 3
    assert proposals.propose_schedule(topic, now=NOW)
    assert journal.read()[0]['tables']['ai_mi_schedules'] == {}


def test_a_yes_before_missing_details_does_not_activate_a_schedule():
    journal, _, proposals, topic = base()
    proposal = proposals.propose_schedule(topic, direct=True, now=NOW)
    with pytest.raises(ValueError, match='complete plan'):
        proposals.decide_schedule(proposal, None, True, 'yes', NOW)
    assert journal.read()[0]['tables']['ai_mi_schedules'] == {}


def test_rejected_suggestion_is_suppressed_for_thirty_days():
    _, _, proposals, topic = base()
    proposal = proposals.propose_schedule(topic, direct=True, now=NOW)
    proposals.decide_schedule(proposal, None, False, 'reject', NOW)
    assert proposals.propose_schedule(topic, direct=True, now=NOW) is None


def test_schedule_requires_exact_presented_plan_and_approved_topic_targets():
    journal, _, proposals, topic = base()
    source, target, link = (SCOPE.key(name) for name in ('source', 'target', 'link'))

    def add(records, state):
        records.upsert('ai_mi_sources', {'source_key': source, 'status': 'active'})
        records.upsert('ai_mi_source_targets', {'target_id': target, 'source_key': source, 'status': 'active'})
        records.upsert('ai_mi_topic_sources', {'topic_source_id': link, 'topic_id': topic, 'target_id': target, 'status': 'active'})

    journal.transact(add)
    proposal = proposals.propose_schedule(topic, direct=True, now=NOW)
    revision = proposals.present_schedule(proposal, WEEKLY, {'window_days': 7, 'target_ids': [target], 'cap_microusd': 10000})
    with pytest.raises(ValueError, match='exact presented'):
        proposals.decide_schedule(proposal, 'stale', True, 'yes-old-plan', NOW)
    schedule = proposals.decide_schedule(proposal, revision, True, 'yes-current-plan', NOW)
    stored = journal.read()[0]['tables']['ai_mi_schedules'][schedule]
    assert stored['status'] == 'active'
    assert stored['next_due_at'] == '2026-11-02T14:00:00+00:00'


def test_calendar_schedule_uses_local_timezone_and_skips_missing_dst_hour():
    plan = {'weekdays': [7], 'hour': 2, 'minute': 30, 'timezone': 'America/New_York'}
    after = datetime(2026, 3, 7, 12, tzinfo=timezone.utc)
    assert next_occurrence(plan, after) == '2026-03-15T06:30:00+00:00'


@pytest.mark.parametrize('changes', [
    {'weekdays': [1, 1]}, {'weekdays': [True]}, {'hour': True}, {'minute': '0'},
])
def test_recurrence_rejects_duplicate_or_coerced_values(changes):
    with pytest.raises(ValueError):
        next_occurrence({**WEEKLY, **changes}, NOW)
