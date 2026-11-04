"""Conversational approvals never take authority from model-generated booleans."""
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from .catalog import public_url, discovery_permitted, PUBLIC_SEARCH_PROFILES, requested_public_urls, SourceSelectionRequired
from .connectors import PROFILES
from .execution import ScheduleScopeError, validate_scheduled_research
from .contracts import ConfirmationDecision, FinancialMetrics, OpportunityDecision, Recurrence, ScheduleResearch, ResearchRequest, PendingClarification
from .repository import digest
from .research import PreparedClarificationIneligible


def research_request_key(flow, request_id, question):
    return flow.research.request_key(request_id, question)


def checked_research_request(flow, value, *, pending=False, check_prepared=True):
    model = PendingClarification if pending else ResearchRequest
    request = model.model_validate(value).model_dump(mode='json')
    expected = research_request_key(flow, request['request_id'], request['question'])
    answer_ids = [answer['request_id'] for answer in request['answers']]
    if (request['scope_key'] != expected or not request['question'].strip()
            or len(set(answer_ids)) != len(answer_ids) or request['request_id'] in answer_ids
            or any(not answer['text'].strip() for answer in request['answers'])):
        raise ValueError('Research request context does not match this authenticated scope')
    if pending and check_prepared and request.get('prepared_run_id'):
        flow.research.check_clarification(request['prepared_run_id'], request)
    return request


def effective_research_request(flow, state):
    value = state.get('effective_request')
    if value is None:
        value = {'version': 1, 'request_id': state['request_id'], 'question': state['question'], 'answers': [],
                 'scope_key': research_request_key(flow, state['request_id'], state['question'])}
    return checked_research_request(flow, value)


def effective_research_context(flow, state):
    if state.get('effective_request') is None:
        return {'question': state.get('question', '')}
    request = effective_research_request(flow, state)
    return {'question': request['question'],
            **({'clarifications': [answer['text'] for answer in request['answers']]} if request['answers'] else {})}


def clarification_reply(message, pending=None):
    return {'intake': {'intent': 'clarification_response', 'user_question': message},
            'pending_clarification': pending, 'effective_request': None, 'clarification_run_id': None}


def prior_report_context(flow, state):
    if not state.get('last_report_id'):
        return {'last_topic': None}
    records = flow.records()
    report = records.get('ai_mi_reports', state['last_report_id'])
    if report is None:
        return {'last_topic': None, 'last_report_id': None}
    run = records.get('ai_mi_research_runs', report['run_id'])
    topic = json.loads(records.get('ai_mi_topics', run['topic_id'])['canonical_scope_json'])
    topic.pop('version', None)
    return {'last_topic': topic}


def end_research_clarification(flow, state, pending, action, intake):
    flow.research.end_clarification(pending['prepared_run_id'], pending, action,
        now=datetime.fromisoformat(state['request_received_at']),
        continuation={'request_id': state['request_id'], 'question': state['question'], 'intake': intake})
    return prior_report_context(flow, state)


def restore_prepared_intake(flow, state, pending):
    run_id = pending['prepared_run_id']
    ended = flow.research.ended_clarification(run_id, pending, state['request_id'], state['question'])
    if ended:
        context = prior_report_context(flow, state)
        if ended['action'] == 'replace':
            return {'intake': ended['intake'], 'effective_request': None, 'pending_clarification': None,
                    'clarification_run_id': None, **context}
        message = ('He cancelado la investigación pendiente; no se inició ninguna búsqueda.' if ended['action'] == 'cancel'
                   else 'Todavía falta delimitar la investigación. Escribe la pregunta completa con los datos que hemos precisado.')
        return {**clarification_reply(message), **context}
    resumed = flow.research.resumed_clarification(run_id, pending, state['request_id'], state['question'])
    if resumed:
        return {'intake': resumed['intake'], 'effective_request': resumed['request'],
                'pending_clarification': None, 'clarification_run_id': run_id,
                'clarification_window_spec': pending.get('prepared_window_spec')}
    flow.research.check_clarification(run_id, pending)
    return None


def pending_research_intake(flow, state):
    if state.get('clarification_legacy'):
        return None, clarification_reply('La aclaración anterior no conserva un contexto verificable. Escribe la pregunta completa para continuar.')
    pending = state.get('pending_clarification')
    if pending is None:
        return None, None
    try:
        pending = checked_research_request(flow, pending, pending=True, check_prepared=False)
        if pending.get('prepared_run_id'):
            restored = restore_prepared_intake(flow, state, pending)
            if restored:
                return None, restored
    except ValueError:
        return None, clarification_reply('No conservo una solicitud pendiente verificable. Escribe la pregunta completa para continuar.')
    return pending, None


