"""Large evidence stays durable; a bounded decision view cannot claim full coverage."""
import asyncio
import copy
import json

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.model import (
    context_size, evidence_context,
)


def observations(count):
    return [{'evidence_id': str(index), 'url': f'https://example.com/{index}',
             'text': '\U0001f600' * 3000, 'metrics': {'value': 12.3456789, 'currency': 'COP'},
             'assessment': {'stance': 'refutes' if index == count - 1 else 'supports'}}
            for index in range(count)]


@pytest.mark.parametrize('count', [51, 500])
def test_bounded_context_keeps_complete_rows_metrics_and_a_late_contradiction(count):
    rows = observations(count)
    before = copy.deepcopy(rows)
    result = evidence_context({'question': 'Compare'}, rows)
    assert context_size(result) <= 90000
    assert rows == before
    assert rows[-1] in result['evidence']
    assert all(row in rows for row in result['evidence'])
    coverage = result['evidence_coverage']
    assert coverage['total'] == count
    assert coverage['included'] + coverage['omitted'] == count
    assert coverage['included_by_stance']['refutes'] == 1
    assert coverage['total_by_stance']['supports'] == count - 1


def test_small_context_does_not_reorder_or_omit_evidence():
    rows = observations(2)
    result = evidence_context({'question': 'Compare'}, rows)
    assert result['evidence'] == rows
    assert result['evidence_coverage']['omitted'] == 0


@pytest.mark.parametrize('repair', [True, False])
@pytest.mark.parametrize('claim,text,basis', [
    ('D1 impulsa una iniciativa educativa.', 'Tiendas D1 impulsa la ...', 'search_title_and_snippet'),
    ('Los comentarios de clientes recomiendan el producto.', 'Nuevos productos D1: título y descripción.', 'title_and_description'),
    ('La empresa tiene 2.500 tiendas.', 'La empresa tiene más de 2.500 tiendas.', 'search_title_and_snippet'),
    ('El descuento es del 30%.', 'Descuentos de hasta el 30%.', 'search_title_and_snippet'),
])
def test_factual_review_repairs_only_the_draft_or_refuses_without_new_research(repair, claim, text, basis):
    from test_market_intelligence_workflow import configured
    flow, model, provider, _ = configured()
    rows = observations(1)
    rows[0].update(text=text, provenance={'content_basis': basis})
    snapshot = copy.deepcopy(flow.journal.read())
    calls = []
    drafts = 0

    async def ask(name, schema, payload):
        nonlocal drafts
        calls.append(name)
        if name == 'draft_report':
            drafts += 1
            if drafts == 2:
                assert 'El contenido capturado no respalda' not in payload['draft_validation']
                assert text in payload['draft_validation']
                assert payload['previous_draft']['findings'][0]['claim'] == claim
                assert claim in payload['previous_draft']['answer_markdown']
                assert 'evidence.invalid' in payload['previous_draft']['answer_markdown']
            assertion = claim if drafts == 1 or not repair else 'El título recuperado menciona productos o actividad de D1.'
            return {'answer_markdown': assertion + ' [Referencia](' + payload['evidence'][0]['url'] + ')',
                'cited_evidence_ids': ['0'], 'findings': [{'claim': assertion, 'kind': 'observation',
                    'supports': ['0'], 'refutes': [], 'limitations': []}], 'limitations': [], 'chart_request': None,
                'decision': {'status': 'answered', 'reason': 'Descripción acotada.', 'material_gaps': []}}
        assert name == 'review_factual_support'
        assert 'previous_draft' not in payload and 'draft_validation' not in payload
        assert payload['evidence'][0]['text'] == text
        assert 'evidence.invalid' in payload['draft']['answer_markdown']
        if drafts == 2 and repair:
            return {'accepted': True, 'issues': []}
        return {'accepted': False, 'issues': [{'claim': claim, 'evidence_id': '0', 'quoted_text': text,
            'reason': 'El contenido capturado no respalda la afirmación; no se leyó esa información.'}]}

    model.ask = ask
    if repair:
        draft, _ = asyncio.run(flow.write_report({'question': '¿Qué se dice de D1?'}, rows))
        assert claim not in draft['answer_markdown']
        assert rows[0]['url'] in draft['answer_markdown']
    else:
        with pytest.raises(ValueError, match='unsupported material claims'):
            asyncio.run(flow.write_report({'question': '¿Qué se dice de D1?'}, rows))
    assert calls == ['draft_report', 'review_factual_support'] * 2
    assert flow.journal.read() == snapshot
    assert not provider.starts


