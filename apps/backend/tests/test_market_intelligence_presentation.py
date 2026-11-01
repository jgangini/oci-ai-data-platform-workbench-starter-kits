"""The native chat must show evidence and consent details, not a JSON dump."""
from copy import deepcopy

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.presentation import response_markdown


def test_financial_table_retains_units_periods_unknowns_and_original_precision():
    response = {'markdown': 'Informe parcial con [referencia](https://example.test/evidence).',
                'report_id': 'private-report-identity', 'chart': {
                    'title': 'WMMVY · Precio de cierre', 'caption': 'Unidad: USD',
                    'columns': ['period', 'close'],
                    'rows': [['2026-10-26', 27.110000610351562], ['2026-10-27', None],
                             ['2026-10-28', 0.00000000000123456789]]}}
    original = deepcopy(response)
    text = response_markdown(response)
    assert response['markdown'] in text
    assert 'Unidad: USD' in text and '| Periodo | Cierre |' in text
    assert '| 2026-10-26 | 27.110001 |' in text
    assert '| 2026-10-27 | — |' in text
    assert '| 2026-10-28 | 1.2345679e-12 |' in text
    assert '8 cifras significativas' in text and 'dato no disponible' in text
    assert 'private-report-identity' not in text
    assert response == original


def test_labels_cannot_insert_html_links_or_extra_table_rows():
    text = response_markdown({'markdown': 'Validado.', 'chart': {
        'title': '<script>title</script>', 'caption': 'Muestra recuperada.',
        'columns': ['source', 'observations'],
        'rows': [['[sitio](javascript:evil) | extra\nsegunda fila', 2]]}})
    assert '<script>' not in text and '&lt;script&gt;' in text
    assert '\\[sitio\\]\\(javascript:evil\\)' in text
    assert '\\| extra segunda fila' in text
    assert len(text.splitlines()) == 11


@pytest.mark.parametrize('value', [float('nan'), float('inf'), True, '27.1'])
def test_invalid_financial_values_do_not_become_displayed_evidence(value):
    with pytest.raises(ValueError, match='finite numbers'):
        response_markdown({'markdown': 'Validado.', 'chart': {
            'title': 'Serie', 'caption': 'Unidad: USD', 'columns': ['period', 'close'],
            'rows': [['2026-10-26', value]]}})


def test_schedule_consent_shows_full_plan_and_keeps_recurring_suggestion():
    response = {'markdown': '¿Lo apruebas?', 'suggestion': 'Este tema es recurrente.',
                'schedule_summary': {
                    'recurrence': {'weekdays': [1, 5], 'hour': 9, 'minute': 5, 'timezone': 'America/Bogota'},
                    'research': {'window_days': 7, 'target_ids': ['internal-target'], 'cap_microusd': 100001},
                    'sources': [{'name': 'Fuente oficial', 'target': 'https://example.test/news'}]}}
    text = response_markdown(response)
    for expected in ('lunes, viernes', '09:05', 'America/Bogota', 'últimos 7 días',
                     'US$0.100001', 'Fuente oficial', 'https://example.test/news',
                     '¿Lo apruebas?', 'Este tema es recurrente.'):
        assert expected in text
    assert 'internal-target' not in text
    response['schedule_summary']['sources'] = []
    with pytest.raises(ValueError, match='visible source'):
        response_markdown(response)


def test_plain_question_and_paused_readiness_do_not_dump_operational_fields():
    assert response_markdown({'markdown': '¿Qué periodo deseas?', 'chart': None}) == '¿Qué periodo deseas?'
    text = response_markdown({'mode': 'configured_paused', 'mcp_verified': True,
                              'generation_id': 'private-generation', 'node_names': ['internal-node']})
    assert 'Conexión MCP verificada' in text and 'pausada' in text
    assert 'private-generation' not in text and 'internal-node' not in text
    with pytest.raises(ValueError, match='has not been verified'):
        response_markdown({'mode': 'configured_paused', 'mcp_verified': False})


def test_untrusted_source_suggestion_cannot_add_markup_or_hide_a_link_destination():
    text = response_markdown({'markdown': 'Informe con referencias verificadas.',
                              'suggestion': '¿Agregamos la fuente? <img src=x> [Validar acceso](https://other.example)'})
    assert '<img' not in text and '&lt;img src=x&gt;' in text
    assert '\\[Validar acceso\\]\\(https://other.example\\)' in text
    assert text.startswith('Informe con referencias verificadas.')
