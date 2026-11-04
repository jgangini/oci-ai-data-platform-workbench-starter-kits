from datetime import datetime, timezone
import json

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.proposals import Proposals, next_occurrence
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Journal
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.research import Research
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.catalog import Catalog
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.contracts import ScheduleDraft, ScheduleResearch
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import digest, json_text
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
    target = Catalog(journal).register_operator_website(topic, 'https://example.com/report', 'register-page')
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


def public_plan(profile='x_search', window_days=7):
    journal, _, proposals, topic = base()
    catalog = Catalog(journal)
    # Existing RAG schedules retain their exact reviewed legacy capability.
    source = (catalog._ensure_public_search('web_search') if profile == 'web_search'
              else getattr(catalog, 'ensure_public_' + profile)())
    proposal = proposals.propose_schedule(topic, direct=True, now=NOW)
    plan = {'window_days': window_days, 'source_keys': [source], 'cap_microusd': 10000}
    return journal, proposals, topic, source, proposal, plan


def replace_source(journal, source, **changes):
    journal.transact(lambda records, state: records.upsert('ai_mi_sources', {
        **records.get('ai_mi_sources', source), **changes}))


@pytest.mark.parametrize('selection', [
    {}, {'target_ids': ['one', 'one']}, {'source_keys': ['one', 'one']},
    {'source_keys': ['one'] * 11}, {'target_ids': ['one'], 'source_keys': [True]},
])
def test_schedule_research_rejects_empty_duplicate_or_invalid_selections(selection):
    with pytest.raises(ValueError):
        ScheduleResearch.model_validate({'window_days': 7, 'cap_microusd': 10000, **selection})


def test_partial_source_draft_is_saved_without_activating_a_schedule():
    journal, proposals, _, source, proposal, _ = public_plan()
    draft = ScheduleDraft.model_validate({'source_keys': [source]}).model_dump(exclude_none=True)
    proposals.save_schedule_draft(proposal, draft)
    value, _ = journal.read()
    assert json.loads(value['tables']['ai_mi_schedule_proposals'][proposal]['proposed_plan_json'])['draft'] == draft
    assert not value['tables']['ai_mi_schedules']


@pytest.mark.parametrize('profile', ['x_search', 'web_search', 'youtube_search'])
def test_public_schedule_binds_exact_capability_without_registering_a_target(profile):
    journal, proposals, _, source, proposal, plan = public_plan(profile)
    revision = proposals.present_schedule(proposal, WEEKLY, plan)
    schedule = proposals.decide_schedule(proposal, revision, True, 'approve', NOW)
    row = journal.read()[0]['tables']['ai_mi_schedules'][schedule]
    saved = json.loads(row['approved_plan_budget_json'])
    assert saved['version'] == 2 and saved['source_keys'] == [source] and saved['target_ids'] == []
    assert set(saved['source_authorizations']) == {'source:' + source}
    assert not journal.read()[0]['tables']['ai_mi_source_targets']
    due = datetime.fromisoformat(row['next_due_at'])
    run = proposals.claim_due(schedule, due)
    assert run and proposals.claim_due(schedule, due, resume_pending=True) == run
    assert proposals.claim_due(schedule, due) is None
    assert len(journal.read()[0]['tables']['ai_mi_research_runs']) == 1


@pytest.mark.parametrize('stage', ['present', 'accept', 'claim', 'resume'])
def test_source_revocation_prevents_schedule_effects_at_every_boundary(stage):
    journal, proposals, _, source, proposal, plan = public_plan()
    if stage == 'present':
        replace_source(journal, source, status='inactive')
        before = journal.read()[0]
        with pytest.raises(ValueError):
            proposals.present_schedule(proposal, WEEKLY, plan)
    else:
        revision = proposals.present_schedule(proposal, WEEKLY, plan)
        if stage == 'accept':
            replace_source(journal, source, status='inactive')
            before = journal.read()[0]
            with pytest.raises(ValueError):
                proposals.decide_schedule(proposal, revision, True, 'approve', NOW)
        else:
            schedule = proposals.decide_schedule(proposal, revision, True, 'approve', NOW)
            due = datetime.fromisoformat(journal.read()[0]['tables']['ai_mi_schedules'][schedule]['next_due_at'])
            if stage == 'resume':
                assert proposals.claim_due(schedule, due)
            replace_source(journal, source, status='inactive')
            before = journal.read()[0]
            with pytest.raises(ValueError):
                proposals.claim_due(schedule, due, resume_pending=True)
    assert journal.read()[0] == before


def test_presented_build_change_requires_a_new_human_confirmation(monkeypatch):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import PROFILES
    journal, proposals, _, _, proposal, plan = public_plan()
    revision = proposals.present_schedule(proposal, WEEKLY, plan)
    monkeypatch.setitem(PROFILES, 'x_search', {**PROFILES['x_search'], 'build': 'different-build'})
    before = journal.read()[0]
    with pytest.raises(ValueError):
        proposals.decide_schedule(proposal, revision, True, 'approve', NOW)
    assert journal.read()[0] == before


