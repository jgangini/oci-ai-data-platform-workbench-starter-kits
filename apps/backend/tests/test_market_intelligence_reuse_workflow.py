"""A fresh capture may supplement, but must not erase, authorized known metadata."""
import asyncio

import pytest

pytest.importorskip('langgraph', reason='Graph execution is checked in the pinned runtime CI job')

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow import MarketWorkflow
from test_market_intelligence_reuse import captures, NOW, OLDER
from test_market_intelligence_workflow import ScenarioModel, SyntheticProvider


def test_fresh_youtube_search_reassesses_the_newer_known_video_and_discloses_reuse(captures):
    research, source, _, capture = captures
    capture('known-newer-video')
    model, provider = ScenarioModel(), SyntheticProvider()
    original = model.ask

    async def ask(name, schema, payload):
        if name == 'understand':
            result = await original(name, schema, payload)
            return {**result, 'selected_source_key': source}
        if name == 'draft_report':
            model.calls.append((name, payload))
            latest = max(payload['evidence'], key=lambda row: row['published_at'])
            return {'answer_markdown': 'El más reciente entre los resultados conocidos es del 31 de octubre. '
                    '[Video](' + latest['url'] + ')', 'cited_evidence_ids': [latest['evidence_id']],
                    'findings': [{'claim': 'Fecha del resultado más reciente de este conjunto sintético',
                        'kind': 'observation', 'supports': [latest['evidence_id']], 'refutes': [],
                        'limitations': ['No demuestra el más reciente de toda la plataforma.']}],
                    'limitations': [], 'chart_request': None, 'decision': {'status': 'answered', 'reason': 'Respuesta acotada de fixture.', 'material_gaps': []}}
        return await original(name, schema, payload)

    async def observations(receipt, call):
        return [{'url': 'https://www.youtube.com/watch?v=bbbbbbbbbbb', 'text': 'Video del día 30',
                 'published_at': OLDER, 'observed_at': NOW.isoformat(), 'metrics': {'viewCount': 20},
                 'provenance': {'source': call['actor'], 'content_basis': 'title_and_description'}}], 1000

    model.ask, provider.observations = ask, observations
    flow = MarketWorkflow(research.journal, model, provider, usage_cost_verified=True, clock=lambda: NOW)
    result = asyncio.run(flow.build().ainvoke({'request_id': 'fresh-search-with-known-metadata',
        'question': 'Busca en YouTube el video más reciente sobre D1 en Colombia'}, {'recursion_limit': 100}))
    assert 'report_id' in result['response'], result['response']
    assert len(provider.starts) == 1
    evidence = research.evidence(result['run_id'])
    assert len(evidence) == 2
    assessment, = [payload for name, payload in model.calls if name == 'assess_evidence']
    assert len(assessment['evidence']) == 2
    assert any(row['provenance'].get('reused_publication_metadata') for row in assessment['evidence'])
    assert '31 de octubre' in result['response']['markdown']
    assert 'No se volvieron a verificar' in result['response']['markdown']
    assert 'captura original' in result['response']['markdown']