def continuation_scope_issue(intake, pending):
    window_spec = pending.get('prepared_window_spec')
    if window_spec is not None and intake.get('window_spec') not in (None, window_spec):
        return clarification_reply('Para cambiar el periodo de una investigación ya preparada, escribe una nueva pregunta completa con el periodo que quieres revisar. No he iniciado otra búsqueda.', pending)
    selected = pending.get('selected_source_key')
    if selected is not None and intake.get('selected_source_key') not in (None, selected):
        return clarification_reply('Para cambiar de fuente, escribe de nuevo la pregunta completa indicando la fuente que quieres usar. No he iniciado la búsqueda.', pending)
    context = pending.get('search_context')
    if context is not None and intake.get('search_context') not in (None, context):
        return clarification_reply('Para cambiar el país o idioma de búsqueda, escribe una nueva pregunta completa. No he iniciado otra búsqueda.', pending)
    return None


def continuation_intake(flow, state, intake, pending):
    issue = continuation_scope_issue(intake, pending)
    if issue is not None:
        return issue
    window_spec, context = pending.get('prepared_window_spec'), pending.get('search_context')
    answers = list(pending['answers'])
    previous = next((answer for answer in answers if answer['request_id'] == state['request_id']), None)
    if previous and previous['text'] != state['question']:
        raise ValueError('Clarification identity was reused for another answer')
    if not previous:
        answers.append({'request_id': state['request_id'], 'text': state['question']})
    if len(answers) > 3:
        context = {}
        if pending.get('prepared_run_id'):
            context = end_research_clarification(flow, state, pending, 'limit', intake)
        return {**clarification_reply('Para conservar el alcance con claridad, escribe ahora la pregunta completa con los datos que hemos precisado.'), **context}
    try:
        new_urls = requested_public_urls(state['question']) - requested_public_urls(pending['question'])
    except SourceSelectionRequired:
        new_urls = True
    if new_urls:
        return clarification_reply('Para cambiar las páginas de la investigación, escribe una nueva pregunta completa con sus URLs públicas. No he iniciado la búsqueda.', pending)
    request = {key: pending[key] for key in ResearchRequest.model_fields}
    request['answers'] = answers
    request = checked_research_request(flow, request)
    intake = dict(intake)
    if pending['topic'] is not None:
        intake['topic'] = pending['topic']
    if pending.get('selected_source_key') is not None:
        intake['selected_source_key'] = pending['selected_source_key']
    if context is not None:
        intake['search_context'] = context
    run_id = pending.get('prepared_run_id')
    if run_id:
        flow.research.resume_clarification(run_id, pending, request,
            now=datetime.fromisoformat(state['request_received_at']), intake=intake)
    return {'intake': intake, 'effective_request': request, 'pending_clarification': None,
            'clarification_run_id': run_id, 'clarification_window_spec': window_spec}


def finish_intake(flow, state, intake, pending):
    if pending is None or intake.get('clarification_action') == 'replace':
        context = {}
        if pending and pending.get('prepared_run_id'):
            context = end_research_clarification(flow, state, pending, 'replace', intake)
        return {'intake': intake, 'effective_request': None, 'pending_clarification': None,
                'clarification_run_id': None, **context}
    if intake.get('clarification_action') == 'cancel' or intake['intent'] == 'cancel':
        context = (end_research_clarification(flow, state, pending, 'cancel', intake)
                   if pending.get('prepared_run_id') else prior_report_context(flow, state))
        return {**clarification_reply('He cancelado la investigación pendiente; no se inició ninguna búsqueda.'), **context}
    if intake.get('clarification_action') != 'continue' or intake['intent'] != 'research':
        return clarification_reply('¿Seguimos precisando la investigación pendiente o prefieres una pregunta nueva? Si cambias de tema, escribe la consulta completa.', pending)
    return continuation_intake(flow, state, intake, pending)


