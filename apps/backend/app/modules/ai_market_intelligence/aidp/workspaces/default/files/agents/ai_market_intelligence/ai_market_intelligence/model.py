"""Structured model calls with bounded data and the six approved prompts."""
import json
from hashlib import sha256
from itertools import zip_longest
from pathlib import Path
from urllib.parse import urlsplit


def context_size(payload):
    return len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8'))


def context_coverage(evidence, selected):
    def counts(rows):
        return {stance: sum((row.get('assessment') or {}).get('stance', 'unassessed') == stance for row in rows)
                for stance in ('supports', 'refutes', 'context', 'unclear', 'unassessed')}
    return {'total': len(evidence), 'included': len(selected), 'omitted': len(evidence) - len(selected),
            'total_by_stance': counts(evidence), 'included_by_stance': counts(selected)}


def evidence_context(payload, evidence):
    """Bound a decision's view, keeping whole rows and explicit omitted coverage."""
    context = {**payload, 'evidence': evidence, 'evidence_coverage': context_coverage(evidence, evidence)}
    if context_size(context) <= 90000:
        return context
    groups = [[row for row in evidence if (row.get('assessment') or {}).get('stance', 'unassessed') == stance]
              for stance in ('refutes', 'supports', 'context', 'unclear', 'unassessed')]
    selected = []
    context = {**payload, 'evidence': selected, 'evidence_coverage': context_coverage(evidence, selected)}
    if context_size(context) > 90000:
        raise ValueError('Decision metadata exceeds the admitted context')
    for row in (row for group in zip_longest(*groups) for row in group if row is not None):
        candidate = [*selected, row]
        proposed = {**payload, 'evidence': candidate, 'evidence_coverage': context_coverage(evidence, candidate)}
        if context_size(proposed) <= 90000:
            context, selected = proposed, candidate
    return context


def report_context(payload, evidence):
    """Draft from captured content, without promoting earlier model reasons to facts."""
    rows = []
    for observation in evidence:
        row = dict(observation)
        row['source_domain'] = urlsplit(row['url']).hostname
        row['url'] = 'https://evidence.invalid/' + sha256(row['evidence_id'].encode()).hexdigest()
        for field, keys in (('assessment', ('stance', 'independence')), ('relevance', ('state',))):
            annotation = row.get(field)
            if isinstance(annotation, dict):
                row[field] = {key: annotation[key] for key in keys if key in annotation}
        rows.append(row)
    metadata = dict(payload)
    metadata.pop('remaining_gap', None)
    if 'window' in metadata:
        metadata['requested_publication_window'] = metadata.pop('window')
    context = evidence_context(metadata, rows)
    context['captured_observations_at'] = sorted({row['observed_at'] for row in context['evidence']
                                                if isinstance(row.get('observed_at'), str)})
    if context_size(context) > 100000:
        raise ValueError('Report capture metadata exceeds the admitted context')
    return context


def context_limitations(context):
    coverage = context['evidence_coverage']
    limits = []
    if coverage['omitted']:
        limits.append(f"Análisis parcial: se revisaron {coverage['included']} de {coverage['total']} observaciones "
            'en esta respuesta. La selección incluye distintas valoraciones y no representa la frecuencia '
            'de los temas ni permite descartar contradicciones en lo que quedó fuera.')
    video_rows = [row for row in context['evidence'] if (row.get('provenance') or {}).get('content_basis') == 'title_and_description']
    limits.extend(page_content_limitations(context['evidence']))
    if any((row.get('provenance') or {}).get('text_truncated') is True for row in video_rows):
        limits.append('Algunas descripciones de video se analizaron parcialmente; no se revisaron los videos completos.')
    if any(row.get('published_at') is None for row in video_rows):
        limits.append('Algunas fechas de video no permiten confirmar su pertenencia al periodo exacto solicitado; se conservan como referencias de fecha incierta.')
    if any((row.get('provenance') or {}).get('reused_publication_metadata') is True for row in context['evidence']):
        limits.append('Se incluyen títulos y descripciones guardados en consultas de las últimas 24 horas, con su fecha de captura original. No se volvieron a verificar esos contenidos ni sus métricas; la selección no garantiza el resultado más reciente de toda la plataforma.')
    return limits


def page_content_limitations(evidence):
    limits = []
    search_rows = [row for row in evidence if (row.get('provenance') or {}).get('content_basis') == 'search_title_and_snippet']
    pages = [row for row in evidence if (row.get('provenance') or {}).get('content_basis') not in {'title_and_description', 'search_title_and_snippet'}]
    truncated = sum((row.get('provenance') or {}).get('text_truncated') is True for row in pages)
    if search_rows:
        limits.append('Se revisaron títulos y fragmentos de resultados de búsqueda; esos metadatos no equivalen a leer las páginas enlazadas.')
    if truncated:
        unit = 'página' if truncated == 1 else 'páginas'
        limits.append(f'Se analizaron fragmentos acotados de {truncated} {unit}; no se verificó todo su contenido.')
    return limits


def evidence_batches(payload, evidence):
    batches, batch = [], []
    def size(rows):
        return len(json.dumps({**payload, 'evidence': rows}, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    for observation in evidence:
        if batch and (len(batch) == 8 or size([*batch, observation]) > 90000):
            batches.append(batch)
            batch = []
        if size([observation]) > 90000:
            raise ValueError('One observation exceeds the admitted decision context')
        batch.append(observation)
    return [*batches, batch] if batch else batches or [[]]


class DecisionModel:
    def __init__(self, llm, *, prompts=None):
        self.llm = llm
        self.prompts = prompts if prompts is not None else json.loads(Path(__file__).with_name('prompts.json').read_text(encoding='utf-8'))

    async def ask(self, name, schema, payload):
        from langchain_core.messages import HumanMessage, SystemMessage
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(data.encode('utf-8')) > 100000:
            raise ValueError('Decision context exceeds the admitted size; no model called')
        return await self.llm.with_structured_output(schema, method='json_schema').ainvoke([
            SystemMessage(content=self.prompts[name]),
            HumanMessage(content=data)])