def test_factual_reviewer_cannot_repair_from_an_invented_excerpt():
    from test_market_intelligence_workflow import configured
    flow, model, provider, _ = configured()
    original = model.ask

    async def ask(name, schema, payload):
        if name != 'review_factual_support':
            return await original(name, schema, payload)
        return {'accepted': False, 'issues': [{'claim': payload['draft']['findings'][0]['claim'],
            'evidence_id': '0', 'quoted_text': 'Texto inexistente', 'reason': 'Objeción no respaldada.'}]}

    model.ask = ask
    with pytest.raises(ValueError, match='actual claim and captured text'):
        asyncio.run(flow.write_report({}, observations(1)))
    assert not provider.starts and not flow.records().rows('ai_mi_reports')


@pytest.mark.parametrize('fact', [
    'Más de 2.500 tiendas al cierre de 2025.',
    'Exactamente 2.500 tiendas al cierre de 2025.',
    'Ventas entre 10 y 12 millones COP en 2024.',
    'Aproximadamente 40 trabajadores en junio de 2023.',
])
def test_repair_receives_the_previous_draft_and_preserves_supported_facts(fact):
    from test_market_intelligence_workflow import configured
    flow, model, provider, _ = configured()
    rows = observations(1)
    rows[0]['text'] = fact.replace(' ', '\u00a0') + ' Tiendas D1 impulsa la ...'
    unsupported = 'D1 impulsa una iniciativa educativa.'
    calls = []
    original = None

    async def recorded(name, schema, payload):
        nonlocal original
        calls.append(name)
        if name == 'draft_report':
            if original is None:
                original = {'answer_markdown': fact + ' ' + unsupported + ' [Fuente](' + payload['evidence'][0]['url'] + ')',
                    'cited_evidence_ids': ['0'], 'findings': [{'claim': fact, 'kind': 'observation',
                        'supports': ['0'], 'refutes': [], 'limitations': []}], 'limitations': [], 'chart_request': None,
                    'decision': {'status': 'partial', 'reason': 'El contenido de la iniciativa está truncado.',
                                 'material_gaps': ['Texto completo de la iniciativa.']}}
                return copy.deepcopy(original)
            assert payload['previous_draft'] == original
            assert fact in payload['evidence'][0]['text'].replace('\u00a0', ' ')
            feedback = json.loads(payload['draft_validation'].split(': ', 1)[1])
            assert feedback[0]['quoted_text'] == rows[0]['text']
            assert 'reason' not in feedback[0]
            repaired = copy.deepcopy(payload['previous_draft'])
            repaired['answer_markdown'] = repaired['answer_markdown'].replace(unsupported, 'La iniciativa no se puede precisar con el fragmento disponible.')
            return repaired
        assert 'previous_draft' not in payload and 'draft_validation' not in payload
        return {'accepted': False, 'issues': [{'claim': unsupported, 'evidence_id': '0',
            'quoted_text': rows[0]['text'].replace('\u00a0', ' '), 'reason': 'No hay respaldo de la atribución educativa.'}]} if len(calls) == 2 else {'accepted': True, 'issues': []}

    before = copy.deepcopy(flow.journal.read())
    model.ask = recorded
    draft, _ = asyncio.run(flow.write_report({'question': 'Resume la información recuperada.'}, rows))
    assert fact in draft['answer_markdown'] and unsupported not in draft['answer_markdown']
    assert draft['findings'][0]['claim'] == fact
    assert rows[0]['url'] in draft['answer_markdown']
    assert draft['decision']['status'] == 'partial'
    assert calls == ['draft_report', 'review_factual_support'] * 2
    assert flow.journal.read() == before and not provider.starts


@pytest.mark.parametrize('text,quotation', [
    ('Más de\u00a02.500 tiendas.', 'Más de 2.500 tiendas.'),
    ('Entre\u202f10 y\u202f12 millones COP.', 'Entre 10 y 12 millones COP.'),
    ('Exactamente 40 trabajadores.', 'Exactamente 40 trabajadores.'),
])
def test_captured_excerpt_restores_the_literal_source_span(text, quotation):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow_actions import captured_excerpt
    assert captured_excerpt(text, quotation) == text


