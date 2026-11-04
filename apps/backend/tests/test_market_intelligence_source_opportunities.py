"""T14 uses synthetic persisted observations; no model or provider is contacted."""
import asyncio
import json
from datetime import timedelta

import pytest

pytest.importorskip('langgraph', reason='Graph execution is checked in the pinned runtime CI job')

from test_market_intelligence_workflow import configured, turn, THREAD, TOPIC


CANDIDATE = 'https://research.example.com/market'


def recurring_source(*, duplicate=False, independence='known', syndicated=False):
    flow, model, provider, graph = configured()
    clock = flow.clock()
    window = {'start': (clock - timedelta(days=7)).isoformat(), 'end': clock.isoformat()}
    evidence_ids = []
    for index in range(3):
        question, topic = flow.research.record_question(
            f'research-{index}', f'Consulta independiente {index}', TOPIC)
        url = 'https://research.example.com/article-' + str(0 if duplicate else index)
        if syndicated and index == 2:
            url = 'https://syndicated.example.com/article-2'
        target = flow.catalog.register_operator_website(topic, url, f'approve-page-{index}')
        source = flow.records().get('ai_mi_source_targets', target)['source_key']
        run = flow.research.prepare_run(question, window, 'Contrastar evidencia sintética', 100000)
        tool = flow.paid.reserve(run, source, target, {'urls': [url]}, 10000,
            'Lectura sintética autorizada',
            authorization_hash=flow.paid.authorize(run, source, target)['authorization_hash'])
        permit = flow.paid.claim_start(tool)
        flow.paid.record_provider(tool, permit, f'synthetic-run-{index}', 'succeeded', {})
        flow.paid.record_cost(tool, f'synthetic-run-{index}', 1000)
        evidence, = flow.research.add_evidence(run, tool, [{
            'url': url, 'text': 'Una observación sintética' if duplicate else f'Observación sintética {index}',
            'published_at': (clock - timedelta(hours=1)).isoformat(),
            'observed_at': clock.isoformat(), 'metrics': {}, 'provenance': {'synthetic': True}}])
        flow.research.assess(run, [{'evidence_id': evidence, 'stance': 'context',
            'reason': 'Fixture, no conclusión factual', 'independence': independence}],
            {'continue': False, 'reason': 'Fin del fixture', 'gap': ''})
        report = flow.research.publish(run, 'Informe sintético.', [evidence], [],
            ['Fixture, no dato real'], {}, 'sufficient')
        evidence_ids.append(evidence)

    # A prior refusal suppresses the schedule suggestion, which otherwise has priority.
    schedule = flow.proposals.propose_schedule(topic, direct=True)
    flow.proposals.decide_schedule(schedule, None, False, 'decline-schedule')
    original = model.ask

    async def suggest_source(name, schema, payload):
        if name != 'detect_opportunities':
            return await original(name, schema, payload)
        model.calls.append((name, payload))
        assert {row['evidence_id'] for row in payload['evidence']} == set(evidence_ids)
        return {'kind': 'source', 'source_key': source, 'target_url': CANDIDATE,
            'evidence_ids': evidence_ids, 'reason': 'Sitio frecuente; pertinencia pendiente de revisión humana',
            'user_question': '¿Agregamos este sitio?'}

    model.ask = suggest_source
    state = {'request_id': 'research-2', 'topic_id': topic, 'run_id': run, 'question': 'Consulta independiente 2',
             'response': flow.report_response(flow.records().get('ai_mi_reports', report)),
             'last_report_id': report, 'last_topic': TOPIC, 'pending': None}
    asyncio.run(graph.aupdate_state(THREAD, state, as_node='publish_result'))
    return flow, model, provider, graph, evidence_ids


@pytest.mark.parametrize('accept', [True, False])
def test_three_independent_research_runs_propose_a_source_but_require_consent(accept):
    flow, model, provider, graph, evidence_ids = recurring_source()
    targets_before = flow.records().rows('ai_mi_source_targets')
    links_before = flow.records().rows('ai_mi_topic_sources')
    result = asyncio.run(graph.ainvoke(None, THREAD))
    candidate, = flow.records().rows('ai_mi_source_candidates')
    assert result['pending']['kind'] == 'source' and result['pending']['id'] == candidate['candidate_id']
    assert CANDIDATE in result['response']['suggestion']
    assert candidate['state'] == 'pending'
    assert set(json.loads(candidate['evidence_ids_json'])) == set(evidence_ids)
    assert json.loads(candidate['quality_frequency_json'])['frequency_is_not_quality'] is True
    assert flow.records().rows('ai_mi_source_targets') == targets_before
    assert flow.records().rows('ai_mi_topic_sources') == links_before
    assert [name for name, _ in model.calls] == ['detect_opportunities']
    assert not provider.starts

    decision = turn(graph, 'source-decision', 'sí' if accept else 'no')
    assert decision['pending'] is None
    assert flow.records().get('ai_mi_source_candidates', candidate['candidate_id'])['state'] == ('approved' if accept else 'rejected')
    assert len(flow.records().rows('ai_mi_source_targets')) == len(targets_before) + int(accept)
    assert len(flow.records().rows('ai_mi_topic_sources')) == len(links_before) + int(accept)
    assert not provider.starts


@pytest.mark.parametrize('variant', ['duplicate_observation', 'unknown_independence', 'duplicate_independence'])
def test_repeated_or_unverified_evidence_does_not_become_a_source_candidate(variant):
    flow, _, provider, graph, _ = recurring_source(
        duplicate=variant == 'duplicate_observation',
        independence={'unknown_independence': 'unknown', 'duplicate_independence': 'duplicate'}.get(variant, 'known'))
    before = flow.records().rows('ai_mi_source_targets')
    result = asyncio.run(graph.ainvoke(None, THREAD))
    assert result['pending'] is None
    assert not flow.records().rows('ai_mi_source_candidates')
    assert flow.records().rows('ai_mi_source_targets') == before
    assert not provider.starts


def test_three_research_runs_cannot_ground_a_candidate_in_another_hosts_republication():
    flow, _, provider, graph, _ = recurring_source(syndicated=True)
    before = flow.records().rows('ai_mi_source_targets')
    with pytest.raises(ValueError, match='grounded in the observed destinations'):
        asyncio.run(graph.ainvoke(None, THREAD))
    assert not flow.records().rows('ai_mi_source_candidates')
    assert flow.records().rows('ai_mi_source_targets') == before
    assert not provider.starts
