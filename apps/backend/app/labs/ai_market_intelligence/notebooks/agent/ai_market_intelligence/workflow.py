"""The research graph: model proposals, deterministic admission and durable effects."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from time import monotonic
from typing import TypedDict

from .catalog import Catalog, public_url, requested_public_urls, SourceSelectionRequired
from .connectors import ConnectorUnavailable, PROFILES, compile_call, resolve_window
from .contracts import IntakeDecision, ResearchPlan, EvidenceAssessment, ReportDraft, FactualReview
from .execution import AdmissionBlocked, ProviderPending, ProviderReceiptUnverified, ProviderStartRejected, PaidOperations, ScheduleScopeError, SourceScopeError, TargetAlreadyInvestigated, execute_selected, resolve_source_scope, validate_scheduled_research, discovered_read_options
from .proposals import Proposals
from .repository import Records, digest
from .research import Research, validate_findings, run_elapsed_seconds, run_is_paused, PreparedClarificationPending, research_search_context
from .provider import ObservationResult
from .model import evidence_batches, evidence_context, report_context, context_limitations
from .media import model_evidence
from .workflow_actions import effective_research_request, effective_research_context, finish_intake, ask_research_clarification, paused_research_reply, pending_research_intake, provider_limitations, all_starts_rejected


class TurnState(TypedDict, total=False):
    request_id: str
    question: str
    derived_read: bool
    intake: dict
    topic_id: str
    question_id: str
    run_id: str
    window: dict
    catalog: list
    plan: dict
    tool_id: str
    call: dict
    provider: dict
    evidence_refs: list[str]
    evidence: list
    assessment: dict
    draft: dict
    chart: dict | None
    reason: str
    provisional_assessment_close: bool
    route: str
    response: dict
    delivery_verified_for: str | None
    last_report_id: str | None
    last_topic: dict | None
    pending: dict | None
    schedule_run_id: str | None
    plan_sources: list
    connector_issue: str
    draft_coverage: dict
    pending_clarification: dict | None
    effective_request: dict | None
    clarification_legacy: bool
    clarification_run_id: str | None
    request_received_at: str
    clarification_window_spec: str | None


class MarketWorkflow:
    def __init__(self, journal, model, provider, *, timezone_name='America/Bogota',
                 run_cap_microusd=100000, step_cap_microusd=50000, max_steps=4,
                 usage_cost_verified=False, public_discovery=False, project=None, clock=None, runtime_binding=None):
        if not 0 <= step_cap_microusd <= run_cap_microusd <= journal.budget:
            raise ValueError('Research limits must fit the bound budget')
        if type(max_steps) is not int or not 1 <= max_steps <= 10:
            raise ValueError('Invalid research step limit')
        self.journal, self.model, self.provider = journal, model, provider
        self.research, self.paid = Research(journal, runtime_binding=runtime_binding), PaidOperations(journal)
        self.catalog, self.proposals = Catalog(journal), Proposals(journal, runtime_binding=runtime_binding)
        self.timezone, self.cap, self.step_cap = timezone_name, run_cap_microusd, step_cap_microusd
        self.max_steps, self.usage_verified = max_steps, usage_cost_verified
        if type(public_discovery) is not bool:
            raise ValueError('Public discovery must be configured by the runtime')
        self.public_discovery = public_discovery
        self.project = project
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def records(self):
        value, _ = self.journal.read()
        return Records(self.journal.scope, value['tables'])

    async def ask(self, name, schema, payload, *, answer_limit=None):
        contract = schema.model_json_schema()
        if answer_limit is not None:
            if schema is not ReportDraft or not 1 <= answer_limit <= 20000:
                raise ValueError('Invalid report body allowance')
            contract['properties']['answer_markdown']['maxLength'] = answer_limit
        result = schema.model_validate(await self.model.ask(name, contract, model_evidence(payload))).model_dump(mode='json')
        if answer_limit is not None and len(result['answer_markdown']) > answer_limit:
            raise ValueError('Report body exceeds its allowance for mandatory notices')
        return result

    def build(self, checkpointer=None):
        import asyncio
        from inspect import isawaitable, iscoroutinefunction
        from langgraph.graph import StateGraph, START, END
        from .workflow_actions import ConversationActions
        actions = ConversationActions(self)
        graph = StateGraph(TurnState)
        nodes = {'bind_request': self.bind_request, 'understand': self.understand,
                 'resolve_context': self.resolve_context, 'prepare_run': self.prepare_run,
                 'plan_research': self.plan_research, 'admit_step': self.admit_step,
                 'execute_step': self.execute_step, 'reconcile_step': self.reconcile_step,
                 'normalize_evidence': self.normalize_evidence, 'assess_evidence': self.assess_evidence,
                 'decide_next': self.decide_next, 'draft_report': self.draft_report,
                 'project_chart': self.project_chart, 'publish_result': self.publish_result,
                 'detect_opportunities': actions.detect_opportunities,
                 'handle_confirmation': actions.handle_confirmation, 'deliver_and_read': self.deliver}
        async def synchronous_updates(handler, state):
            pending = asyncio.gather(asyncio.to_thread(handler, state), return_exceptions=True)
            cancelled = False
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError()
            result = pending.result()[0]
            if isinstance(result, BaseException):
                raise result
            return result

        revision_checked = set()
        def checkpoint_references_only(handler):
            async def invoke(state):
                run_id = state.get('run_id') or state.get('schedule_run_id') or state.get('clarification_run_id')
                if handler != self.bind_request and run_id and run_id not in revision_checked:
                    await asyncio.to_thread(self.research.check_runtime_revision, run_id, continuing=handler != self.deliver)
                    revision_checked.add(run_id)
                updates = handler(state) if iscoroutinefunction(handler) else await synchronous_updates(handler, state)
                if isawaitable(updates):
                    updates = await updates
                return {**updates, 'evidence': []}
            return invoke
        for name, node in nodes.items():
            graph.add_node(name, checkpoint_references_only(node))
        graph.add_edge(START, 'bind_request')
        graph.add_edge('bind_request', 'understand')
        graph.add_edge('understand', 'resolve_context')
        routes = {
            'resolve_context': ['prepare_run', 'handle_confirmation', 'draft_report', 'deliver_and_read'],
            'plan_research': ['admit_step', 'draft_report', 'deliver_and_read'],
            'admit_step': ['execute_step', 'draft_report', 'deliver_and_read'],
            'execute_step': ['reconcile_step', 'normalize_evidence', 'draft_report', 'plan_research'],
            'reconcile_step': ['normalize_evidence', 'draft_report', 'deliver_and_read', 'plan_research'],
            'decide_next': ['plan_research', 'draft_report'],
        }
        for name, destinations in routes.items():
            graph.add_conditional_edges(name, lambda state: state['route'], destinations)
        for first, second in [('prepare_run', 'plan_research'), ('normalize_evidence', 'assess_evidence'),
                              ('assess_evidence', 'decide_next'), ('draft_report', 'project_chart'),
                              ('project_chart', 'publish_result'), ('publish_result', 'detect_opportunities'),
                              ('detect_opportunities', 'deliver_and_read'), ('handle_confirmation', 'deliver_and_read')]:
            graph.add_edge(first, second)
        graph.add_edge('deliver_and_read', END)
        return graph.compile(checkpointer=checkpointer)

    def bind_request(self, state):
        request_id, question = state.get('request_id'), state.get('question')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError('An authenticated stable request ID is required')
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 500:
            raise ValueError('Question must contain 1–500 characters')
        prior = state.get('intake') or {}
        legacy = bool(not state.get('pending') and state.get('pending_clarification') is None
                      and prior.get('intent') == 'research' and (prior.get('missing_fields') or not prior.get('topic')))
        return {'intake': {}, 'catalog': [], 'plan': {}, 'provider': {}, 'evidence_refs': [], 'evidence': [],
                'assessment': {}, 'draft': {}, 'chart': None, 'reason': '', 'provisional_assessment_close': False,
                'response': {}, 'delivery_verified_for': None,
                'run_id': '', 'question_id': '', 'topic_id': '', 'tool_id': '', 'call': {}, 'connector_issue': '', 'draft_coverage': {}, 'derived_read': False,
                'effective_request': None, 'clarification_legacy': legacy,
                'clarification_run_id': None, 'request_received_at': self.clock().isoformat(),
                'clarification_window_spec': None}

    async def understand(self, state):
        if state.get('schedule_run_id'):
            records = self.records()
            run = records.get('ai_mi_research_runs', state['schedule_run_id'])
            schedule = records.get('ai_mi_schedules', run['schedule_id']) if run else None
            if not schedule or schedule['status'] != 'active':
                raise ValueError('The scheduled run is not approved and active')
            if json.loads(run['brief_json'])['schedule_revision'] != schedule['revision']:
                raise ValueError('The approved schedule changed after admission')
            topic = json.loads(records.get('ai_mi_topics', run['topic_id'])['canonical_scope_json'])
            topic.pop('version', None)
            return {'intake': {'intent': 'scheduled', 'topic': topic}, 'pending_clarification': None, 'effective_request': None}
        if state.get('pending'):
            return {'intake': {'intent': 'schedule_decision', 'topic': state.get('last_topic')},
                    'pending_clarification': None, 'effective_request': None}
        pending, restored = pending_research_intake(self, state)
        if restored:
            return restored
        records = self.records()
        sources = self.catalog.visible_sources(records)
        targets = {row['target_id'] for row in records.rows('ai_mi_source_targets')
                   if row['status'] == 'active' and row['source_key'] in {source['source_key'] for source in sources}}
        topics = {row['topic_id'] for row in records.rows('ai_mi_topic_sources')
                  if row['status'] == 'active' and row['target_id'] in targets}
        recent = sorted(records.rows('ai_mi_topics'), key=lambda row: row['created_at'] or '', reverse=True)
        registered = [row for row in recent if row['topic_id'] in topics][:20]
        researched = {row['topic_id'] for row in records.rows('ai_mi_questions') if row['question_kind'] == 'conversation_research'}
        history = [row for row in recent if row['topic_id'] in researched - topics
                   and self.catalog.approved_base(records, row['topic_id'])][:20 - len(registered)]
        payload = {
            'question': state['question'], 'last_topic': state.get('last_topic'),
            'has_previous_report': bool(state.get('last_report_id')),
            'sources': [{'source_key': row['source_key'], 'name': row['name']} for row in sources]
                       + (self.catalog.public_search_options(records) if self.public_discovery else []),
            'registered_topics': [json.loads(row['canonical_scope_json']) for row in registered],
            'recent_research_topics': [json.loads(row['canonical_scope_json']) for row in history],
            'now': self.clock().isoformat(), 'timezone': self.timezone}
        if pending is not None:
            payload['pending_clarification'] = {'question': pending['question'], 'topic': pending['topic'],
                'answers': [answer['text'] for answer in pending['answers']],
                'missing_fields': pending['missing_fields'], 'user_question': pending['user_question'],
                'search_context': pending.get('search_context')}
        result = await self.ask('understand', IntakeDecision, payload)
        return finish_intake(self, state, result, pending)

    async def explain_report(self, state):
        report = self.records().get('ai_mi_reports', state.get('last_report_id'))
        if not report:
            return {'markdown': 'No hay un informe anterior disponible en esta conversación.'}
        evidence = self.research.evidence(report['run_id'])
        draft, _ = await self.write_report({'followup': state['question'],
            'published_report': report['markdown'], 'published_at': report['published_at'],
            'original_coverage': json.loads(report['coverage_json']),
            'window': json.loads(report['coverage_json']).get('window'),
            'comparison_context': json.loads(self.records().get('ai_mi_research_runs', report['run_id'])['comparison_context_json'] or 'null')}, evidence)
        from .workflow_actions import chart_projection
        chart = chart_projection(self.records(), draft, report['run_id'])
        return {**self.report_response(report, citations=draft['cited_evidence_ids']),
                'markdown': draft['answer_markdown'], 'chart': chart, 'reused': True}

    async def resolve_context(self, state):
        intent = state['intake']['intent']
        if intent == 'clarification_response':
            return {'route': 'deliver_and_read', 'response': {'markdown': state['intake']['user_question']}}
        if state.get('pending') or intent in {'source_request', 'source_decision', 'list_sources', 'schedule_request', 'schedule_decision', 'cancel', 'read_report'}:
            topic = state['intake'].get('topic')
            if topic and not state.get('pending'):
                self.research.record_question(state['request_id'], state['question'], topic, 'administration')
            return {'route': 'handle_confirmation', 'last_topic': topic or state.get('last_topic')}
        intake = state['intake']
        if intake.get('missing_fields') or not intake['topic']:
            if intent == 'research':
                return ask_research_clarification(self, state)
            return {'route': 'deliver_and_read', 'response': {'markdown': intake['user_question'] or '¿Qué empresa, mercado y objetivo deseas investigar?'}}
        question_id, topic_id, run = self.research_turn(state)
        if run and run['state'] == 'closed':
            report = next(row for row in self.records().rows('ai_mi_reports') if row['run_id'] == run['run_id'])
            return {'question_id': question_id, 'topic_id': topic_id, 'run_id': run['run_id'],
                    'last_report_id': report['report_id'], 'last_topic': intake['topic'],
                    'route': 'deliver_and_read', 'response': self.report_response(report)}
        if intent == 'followup':
            return {'question_id': question_id, 'topic_id': topic_id, 'route': 'deliver_and_read',
                    'response': await self.explain_report(state)}
        return self.resolve_research_base(state, question_id, topic_id, run)

    def resolve_research_base(self, state, question_id, topic_id, run):
        intake = state['intake']
        intent = intake['intent']
        catalog, issue = self.research_sources(state, topic_id, run if intent == 'scheduled' else None)
        if issue and run:
            return {'question_id': question_id, 'topic_id': topic_id, 'run_id': run['run_id'],
                    **self.evidence_references(self.research.evidence(run['run_id'])),
                    'window': {key: json.loads(run['plan_window_json'])[key] for key in ('start', 'end')},
                    'route': 'draft_report', 'reason': 'unresolved', 'connector_issue': issue}
        if issue:
            if intent == 'research' and not re.search(r'https?://', effective_research_context(self, state)['question'], re.I):
                return {**ask_research_clarification(self, {**state, 'question_id': question_id, 'topic_id': topic_id,
                    'intake': {**intake, 'selected_source_key': None}}, question=issue, missing_fields=['fuente']),
                    'last_topic': intake['topic']}
            return {'question_id': question_id, 'topic_id': topic_id, 'last_topic': intake['topic'],
                    'route': 'deliver_and_read', 'response': {'markdown': issue}}
        return {'question_id': question_id, 'topic_id': topic_id, 'last_topic': intake['topic'],
                'window': {key: json.loads(run['plan_window_json'])[key] for key in ('start', 'end')} if run else resolve_window(intake['window_spec'], self.clock(), self.timezone),
                'catalog': catalog, 'route': 'prepare_run'}

    def research_turn(self, state):
        intent = state['intake']['intent']
        if intent == 'scheduled':
            run = self.records().get('ai_mi_research_runs', state['schedule_run_id'])
            return None, run['topic_id'], run
        if state.get('clarification_run_id'):
            run = self.records().get('ai_mi_research_runs', state['clarification_run_id'])
            if not run or run['state'] not in {'running', 'closed'} or run_is_paused(run):
                raise ValueError('Prepared research is not ready to continue')
            return run['question_id'], run['topic_id'], run
        kind = 'followup' if intent == 'followup' else 'conversation_research'
        request = effective_research_request(self, state) if intent == 'research' else state
        question_id, topic_id = self.research.record_question(request['request_id'], request['question'], state['intake']['topic'], kind)
        return question_id, topic_id, None

    def research_sources(self, state, topic_id, run):
        question = effective_research_context(self, state)['question']
        try:
            urls = requested_public_urls(question)
            if urls and state['intake']['intent'] == 'research':
                return self.pointed_sources(state, topic_id, urls), None
            if state['intake']['intent'] == 'research' and self.public_discovery:
                self.catalog.ensure_public_x_search()
                self.catalog.ensure_public_web_search()
                self.catalog.ensure_public_youtube_search()
            catalog = self.catalog_for_question(state, topic_id, run)
        except ScheduleScopeError as error:
            return [], str(error)
        except SourceSelectionRequired:
            if re.search(r'https?://', question, re.I):
                return [], 'No pude preparar la lectura puntual con un lector activo. Revisa la fuente elegida y usa páginas HTTPS públicas, sin credenciales ni destinos privados; no inicié otra búsqueda.'
            return [], 'No identifiqué esa fuente con certeza. Indica su nombre registrado o su URL exacta para continuar.'
        if not catalog and state['intake']['intent'] != 'scheduled':
            return [], ('Todavía no tienes fuentes habilitadas para investigar este tema. '
                'Puedes indicarme una página web o pedirme habilitar búsquedas públicas en X para este tema. '
                'No he realizado búsquedas. ¿Qué fuente deseas usar?')
        return catalog, None

    def pointed_sources(self, state, topic_id, urls):
        try:
            return self.catalog_for_question(state, topic_id, None)
        except SourceSelectionRequired:
            selected = state['intake'].get('selected_source_key')
            managed = self.journal.scope.key(digest(['source', 'website']))
            if self.public_discovery and selected in (None, managed):
                self.catalog.ensure_website_reader()
            return self.catalog.pointed_base(self.records(), urls, selected)

    def catalog_for_question(self, state, topic_id, scheduled_run):
        """Keep conversational source selection separate from catalog row eligibility."""
        records = self.records()
        permitted, sources = None, None
        if scheduled_run:
            schedule = records.get('ai_mi_schedules', scheduled_run['schedule_id'])
            plan = validate_scheduled_research(records, topic_id, json.loads(schedule['approved_plan_budget_json']))
            permitted, sources = set(plan['target_ids']), set(plan.get('source_keys', []))
        catalog = self.catalog.approved_base(records, topic_id, permitted, sources)
        context = effective_research_context(self, state)
        return self.catalog.select_for_question(records, topic_id, catalog,
            state['intake'].get('selected_source_key'), context['question'],
            source_question='\n'.join([context['question'], *context.get('clarifications', [])]))

    def prepare_run(self, state):
        run = state.get('schedule_run_id') or state.get('clarification_run_id') or self.research.prepare_run(state['question_id'], state['window'],
            effective_research_context(self, state)['question'], self.cap, max_steps=self.max_steps,
            clarifications=effective_research_request(self, state)['answers'], search_context=state['intake'].get('search_context'))
        records = self.records()
        prior = next((row for row in records.rows('ai_mi_reports') if row['run_id'] == run), None)
        window = json.loads(records.get('ai_mi_research_runs', run)['plan_window_json'])
        return {'run_id': run, **self.evidence_references(self.research.evidence(run)),
                'window': {key: window[key] for key in ('start', 'end')},
                'response': self.report_response(prior) if prior else {}}

    @staticmethod
    def evidence_references(evidence):
        return {'evidence_refs': [row['evidence_id'] for row in evidence], 'evidence': []}

    def run_evidence(self, state):
        records = self.records()
        run = records.get('ai_mi_research_runs', state.get('run_id'))
        if not run or any(key not in state or state[key] != run[key] for key in ('topic_id', 'question_id')):
            raise ValueError('Evidence requires this research run in the authenticated scope')
        evidence = self.research.evidence(run['run_id'], records=records)
        if 'evidence_refs' in state:
            refs = state['evidence_refs']
            if (not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)
                    or len(refs) != len(set(refs)) or set(refs) != {row['evidence_id'] for row in evidence}):
                raise ValueError('Checkpoint evidence references do not match this research')
        return evidence

    async def plan_research(self, state):
        if state['response']:
            return {'route': 'deliver_and_read'}
        if run_is_paused(self.records().get('ai_mi_research_runs', state['run_id'])):
            return paused_research_reply(self, state['run_id'])
        limit = self.close_limit(state['run_id'])
        if limit:
            return {'reason': limit, 'route': 'draft_report'}
        evidence = self.run_evidence(state)
        if not state['catalog']:
            return {'reason': 'no_data', 'route': 'draft_report'}
        pointed = next((row for row in state['catalog'] if row.get('pointed_urls')), None)
        if pointed:
            return self.plan_pointed_pages(state, pointed, evidence)
        try:
            available = self.available_research_sources(state)
        except SourceScopeError as error:
            return {'reason': 'unresolved', 'route': 'draft_report', 'connector_issue': error.user_message}
        if not available:
            return {'reason': 'unresolved' if evidence else 'no_data', 'route': 'draft_report'}
        context = evidence_context({
            **effective_research_context(self, state), 'topic': state['intake']['topic'], 'window': state['window'], 'catalog': available,
            'timezone': self.timezone,
            'search_context': research_search_context(self.records().get('ai_mi_research_runs', state['run_id'])),
            'assessment': {key: value for key, value in state['assessment'].items() if key != 'assessments'},
            'retrieval_coverage': self.retrieval_context(state['run_id']),
            'provider_limitations': provider_limitations(self.records(), state['run_id'])}, evidence)
        plan = await self.ask('plan_research', ResearchPlan, context)
        if plan['next_action'] == 'request_extension' and evidence:
            return {'plan': plan, 'route': 'draft_report', 'reason': 'unresolved'}
        if plan['next_action'] in {'clarify', 'request_extension'}:
            return {'plan': plan, **ask_research_clarification(self, state,
                question=plan['user_question'] or '¿Qué dato falta precisar para continuar?', missing_fields=['alcance'])}
        if plan['next_action'] != 'execute':
            return {'plan': plan, 'route': 'draft_report', 'reason':
                    'unresolved' if context['evidence_coverage']['omitted'] else 'sufficient' if evidence else 'no_data'}
        bound_plan = self.catalog.bind_plan_target(plan, available)
        if bound_plan is None:
            return {'plan': plan, **ask_research_clarification(self, state,
                question='¿Qué fuente registrada quieres usar para esta investigación?', missing_fields=['fuente'])}
        return {'plan': bound_plan, 'plan_sources': available, 'route': 'admit_step'}

    def available_research_sources(self, state):
        snapshot, _ = self.journal.read()
        records = Records(self.journal.scope, snapshot['tables'])
        rejected = {records.get('ai_mi_tool_runs', key)['source_key'] for key, operation in snapshot['operations'].items()
                    if operation['run_id'] == state['run_id'] and operation.get('start_rejection') == 'build_not_found'}
        visited = {row['target_id'] for row in records.rows('ai_mi_tool_runs') if row['run_id'] == state['run_id']}
        base = [{**row, 'targets': [target for target in row['targets'] if target['target_id'] not in visited]} for row in state['catalog']]
        base = [row for row in base if row['targets']]
        discovery = [row for row in state['catalog'] if not row['targets']]
        available = [*base, *discovery] if visited else base or discovery
        reader = discovered_read_options(records, state['topic_id'], state['run_id'])
        if reader:
            existing = next((row for row in available if row['source_key'] == reader['source_key']), None)
            if existing is None:
                available.append(reader)
            else:
                existing['discovered_urls'] = reader['discovered_urls']
        return [row for row in available if row['source_key'] not in rejected]

    def plan_pointed_pages(self, state, source, evidence):
        visited = any(row['run_id'] == state['run_id'] and row['source_key'] == source['source_key']
                      for row in self.records().rows('ai_mi_tool_runs'))
        if visited:
            return {'route': 'draft_report', 'reason': 'unresolved' if state['assessment'].get('gap') else 'sufficient' if evidence else 'no_data'}
        plan = {'next_action': 'execute', 'source_key': source['source_key'], 'target_id': None,
                'arguments': {'query': '', 'urls': source['pointed_urls'], 'max_items': len(source['pointed_urls']), 'sort': 'Latest'},
                'purpose': 'Leer las páginas indicadas en la solicitud',
                'expected_contribution': 'Responder con el contenido recuperado de esas páginas', 'user_question': None}
        return {'plan': plan, 'plan_sources': [source], 'route': 'admit_step'}

    def admit_step(self, state):
        plan, records = state['plan'], self.records()
        source = records.get('ai_mi_sources', plan['source_key'])
        framing_reply = self.accept_search_framing(state, source)
        if framing_reply is not None:
            return framing_reply
        records = self.records()
        requested_urls = ((plan['arguments'] or {}).get('urls')
                          if source and source['connector_revision'] == 'website' and plan['target_id'] is None else None)
        try:
            if state.get('schedule_run_id'):
                self.catalog_for_question(state, state['topic_id'], records.get('ai_mi_research_runs', state['schedule_run_id']))
            scope = resolve_source_scope(records, state['topic_id'], plan['source_key'], plan['target_id'], run_id=state.get('run_id'),
                                         requested_urls=requested_urls)
        except (ScheduleScopeError, SourceScopeError) as error:
            return {'reason': 'unresolved', 'route': 'draft_report', 'connector_issue': getattr(error, 'user_message', str(error))}
        self.require_plan_source(state)
        try:
            call = compile_call(scope['connector_revision'], plan['arguments'], state['window'], self.step_cap,
                                approved_urls=scope['urls'], approved_finance=scope['financial'], usage_cost_verified=self.usage_verified,
                                approved_search_context=scope.get('search_context'),
                                require_all_urls=bool(scope.get('pointed_urls')))
        except ConnectorUnavailable as error:
            return {'route': 'draft_report', 'reason': 'unresolved', 'connector_issue': str(error)}
        except ValueError:
            if scope['financial'] is None:
                raise
            return ask_research_clarification(self, state,
                question='No pude validar la selección financiera y su periodo. Indica los destinos registrados y las fechas o años exactos que deseas comparar.',
                missing_fields=['selección financiera'])
        try:
            tool = self.paid.reserve(state['run_id'], plan['source_key'], plan['target_id'], call, self.step_cap,
                                     plan['purpose'], authorization_hash=scope['authorization_hash'])
        except PreparedClarificationPending:
            return paused_research_reply(self, state['run_id'])
        except (TargetAlreadyInvestigated, ScheduleScopeError, SourceScopeError) as error:
            return {'reason': 'unresolved', 'route': 'draft_report', 'connector_issue': getattr(error, 'user_message', str(error))}
        except AdmissionBlocked as error:
            return {'reason': error.reason, 'route': 'draft_report'}
        previous = self.records().get('ai_mi_tool_runs', tool)
        if previous['state'] in {'succeeded', 'failed', 'aborted', 'timed_out'}:
            return {'reason': 'unresolved', 'route': 'draft_report'}
        return {'tool_id': tool, 'call': call, 'derived_read': bool(scope.get('discovered_urls')), 'route': 'execute_step'}

    def accept_search_framing(self, state, source):
        if not source or source['connector_revision'] not in {'web_search_localized', 'web_search_serp'}:
            return None
        context = state['intake'].get('search_context') or research_search_context(self.records().get('ai_mi_research_runs', state['run_id']))
        if context is None:
            return ask_research_clarification(self, state,
                question='¿Qué país e idioma quieres priorizar para ordenar la búsqueda web?', missing_fields=['país e idioma de búsqueda'])
        try:
            self.research.bind_search_context(state['run_id'], context)
        except ValueError:
            return ask_research_clarification(self, state,
                question='No pude conservar el país e idioma de búsqueda originales. Escribe una nueva pregunta completa con ese encuadre.',
                missing_fields=['país e idioma de búsqueda'])
        return None

    @staticmethod
    def require_plan_source(state):
        plan = state['plan']
        choices = state.get('plan_sources', state.get('catalog', []))
        if choices and plan['source_key'] not in {row['source_key'] for row in choices}:
            raise ValueError('Registered research base must be consulted before discovery')
        choice = next((row for row in choices if row['source_key'] == plan['source_key']), None)
        derived = choice and choice.get('discovered_urls') and plan['target_id'] is None
        if choice and choice['targets'] and not derived and plan['target_id'] not in {row['target_id'] for row in choice['targets']}:
            raise ValueError('Select one of the remaining approved base targets')

    def scheduled_input(self, run_id):
        run = self.records().get('ai_mi_research_runs', run_id)
        if not run or not run['schedule_id']:
            raise ValueError('An owned scheduled run is required')
        return {'request_id': digest(['scheduled', run_id]), 'question': json.loads(run['brief_json'])['objective'],
                'schedule_run_id': run_id, 'pending': None}

    async def execute_step(self, state):
        try:
            result = await execute_selected(self.paid, state['tool_id'], state['call'], self.provider.start)
        except PreparedClarificationPending:
            return paused_research_reply(self, state['run_id'])
        except AdmissionBlocked as error:
            return {'reason': error.reason, 'route': 'draft_report'}
        except ProviderStartRejected:
            return {'reason': 'unresolved', 'route': 'plan_research', 'connector_issue': '', 'provider': {}}
        except (ConnectorUnavailable, TargetAlreadyInvestigated, ScheduleScopeError, SourceScopeError) as error:
            return {'reason': 'unresolved', 'route': 'draft_report', 'connector_issue': getattr(error, 'user_message', str(error))}
        except RuntimeError:
            return {'reason': 'uncertain', 'route': 'draft_report'}
        # Even immediate completion needs a later accounting receipt before content is consumed.
        return {'provider': result, 'route': 'reconcile_step'}

    async def reconcile_step(self, state):
        value, _ = self.journal.read()
        operation = value['operations'][state['tool_id']]
        if operation.get('start_rejection') == 'build_not_found' and operation['state'] == 'failed' and not operation.get('provider_run_id'):
            return {'reason': 'unresolved', 'route': 'plan_research', 'connector_issue': '', 'provider': {}}
        if not operation.get('provider_run_id'):
            return {'route': 'deliver_and_read', 'response': {'markdown': 'El inicio anterior requiere conciliación; no se iniciará otra captura.'}}
        wait = state.get('call', {}).get('callOptions', {}).get('timeout', 60)
        result = await self.poll_provider(operation['provider_run_id'], max_wait_seconds=wait)
        status = {'SUCCEEDED': 'succeeded', 'FAILED': 'failed', 'ABORTED': 'aborted', 'TIMED-OUT': 'timed_out', 'RUNNING': 'running', 'READY': 'running'}[result['status']]
        self.paid.record_provider(state['tool_id'], operation['permit'], operation['provider_run_id'], status,
                                  {key: result[key] for key in ('runId', 'status')})
        if status in {'succeeded', 'failed', 'aborted', 'timed_out'} and result.get('spent_microusd') is not None:
            self.paid.record_cost(state['tool_id'], result['runId'], result['spent_microusd'])
        if status == 'succeeded':
            if 'datasetId' in result and not result['datasetId']:
                raise ProviderReceiptUnverified('The completed capture has no verified dataset; resume this same operation')
            return {'provider': result, 'route': 'normalize_evidence'}
        if status == 'running':
            raise ProviderPending('The provider is still running; resume this exact request')
        return {'route': 'draft_report', 'reason': 'unresolved'}

    async def poll_provider(self, run_id, *, max_wait_seconds=60):
        import asyncio
        if type(max_wait_seconds) is not int or max_wait_seconds not in (60, 120):
            raise ValueError('Use the admitted provider timeout')
        deadline = monotonic() + max_wait_seconds
        attempts = max_wait_seconds // 2 + 1
        for attempt in range(attempts):
            if attempt and monotonic() >= deadline:
                break
            try:
                result = await self.provider.read(run_id)
            except ValueError:
                raise ProviderReceiptUnverified('The provider status could not be bound to this capture') from None
            if (not isinstance(result, dict) or result.get('runId') != run_id or not isinstance(result.get('status'), str)
                    or result.get('status') not in {'RUNNING', 'READY', 'SUCCEEDED', 'FAILED', 'ABORTED', 'TIMED-OUT'}):
                raise ProviderReceiptUnverified('The provider status could not be bound to this capture')
            if result['status'] not in {'RUNNING', 'READY'}:
                return result
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            if attempt < attempts - 1:
                await asyncio.sleep(min(2, remaining))
        return result

    async def normalize_evidence(self, state):
        try:
            if state.get('derived_read'):
                result = await self.provider.observations(state['provider'], state['call'], allow_partial_pages=True)
            else:
                result = await self.provider.observations(state['provider'], state['call'])
            if isinstance(result, ObservationResult):
                observations, cost, coverage = result.items, result.cost, result.coverage
            else:
                observations, cost = result
                coverage = None
        except ProviderPending:
            raise
        except (ValueError, RuntimeError):
            return {**self.evidence_references(self.research.evidence(state['run_id'])), 'connector_issue':
                'No pude verificar el contenido recibido para el alcance solicitado. Ese contenido no se utilizó como evidencia; se conservan los datos ya verificados.'}
        if cost is not None:
            self.paid.record_cost(state['tool_id'], state['provider']['runId'], cost)
        if coverage is not None:
            if coverage.get('accepted_rows') != len(observations):
                raise ValueError('Retrieval coverage does not match normalized observations')
            self.paid.record_retrieval(state['tool_id'], state['provider']['runId'], coverage)
        self.research.add_evidence(state['run_id'], state['tool_id'], observations)
        self.research.reuse_publication_metadata(state['run_id'],
            {row['source_key'] for row in state.get('catalog', [])},
            scope_resolver=resolve_source_scope, now=self.clock())
        return self.evidence_references(self.research.evidence(state['run_id']))

    def retrieval_context(self, run_id):
        from .workflow_actions import retrieval_page_context
        result = []
        for row in self.records().rows('ai_mi_tool_runs'):
            if row['run_id'] == run_id:
                coverage = retrieval_page_context(row)
                if coverage is not None:
                    result.append(coverage)
        return result

    def research_source_options(self, state):
        attempts = [row for row in self.records().rows('ai_mi_tool_runs') if row['run_id'] == state.get('run_id')]
        return [{**{key: source[key] for key in ('source_key', 'profile_id', 'name') if key in source},
                 'attempts': sum(row['source_key'] == source['source_key'] for row in attempts)}
                for source in state.get('catalog', [])]

    async def assess_batch(self, state, evidence, semaphore, retrieval_coverage, *, corpus_evidence_count=None,
                           source_options=None):
        async with semaphore:
            result = await self.ask('assess_evidence', EvidenceAssessment, {
                **effective_research_context(self, state), 'topic': state['intake']['topic'],
                'window': state['window'], 'evidence': evidence,
                'corpus_evidence_count': len(evidence) if corpus_evidence_count is None else corpus_evidence_count,
                'retrieval_coverage': retrieval_coverage,
                'research_source_options': self.research_source_options(state) if source_options is None else source_options})
        ids = [row['evidence_id'] for row in result['assessments']]
        if len(ids) != len(set(ids)) or set(ids) != {row['evidence_id'] for row in evidence}:
            raise ValueError('Assessment must cover exactly its evidence batch')
        return result

    async def assess_evidence(self, state):
        evidence = self.run_evidence(state)
        if state.get('connector_issue'):
            assessment = {'assessments': [], 'continue_research': False,
                          'reason': 'Salida del proveedor no validada', 'gap': state['connector_issue']}
        else:
            import asyncio
            semaphore = asyncio.Semaphore(2)
            retrieval_coverage = self.retrieval_context(state['run_id'])
            source_options = self.research_source_options(state)
            batches = evidence_batches({**effective_research_context(self, state), 'topic': state['intake']['topic'],
                'window': state['window'], 'retrieval_coverage': retrieval_coverage,
                'corpus_evidence_count': len(evidence), 'research_source_options': source_options}, evidence)
            tasks = [asyncio.create_task(self.assess_batch(state, batch, semaphore, retrieval_coverage,
                corpus_evidence_count=len(evidence), source_options=source_options)) for batch in batches]
            try:
                results = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            def summary(field):
                return '\n'.join(dict.fromkeys(item[field] for item in results if item[field]))[:2000]
            assessment = {'assessments': [row for item in results for row in item['assessments']],
                'continue_research': any(item['continue_research'] for item in results),
                'reason': summary('reason'), 'gap': summary('gap')}
        self.research.assess(state['run_id'], assessment['assessments'], {
            'continue': assessment['continue_research'], 'reason': assessment['reason'], 'gap': assessment['gap']})
        return {'assessment': assessment, **self.evidence_references(evidence)}

    def close_limit(self, run_id):
        records = self.records()
        run = records.get('ai_mi_research_runs', run_id)
        policy = json.loads(run['completion_policy_json'])
        if run_elapsed_seconds(run, self.clock()) >= policy['max_elapsed_seconds']:
            return 'time'
        if sum(row['run_id'] == run_id for row in records.rows('ai_mi_tool_runs')) >= policy['max_steps']:
            return 'step_limit'
        return None

    def decide_next(self, state):
        limit = self.close_limit(state['run_id'])
        more = state['assessment']['continue_research'] and bool(state['assessment']['gap']) and not limit
        reason = 'unresolved' if state['assessment']['gap'] else ('sufficient' if self.run_evidence(state) else 'no_data')
        provisional = (not more and not limit and reason == 'unresolved'
                       and not state.get('connector_issue') and bool(self.run_evidence(state)))
        return {'route': 'plan_research' if more else 'draft_report', 'reason': limit or reason,
                'provisional_assessment_close': provisional}

    async def write_report(self, payload, evidence):
        from .workflow_actions import restore_report_links, grounded_factual_feedback, factual_repair_context
        from .presentation import extension_markdown
        context = report_context({**payload, 'timezone': self.timezone}, evidence)
        review_context = {key: value for key, value in context.items()
                          if key not in {'previous_draft', 'draft_validation', 'repair_context_limited', 'repair_evidence_coverage'}}
        limits = context_limitations(context)
        notices = [*payload.get('provider_limitations', []), *limits]
        suffix = extension_markdown(payload.get('requested_extension'))
        reserved = (len('\n\n'.join([*notices, ''])) if notices else 0) + len(suffix)
        if not context['evidence']:
            draft = {'answer_markdown': 'Se recuperó información, pero no pude analizarla con suficiente fiabilidad para responder. Puedes acotar la pregunta.',
                     'cited_evidence_ids': [], 'findings': [], 'limitations': [], 'chart_request': None,
                     'decision': {'status': 'unresolved', 'reason': 'No hay evidencia utilizable en el contexto.',
                                  'material_gaps': ['Falta evidencia utilizable para responder.']}}
        else:
            for attempt in range(2):
                factual_feedback = None
                try:
                    draft = await self.ask('draft_report', ReportDraft, context, answer_limit=20000 - reserved)
                    ReportDraft.model_validate(draft)
                    validate_findings(draft['findings'], set(draft['cited_evidence_ids']))
                    restored = restore_report_links(draft, review_context['evidence'], evidence)
                    if len(restored['answer_markdown']) > 20000 - reserved:
                        raise ValueError('Restored report exceeds its body allowance')
                    ReportDraft.model_validate(restored)
                    review = await self.ask('review_factual_support', FactualReview, {**review_context, 'draft': draft})
                    if not review['accepted']:
                        factual_feedback, feedback_issues = grounded_factual_feedback(review, draft, review_context['evidence'])
                        raise ValueError('Draft has unsupported material claims')
                    draft = restored
                    break
                except ValueError:
                    if attempt:
                        raise
                    context = {**context, 'draft_validation': factual_feedback or 'Corrige el borrador: cumple todo el esquema y su extensión. Cada hallazgo necesita evidencia a favor o en contra dentro de las citas. Lo desconocido y las recomendaciones sin evidencia van en limitations o en el texto, nunca como hallazgos. Los únicos destinos de enlace permitidos son los campos url principales de las observaciones citadas: no uses channel_url, URLs de provenance ni enlaces dentro de las descripciones. Escribe el nombre del canal en texto sin enlace y cita la URL del video.'}
                    if factual_feedback:
                        context = factual_repair_context(context, review_context, draft, feedback_issues)
        draft['answer_markdown'] += suffix
        ReportDraft.model_validate(draft)
        draft['answer_markdown'] = '\n\n'.join([*limits, draft['answer_markdown']])
        draft['limitations'] = list(dict.fromkeys([*limits, *draft['limitations']]))[:20]
        return draft, review_context['evidence_coverage']

    async def draft_report(self, state):
        evidence = self.run_evidence(state)
        comparison = self.research.compare(state['run_id'], scope_resolver=resolve_source_scope)
        records = self.records()
        failures = provider_limitations(records, state['run_id'], state.get('connector_issue'))
        if not evidence:
            rejected = all_starts_rejected(records, state['run_id'])
            reason = 'unresolved' if rejected else state['reason']
            if rejected:
                text = ('No pude iniciar la investigación en las fuentes seleccionadas; no puedo concluir qué se dice sobre el tema a partir de este intento. '
                        'Para continuar con esas fuentes, primero debe revisarse su disponibilidad. '
                        'Puedes pedirme otra fuente o compartir una publicación pública fechada.')
            elif any(row.get('pointed_urls') for row in state.get('catalog', [])):
                text = ('No pude recuperar contenido verificable de las páginas indicadas. '
                        'No puedo confirmar si la causa es la dirección, el acceso o la respuesta del sitio. '
                        'Comprueba el enlace o comparte otra página pública.')
            else:
                text = ('No se obtuvo evidencia verificable para responder. La ausencia de resultados no demuestra que el problema no exista. '
                        'Puedes compartir una publicación o una URL pública relacionada con el hecho que quieres comprobar, '
                        'o indicar otro periodo para una nueva consulta.')
            return {'reason': reason, 'draft': {'answer_markdown': '\n\n'.join([*failures, text]),
                             'cited_evidence_ids': [], 'findings': [], 'limitations': [*failures, 'Sin evidencia recuperada; cierre: ' + reason], 'chart_request': None,
                             'decision': {'status': 'unresolved', 'reason': 'No se recuperó evidencia verificable.',
                                          'material_gaps': ['Falta evidencia verificable para responder.']}}}
        draft, coverage = await self.write_report({**effective_research_context(self, state), 'topic': state['intake']['topic'], 'window': state['window'], 'close_reason': state['reason'],
            'timezone': self.timezone,
            'remaining_gap': state['assessment'].get('gap', ''), 'provider_limitations': failures,
            'requested_extension': state.get('plan', {}).get('user_question') if state.get('plan', {}).get('next_action') == 'request_extension' else None,
            'comparison_context': comparison, 'retrieval_coverage': self.retrieval_context(state['run_id']),
            'research_source_options': self.research_source_options(state)}, evidence)
        if failures:
            draft['answer_markdown'] = '\n\n'.join([*failures, draft['answer_markdown']])
            draft['limitations'] = list(dict.fromkeys([*failures, *draft['limitations']]))[:20]
        return {'draft': draft, 'draft_coverage': coverage,
                'reason': self.reviewed_close_reason(state['reason'], draft, coverage,
                    provisional=state.get('provisional_assessment_close', False))}

    @staticmethod
    def reviewed_close_reason(reason, draft, coverage, *, provisional=False):
        if (provisional is True and reason == 'unresolved'
                and type(coverage.get('omitted')) is int and coverage['omitted'] == 0
                and draft['decision']['status'] == 'answered' and draft['decision']['material_gaps'] == []):
            return 'sufficient'
        if reason == 'sufficient' and (coverage.get('omitted') or draft['decision']['status'] != 'answered'):
            return 'unresolved'
        return reason

    def project_chart(self, state):
        from .workflow_actions import chart_projection
        return {'chart': chart_projection(self.records(), state['draft'], state['run_id'])}

    async def publish_result(self, state):
        prior = next((row for row in self.records().rows('ai_mi_reports') if row['run_id'] == state['run_id']), None)
        if prior:
            return {'last_report_id': prior['report_id'], 'response': self.report_response(prior)}
        draft = state['draft']
        repaired = {}
        try:
            ReportDraft.model_validate(draft)
            validate_findings(draft['findings'], set(draft['cited_evidence_ids']))
        except ValueError:
            # A checkpoint from an older candidate may precede draft validation.
            repaired = await self.draft_report(state)
            state = {**state, **repaired}
            repaired.update(self.project_chart(state))
            state = {**state, **repaired}
            draft = state['draft']
        reason = self.reviewed_close_reason(state['reason'], draft, state.get('draft_coverage', {}),
            provisional=state.get('provisional_assessment_close', False))
        report_id = self.research.publish(state['run_id'], draft['answer_markdown'], draft['cited_evidence_ids'],
            draft['findings'], draft['limitations'], {'window': state['window'], 'scope': 'selected_sources_only',
            'sources': sorted({item['provenance'].get('source', 'unknown') for item in self.research.evidence(state['run_id'])}),
            'model_context': state.get('draft_coverage', {}),
            'runtime_binding': json.loads(self.records().get('ai_mi_research_runs', state['run_id'])['brief_json']).get('runtime_binding'),
            'retrieval_coverage': self.retrieval_context(state['run_id']),
            'conclusion': {'version': 1, **draft['decision']}}, reason, state['chart'])
        report = self.records().get('ai_mi_reports', report_id)
        return {**repaired, 'last_report_id': report_id, 'response': self.report_response(report)}

    def report_response(self, report, *, citations=None):
        selected = json.loads(report['cited_evidence_ids_json']) if citations is None else citations
        return {'report_id': report['report_id'], 'markdown': report['markdown'],
                'chart': json.loads(report['chart_spec_json']) if report['chart_spec_json'] else None,
                'references': self.research.reference_urls(report['run_id'], selected),
                'published_at': report['published_at'], 'reused': False}

    async def response_for_delivery(self, state):
        response = state['response']
        if not response.get('report_id') or 'references' in response:
            return response
        import asyncio
        report = await asyncio.to_thread(lambda: self.records().get('ai_mi_reports', response['report_id']))
        if not report:
            raise ValueError('Cached response has no owned report')
        # Old followup checkpoints did not retain their own citation selection.
        if (state.get('intake', {}).get('intent') not in {'research', 'scheduled', 'read_report'}
                or response.get('markdown') != report['markdown']):
            return {'markdown': 'Esta respuesta anterior no conserva todas sus referencias. '
                    'Vuelve a pedir el resumen o abre el informe original; usaré la información guardada sin repetir la búsqueda.',
                    'reused': True}
        references = await asyncio.to_thread(self.research.reference_urls,
            report['run_id'], json.loads(report['cited_evidence_ids_json']))
        return {**response, 'references': references}

    async def deliver(self, state):
        if self.project:
            await self.project(self.records())
        return {'response': state['response'], 'delivery_verified_for': state['request_id'],
                'evidence': [], 'provider': {}, 'call': {}, 'catalog': []}