@pytest.mark.parametrize('quotation', ['2.600 tiendas', '2.500 tiendas al cierre de 2026', 'hasta 2.500 tiendas', ''])
def test_captured_excerpt_does_not_normalize_numbers_dates_or_meaning(quotation):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow_actions import captured_excerpt
    with pytest.raises(ValueError, match='captured text'):
        captured_excerpt('Más de\u00a02.500 tiendas al cierre de 2025.', quotation)


@pytest.mark.parametrize('all_sources_required', [False, True])
def test_large_repair_keeps_critic_sources_and_reviews_the_full_original_context(all_sources_required):
    from test_market_intelligence_workflow import configured
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.model import DecisionModel
    flow, _, provider, _ = configured()
    rows = observations(2)
    for index, row in enumerate(rows):
        row['text'] = ('x' if index == 0 else 'y') * 43800
    calls = []
    unsupported = 'q' * 1900
    fact = 'Más de 2.500 tiendas al cierre de 2025.'

    class RecordedLLM:
        def with_structured_output(self, schema, method):
            return self

        async def ainvoke(self, messages):
            data = json.loads(messages[1].content)
            assert len(messages[1].content.encode()) <= 100000
            calls.append(data)
            if len(calls) == 2:
                assert {row['evidence_id'] for row in data['evidence']} == {'0', '1'}
                return {'accepted': False, 'issues': [{'claim': unsupported, 'evidence_id': str(index),
                    'quoted_text': ('x' if index == 0 else 'y') * 1900,
                    'reason': 'Synthetic capacity control.'} for index in range(2 if all_sources_required else 1)]}
            if len(calls) == 4:
                assert {row['evidence_id'] for row in data['evidence']} == {'0', '1'}
                assert 'previous_draft' not in data and 'repair_context_limited' not in data
                return {'accepted': True, 'issues': []}
            if len(calls) == 3:
                assert data['repair_context_limited'] is True
                assert [row['evidence_id'] for row in data['evidence']] == ['0']
                assert data['repair_evidence_coverage']['omitted'] == 1
                assert data['evidence_coverage']['omitted'] == 0
                assert fact in data['previous_draft']['answer_markdown']
                return {**data['previous_draft'], 'answer_markdown': fact + ' ' + ' '.join(
                    '[Fuente](' + row['url'] + ')' for row in calls[0]['evidence'])}
            links = ' '.join('[Fuente](' + row['url'] + ')' for row in data['evidence'])
            return {'answer_markdown': (unsupported + ' ' + 'r' * 7000 + ' ' if len(calls) == 1 else '') + fact + ' ' + links,
                'cited_evidence_ids': [row['evidence_id'] for row in data['evidence']],
                'findings': [], 'limitations': [], 'chart_request': None,
                'decision': {'status': 'partial', 'reason': 'Synthetic capacity fixture.', 'material_gaps': ['Missing context.']}}

    flow.model = DecisionModel(RecordedLLM())
    before = copy.deepcopy(flow.journal.read())
    if all_sources_required:
        with pytest.raises(ValueError, match='Required factual repair context exceeds'):
            asyncio.run(flow.write_report({'question': 'Resume la evidencia.'}, rows))
        assert len(calls) == 2
    else:
        draft, coverage = asyncio.run(flow.write_report({'question': 'Resume la evidencia.'}, rows))
        assert fact in draft['answer_markdown']
        assert draft['cited_evidence_ids'] == ['0', '1']
        assert coverage['included'] == coverage['total'] == 2 and coverage['omitted'] == 0
        assert len(calls) == 4
    assert flow.journal.read() == before and not provider.starts


