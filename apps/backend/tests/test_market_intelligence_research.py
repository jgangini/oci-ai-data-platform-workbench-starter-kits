import json

import pytest

from app.labs.ai_market_intelligence.research import Research
from app.labs.ai_market_intelligence.execution import PaidOperations
from app.labs.ai_market_intelligence.repository import Journal, json_text
from test_market_intelligence_repository import MemorySnapshot, SCOPE


WINDOW = {'start': '2026-10-23T00:00:00+00:00', 'end': '2026-10-30T00:00:00+00:00'}
TOPIC = {'entity': 'D1', 'market': 'Colombia', 'objective': 'Novedades'}


def running():
    journal = Journal(MemorySnapshot(), 100000)
    research = Research(journal)
    question, _ = research.record_question('request-1', 'Novedades de D1 en Colombia', TOPIC)
    run = research.prepare_run(question, WINDOW, 'Contrastar novedades recientes', 100000)
    source = SCOPE.key('source-1')
    journal.transact(lambda records, state: records.upsert('ai_mi_sources', {'source_key': source, 'status': 'active',
        'connector_revision': 'x_search', 'allowed_scope_json': json_text({'allow_discovery': True})}))
    paid = PaidOperations(journal)
    tool = paid.reserve(run, source, None, {}, 10000, 'Buscar menciones recientes', authorization_hash=paid.authorize(run, source, None)['authorization_hash'])
    permit = paid.claim_start(tool)
    paid.record_provider(tool, permit, 'provider-1', 'succeeded', {})
    return research, run, tool


def observation(text='Una opinión', url='https://example.com/publicacion'):
    return {'url': url, 'text': text, 'published_at': '2026-10-29T12:00:00+00:00',
            'observed_at': '2026-10-30T00:00:00+00:00', 'metrics': {},
            'provenance': {'source': 'test', 'independence': 'unknown'}}


def test_question_retries_are_idempotent_but_cannot_change_content():
    research = Research(Journal(MemorySnapshot(), 0))
    first = research.record_question('same', 'Consulta', TOPIC)
    assert research.record_question('same', 'Consulta', TOPIC) == first
    assert len(research.journal.read()[0]['tables']['ai_mi_questions']) == 1
    with pytest.raises(ValueError, match='reused'):
        research.record_question('same', 'Otra consulta', TOPIC)


def test_followups_cannot_become_another_paid_research():
    research = Research(Journal(MemorySnapshot(), 0))
    question, _ = research.record_question('followup', 'Resúmelo', TOPIC, 'followup')
    with pytest.raises(ValueError, match='new research question'):
        research.prepare_run(question, WINDOW, 'Reformatear', 0)


def test_same_content_deduplicates_but_changed_content_gets_a_new_observation():
    research, run, tool = running()
    first, = research.add_evidence(run, tool, [observation()])
    same, = research.add_evidence(run, tool, [observation()])
    changed, = research.add_evidence(run, tool, [observation('Contenido editado')])
    assert first == same and changed != first
    value, _ = research.journal.read()
    documents = list(value['tables']['ai_mi_documents'].values())
    assert len(documents) == 2 and len({d['document_key'] for d in documents}) == 1


def test_equivalent_urls_do_not_count_as_independent_documents():
    research, run, tool = running()
    first, = research.add_evidence(run, tool, [observation(url='https://twitter.com/user/status/1?utm_source=feed')])
    second, = research.add_evidence(run, tool, [observation(url='https://x.com/user/status/1')])
    assert first == second
    assert len(research.evidence(run)) == 1


def test_provider_day_search_does_not_expand_the_exact_research_window():
    research, run, tool = running()
    def narrow(records, state):
        row = records.get('ai_mi_research_runs', run)
        records.upsert('ai_mi_research_runs', {**row, 'plan_window_json': json.dumps({
            'version': 1, 'start': '2026-10-29T14:30:00+00:00', 'end': '2026-10-30T14:30:00+00:00'})})
    research.journal.transact(narrow)
    early = {**observation(), 'published_at': '2026-10-29T01:00:00+00:00'}
    inside = {**observation(), 'published_at': '2026-10-29T15:00:00+00:00'}
    boundary = {**observation(), 'published_at': '2026-10-30T14:30:00+00:00'}
    assert len(research.add_evidence(run, tool, [early, inside, boundary])) == 1
    assert len(research.evidence(run)) == 1


def test_supporting_and_contradictory_evidence_are_retained():
    research, run, tool = running()
    support, refute = research.add_evidence(run, tool, [observation('A favor'), observation('En contra', 'https://example.com/otra')])
    research.assess(run, [{'evidence_id': support, 'stance': 'supports', 'reason': 'Declaración', 'independence': 'unknown'},
                         {'evidence_id': refute, 'stance': 'refutes', 'reason': 'Declaración contraria', 'independence': 'unknown'}],
                    {'continue': False, 'reason': 'Muestra parcial', 'gap': ''})
    finding = {'claim': 'Las opiniones recopiladas difieren', 'kind': 'observation',
               'supports': [support], 'refutes': [refute], 'limitations': ['Muestra no representativa']}
    report = research.publish(run, 'Las opiniones recopiladas difieren.', [support, refute], [finding],
                              ['Muestra no representativa'], {'sources': 1}, 'sufficient')
    value, _ = research.journal.read()
    stored = value['tables']['ai_mi_reports'][report]
    assert json.loads(stored['findings_json'])[0]['refutes'] == [refute]
    assert stored['chart_spec_json'] is None
    assert value['tables']['ai_mi_research_runs'][run]['state'] == 'closed'


def test_reports_cannot_cite_an_unrelated_or_invented_observation():
    research, run, _ = running()
    with pytest.raises(ValueError, match='outside this research'):
        research.publish(run, 'Una afirmación', ['invented'], [], [], {}, 'sufficient')


@pytest.mark.parametrize('markdown', ['<script>alert(1)</script>', '```python\npass\n```'])
def test_report_rejects_raw_html_and_code(markdown):
    research, run, _ = running()
    with pytest.raises(ValueError, match='Markdown'):
        research.publish(run, markdown, [], [], [], {}, 'no_data')
