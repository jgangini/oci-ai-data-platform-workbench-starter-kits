"""Conversational approvals never take authority from model-generated booleans."""
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from .catalog import public_url
from .contracts import ConfirmationDecision, FinancialMetrics, OpportunityDecision, Recurrence, ScheduleResearch
from .repository import digest


def provider_limitations(records, run_id, connector_issue=None):
    failures = [connector_issue] if connector_issue else []
    messages = {'failed': 'El proveedor falló', 'aborted': 'La extracción se interrumpió',
                'timed_out': 'La extracción agotó su tiempo', 'uncertain': 'El resultado de la extracción requiere conciliación'}
    for row in records.rows('ai_mi_tool_runs'):
        if row['run_id'] == run_id and row['state'] in messages:
            source = records.get('ai_mi_sources', row['source_key'])
            name = (source or {}).get('name') or 'Fuente seleccionada'
            failures.append(name + ': ' + messages[row['state']] + '. Su cobertura no está verificada; no equivale a una búsqueda sin resultados.')
    return list(dict.fromkeys(failures))


def validate_report_links(draft, evidence):
    selected = set(draft['cited_evidence_ids'])
    available = {item['evidence_id']: item['url'] for item in evidence}
    if not selected.issubset(available):
        raise ValueError('Draft cites evidence outside this research')
    allowed = {available[key] for key in selected}
    destinations = re.findall(r'\]\(([^)\n]*)\)', draft['answer_markdown'])
    if any(destination not in allowed for destination in destinations) or re.search(r'\]\[|(?m:^\s*\[[^\]]+\]:)', draft['answer_markdown']):
        raise ValueError('Only inline references to selected HTTPS evidence are allowed')
    links = re.findall(r'https?://[^\s<>\]"\x27)]+', draft['answer_markdown'])
    if any(link.rstrip('.,;') not in allowed for link in links):
        raise ValueError('Draft contains an unverified reference URL')


def chart_projection(records, draft, run_id):
    request = draft['chart_request']
    if not request:
        return None
    citations = set(draft['cited_evidence_ids'])
    evidence = [row for row in records.rows('ai_mi_run_evidence') if row['run_id'] == run_id and row['evidence_key'] in citations]
    if request['metric'] in {'close', 'value'}:
        return _financial_chart(records, request, evidence, citations)
    if request['metric'] != 'observations':
        return None
    groups = Counter()
    for row in evidence:
        document = records.get('ai_mi_documents', row['observation_key'])
        provenance = json.loads(document['provenance_json'])
        label = provenance.get('source') if request['group_by'] == 'source' else (document['published_at'] or '')[:10]
        if not label:
            return None
        groups[label] += 1
    if not groups:
        return None
    return {'version': 1, 'type': request['kind'], 'title': 'Observaciones recuperadas',
            'caption': 'Muestra recuperada; no representa cuota de mercado.', 'columns': [request['group_by'], 'observations'],
            'rows': [[name, count] for name, count in sorted(groups.items())],
            'scope': 'retrieved_observations_not_market_share', 'evidence_ids': sorted(citations)}


def _financial_chart(records, request, evidence, citations):
    if request['group_by'] != 'date' or not evidence:
        return None
    series, identity = {}, None
    for row in evidence:
        document = records.get('ai_mi_documents', row['observation_key'])
        metrics = json.loads(document['metrics_json'])
        validated = _chart_series(metrics, request['metric'])
        if validated is None:
            return None
        key, points = validated
        if identity not in (None, key):
            return None
        identity = key
        for point in points:
            period, value = point['period'], point['value']
            if period in series and series[period] != value:
                return None
            series[period] = value
    if len({len(period) for period in series}) > 1:
        return None
    if not series or len(series) > 50 or not any(value is not None for value in series.values()):
        return None
    if request['kind'] == 'line' and len(series) < 2:
        return None
    title = identity[0] + (' · Precio de cierre' if request['metric'] == 'close' else ' · Indicador anual')
    return {'version': 1, 'type': request['kind'], 'title': title,
            'caption': 'Serie recuperada; periodo del dato, no fecha de publicación. Unidad: ' + identity[2],
            'columns': ['period', request['metric']], 'rows': [[period, value] for period, value in sorted(series.items())],
            'scope': 'retrieved_financial_series', 'unit': identity[2], 'currency': identity[3],
            'evidence_ids': sorted(citations)}