@pytest.mark.parametrize('captured_text', ['Tiendas D1 impulsa la ...', 'D1 anuncia una iniciativa educativa.'])
def test_drafting_uses_captured_text_without_repeating_prior_generated_reasons(captured_text):
    from test_market_intelligence_workflow import configured
    flow, model, provider, _ = configured()
    rows = observations(2)
    rows[0].update(text=captured_text, published_at=None, observed_at='2026-11-02T15:02:18+00:00',
                   assessment={'stance': 'supports', 'independence': 'unknown',
                               'reason': 'D1 impulsa educación: conclusión generada sin respaldo.'},
                   relevance={'state': 'relevant', 'reason': 'Promueve una iniciativa educativa.'},
                   provenance={'content_basis': 'search_title_and_snippet'})
    before = copy.deepcopy(rows)
    original = model.ask
    seen = []

    async def inspect(name, schema, payload):
        if name == 'draft_report':
            seen.append(copy.deepcopy(payload))
        return await original(name, schema, payload)

    model.ask = inspect
    publication_window = {'start': '2026-10-26T15:02:18+00:00', 'end': '2026-11-02T15:02:18+00:00'}
    draft, coverage = asyncio.run(flow.write_report({'question': '¿Qué se dice de D1?', 'window': publication_window,
        'remaining_gap': 'Falta otra pieza del corpus: interpretación generada, no fuente.'}, rows))
    assert rows == before
    assert evidence_context({}, rows)['evidence'] == before
    assert len(seen) == 1 and seen[0]['evidence'][0]['text'] == captured_text
    assert seen[0]['evidence'][0]['assessment'] == {'stance': 'supports', 'independence': 'unknown'}
    assert seen[0]['evidence'][0]['relevance'] == {'state': 'relevant'}
    assert seen[0]['evidence'][0]['published_at'] is None
    assert seen[0]['evidence'][0]['observed_at'] == before[0]['observed_at']
    assert seen[0]['evidence'][0]['provenance'] == before[0]['provenance']
    assert seen[0]['evidence'][0]['metrics'] == before[0]['metrics']
    assert 'window' not in seen[0]
    assert 'remaining_gap' not in seen[0]
    assert seen[0]['requested_publication_window'] == publication_window
    assert seen[0]['captured_observations_at'] == ['2026-11-02T15:02:18+00:00']
    assert seen[0]['evidence'][0]['source_domain'] == 'example.com'
    assert seen[0]['evidence'][0]['url'].startswith('https://evidence.invalid/')
    assert seen[0]['evidence'][0]['url'] != before[0]['url']
    assert coverage['included_by_stance'] == coverage['total_by_stance']
    assert coverage['total_by_stance']['supports'] == coverage['total_by_stance']['refutes'] == 1
    assert set(draft['cited_evidence_ids']) == {'0', '1'}
    assert before[0]['url'] in draft['answer_markdown']
    assert 'evidence.invalid' not in json.dumps(draft)
    assert not provider.starts


@pytest.mark.parametrize('destination', ['https://unverified.example/claim', 'javascript:alert',
                                       'https://evidence.invalid/unknown'])
def test_draft_reference_restore_rejects_unverified_destinations(destination):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.model import report_context
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow_actions import restore_report_links
    rows = observations(2)
    context = report_context({}, rows)
    draft = {'cited_evidence_ids': ['0'], 'answer_markdown': f'[Reference]({destination})'}
    with pytest.raises(ValueError):
        restore_report_links(draft, context['evidence'], rows)
    draft['answer_markdown'] = '[Reference](' + context['evidence'][1]['url'] + ')'
    with pytest.raises(ValueError):
        restore_report_links(draft, context['evidence'], rows)


@pytest.mark.parametrize('field', ['limitations', 'findings', 'decision'])
def test_reference_alias_cannot_escape_in_a_structured_field(field):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.model import report_context
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow_actions import restore_report_links
    rows = observations(2)
    context = report_context({}, rows)
    unselected = context['evidence'][1]['url']
    draft = {'cited_evidence_ids': ['0'], 'answer_markdown': 'A bounded response.',
             field: [unselected] if field == 'limitations' else {'claim' if field == 'findings' else 'reason': unselected}}
    with pytest.raises(ValueError, match='unresolved evidence reference'):
        restore_report_links(draft, context['evidence'], rows)


def test_expanding_a_reference_cannot_consume_space_reserved_for_required_notices():
    from test_market_intelligence_workflow import configured
    flow, model, _, _ = configured()
    rows = observations(1)
    rows[0]['url'] = 'https://example.com/' + 'a' * 500
    requested = []

    async def full_body(name, schema, payload):
        limit = schema['properties']['answer_markdown']['maxLength']
        requested.append(limit)
        link = '[Reference](' + payload['evidence'][0]['url'] + ')'
        return {'answer_markdown': 'a' * (limit - len(link)) + link, 'cited_evidence_ids': ['0'],
                'findings': [], 'limitations': [], 'chart_request': None,
                'decision': {'status': 'answered', 'reason': 'Bounded fixture', 'material_gaps': []}}

    model.ask = full_body
    with pytest.raises(ValueError, match='allowance'):
        asyncio.run(flow.write_report({'provider_limitations': ['Required notice.']}, rows))
    assert len(requested) == 2