def ask_research_clarification(flow, state, *, question=None, missing_fields=None):
    request = effective_research_request(flow, state)
    intake = state['intake']
    run_id = state.get('clarification_run_id') or state.get('run_id')
    question = question or intake['user_question'] or '¿Qué empresa, mercado y objetivo deseas investigar?'
    pending = PendingClarification.model_validate({**request, 'topic': intake['topic'],
        'missing_fields': intake['missing_fields'] if missing_fields is None else missing_fields,
        'user_question': question, 'selected_source_key': intake.get('selected_source_key'),
        'prepared_run_id': run_id or None,
        'search_context': intake.get('search_context'),
        'prepared_window_spec': (state.get('clarification_window_spec') or intake.get('window_spec')) if run_id else None}).model_dump(mode='json')
    if run_id:
        records = flow.records()
        if state.get('schedule_run_id') or any(row['run_id'] == run_id for row in records.rows('ai_mi_tool_runs')):
            return {'route': 'draft_report', 'reason': 'unresolved', 'connector_issue': question}
        try:
            flow.research.pause_clarification(run_id, pending, now=flow.clock())
        except PreparedClarificationIneligible:
            return {'route': 'deliver_and_read', 'response': {'markdown':
                'La investigación ya cambió de estado antes de guardar la aclaración. Retoma la solicitud original para revisar su estado; no he iniciado otra búsqueda.'}}
    if len(request['answers']) >= 3:
        if run_id:
            flow.research.end_clarification(run_id, pending, 'limit', now=flow.clock(),
                continuation={'request_id': state['request_id'], 'question': state['question'], 'intake': intake})
        return {**clarification_reply('Todavía falta delimitar la investigación. Escribe la pregunta completa con los datos que hemos precisado.'),
                'route': 'deliver_and_read', 'response': {'markdown': 'Todavía falta delimitar la investigación. Escribe la pregunta completa con los datos que hemos precisado.'},
                **prior_report_context(flow, state)}
    return {'route': 'deliver_and_read', 'pending_clarification': pending,
            'effective_request': request, 'response': {'markdown': question}}


def paused_research_reply(flow, run_id):
    pending = flow.research.paused_clarification(run_id)
    if pending is None:
        raise ValueError('Research no longer has a pending clarification')
    pending = checked_research_request(flow, pending, pending=True)
    return {'route': 'deliver_and_read', 'pending_clarification': pending,
            'effective_request': None, 'clarification_run_id': None,
            'response': {'markdown': pending['user_question']}}


def retrieval_page_context(row):
    from collections import Counter
    coverage = json.loads(row['provider_receipt_json'] or '{}').get('retrieval_coverage')
    if coverage is None:
        return None
    result = {'source_key': row.get('source_key'), 'total_rows': coverage['total_rows'],
        'accepted_rows': coverage['accepted_rows'],
        'failed_reasons': dict(Counter(page['reason'] for page in coverage['failed_pages']))}
    requested = json.loads(row.get('step_purpose_json') or '{}').get('requested_urls')
    if isinstance(requested, list) and requested:
        result.update(requested_pages=len(requested), unreturned_pages=max(0, len(requested) - coverage['total_rows']))
        if coverage['total_rows'] == 0:
            result['unreturned_urls'] = requested
    return result


def all_starts_rejected(records, run_id):
    attempts = [row for row in records.rows('ai_mi_tool_runs') if row['run_id'] == run_id]
    return bool(attempts) and all(row['state'] == 'failed' and not row['provider_run_id']
        and json.loads(row['provider_receipt_json'] or '{}').get('rejected_before_start') == 'build_not_found'
        for row in attempts)