def _chart_series(metrics, metric):
    """Validate persisted numeric input independently of chart presentation."""
    try:
        metrics = FinancialMetrics.model_validate(metrics).model_dump()
    except ValueError:
        return None
    if metrics['metric'] != metric or metrics['unit'].strip().lower() in ('', 'unknown'):
        return None
    key = tuple(metrics.get(name) for name in ('entity', 'indicator', 'unit', 'currency'))
    if metric == 'close' and not key[3]:
        return None
    return key, metrics['points']


def explicit_decision(text):
    text = ''.join(c for c in unicodedata.normalize('NFKD', text.strip().lower()) if not unicodedata.combining(c))
    return {'si': True, 'aprobar': True, 'confirmo': True, 'no': False, 'rechazar': False,
            'cancelar': False, 'cancela': False}.get(text.rstrip('.!'))


class ConversationActions:
    def __init__(self, workflow):
        self.flow = workflow

    async def detect_opportunities(self, state):
        flow = self.flow
        proposal_id = flow.proposals.propose_schedule(state['topic_id'])
        if proposal_id:
            return {'pending': {'kind': 'schedule', 'id': proposal_id, 'hash': None},
                    'response': {**state['response'], 'suggestion': 'Este tema aparece en al menos tres preguntas independientes. ¿Quieres programar la investigación?'}}
        records = flow.records()
        cutoff = flow.clock() - timedelta(days=30)
        topic_runs = [row for row in records.rows('ai_mi_research_runs') if row['topic_id'] == state['topic_id']
                      and datetime.fromisoformat(json.loads(row['started_finished_at_json'])['started_at']) >= cutoff]
        historical = []
        for run in sorted(topic_runs, key=lambda row: json.loads(row['started_finished_at_json'])['started_at']):
            historical.extend(flow.research.evidence(run['run_id'], records=records))
        historical = historical[-50:]
        opportunity = await flow.ask('detect_opportunities', OpportunityDecision, {
            'topic_id': state['topic_id'], 'schedule_eligible': False,
            'evidence': historical, 'question': state['question']})
        if opportunity['kind'] != 'source':
            return {'pending': None}
        if not set(opportunity['evidence_ids']).issubset({row['evidence_id'] for row in historical}):
            raise ValueError('Source suggestion cites evidence not provided to the decision')
        linked = [row for row in records.rows('ai_mi_run_evidence') if row['evidence_key'] in opportunity['evidence_ids']]
        runs = {row['run_id'] for row in linked if json.loads(row['assessment_json'] or '{}').get('independence') == 'known'}
        observations = {row['observation_key'] for row in linked}
        if len(runs) < 3 or len(observations) < 3:
            return {'pending': None}
        candidate_host = urlsplit(public_url(opportunity['target_url'])).hostname
        if any(urlsplit(records.get('ai_mi_documents', row['observation_key'])['source_url']).hostname != candidate_host for row in linked):
            raise ValueError('Recurring source must be grounded in the observed destinations')
        candidate_id = flow.catalog.propose_target(state['topic_id'], opportunity['source_key'], opportunity['target_url'], opportunity['reason'], opportunity['evidence_ids'])
        candidate = flow.records().get('ai_mi_source_candidates', candidate_id)
        if candidate['state'] != 'pending':
            return {'pending': None}
        target = json.loads(candidate['proposed_target_json'])
        return {'pending': {'kind': 'source', 'id': candidate_id, 'hash': digest(target)},
                'response': {**state['response'], 'suggestion': '¿Agregamos a la base ' + target['url'] + '? ' + target['description']}}

    async def handle_confirmation(self, state):
        pending = state.get('pending')
        intent = state['intake']['intent']
        if not pending:
            if intent == 'read_report':
                return self.read_report(state)
            requests = {'schedule_request': self.request_schedule, 'source_request': self.request_website}
            handler = requests.get(intent)
            if handler and state.get('last_topic'):
                return handler(state)
            return self.missing_proposal()
        accept = explicit_decision(state['question'])
        if accept is not None and (pending['hash'] or not accept):
            return self.apply_decision(state, accept)
        if pending['kind'] != 'schedule':
            return {'response': {'markdown': 'La fuente propuesta sigue pendiente. Indica “sí” o “no”; los cambios requieren una nueva propuesta.'}}
        return await self.complete_schedule(state)

    @staticmethod
    def missing_proposal():
        return {'response': {'markdown': 'Necesito una propuesta concreta del tema y sus fuentes antes de confirmar ese cambio.'}}

    def read_report(self, state):
        flow = self.flow
        report = flow.records().get('ai_mi_reports', state.get('last_report_id'))
        return {'response': {**flow.report_response(report), 'reused': True} if report else {
            'markdown': 'Selecciona el informe o aviso que deseas leer; leer no inicia una extracción.'}}

    def request_schedule(self, state):
        flow = self.flow
        topic = flow.journal.scope.key(digest(['topic', state['last_topic']]))
        proposal = flow.proposals.propose_schedule(topic, direct=True)
        if not proposal:
            return self.missing_proposal()
        return {'pending': {'kind': 'schedule', 'id': proposal, 'hash': None},
                'response': {'markdown': '¿Qué días, hora y zona horaria? Indica también fuentes, ventana y presupuesto por ejecución.'}}

    def request_website(self, state):
        topic = self.flow.journal.scope.key(digest(['topic', state['last_topic']]))
        urls = {public_url(url.rstrip('.,;')) for url in re.findall(r'https://[^\s<>\]"\x27)]+', state['question'])}
        if len(urls) != 1:
            return {'response': {'markdown': 'Indica una URL exacta para registrar. Si son varias páginas, dime cuál incorporamos primero; todavía no se guardó ninguna.'}}
        url = next(iter(urls))
        return {'pending': {'kind': 'operator_website', 'topic_id': topic, 'url': url, 'hash': digest([topic, url])},
                'response': {'markdown': '¿Confirmas registrar ' + url + ' para este tema? Se consultará solo esa página, sin seguir enlaces ni iniciar capturas al guardarla.'}}

    def apply_decision(self, state, accept):
        flow, pending = self.flow, state['pending']
        if pending['kind'] == 'operator_website':
            result = flow.catalog.register_operator_website(pending['topic_id'], pending['url'], state['request_id']) if accept else None
        elif pending['kind'] == 'schedule':
            result = flow.proposals.decide_schedule(pending['id'], pending['hash'], accept, state['request_id'])
        elif pending['kind'] == 'source':
            result = flow.catalog.decide_target(pending['id'], pending['hash'], accept, state['request_id'])
        else:
            raise ValueError('Unknown pending proposal kind')
        return {'pending': None, 'response': {'markdown': 'Cambio aprobado y guardado.' if accept else 'Propuesta rechazada; no se activó.', 'record_id': result}}

    async def complete_schedule(self, state):
        flow, pending = self.flow, state['pending']
        records = flow.records()
        proposal = records.get('ai_mi_schedule_proposals', pending['id'])
        saved = json.loads(proposal['proposed_plan_json'])
        targets = {row['target_id']: row for row in records.rows('ai_mi_source_targets') if row['status'] == 'active'}
        choices = [targets[row['target_id']] for row in records.rows('ai_mi_topic_sources') if row['topic_id'] == proposal['topic_id'] and row['status'] == 'active' and row['target_id'] in targets]
        decision = await flow.ask('handle_confirmation', ConfirmationDecision, {'question': state['question'],
            'pending': pending, 'saved_plan': saved, 'approved_targets': choices, 'budget_microusd': flow.journal.budget})
        draft = {**saved.get('draft', {}), **{key: value for key, value in (decision.get('draft') or {}).items() if value is not None}}
        draft.update(decision['recurrence'] or {})
        draft.update(decision['research_plan'] or {})
        flow.proposals.save_schedule_draft(pending['id'], draft)
        required = {'weekdays', 'hour', 'minute', 'timezone', 'window_days', 'target_ids', 'cap_microusd'}
        if not required.issubset(draft):
            return {'response': {'markdown': decision['user_question'] or 'Faltan horario, zona, fuentes, ventana o presupuesto. Todavía no se ha programado.'}}
        recurrence = Recurrence.model_validate({key: draft[key] for key in ('weekdays', 'hour', 'minute', 'timezone')}).model_dump()
        research = ScheduleResearch.model_validate({key: draft[key] for key in ('window_days', 'target_ids', 'cap_microusd')}).model_dump()
        summary_hash = flow.proposals.present_schedule(pending['id'], recurrence, research)
        summary = {'recurrence': recurrence, 'research': research, 'sources': [
            {'name': records.get('ai_mi_sources', targets[target_id]['source_key'])['name'],
             'target': targets[target_id]['canonical_url_or_id']}
            for target_id in research['target_ids']]}
        return {'pending': {**pending, 'hash': summary_hash}, 'response': {
            'markdown': 'Confirma el horario, fuentes, ventana y presupuesto mostrados antes de activarlo. ¿Lo apruebas?', 'schedule_summary': summary}}