def test_video_metadata_with_uncertain_dates_always_keeps_the_temporal_limitation():
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.model import context_limitations
    rows = [{'evidence_id': 'video', 'text': 'Título y descripción', 'published_at': None,
             'provenance': {'content_basis': 'title_and_description', 'publication_date_precision': 'day',
                            'publication_date_raw': '2026-10-30', 'text_truncated': True}}]
    limits = context_limitations(evidence_context({}, rows))
    assert any('periodo exacto' in line and 'fecha incierta' in line for line in limits)
    assert any('descripciones' in line and 'no se revisaron los videos completos' in line for line in limits)
    assert all('página' not in line for line in limits)


def test_reused_publication_metadata_always_discloses_its_capture_and_freshness_limit():
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.model import context_limitations
    rows = [{'evidence_id': 'video', 'text': 'Título guardado', 'published_at': '2026-10-31T10:00:00+00:00',
             'provenance': {'content_basis': 'title_and_description', 'reused_publication_metadata': True,
                            'original_observed_at': '2026-11-01T16:00:00+00:00'}}]
    limits = context_limitations(evidence_context({}, rows))
    assert len(limits) == 1
    assert '24 horas' in limits[0] and 'captura original' in limits[0]
    assert 'No se volvieron a verificar' in limits[0] and 'más reciente de toda la plataforma' in limits[0]


def test_oversized_rows_are_counted_without_cutting_quotes_or_blocking_smaller_rows():
    rows = observations(2)
    rows[0]['text'] *= 20
    result = evidence_context({'question': 'Compare'}, rows)
    assert result['evidence'] == [rows[1]]
    assert result['evidence_coverage']['omitted'] == 1
    assert len(rows[0]['text']) == 60000
    with pytest.raises(ValueError, match='metadata'):
        evidence_context({'question': '\U0001f600' * 30000}, rows)


def test_full_graph_persists_all_evidence_but_marks_bounded_report_and_followup_partial():
    pytest.importorskip('langgraph')
    from test_market_intelligence_workflow import configured, turn
    flow, model, provider, graph = configured()
    original_ask, original_observations = model.ask, provider.observations

    async def capture(receipt, call):
        rows, cost = await original_observations(receipt, call)
        return [{**rows[0], 'url': f'https://example.com/reference/{index}',
                 'text': '\U0001f600' * 3000} for index in range(50)], cost

    async def ask(name, schema, payload):
        assert context_size(payload) <= 100000
        result = await original_ask(name, schema, payload)
        if name == 'assess_evidence':
            result.update(continue_research=False, gap='')
        return result

    model.ask, provider.observations = ask, capture
    result = turn(graph, 'large-report')
    report = flow.records().get('ai_mi_reports', result['response']['report_id'])
    coverage = json.loads(report['coverage_json'])['model_context']
    assert len(flow.research.evidence(result['run_id'])) == 50
    assert 0 < coverage['included'] < 50 and coverage['omitted'] > 0
    assert result['reason'] == 'unresolved'
    assert 'Análisis parcial:' in report['markdown']
    assert 'Análisis parcial:' in report['limitations_json']
    assert len(provider.starts) == 1
    model.intents = ['followup']
    followup = turn(graph, 'large-followup', 'Resume lo ya encontrado')
    assert followup['response']['reused'] is True
    assert 'Análisis parcial:' in followup['response']['markdown']
    assert len(provider.starts) == 1 and len(flow.records().rows('ai_mi_reports')) == 1


