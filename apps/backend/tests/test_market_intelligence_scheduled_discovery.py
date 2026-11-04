"""A scheduled public search uses only the capability the user confirmed."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip('langgraph', reason='Graph execution is checked in the pinned runtime CI job')

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import compile_call
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import ScheduleScopeError
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.presentation import response_markdown
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import digest, json_text
from test_market_intelligence_workflow import configured, turn, TOPIC, SCOPE


RECURRENCE = {'weekdays': [1], 'hour': 9, 'minute': 0, 'timezone': 'America/Bogota'}
TOPIC_ID = SCOPE.key(digest(['topic', TOPIC]))


def approved_due_search(*, include_web=False):
    flow, model, provider, graph = configured()
    source = flow.catalog.ensure_public_x_search()
    sources = [source]
    if include_web:
        sources.append(flow.catalog._ensure_public_search('web_search'))
    now = datetime.now(timezone.utc)
    recurrence = {'weekdays': [now.isoweekday()], 'hour': now.hour,
                  'minute': now.minute, 'timezone': 'UTC'}
    proposal = flow.proposals.propose_schedule(TOPIC_ID, direct=True)
    plan = {'window_days': 7, 'target_ids': [], 'source_keys': sources, 'cap_microusd': 100000 if include_web else 50000}
    presented = flow.proposals.present_schedule(proposal, recurrence, plan)
    schedule = flow.proposals.decide_schedule(proposal, presented, True, 'explicit-test-consent', now - timedelta(days=7))
    run = flow.proposals.claim_due(schedule, now)
    assert run
    return flow, model, provider, graph, source, schedule, run


def confirmation(plan):
    return {'action': 'present_summary', 'proposal_id': None, 'recurrence': RECURRENCE,
            'research_plan': plan, 'missing_fields': [], 'user_question': None, 'draft': None}


def test_direct_complete_request_presents_a_public_search_without_reasking_its_details():
    flow, model, provider, graph = configured()
    flow.public_discovery = True
    model.intents = ['schedule_request']
    original = model.ask
    async def decide(name, schema, payload):
        if name != 'handle_confirmation':
            return await original(name, schema, payload)
        model.calls.append((name, payload))
        source = next(row for row in payload['approved_public_sources'] if row['profile_id'] == 'youtube_search')
        return confirmation({'window_days': 7, 'target_ids': [], 'source_keys': [source['source_key']], 'cap_microusd': 50000})
    model.ask = decide
    result = turn(graph, 'direct-public-schedule', 'Programa YouTube los lunes a las 09:00 en Colombia, siete días, US$0,05 por ejecución')
    assert result['pending']['hash']
    text = response_markdown(result['response'])
    assert 'YouTube' in text and 'Búsqueda pública por tema' in text
    assert 'D1' in text and 'Colombia' in text and 'Novedades' in text
    assert '09:00' in text and 'America/Bogota' in text and 'US$0.050000' in text
    assert not flow.records().rows('ai_mi_schedules') and not provider.starts
    approved = turn(graph, 'approve-public-schedule', 'sí')
    schedule = flow.records().get('ai_mi_schedules', approved['response']['record_id'])
    plan = json.loads(schedule['approved_plan_budget_json'])
    assert plan['version'] == 2 and plan['target_ids'] == [] and len(plan['source_keys']) == 1
    assert 'source_authorizations' not in text and plan['source_keys'][0] not in text
    assert not provider.starts and len(flow.records().rows('ai_mi_source_targets')) == 1


def test_scheduled_catalog_keeps_the_exact_source_and_does_not_add_a_new_capability():
    flow, _, provider, graph, source, _, run = approved_due_search()
    flow.catalog.ensure_public_web_search()
    scheduled = flow.records().get('ai_mi_research_runs', run)
    catalog = flow.catalog_for_question({'intake': {}, 'question': 'Ejecutar la programación'}, TOPIC_ID, scheduled)
    assert [(row['source_key'], row['targets']) for row in catalog] == [(source, [])]
    result = asyncio.run(graph.ainvoke(flow.scheduled_input(run), {'configurable': {'thread_id': 'scheduled-public'}}))
    assert result['response']['report_id'] and len(provider.starts) == 1
    assert provider.starts[0]['actor'] == 'apidojo/twitter-scraper-lite'
    notices = flow.records().rows('ai_mi_notifications')
    assert len(notices) == 1 and notices[0]['report_id'] == result['response']['report_id']
    flow.proposals.read_notification(notices[0]['notification_id'])
    read_at = flow.records().get('ai_mi_notifications', notices[0]['notification_id'])['read_at']
    flow.proposals.read_notification(notices[0]['notification_id'])
    assert flow.records().get('ai_mi_notifications', notices[0]['notification_id'])['read_at'] == read_at
    assert len(provider.starts) == 1 and not flow.proposals.recurring_questions(TOPIC_ID)


@pytest.mark.parametrize('selection', ['unapproved_public', 'unapproved_target'])
def test_an_approved_public_schedule_cannot_reserve_another_source(selection):
    flow, _, provider, _, _, _, run = approved_due_search()
    if selection == 'unapproved_public':
        source, target = flow.catalog._ensure_public_search('web_search'), None
    else:
        source, target = SCOPE.key('web'), SCOPE.key('reference')
    auth = flow.paid.authorize(run, source, target)
    with pytest.raises(ValueError, match='approved plan'):
        flow.paid.reserve(run, source, target, {'query': 'not admitted'}, 10000,
                          'Must not execute', authorization_hash=auth['authorization_hash'])
    assert not flow.journal.read()[0]['operations'] and not provider.starts


def test_revocation_after_reservation_cannot_obtain_a_start_permit_or_release_uncertain_money():
    flow, _, provider, _, source, _, run = approved_due_search()
    window = json.loads(flow.records().get('ai_mi_research_runs', run)['plan_window_json'])
    window.pop('version')
    args = {'query': 'D1 Colombia', 'urls': [], 'max_items': 1, 'sort': 'Latest'}
    call = compile_call('x_search', args, window, 20000)
    auth = flow.paid.authorize(run, source, None)
    tool = flow.paid.reserve(run, source, None, call, 20000, 'Approved step', authorization_hash=auth['authorization_hash'])
    flow.journal.transact(lambda records, state: records.upsert('ai_mi_sources', {
        **records.get('ai_mi_sources', source), 'status': 'inactive'}))
    with pytest.raises(ScheduleScopeError):
        flow.paid.claim_start(tool)
    operation = flow.journal.read()[0]['operations'][tool]
    assert operation['state'] == 'reserved' and 'permit' not in operation
    assert operation['spent_microusd'] is None and not provider.starts


def test_youtube_window_limit_asks_for_review_without_presenting_or_activating_a_plan():
    flow, model, provider, graph = configured()
    flow.public_discovery = True
    source = flow.catalog.ensure_public_youtube_search()
    model.intents = ['schedule_request']
    model.confirmations = [confirmation({'window_days': 8, 'target_ids': [], 'source_keys': [source], 'cap_microusd': 50000})]
    result = turn(graph, 'youtube-eight-days', 'Programa YouTube para los últimos ocho días')
    assert result['pending']['hash'] is None and 'siete días' in result['response']['markdown']
    assert 'schedule_summary' not in result['response']
    assert not flow.records().rows('ai_mi_schedules') and not provider.starts


def test_summary_uses_the_scope_read_after_presentation_instead_of_stale_intake_urls():
    flow, model, provider, graph = configured()
    model.intents = ['schedule_request']
    original = model.ask
    changed = 'https://example.com/reviewed-change'
    async def decide(name, schema, payload):
        if name != 'handle_confirmation':
            return await original(name, schema, payload)
        def change(records, state):
            records.upsert('ai_mi_source_targets', {**records.get('ai_mi_source_targets', SCOPE.key('reference')),
                'canonical_url_or_id': changed})
            records.upsert('ai_mi_topic_sources', {**records.get('ai_mi_topic_sources', SCOPE.key('link')),
                'approved_scope_json': json_text({'urls': [changed]})})
        flow.journal.transact(change)
        return confirmation({'window_days': 7, 'target_ids': [SCOPE.key('reference')], 'cap_microusd': 50000})
    model.ask = decide
    result = turn(graph, 'concurrent-summary', 'Programa la página registrada los lunes a las nueve')
    text = response_markdown(result['response'])
    assert result['pending']['hash'] and changed in text and 'https://example.com/reference' not in text
    assert not flow.records().rows('ai_mi_schedules') and not provider.starts


@pytest.mark.parametrize('phase', ['context', 'planning', 'reservation', 'start'])
def test_schedule_scope_change_delivers_a_partial_report_without_another_capture(phase):
    flow, model, provider, graph, source, _, run = approved_due_search(include_web=True)
    web = next(row['source_key'] for row in flow.records().rows('ai_mi_sources')
               if row['connector_revision'] == 'web_search')

    def revoke():
        flow.journal.transact(lambda records, state: records.upsert('ai_mi_sources', {
            **records.get('ai_mi_sources', web), 'status': 'inactive'}))

    if phase == 'context':
        revoke()
    elif phase == 'planning':
        original = model.ask
        async def changed_plan(name, schema, payload):
            result = await original(name, schema, payload)
            if name == 'plan_research' and flow.research.evidence(run):
                revoke()
            return result
        model.ask = changed_plan
    else:
        method = 'reserve' if phase == 'reservation' else 'claim_start'
        original = getattr(flow.paid, method)
        def change_at_admission(*args, **kwargs):
            if flow.research.evidence(run):
                revoke()
            return original(*args, **kwargs)
        setattr(flow.paid, method, change_at_admission)

    result = asyncio.run(graph.ainvoke(flow.scheduled_input(run), {
        'configurable': {'thread_id': 'changed-schedule-' + phase}}))
    report = flow.records().get('ai_mi_reports', result['response']['report_id'])
    assert result['reason'] == 'unresolved'
    assert len(provider.starts) == (0 if phase == 'context' else 1)
    assert len(flow.research.evidence(run)) == len(provider.starts)
    assert all(call['actor'] == 'apidojo/twitter-scraper-lite' for call in provider.starts)
    assert result['connector_issue'] in report['markdown']
    assert result['connector_issue'] in json.loads(report['limitations_json'])
    assert len(flow.records().rows('ai_mi_notifications')) == 1
    if phase == 'start':
        reserved = [operation for operation in flow.journal.read()[0]['operations'].values()
                    if operation['state'] == 'reserved']
        assert len(reserved) == 1 and reserved[0]['spent_microusd'] is None
        assert 'permit' not in reserved[0]


def test_resumed_schedule_with_revoked_scope_keeps_prior_evidence_in_its_partial_report():
    flow, model, provider, graph, _, _, run = approved_due_search(include_web=True)
    initial = asyncio.run(graph.ainvoke(flow.scheduled_input(run), {
        'configurable': {'thread_id': 'scheduled-before-revocation'}},
        interrupt_after=['normalize_evidence']))
    evidence = flow.research.evidence(run)
    assert len(evidence) == len(provider.starts) == 1
    assert initial['evidence_refs'] == [row['evidence_id'] for row in evidence]
    operations = flow.journal.read()[0]['operations']
    web = next(row for row in flow.records().rows('ai_mi_sources')
               if row['connector_revision'] == 'web_search')
    flow.journal.transact(lambda records, state: records.upsert('ai_mi_sources', {
        **web, 'status': 'inactive'}))
    before = len(model.calls)

    result = asyncio.run(graph.ainvoke(flow.scheduled_input(run), {
        'configurable': {'thread_id': 'scheduled-after-revocation'}}))

    report = flow.records().get('ai_mi_reports', result['response']['report_id'])
    assert result['run_id'] == run and result['reason'] == 'unresolved'
    assert result['evidence_refs'] == initial['evidence_refs'] and result['evidence'] == []
    assert json.loads(report['cited_evidence_ids_json']) == initial['evidence_refs']
    assert result['connector_issue'] in report['markdown']
    assert len(provider.starts) == 1 and flow.journal.read()[0]['operations'] == operations
    assert flow.research.evidence(run) == evidence
    calls = [name for name, _ in model.calls[before:]]
    assert calls.count('draft_report') == 1 and not {'plan_research', 'assess_evidence'} & set(calls)
