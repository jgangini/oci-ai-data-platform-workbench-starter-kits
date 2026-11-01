"""Present verified responses as Markdown without changing their stored evidence."""
from decimal import Decimal
from html import escape
import math


def _label(value):
    text = escape(str(value), quote=False).replace('\n', ' ').replace('\r', ' ')
    for char in ('\\', '`', '*', '_', '[', ']', '(', ')', '|', '!'):
        text = text.replace(char, '\\' + char)
    return text


def _chart_table(chart):
    columns, rows = chart['columns'], chart['rows']
    if len(columns) != 2 or not 1 <= len(rows) <= 50:
        raise ValueError('A bounded two-column evidence series is required')
    labels = {'period': 'Periodo', 'date': 'Fecha', 'source': 'Fuente',
              'close': 'Cierre', 'value': 'Valor', 'observations': 'Observaciones'}
    lines = ['**' + _label(chart['title']) + '**', '', _label(chart['caption']), '',
             '| ' + ' | '.join(labels.get(column, _label(column)) for column in columns) + ' |',
             '| --- | ---: |']
    for row in rows:
        if len(row) != 2:
            raise ValueError('An evidence series row must contain a label and value')
        value = row[1]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError('Evidence values must be finite numbers or unknown')
        displayed = '—' if value is None else format(value, '.8g')
        lines.append('| ' + _label(row[0]) + ' | ' + displayed + ' |')
    lines.extend(['', 'Hasta 8 cifras significativas; — indica un dato no disponible.'])
    return '\n'.join(lines)


def _schedule_summary(summary):
    recurrence, research = summary['recurrence'], summary['research']
    days = ('lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo')
    selected_days = ', '.join(days[number - 1] for number in recurrence['weekdays'])
    clock = f"{recurrence['hour']:02d}:{recurrence['minute']:02d}"
    sources = summary['sources']
    if not sources or len(sources) != len(research['target_ids']):
        raise ValueError('Every scheduled target must have a visible source')
    budget = Decimal(research['cap_microusd']) / Decimal(1_000_000)
    return '\n'.join([
        '**Programación propuesta**', '',
        '- Días: ' + selected_days + '.',
        '- Hora: ' + clock + ' (' + _label(recurrence['timezone']) + ').',
        '- Ventana: últimos ' + str(research['window_days']) + ' días en cada ejecución.',
        f'- Tope por ejecución: US${budget:.6f}. No es un precio garantizado.',
        '- Fuentes: ' + '; '.join(_label(source['name']) + ' · ' + _label(source['target']) for source in sources),
    ])


def response_markdown(response):
    if response.get('mode') == 'configured_paused':
        if response.get('mcp_verified') is not True:
            raise ValueError('MCP connectivity has not been verified')
        return 'Conexión MCP verificada. La investigación está pausada; no se iniciaron capturas ni consultas al modelo.'
    markdown = response.get('markdown')
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError('A readable verified response is required')
    parts = [markdown]
    if response.get('chart') is not None:
        parts.append(_chart_table(response['chart']))
    if response.get('schedule_summary') is not None:
        parts.append(_schedule_summary(response['schedule_summary']))
    if response.get('suggestion'):
        parts.append(_label(response['suggestion']))
    return '\n\n'.join(parts)