def provider_limitations(records, run_id, connector_issue=None):
    failures = [connector_issue] if connector_issue else []
    received = accepted = failed = batches = unreturned = 0
    messages = {'failed': 'El proveedor falló', 'aborted': 'La extracción se interrumpió',
                'timed_out': 'La extracción agotó su tiempo', 'uncertain': 'El resultado de la extracción requiere conciliación'}
    for row in records.rows('ai_mi_tool_runs'):
        if row['run_id'] == run_id:
            coverage = json.loads(row['provider_receipt_json'] or '{}').get('retrieval_coverage')
            if coverage:
                batches += 1
                received += coverage['total_rows']
                accepted += coverage['accepted_rows']
                failed += len(coverage['failed_pages'])
                unreturned += retrieval_page_context(row).get('unreturned_pages', 0)
        if row['run_id'] == run_id and row['state'] in messages:
            source = records.get('ai_mi_sources', row['source_key'])
            name = (source or {}).get('name') or 'Fuente seleccionada'
            receipt = json.loads(row['provider_receipt_json'] or '{}')
            if row['state'] == 'failed' and receipt.get('rejected_before_start') == 'build_not_found':
                failures.append(name + ': No se pudo iniciar la búsqueda con la configuración disponible; hay que revisarla antes de volver a consultar esta fuente. No es una búsqueda sin resultados.')
            else:
                failures.append(name + ': ' + messages[row['state']] + '. Su cobertura no está verificada; no equivale a una búsqueda sin resultados.')
    if failed:
        failures.append(f"En {batches} consultas se recibieron {received} resultados de páginas: {accepted} aportaron contenido verificable y "
            f"{failed} no pudieron leerse con contenido utilizable. Los resultados pueden repetir una misma página. "
            'El contenido no recuperado no se usa como evidencia; no equivale a ausencia de menciones.')
    if unreturned:
        failures.append(f'La lectura no devolvió resultados para {unreturned} de las páginas solicitadas; los intentos pueden repetir una misma página. No pude comprobar su contenido ni la causa. '
            'Puedes compartir el texto fechado de esas publicaciones o pedirme otra búsqueda en medios y comunicados de la empresa, manteniendo o precisando el periodo.')
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


def restore_report_links(draft, model_evidence, evidence):
    validate_report_links(draft, model_evidence)
    originals = {row['evidence_id']: row['url'] for row in evidence}
    links = {row['url']: originals[row['evidence_id']] for row in model_evidence
             if row['evidence_id'] in draft['cited_evidence_ids']}

    def restore(value):
        if isinstance(value, str):
            for alias, original in links.items():
                value = value.replace(alias, original)
            if 'evidence.invalid' in value:
                raise ValueError('Draft contains an unresolved evidence reference')
            return value
        if isinstance(value, list):
            return [restore(item) for item in value]
        if isinstance(value, dict):
            return {key: restore(item) for key, item in value.items()}
        return value

    restored = restore(draft)
    validate_report_links(restored, evidence)
    return restored


def schedule_options(records, topic_id, base):
    targets, public = [], []
    for source in base:
        profile = PROFILES.get(source['profile_id'])
        if not profile or profile.get('unavailable_reason'):
            continue
        if source['profile_id'] not in PUBLIC_SEARCH_PROFILES:
            targets.extend(source['targets'])
            continue
        row = records.get('ai_mi_sources', source['source_key'])
        if discovery_permitted(json.loads(row['allowed_scope_json'] or '{}'), topic_id):
            public.append({'source_key': source['source_key'], 'name': source['name'],
                'profile_id': source['profile_id'], 'max_window_days': 7 if source['profile_id'] == 'youtube_search' else 31})
    return targets, public


def verified_schedule_summary(records, proposal_id, presented_hash):
    proposal = records.get('ai_mi_schedule_proposals', proposal_id)
    saved = json.loads(proposal['proposed_plan_json'])
    if proposal['state'] != 'awaiting_confirmation' or saved['presented_hash'] != presented_hash:
        raise ScheduleScopeError('La propuesta cambió; revisa un nuevo resumen antes de confirmarla.')
    plan = saved['plan']
    research = validate_scheduled_research(records, proposal['topic_id'], plan['research'])
    sources = []
    for target_id in research['target_ids']:
        target = records.get('ai_mi_source_targets', target_id)
        sources.append({'name': records.get('ai_mi_sources', target['source_key'])['name'],
                        'target': target['canonical_url_or_id']})
    sources.extend({'name': records.get('ai_mi_sources', key)['name'], 'target': 'Búsqueda pública por tema'}
                   for key in research.get('source_keys', []))
    topic = json.loads(records.get('ai_mi_topics', proposal['topic_id'])['canonical_scope_json'])
    return {'recurrence': plan['recurrence'], 'research': research, 'sources': sources, 'topic': topic}


