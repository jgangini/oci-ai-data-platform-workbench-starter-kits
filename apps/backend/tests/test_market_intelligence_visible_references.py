"""Evidence references remain visible when native clients discard message metadata."""
import asyncio
import copy
from datetime import datetime, timedelta

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.native import AgentRuntime
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.presentation import response_markdown
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.streaming import stream_turn
from test_market_intelligence_streaming import native_auth
from test_market_intelligence_workflow import configured, turn, THREAD, SCOPE


def test_publication_and_readback_show_all_cited_sources_without_rewriting_the_report():
    flow, model, provider, graph = configured()
    result = turn(graph, 'reference-publication')
    report_id = result['response']['report_id']
    saved = copy.deepcopy(flow.records().get('ai_mi_reports', report_id))
    urls = ['https://example.com/reference', 'https://x.com/fixture/status/1']
    assert result['response']['references'] == urls
    assert all(url in response_markdown(result['response']) for url in urls)
    # The synthetic draft linked only one source. Rendering resolves its other stored citation.
    assert urls[1] not in saved['markdown']
    calls, starts = len(model.calls), len(provider.starts)
    assert flow.report_response(saved) == result['response']
    assert len(model.calls) == calls and len(provider.starts) == starts
    model.intents = ['read_report']
    read = turn(graph, 'reference-read', 'Abre el informe anterior sin cambiarlo.')
    assert read['response']['references'] == urls and read['response']['reused']
    assert all(url in response_markdown(read['response']) for url in urls)
    assert len(provider.starts) == starts
    assert flow.records().get('ai_mi_reports', report_id) == saved


def test_followup_replaces_the_original_reference_set_with_its_own_validated_citations():
    flow, model, provider, graph = configured()
    initial = turn(graph, 'reference-followup-base')
    saved = copy.deepcopy(flow.records().get('ai_mi_reports', initial['response']['report_id']))
    original = model.ask
    async def one_source(name, schema, payload):
        draft = await original(name, schema, payload)
        if name == 'draft_report' and 'followup' in payload:
            item = next(row for row in payload['evidence'] if row['source_domain'] == 'x.com')
            draft.update(answer_markdown='La publicación recuperada expresa una opinión sintética.',
                cited_evidence_ids=[item['evidence_id']],
                findings=[{'claim': 'Opinión sintética', 'kind': 'opinion',
                    'supports': [item['evidence_id']], 'refutes': [], 'limitations': []}])
        return draft
    model.ask = one_source
    model.intents = ['followup']
    result = turn(graph, 'reference-followup', 'Resume solo la publicación de X, sin actualizar.')
    assert result['response']['references'] == ['https://x.com/fixture/status/1']
    rendered = response_markdown(result['response'])
    assert 'https://x.com/fixture/status/1' in rendered and 'https://example.com/reference' not in rendered
    assert len(provider.starts) == 2 and len(flow.records().rows('ai_mi_reports')) == 1
    assert flow.records().get('ai_mi_reports', saved['report_id']) == saved


def test_scheduled_notification_uses_the_same_reference_resolution_without_another_capture():
    flow, _, provider, graph = configured()
    initial = turn(graph, 'reference-notification-base')
    proposal = flow.proposals.propose_schedule(initial['topic_id'], direct=True)
    summary = flow.proposals.present_schedule(proposal,
        {'weekdays': [1], 'hour': 9, 'minute': 0, 'timezone': 'America/Bogota'},
        {'window_days': 7, 'target_ids': [SCOPE.key('reference')], 'cap_microusd': 100000})
    schedule = flow.proposals.decide_schedule(proposal, summary, True, 'approve-reference-schedule')
    due = datetime.fromisoformat(flow.records().get('ai_mi_schedules', schedule)['next_due_at'])
    flow.clock = lambda: due
    observations = provider.observations
    async def scheduled_observations(receipt, call):
        items, cost = await observations(receipt, call)
        for item in items:
            item.update(published_at=(due - timedelta(hours=1)).isoformat(),
                        observed_at=due.isoformat())
        return items, cost
    provider.observations = scheduled_observations
    run = flow.proposals.claim_due(schedule, due)
    thread = {'configurable': {'thread_id': 'reference-scheduled'}, 'recursion_limit': 100}
    result = asyncio.run(_collect(graph, flow.scheduled_input(run), thread))[-1]['state']['response']
    notification = flow.records().rows('ai_mi_notifications')[0]
    report = flow.proposals.read_notification(notification['notification_id'])
    saved = copy.deepcopy(report)
    starts = len(provider.starts)
    read = flow.report_response(report)
    assert read['references'] == result['references'] == ['https://example.com/reference']
    assert 'https://example.com/reference' in response_markdown(read)
    assert len(provider.starts) == starts
    assert flow.records().get('ai_mi_reports', saved['report_id']) == saved


async def _collect(graph, request, config):
    return [event async for event in stream_turn(graph, request, config)]


def test_checkpoint_replay_retains_references_without_models_or_duplicate_capture():
    flow, model, provider, graph = configured()
    request = {'request_id': 'reference-replay', 'question': 'Novedades de D1 en Colombia'}
    first = asyncio.run(_collect(graph, request, THREAD))
    counts = len(model.calls), len(provider.starts), len(flow.records().rows('ai_mi_reports'))
    replay = asyncio.run(_collect(graph, request, THREAD))
    assert len(replay) == 1 and replay[0]['event'] == 'result'
    response = replay[0]['state']['response']
    assert response == first[-1]['state']['response']
    assert all(url in response_markdown(response) for url in response['references'])
    assert counts == (len(model.calls), len(provider.starts), len(flow.records().rows('ai_mi_reports')))