def test_report_cannot_cite_omitted_evidence_and_no_model_runs_if_no_row_fits():
    pytest.importorskip('langgraph')
    from test_market_intelligence_workflow import configured
    flow, model, provider, _ = configured()
    rows = observations(51)
    selected = evidence_context({}, rows)['evidence']
    omitted = next(row for row in rows if row not in selected)
    original = model.ask

    async def cite_omitted(name, schema, payload):
        result = await original(name, schema, payload)
        result['cited_evidence_ids'] = [omitted['evidence_id']]
        return result

    model.ask = cite_omitted
    with pytest.raises(ValueError):
        asyncio.run(flow.write_report({}, rows))
    model.calls.clear()
    rows[0]['text'] *= 20
    draft, coverage = asyncio.run(flow.write_report({}, rows[:1]))
    assert coverage['included'] == 0 and draft['cited_evidence_ids'] == []
    assert 'no pude analizarla' in draft['answer_markdown']
    assert not model.calls and not provider.starts


def test_report_schema_reserves_notice_space_without_cutting_markdown_links_or_relaxing_bounds():
    pytest.importorskip('langgraph')
    from test_market_intelligence_workflow import configured
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.contracts import ReportDraft
    flow, model, _, _ = configured()
    requested_limits = []

    async def longest_allowed(name, schema, payload):
        if name == 'review_factual_support':
            return {'accepted': True, 'issues': []}
        limit = schema['properties']['answer_markdown']['maxLength']
        requested_limits.append(limit)
        return {'answer_markdown': 'a' * limit, 'cited_evidence_ids': [], 'findings': [],
                'limitations': [], 'chart_request': None, 'decision': {'status': 'answered', 'reason': 'Respuesta acotada de fixture.', 'material_gaps': []}}

    model.ask = longest_allowed
    provider_notice = 'La fuente seleccionada no respondió.'
    extension = '¿Puedes aportar la etiqueta con el fabricante?'
    draft, coverage = asyncio.run(flow.write_report(
        {'provider_limitations': [provider_notice], 'requested_extension': extension}, observations(51)))
    composed = provider_notice + '\n\n' + draft['answer_markdown']
    ReportDraft.model_validate({**draft, 'answer_markdown': composed})
    assert coverage['omitted'] > 0 and len(composed) == 20000
    assert requested_limits[0] < 20000
    assert composed.endswith(extension)

    async def ignores_allowance(*args):
        return {'answer_markdown': 'a' * 20000, 'cited_evidence_ids': [], 'findings': [],
                'limitations': [], 'chart_request': None, 'decision': {'status': 'answered', 'reason': 'Respuesta acotada de fixture.', 'material_gaps': []}}

    model.ask = ignores_allowance
    with pytest.raises(ValueError, match='allowance'):
        asyncio.run(flow.write_report({}, observations(51)))


@pytest.mark.parametrize('visible', [True, False])
def test_extension_is_a_safe_pending_question_not_a_factual_citation_even_without_visible_evidence(visible):
    from test_market_intelligence_workflow import configured
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.contracts import ReportDraft
    flow, model, provider, _ = configured()
    rows = observations(1)
    if not visible:
        rows[0]['text'] *= 20
    question = '¿Autorizas https://example.org/nueva y [esta](javascript:alert)? <script>no</script>'
    draft, _ = asyncio.run(flow.write_report({'requested_extension': question}, rows))
    ReportDraft.model_validate(draft)
    assert '**Para continuar**' in draft['answer_markdown']
    assert 'https://example.org/nueva' in draft['answer_markdown']
    assert '<script>' not in draft['answer_markdown']
    assert r'\[esta\]\(javascript:alert\)' in draft['answer_markdown']
    assert not provider.starts
    if not visible:
        assert not model.calls


def test_extension_cannot_propose_a_private_network_url():
    from test_market_intelligence_workflow import configured
    flow, model, provider, _ = configured()
    with pytest.raises(ValueError):
        asyncio.run(flow.write_report({'requested_extension': '¿Autorizas https://127.0.0.1/secrets?'}, observations(1)))
    assert not model.calls and not provider.starts


def test_a_truncated_web_capture_gets_a_mandatory_notice_even_when_all_evidence_rows_fit():
    from test_market_intelligence_workflow import configured
    flow, model, provider, _ = configured()
    rows = observations(1)
    rows[0]['provenance'] = {'text_truncated': True, 'original_character_count': 14000}
    draft, coverage = asyncio.run(flow.write_report({}, rows))
    assert coverage['omitted'] == 0
    assert 'fragmentos acotados de 1 página;' in draft['answer_markdown']
    assert 'no se verificó todo su contenido' in draft['limitations'][0]
    assert not provider.starts