def captured_excerpt(text, quotation):
    spaces = {0xA0: ' ', 0x202F: ' '}
    start = text.translate(spaces).find(quotation.translate(spaces))
    if not quotation or start < 0:
        raise ValueError('Factual feedback must cite the actual claim and captured text')
    return text[start:start + len(quotation)]


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
        records = flow.records()
        if all_starts_rejected(records, state['run_id']) and not flow.research.evidence(state['run_id'], records=records):
            return {'pending': None}
        proposal_id = flow.proposals.propose_schedule(state['topic_id'])
        if proposal_id:
            return {'pending': {'kind': 'schedule', 'id': proposal_id, 'hash': None},
                    'response': {**state['response'], 'suggestion': 'Este tema aparece en al menos tres preguntas independientes. ¿Quieres programar la investigación?'}}
        return await self.detect_source_opportunity(state)

    async def detect_source_opportunity(self, state):
        flow = self.flow
        records = flow.records()
        cutoff = flow.clock() - timedelta(days=30)
        topic_runs = [row for row in records.rows('ai_mi_research_runs') if row['topic_id'] == state['topic_id']
                      and datetime.fromisoformat(json.loads(row['started_finished_at_json'])['started_at']) >= cutoff]
        historical = []
        for run in sorted(topic_runs, key=lambda row: json.loads(row['started_finished_at_json'])['started_at']):
            historical.extend(flow.research.evidence(run['run_id'], records=records))
        historical = historical[-50:]
        from .model import evidence_context
        context = evidence_context({
            'topic_id': state['topic_id'], 'schedule_eligible': False,
            'candidate_connectors': [{'source_key': row['source_key'], 'name': row['name']}
                for row in records.rows('ai_mi_sources') if row['status'] == 'active' and row['connector_revision'] == 'website'],
            **effective_research_context(flow, state)}, historical)
        if historical and not context['evidence']:
            return {'pending': None}
        opportunity = await flow.ask('detect_opportunities', OpportunityDecision, context)
        if opportunity['kind'] != 'source':
            return {'pending': None}
        if not set(opportunity['evidence_ids']).issubset({row['evidence_id'] for row in context['evidence']}):
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
        if intent == 'list_sources':
            topic = state['intake']['topic'] or state.get('last_topic')
            if not topic or state['intake']['missing_fields']:
                return {'response': {'markdown': '¿Para qué empresa, mercado y tema quieres ver las fuentes registradas?'}}
            from .presentation import source_base_markdown
            topic_id = self.flow.journal.scope.key(digest(['topic', topic]))
            base = self.flow.catalog.approved_base(self.flow.records(), topic_id)
            return {'response': {'markdown': source_base_markdown(topic, base)}}
        if not pending:
            if intent == 'read_report':
                return self.read_report(state)
            if state.get('last_topic'):
                if intent == 'schedule_request':
                    return await self.request_schedule(state)
                if intent == 'source_request':
                    return self.request_website(state)
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

    async def request_schedule(self, state):
        flow = self.flow
        topic = flow.journal.scope.key(digest(['topic', state['last_topic']]))
        if flow.public_discovery:
            flow.catalog.ensure_public_x_search()
            flow.catalog.ensure_public_web_search()
            flow.catalog.ensure_public_youtube_search()
        proposal = flow.proposals.propose_schedule(topic, direct=True)
        if not proposal:
            return self.missing_proposal()
        return await self.complete_schedule({**state, 'pending': {'kind': 'schedule', 'id': proposal, 'hash': None}})

    def request_website(self, state):
        topic = self.flow.journal.scope.key(digest(['topic', state['last_topic']]))
        urls = {public_url(url.rstrip('.,;')) for url in re.findall(r'https://[^\s<>\]"\x27)]+', state['question'])}
        if not urls and re.search(r'\b(?:X|Twitter)\b', state['question'], re.I):
            return {'pending': {'kind': 'operator_x_search', 'topic_id': topic, 'hash': digest([topic, 'x_search'])},
                    'response': {'markdown': '¿Confirmas habilitar búsquedas de menciones públicas en X solo para este tema? '
                        'No se limitarán a una cuenta. Guardar esta fuente no inicia una búsqueda ni programa consultas.'}}
        if len(urls) != 1:
            return {'response': {'markdown': 'Indica una URL exacta para registrar. Si son varias páginas, dime cuál incorporamos primero; todavía no se guardó ninguna.'}}
        url = next(iter(urls))
        return {'pending': {'kind': 'operator_website', 'topic_id': topic, 'url': url, 'hash': digest([topic, url])},
                'response': {'markdown': '¿Confirmas registrar ' + url + ' para este tema? Se consultará solo esa página, sin seguir enlaces ni iniciar capturas al guardarla.'}}

    def apply_decision(self, state, accept):
        flow, pending = self.flow, state['pending']
        if pending['kind'] == 'operator_website':
            result = flow.catalog.register_operator_website(pending['topic_id'], pending['url'], state['request_id']) if accept else None
        elif pending['kind'] == 'operator_x_search':
            if pending['hash'] != digest([pending['topic_id'], 'x_search']):
                raise ValueError('Discovery approval differs from the presented topic')
            result = flow.catalog.register_operator_x_search(pending['topic_id'], state['request_id']) if accept else None
        elif pending['kind'] == 'schedule':
            try:
                result = flow.proposals.decide_schedule(pending['id'], pending['hash'], accept, state['request_id'])
            except ScheduleScopeError as error:
                return {'pending': {**pending, 'hash': None}, 'response': {'markdown': str(error)}}
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
        choices, public = schedule_options(records, proposal['topic_id'], flow.catalog.approved_base(records, proposal['topic_id']))
        decision = await flow.ask('handle_confirmation', ConfirmationDecision, {'question': state['question'],
            'pending': pending, 'saved_plan': saved, 'approved_targets': choices,
            'approved_public_sources': public, 'budget_microusd': flow.journal.budget})
        draft = {**saved.get('draft', {}), **{key: value for key, value in (decision.get('draft') or {}).items() if value is not None}}
        draft.update(decision['recurrence'] or {})
        draft.update(decision['research_plan'] or {})
        flow.proposals.save_schedule_draft(pending['id'], draft)
        required = {'weekdays', 'hour', 'minute', 'timezone', 'window_days', 'cap_microusd'}
        if not required.issubset(draft) or not (draft.get('target_ids') or draft.get('source_keys')):
            return {'pending': {**pending, 'hash': None}, 'response': {'markdown': decision['user_question'] or 'Faltan horario, zona, fuentes, ventana o presupuesto. Todavía no se ha programado.'}}
        recurrence = Recurrence.model_validate({key: draft[key] for key in ('weekdays', 'hour', 'minute', 'timezone')}).model_dump()
        research = ScheduleResearch.model_validate({key: draft[key] for key in ('window_days', 'target_ids', 'source_keys', 'cap_microusd') if key in draft}).model_dump()
        try:
            summary_hash = flow.proposals.present_schedule(pending['id'], recurrence, research)
            summary = verified_schedule_summary(flow.records(), pending['id'], summary_hash)
        except ScheduleScopeError as error:
            return {'pending': {**pending, 'hash': None}, 'response': {'markdown': str(error)}}
        return {'pending': {**pending, 'hash': summary_hash}, 'response': {
            'markdown': 'Confirma el horario, fuentes, ventana y presupuesto mostrados antes de activarlo. ¿Lo apruebas?', 'schedule_summary': summary}}