def test_legacy_original_checkpoint_rehydrates_owned_references_without_modifying_cache_or_research():
    flow, model, provider, graph = configured()
    request = {'request_id': 'legacy-reference-replay', 'question': 'Novedades de D1 en Colombia'}
    first = asyncio.run(_collect(graph, request, THREAD))[-1]['state']
    legacy = {key: value for key, value in first['response'].items() if key != 'references'}
    asyncio.run(graph.aupdate_state(THREAD, {'response': legacy}, as_node='deliver_and_read'))
    before = copy.deepcopy(flow.journal.read()[0])
    calls, starts = len(model.calls), len(provider.starts)
    replay = asyncio.run(_collect(graph, request, THREAD))[0]['state']
    assert 'references' not in replay['response']
    response = asyncio.run(flow.response_for_delivery(replay))
    assert response['markdown'] == legacy['markdown']
    assert response['references'] == first['response']['references']
    assert all(url in response_markdown(response) for url in response['references'])
    assert 'references' not in asyncio.run(graph.aget_state(THREAD)).values['response']
    assert flow.journal.read()[0] == before and (len(model.calls), len(provider.starts)) == (calls, starts)


@pytest.mark.parametrize('intent,body', [('followup', 'Resumen anterior sin sus propias referencias.'),
    ('followup', None), ('research', 'Texto distinto del informe'), (None, None)])
def test_legacy_followup_or_unverifiable_body_never_borrows_original_report_citations(intent, body):
    flow, model, provider, graph = configured()
    first = turn(graph, 'legacy-uncertain-reference')
    legacy = {key: value for key, value in first['response'].items() if key != 'references'}
    if body is not None:
        legacy['markdown'] = body
    before = copy.deepcopy(flow.journal.read()[0])
    calls, starts = len(model.calls), len(provider.starts)
    response = asyncio.run(flow.response_for_delivery({'response': legacy, 'intake': {'intent': intent}}))
    assert 'no conserva todas sus referencias' in response['markdown']
    assert 'references' not in response and 'sin repetir la búsqueda' in response['markdown']
    assert flow.journal.read()[0] == before and (len(model.calls), len(provider.starts)) == (calls, starts)


def test_legacy_checkpoint_requires_an_owned_report_before_rehydration():
    flow, _, provider, _ = configured()
    with pytest.raises(ValueError, match='owned report'):
        asyncio.run(flow.response_for_delivery({'response': {'report_id': SCOPE.key('missing'), 'markdown': 'Texto'},
            'intake': {'intent': 'research'}}))
    assert not provider.starts


@pytest.mark.parametrize('stream', [True, False])
def test_native_message_text_contains_references_even_when_extra_fields_are_discarded(native_auth, stream):
    class ReferenceRuntime(AgentRuntime):
        async def events(self, user_query, native_config, kwargs):
            yield {'event': 'result', 'response': {'markdown': 'Respuesta sustentada.',
                'references': ['https://example.com/report'], 'chart': None}}
    async def check():
        result = await ReferenceRuntime().invoke('fixture', stream=stream)
        messages = [item async for item in result] if stream else result['messages']
        # Model the gateway retaining only text, dropping additional_kwargs.
        text = ''.join(item.content for item in messages)
        assert '**Referencias**' in text and '[1 · example.com](https://example.com/report)' in text
    asyncio.run(check())
    assert native_auth == ['start', 'end']


def test_reference_links_escape_markdown_path_delimiters_without_changing_authoritative_urls():
    url = 'https://example.com/review/(x)<script>[s]\\p&copy;?q=a&other=b'
    response = {'markdown': 'Informe.', 'references': [url, url]}
    original = copy.deepcopy(response)
    text = response_markdown(response)
    assert text.count('- [') == 1 and '<script>' not in text
    assert '%28x%29%3Cscript%3E%5Bs%5D%5Cp%26copy;' in text
    assert '?q=a&other=b' in text and response == original


@pytest.mark.parametrize('references', [None, {}, 'https://example.com/report',
    ['https://example.com/report'] * 51, ['javascript:alert(1)'],
    ['http://example.com/report'], ['https://127.0.0.1/private'],
    ['https://example.com/path\nnext'], ['https://user:secret@example.com/path']])
def test_invalid_reference_sets_fail_before_they_can_be_rendered(references):
    with pytest.raises(ValueError):
        response_markdown({'markdown': 'Informe.', 'references': references})


def test_fifty_long_references_are_complete_separate_from_the_unchanged_report_body_limit():
    body = 'x' * 20000
    urls = [('https://example.com/' + str(i) + '/').ljust(2000, 'x') for i in range(50)]
    response = {'markdown': body, 'references': urls}
    text = response_markdown(response)
    assert text.startswith(body + '\n\n**Referencias**') and len(text) > 120000
    assert text.count('- [') == 50 and all('](' + url + ')' in text for url in urls)
    assert response['markdown'] == body and response['references'] == urls