@pytest.mark.parametrize('change', ['other_topic', 'not_public', 'unknown_source'])
def test_schedule_cannot_authorize_a_source_outside_its_public_topic_scope(change):
    journal, proposals, _, source, proposal, plan = public_plan()
    if change == 'other_topic':
        replace_source(journal, source, allowed_scope_json=json_text({'allow_discovery': True, 'topic_id': SCOPE.key('other-topic')}))
    elif change == 'not_public':
        replace_source(journal, source, connector_revision='website')
    else:
        plan['source_keys'] = [SCOPE.key('unknown-source')]
    before = journal.read()[0]
    with pytest.raises(ValueError):
        proposals.present_schedule(proposal, WEEKLY, plan)
    assert journal.read()[0] == before


def test_schedule_summary_binds_both_fixed_target_and_public_source():
    journal, proposals, topic, source, proposal, plan = public_plan()
    target = Catalog(journal).register_operator_website(topic, 'https://example.com/report', 'register-page')
    revision = proposals.present_schedule(proposal, WEEKLY, {**plan, 'target_ids': [target]})
    schedule = proposals.decide_schedule(proposal, revision, True, 'approve', NOW)
    saved = json.loads(journal.read()[0]['tables']['ai_mi_schedules'][schedule]['approved_plan_budget_json'])
    assert set(saved['source_authorizations']) == {'target:' + target, 'source:' + source}


def test_an_equivalent_new_public_source_does_not_replace_the_approved_identity():
    journal, proposals, topic, source, proposal, plan = public_plan()
    revision = proposals.present_schedule(proposal, WEEKLY, plan)
    schedule = proposals.decide_schedule(proposal, revision, True, 'approve', NOW)
    replace_source(journal, source, status='inactive')
    assert Catalog(journal).register_operator_x_search(topic, 'approve-other-source') != source
    before = journal.read()[0]
    due = datetime.fromisoformat(before['tables']['ai_mi_schedules'][schedule]['next_due_at'])
    with pytest.raises(ValueError):
        proposals.claim_due(schedule, due)
    assert journal.read()[0] == before


def test_claim_rejects_unknown_persisted_plan_version_without_advancing_the_schedule():
    journal, proposals, _, _, proposal, plan = public_plan()
    revision = proposals.present_schedule(proposal, WEEKLY, plan)
    schedule = proposals.decide_schedule(proposal, revision, True, 'approve', NOW)
    def change(records, state):
        row = records.get('ai_mi_schedules', schedule)
        saved = json.loads(row['approved_plan_budget_json'])
        records.upsert('ai_mi_schedules', {**row, 'approved_plan_budget_json': json_text({**saved, 'version': 99})})
    journal.transact(change)
    before = journal.read()[0]
    due = datetime.fromisoformat(before['tables']['ai_mi_schedules'][schedule]['next_due_at'])
    with pytest.raises(ValueError):
        proposals.claim_due(schedule, due)
    assert journal.read()[0] == before


@pytest.mark.parametrize('version', [1, 99])
def test_legacy_target_plan_is_not_upgraded_and_unknown_version_has_no_effects(version):
    journal, _, proposals, topic = base()
    target = Catalog(journal).register_operator_website(topic, 'https://example.com/report', 'register-page')
    proposal = proposals.propose_schedule(topic, direct=True, now=NOW)
    legacy = {'version': version, 'window_days': 7, 'target_ids': [target], 'cap_microusd': 10000}
    plan = {'recurrence': WEEKLY, 'research': legacy}
    summary_hash = digest(plan)
    journal.transact(lambda records, state: records.upsert('ai_mi_schedule_proposals', {
        **records.get('ai_mi_schedule_proposals', proposal), 'state': 'awaiting_confirmation',
        'proposed_plan_json': json_text({'revision': 2, 'plan': plan, 'presented_hash': summary_hash})}))
    if version == 99:
        before = journal.read()[0]
        with pytest.raises(ValueError):
            proposals.decide_schedule(proposal, summary_hash, True, 'approve', NOW)
        assert journal.read()[0] == before
        return
    schedule = proposals.decide_schedule(proposal, summary_hash, True, 'approve', NOW)
    row = journal.read()[0]['tables']['ai_mi_schedules'][schedule]
    assert json.loads(row['approved_plan_budget_json']) == legacy
    due = datetime.fromisoformat(row['next_due_at'])
    assert proposals.claim_due(schedule, due)
    assert json.loads(journal.read()[0]['tables']['ai_mi_schedules'][schedule]['approved_plan_budget_json']) == legacy


@pytest.mark.parametrize('days', [8, 31])
def test_weekly_youtube_capability_cannot_promise_a_longer_schedule_window(days):
    journal, proposals, _, _, proposal, plan = public_plan('youtube_search', days)
    before = journal.read()[0]
    with pytest.raises(ValueError):
        proposals.present_schedule(proposal, WEEKLY, plan)
    assert journal.read()[0] == before