def grounded_factual_feedback(review, draft, evidence):
    observations = {row['evidence_id']: row for row in evidence}
    claims = '\n'.join([draft['answer_markdown'], *[row['claim'] for row in draft['findings']]])
    if any(issue['claim'] not in claims or issue['evidence_id'] not in observations
           for issue in review['issues']):
        raise ValueError('Factual feedback must cite the actual claim and captured text')
    issues = [{'claim': issue['claim'], 'evidence_id': issue['evidence_id'],
        'quoted_text': captured_excerpt(observations[issue['evidence_id']]['text'], issue['quoted_text'])}
        for issue in review['issues']]
    feedback = ('Corrige previous_draft con estas objeciones y la evidencia capturada; '
        'conserva el contenido restante respaldado, con sus cantidades y calificadores. '
        'El borrador previo no es evidencia. No investigues ni añadas hechos desde URLs o anotaciones: '
        + json.dumps(issues, ensure_ascii=False))
    return feedback, issues


def factual_repair_context(context, review_context, draft, issues):
    from .model import context_size, context_coverage
    context = {**context, 'previous_draft': draft}
    if context_size(context) <= 100000:
        return context
    required = {issue['evidence_id'] for issue in issues}
    context['evidence'] = [row for row in review_context['evidence'] if row['evidence_id'] in required]
    context['repair_evidence_coverage'] = context_coverage(review_context['evidence'], context['evidence'])
    context['repair_context_limited'] = True
    context['draft_validation'] = ('Esta vista de reparación incluye sólo las fuentes de las objeciones; '
        'conserva el resto respaldado del borrador. La revisión final usa toda la evidencia original. '
        + context['draft_validation'])
    if context_size(context) > 100000:
        raise ValueError('Required factual repair context exceeds the admitted size; no model called')
    return context
