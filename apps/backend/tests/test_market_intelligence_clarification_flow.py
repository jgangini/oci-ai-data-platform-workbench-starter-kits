"""Scripted intake decisions exercise real graph continuity, not LLM semantics."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Scope
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow_actions import checked_research_request
from test_market_intelligence_workflow import configured, turn, THREAD, TOPIC, SCOPE


ORIGINAL = 'Investiga en la web la alianza entre Nu y Tiendas D1 en Colombia.'
ANSWER = 'Los últimos siete días.'
NU = {'entity': 'Nu y Tiendas D1', 'market': 'Colombia', 'objective': 'Alianza entre Nu y Tiendas D1'}


def intake(*, topic=NU, missing=(), action=None, source=None, question='¿Qué periodo deseas investigar?',
           canonical=ORIGINAL, intent='research', search_context=None):
    return {'intent': intent, 'canonical_question': canonical, 'topic': topic,
            'window_spec': 'last_7_days', 'missing_fields': list(missing),
            'user_question': question if missing else None,
            'selected_source_key': source, 'clarification_action': action,
            'search_context': {'country_code': 'CO', 'language_code': 'es'} if search_context is None else search_context}


def scenario(replies, *, prior=False):
    flow, model, provider, graph = configured()
    flow.public_discovery = True
    web = flow.catalog.ensure_public_web_search()
    original = model.ask
    async def scripted(name, schema, payload):
        if name == 'understand' and payload['question'] in replies:
            model.calls.append((name, copy.deepcopy(payload)))
            result = copy.deepcopy(replies[payload['question']])
            if result['selected_source_key'] == 'web':
                result['selected_source_key'] = web
            return result
        result = await original(name, schema, payload)
        if payload.get('topic') != TOPIC:
            if name == 'plan_research' and result['arguments']:
                result['arguments']['query'] = payload['topic']['entity'] + ' ' + payload['topic']['market']
                result['arguments']['max_items'] = 10 if result['source_key'] == web else 1
            if name == 'assess_evidence':
                result.update(continue_research=False, gap='')
            if name == 'draft_report':
                result['findings'][0]['claim'] = 'Resultado explícitamente sintético de la solicitud aclarada'
        return result
    model.ask = scripted
    if prior:
        turn(graph, 'prior-d1-report', 'Novedades de D1 en Colombia')
    return flow, model, provider, graph, web


def test_short_window_answer_preserves_the_new_objective_despite_an_old_report_and_wrong_topic_proposal():
    flow, model, provider, graph, web = scenario({
        ORIGINAL: intake(missing=['periodo'], source='web'),
        ANSWER: intake(topic=TOPIC, action='continue', canonical='Novedades de D1 en Colombia')}, prior=True)
    starts = len(provider.starts)
    before_questions = len(flow.records().rows('ai_mi_questions'))
    first = turn(graph, 'clarification-initial', ORIGINAL)
    assert first['pending_clarification']['question'] == ORIGINAL
    assert len(flow.records().rows('ai_mi_questions')) == before_questions
    assert len(provider.starts) == starts
    result = turn(graph, 'clarification-window', ANSWER)
    assert result['question'] == ANSWER
    assert result['request_id'] == 'clarification-window'
    assert result['effective_request']['question'] == ORIGINAL
    assert result['last_topic'] == NU
    assert result['pending_clarification'] is None
    assert result['intake']['selected_source_key'] == web
    question = flow.records().get('ai_mi_questions', result['question_id'])
    assert question['client_request_id'] == 'clarification-initial'
    assert question['question_text'] == ORIGINAL
    assert len(flow.records().rows('ai_mi_questions')) == before_questions + 1
    run = flow.records().get('ai_mi_research_runs', result['run_id'])
    assert json.loads(run['brief_json'])['clarifications'] == [{'request_id': 'clarification-window', 'text': ANSWER}]
    assert len(flow.proposals.recurring_questions(result['topic_id'])) == 1
    assert len(provider.starts) == starts + 1
    assert provider.starts[-1]['actor'] == 'scraperlink/google-search-results-serp-scraper'
    assert provider.starts[-1]['input']['gl'] == 'CO'
    assert provider.starts[-1]['input']['hl'] == 'es'
    relevant = [payload for name, payload in model.calls if name in {'plan_research', 'assess_evidence', 'draft_report'} and payload.get('topic') == NU]
    assert relevant
    for payload in relevant:
        assert payload['question'] == ORIGINAL
        assert payload['clarifications'] == [ANSWER]
    assert all((payload.get('requested_publication_window') or payload.get('window'))['end'] !=
               (payload.get('requested_publication_window') or payload.get('window'))['start'] for payload in relevant)


def test_two_clarifications_and_replay_create_one_research_question_without_duplicate_answers_or_effects():
    flow, model, provider, graph, _ = scenario({
        ORIGINAL: intake(topic=None, missing=['mercado', 'periodo'], source='web'),
        'Colombia': intake(missing=['periodo'], action='continue'),
        ANSWER: intake(action='continue')})
    turn(graph, 'two-initial', ORIGINAL)
    second = turn(graph, 'two-market', 'Colombia')
    assert second['pending_clarification']['answers'] == [{'request_id': 'two-market', 'text': 'Colombia'}]
    calls = len(model.calls)
    replay = turn(graph, 'two-market', 'Colombia')
    assert replay['pending_clarification'] == second['pending_clarification'] and len(model.calls) == calls
    result = turn(graph, 'two-window', ANSWER)
    answers = [{'request_id': 'two-market', 'text': 'Colombia'}, {'request_id': 'two-window', 'text': ANSWER}]
    assert result['effective_request']['answers'] == answers and result['pending_clarification'] is None
    assert len(flow.records().rows('ai_mi_questions')) == 1 and len(provider.starts) == 1
    counts = len(model.calls), len(provider.starts), len(flow.records().rows('ai_mi_reports'))
    turn(graph, 'two-window', ANSWER)
    assert counts == (len(model.calls), len(provider.starts), len(flow.records().rows('ai_mi_reports')))


@pytest.mark.parametrize('original,first_source,next_source', [
    ('Investiga en Búsqueda web las condiciones de retiro Nu–D1 en Colombia.', 'web', 'x'),
    (ORIGINAL, 'web', 'x'),
    ('Investiga en X las condiciones de retiro Nu–D1 en Colombia.', 'x', 'web'),
])
def test_changed_source_during_clarification_asks_before_capturing_either_source(original, first_source, next_source):
    replies = {}
    flow, _, provider, graph, web = scenario(replies)
    sources = {'web': web, 'x': flow.catalog.ensure_public_x_search()}
    answer = 'Solo en X, los últimos siete días.' if next_source == 'x' else 'Solo en Búsqueda web, los últimos siete días.'
    replies[original] = intake(missing=['periodo'], source=sources[first_source])
    replies[answer] = intake(action='continue', source=sources[next_source])
    first = turn(graph, 'changed-source-initial', original)
    before, _ = flow.journal.read()
    result = turn(graph, 'changed-source-answer', answer)
    after, _ = flow.journal.read()
    assert result['pending_clarification'] == first['pending_clarification']
    assert result['pending_clarification']['answers'] == []
    assert result['effective_request'] is None
    assert 'fuente' in result['response']['markdown']
    assert after == before
    assert provider.starts == []


@pytest.mark.parametrize('intent', ['research', 'followup', 'source_request'])
def test_ambiguous_or_inconsistent_continuation_preserves_pending_goal_without_effects(intent):
    flow, _, provider, graph, _ = scenario({
        ORIGINAL: intake(missing=['periodo'], source='web'),
        'sí': intake(intent=intent, action=None if intent == 'research' else 'continue')})
    first = turn(graph, 'ambiguous-initial', ORIGINAL)
    before = copy.deepcopy(flow.journal.read()[0])
    result = turn(graph, 'ambiguous-yes', 'sí')
    assert result['pending_clarification'] == first['pending_clarification']
    assert result['response']['markdown'] and not provider.starts
    assert flow.journal.read()[0] == before


def test_cancellation_clears_only_the_pending_research_without_creating_records():
    flow, _, provider, graph, _ = scenario({
        ORIGINAL: intake(missing=['periodo'], source='web'),
        'cancelar': intake(topic=None, action='cancel', intent='cancel')})
    turn(graph, 'cancel-initial', ORIGINAL)
    before = copy.deepcopy(flow.journal.read()[0])
    result = turn(graph, 'cancel-pending', 'cancelar')
    assert result['pending_clarification'] is None and result['effective_request'] is None
    assert 'cancelado' in result['response']['markdown'] and not provider.starts
    assert flow.journal.read()[0] == before


def test_replacement_uses_only_the_new_literal_request_and_drops_the_old_goal():
    replacement = 'Investiga en la web las novedades de Ara en Perú de los últimos siete días.'
    topic = {'entity': 'Ara', 'market': 'Perú', 'objective': 'Novedades'}
    flow, _, provider, graph, _ = scenario({
        ORIGINAL: intake(missing=['periodo'], source='web'),
        replacement: intake(topic=topic, action='replace', source='web', canonical=replacement,
            search_context={'country_code': 'PE', 'language_code': 'es'})})
    turn(graph, 'replace-initial', ORIGINAL)
    result = turn(graph, 'replace-new', replacement)
    question = flow.records().get('ai_mi_questions', result['question_id'])
    assert question['question_text'] == replacement and question['client_request_id'] == 'replace-new'
    assert result['last_topic'] == topic and result['pending_clarification'] is None
    assert len(flow.records().rows('ai_mi_questions')) == 1 and len(provider.starts) == 1
    assert provider.starts[0]['input']['keyword'] == 'Ara Perú'
    assert 'clarifications' not in json.loads(flow.records().get('ai_mi_research_runs', result['run_id'])['brief_json'])


@pytest.mark.parametrize('new_url', ['https://other.example.org/new', 'https://127.0.0.1/private'])
def test_new_page_in_a_continuation_does_not_expand_authority(new_url):
    reply = ANSWER + ' Usa también ' + new_url
    flow, _, provider, graph, _ = scenario({
        ORIGINAL: intake(missing=['periodo'], source='web'), reply: intake(action='continue')})
    first = turn(graph, 'new-url-initial', ORIGINAL)
    before = copy.deepcopy(flow.journal.read()[0])
    result = turn(graph, 'new-url-reply', reply)
    assert result['pending_clarification'] == first['pending_clarification']
    assert 'nueva pregunta completa' in result['response']['markdown']
    assert flow.journal.read()[0] == before and not provider.starts


@pytest.mark.parametrize('reply,source,actor', [
    ('Solo en X, durante los últimos siete días.', SCOPE.key('x'), 'apidojo/twitter-scraper-lite'),
    ('En la web, durante los últimos siete días.', 'web', 'scraperlink/google-search-results-serp-scraper'),
])
def test_a_verified_clarification_can_select_an_active_source_name_without_granting_page_urls(reply, source, actor):
    original = 'Investiga la alianza entre Nu y Tiendas D1 en Colombia.'
    flow, _, provider, graph, _ = scenario({
        original: intake(missing=['fuente', 'periodo']),
        reply: intake(action='continue', source=source)})
    turn(graph, 'source-clarification-initial', original)
    result = turn(graph, 'source-clarification-answer', reply)
    assert len(provider.starts) == 1 and provider.starts[0]['actor'] == actor
    saved = flow.records().get('ai_mi_questions', result['question_id'])
    assert saved['question_text'] == original
    assert json.loads(flow.records().get('ai_mi_research_runs', result['run_id'])['brief_json'])['clarifications'][0]['text'] == reply


def test_source_name_wording_cannot_replace_the_original_url_authority_in_catalog_selection():
    flow, _, _, _, _ = scenario({})
    records = flow.records()
    topic = next(row['topic_id'] for row in records.rows('ai_mi_topics'))
    base = flow.catalog.approved_base(records, topic)
    selected = flow.catalog.select_for_question(records, topic, base, SCOPE.key('web'),
        'Lee https://example.com/reference', source_question='web https://other.example.org/unapproved')
    assert [target['canonical_url_or_id'] for target in selected[0]['targets']] == ['https://example.com/reference']


def test_original_literal_page_survives_clarification_and_an_invented_canonical_url_has_no_authority():
    question = 'Lee https://example.com/reference para comparar su información.'
    flow, _, provider, graph, _ = scenario({
        question: intake(missing=['periodo'], canonical=question),
        ANSWER: intake(action='continue', canonical='Lee https://other.example.org/invented')})
    turn(graph, 'pointed-clarification-initial', question)
    result = turn(graph, 'pointed-clarification-answer', ANSWER)
    assert provider.starts[0]['input']['startUrls'] == [{'url': 'https://example.com/reference'}]
    assert len(provider.starts) == 1
    saved = flow.records().get('ai_mi_questions', result['question_id'])
    assert saved['question_text'] == question and 'other.example.org' not in saved['question_text']
    assert flow.records().rows('ai_mi_source_targets')[0]['canonical_url_or_id'] == 'https://example.com/reference'


@pytest.mark.parametrize('corruption', ['empty', 'version', 'scope'])
def test_unknown_or_foreign_pending_context_asks_for_full_question_before_any_model_or_capture(corruption):
    flow, model, provider, graph, _ = scenario({ORIGINAL: intake(missing=['periodo'], source='web')})
    first = turn(graph, 'corrupt-initial', ORIGINAL)
    pending = copy.deepcopy(first['pending_clarification'])
    if corruption == 'empty':
        pending = {}
    elif corruption == 'version':
        pending['version'] = 2
    else:
        other = Scope(SCOPE.tenant_id, 'another-owner', SCOPE.generation_id)
        pending['scope_key'] = other.key('foreign-pending')
    asyncio.run(graph.aupdate_state(THREAD, {'pending_clarification': pending}, as_node='deliver_and_read'))
    calls = len(model.calls)
    before = copy.deepcopy(flow.journal.read()[0])
    result = turn(graph, 'corrupt-answer', ANSWER)
    assert result['pending_clarification'] is None and 'pregunta completa' in result['response']['markdown']
    assert len(model.calls) == calls and not provider.starts and flow.journal.read()[0] == before


@pytest.mark.parametrize('different', ['owner', 'tenant'])
def test_a_real_pending_object_cannot_be_copied_to_another_identity_with_the_same_generation(different):
    flow, _, _, graph, _ = scenario({ORIGINAL: intake(missing=['periodo'], source='web')})
    pending = turn(graph, 'real-scope-initial', ORIGINAL)['pending_clarification']
    assert checked_research_request(flow, pending, pending=True) == pending
    other = Scope('different-tenant' if different == 'tenant' else SCOPE.tenant_id,
        'different-owner' if different == 'owner' else SCOPE.owner_id, SCOPE.generation_id)
    assert other.key('same-local-id') == SCOPE.key('same-local-id')
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.research import Research
    journal = SimpleNamespace(scope=other)
    other_flow = SimpleNamespace(journal=journal, research=Research(journal))
    with pytest.raises(ValueError, match='authenticated scope'):
        checked_research_request(other_flow, pending, pending=True)


def test_a_legacy_incomplete_checkpoint_without_its_original_context_cannot_fall_back_to_an_old_topic():
    flow, model, provider, graph, _ = scenario({
        ORIGINAL: intake(missing=['periodo'], source='web'), ANSWER: intake(topic=TOPIC)}, prior=True)
    turn(graph, 'legacy-incomplete-initial', ORIGINAL)
    asyncio.run(graph.aupdate_state(THREAD, {'pending_clarification': None}, as_node='deliver_and_read'))
    calls, starts = len(model.calls), len(provider.starts)
    before = copy.deepcopy(flow.journal.read()[0])
    result = turn(graph, 'legacy-incomplete-answer', ANSWER)
    assert 'pregunta completa' in result['response']['markdown'] and result['pending_clarification'] is None
    assert (len(model.calls), len(provider.starts)) == (calls, starts)
    assert flow.journal.read()[0] == before


def test_three_incomplete_answers_stop_without_unbounded_memory_or_research():
    replies = {ORIGINAL: intake(missing=['periodo'], source='web')}
    replies.update({str(i): intake(action='continue', missing=['periodo']) for i in range(3)})
    flow, _, provider, graph, _ = scenario(replies)
    turn(graph, 'max-initial', ORIGINAL)
    for i in range(3):
        result = turn(graph, 'max-answer-' + str(i), str(i))
    assert result['pending_clarification'] is None and 'pregunta completa' in result['response']['markdown']
    assert not provider.starts and not flow.records().rows('ai_mi_questions')


def test_source_or_schedule_proposal_retains_its_existing_priority_and_consent_path():
    flow, model, provider, graph, _ = scenario({ORIGINAL: intake(missing=['periodo'], source='web')})
    initial = turn(graph, 'proposal-priority', ORIGINAL)
    calls = len(model.calls)
    updates = asyncio.run(flow.understand({**initial, 'question': 'sí',
        'pending': {'kind': 'schedule', 'id': 'synthetic-proposal', 'hash': None}}))
    assert updates['intake']['intent'] == 'schedule_decision' and updates['pending_clarification'] is None
    assert len(model.calls) == calls and not provider.starts
