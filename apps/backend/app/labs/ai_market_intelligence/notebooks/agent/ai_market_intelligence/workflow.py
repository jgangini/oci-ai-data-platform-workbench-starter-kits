"""The research graph: model proposals, deterministic admission and durable effects."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import TypedDict

from .catalog import Catalog, public_url
from .connectors import PROFILES, compile_call, resolve_window
from .contracts import IntakeDecision, ResearchPlan, EvidenceAssessment, ReportDraft
from .execution import AdmissionBlocked, ProviderPending, PaidOperations, execute_selected, resolve_source_scope
from .proposals import Proposals
from .repository import Records, digest
from .research import Research


class SourceSelectionRequired(ValueError):
    pass


class TurnState(TypedDict, total=False):
    request_id: str
    question: str
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
    evidence: list
    assessment: dict
    draft: dict
    chart: dict | None
    reason: str
    route: str
    response: dict
    last_report_id: str | None
    last_topic: dict | None
    pending: dict | None
    schedule_run_id: str | None
    plan_sources: list


class MarketWorkflow:
    def __init__(self, journal, model, provider, *, timezone_name='America/Bogota',
                 run_cap_microusd=100000, step_cap_microusd=50000, max_steps=4,
                 usage_cost_verified=False, project=None, clock=None):
        if not 0 <= step_cap_microusd <= run_cap_microusd <= journal.budget:
            raise ValueError('Research limits must fit the bound budget')
        if type(max_steps) is not int or not 1 <= max_steps <= 10:
            raise ValueError('Invalid research step limit')
        self.journal, self.model, self.provider = journal, model, provider
        self.research, self.paid = Research(journal), PaidOperations(journal)
        self.catalog, self.proposals = Catalog(journal), Proposals(journal)
        self.timezone, self.cap, self.step_cap = timezone_name, run_cap_microusd, step_cap_microusd
        self.max_steps, self.usage_verified = max_steps, usage_cost_verified
        self.project = project
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def records(self):
        value, _ = self.journal.read()
        return Records(self.journal.scope, value['tables'])

    async def ask(self, name, schema, payload):
        result = await self.model.ask(name, schema.model_json_schema(), payload)
        return schema.model_validate(result).model_dump(mode='json')

    def build(self, checkpointer=None):
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
        for name, node in nodes.items():
            graph.add_node(name, node)
        graph.add_edge(START, 'bind_request')
        graph.add_edge('bind_request', 'understand')
        graph.add_edge('understand', 'resolve_context')
        routes = {
            'resolve_context': ['prepare_run', 'handle_confirmation', 'deliver_and_read'],
            'plan_research': ['admit_step', 'draft_report', 'deliver_and_read'],
            'admit_step': ['execute_step', 'draft_report', 'deliver_and_read'],
            'execute_step': ['reconcile_step', 'normalize_evidence', 'draft_report'],
            'reconcile_step': ['normalize_evidence', 'draft_report', 'deliver_and_read'],
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
        return {'intake': {}, 'catalog': [], 'plan': {}, 'provider': {}, 'evidence': [],
                'assessment': {}, 'draft': {}, 'chart': None, 'reason': '', 'response': {},
                'run_id': '', 'question_id': '', 'topic_id': '', 'tool_id': '', 'call': {}}

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
            return {'intake': {'intent': 'scheduled', 'topic': topic}}
        if state.get('pending'):
            return {'intake': {'intent': 'schedule_decision', 'topic': state.get('last_topic')}}
        records = self.records()
        sources = [row for row in records.rows('ai_mi_sources') if row['status'] == 'active']
        targets = {row['target_id'] for row in records.rows('ai_mi_source_targets')
                   if row['status'] == 'active' and row['source_key'] in {source['source_key'] for source in sources}}
        topics = {row['topic_id'] for row in records.rows('ai_mi_topic_sources')
                  if row['status'] == 'active' and row['target_id'] in targets}
        registered = sorted((row for row in records.rows('ai_mi_topics') if row['topic_id'] in topics),
                            key=lambda row: row['created_at'] or '', reverse=True)[:20]
        result = await self.ask('understand', IntakeDecision, {
            'question': state['question'], 'last_topic': state.get('last_topic'),
            'has_previous_report': bool(state.get('last_report_id')),
            'sources': [{'source_key': row['source_key'], 'name': row['name']} for row in sources],
            'registered_topics': [json.loads(row['canonical_scope_json']) for row in registered],
            'now': self.clock().isoformat(), 'timezone': self.timezone})
        return {'intake': result}

    async def resolve_context(self, state):
        intent = state['intake']['intent']
        if state.get('pending') or intent in {'source_request', 'source_decision', 'schedule_request', 'schedule_decision', 'cancel', 'read_report'}:
            topic = state['intake'].get('topic')
            if topic and not state.get('pending'):
                self.research.record_question(state['request_id'], state['question'], topic, 'administration')
            return {'route': 'handle_confirmation', 'last_topic': topic or state.get('last_topic')}
        intake = state['intake']
        if intake.get('missing_fields') or not intake['topic']:
            return {'route': 'deliver_and_read', 'response': {'markdown': intake['user_question'] or '¿Qué empresa, mercado y objetivo deseas investigar?'}}
        run = None
        if intent == 'scheduled':
            run = self.records().get('ai_mi_research_runs', state['schedule_run_id'])
            question_id, topic_id = None, run['topic_id']
        else:
            kind = 'followup' if intent == 'followup' else 'conversation_research'
            question_id, topic_id = self.research.record_question(state['request_id'], state['question'], intake['topic'], kind)
        if intent == 'followup':
            report = self.records().get('ai_mi_reports', state.get('last_report_id'))
            response = {'markdown': 'No hay un informe anterior disponible en esta conversación.'}
            if report:
                evidence = self.research.evidence(report['run_id'])
                draft = await self.ask('draft_report', ReportDraft, {'followup': state['question'],
                    'published_report': report['markdown'], 'published_at': report['published_at'], 'evidence': evidence})
                from .workflow_actions import validate_report_links
                validate_report_links(draft, evidence)
                response = {**self.report_response(report), 'markdown': draft['answer_markdown'], 'chart': None, 'reused': True}
            return {'question_id': question_id, 'topic_id': topic_id, 'route': 'deliver_and_read',
                    'response': response}
        try:
            catalog = self.catalog_for_question(state, topic_id, run)
        except SourceSelectionRequired:
            return {'question_id': question_id, 'topic_id': topic_id, 'last_topic': intake['topic'],
                    'route': 'deliver_and_read', 'response': {'markdown':
                        'No identifiqué esa fuente con certeza. Indica su nombre registrado o su URL exacta para continuar.'}}
        return {'question_id': question_id, 'topic_id': topic_id, 'last_topic': intake['topic'],
                'window': {key: json.loads(run['plan_window_json'])[key] for key in ('start', 'end')} if intent == 'scheduled' else resolve_window(intake['window_spec'], self.clock(), self.timezone),
                'catalog': catalog, 'route': 'prepare_run'}

    def catalog_for_question(self, state, topic_id, scheduled_run):
        """Keep conversational source selection separate from catalog row eligibility."""
        records = self.records()
        permitted = None
        if scheduled_run:
            schedule = records.get('ai_mi_schedules', scheduled_run['schedule_id'])
            permitted = set(json.loads(schedule['approved_plan_budget_json'])['target_ids'])
        catalog = self.catalog.approved_base(records, topic_id, permitted)
        selected = state['intake'].get('selected_source_key')
        if selected:
            source = next((row for row in catalog if row['source_key'] == selected), None)
            urls = set()
            for raw in re.findall(r'https://[^\s<>\]"\x27)]+', state['question']):
                try:
                    urls.add(public_url(raw.rstrip('.,;')))
                except ValueError:
                    continue
            named = source and source['name'] and re.search(r'\b' + re.escape(source['name']) + r'\b', state['question'], re.I)
            linked = source and any(target['canonical_url_or_id'] in urls for target in source['targets'])
            if not (named or linked):
                raise SourceSelectionRequired('An explicit source selection must be grounded in the user question')
            if linked:
                source = {**source, 'targets': [target for target in source['targets'] if target['canonical_url_or_id'] in urls]}
            catalog = [source]
        return catalog

    def prepare_run(self, state):
        run = state.get('schedule_run_id') or self.research.prepare_run(state['question_id'], state['window'], state['question'], self.cap, max_steps=self.max_steps)
        records = self.records()
        prior = next((row for row in records.rows('ai_mi_reports') if row['run_id'] == run), None)
        window = json.loads(records.get('ai_mi_research_runs', run)['plan_window_json'])
        return {'run_id': run, 'window': {key: window[key] for key in ('start', 'end')},
                'response': self.report_response(prior) if prior else {}}

    async def plan_research(self, state):
        if state['response']:
            return {'route': 'deliver_and_read'}
        limit = self.close_limit(state['run_id'])
        if limit:
            return {'reason': limit, 'route': 'draft_report'}
        evidence = self.research.evidence(state['run_id'])
        if not state['catalog']:
            return {'reason': 'no_data', 'route': 'draft_report'}
        visited = {row['target_id'] for row in self.records().rows('ai_mi_tool_runs') if row['run_id'] == state['run_id']}
        base = [{**row, 'targets': [target for target in row['targets'] if target['target_id'] not in visited]} for row in state['catalog']]
        base = [row for row in base if row['targets']]
        available = base or state['catalog']
        plan = await self.ask('plan_research', ResearchPlan, {
            'question': state['question'], 'topic': state['intake']['topic'], 'window': state['window'], 'catalog': available,
            'evidence': evidence, 'assessment': state['assessment']})
        if plan['next_action'] == 'request_extension' and evidence:
            return {'plan': plan, 'route': 'draft_report', 'reason': 'unresolved'}
        if plan['next_action'] in {'clarify', 'request_extension'}:
            return {'plan': plan, 'route': 'deliver_and_read', 'response': {'markdown': plan['user_question'] or 'Necesito delimitar el alcance antes de continuar.'}}
        if plan['next_action'] != 'execute':
            return {'plan': plan, 'route': 'draft_report', 'reason': 'sufficient' if evidence else 'no_data'}
        bound_plan = self.catalog.bind_plan_target(plan, available)
        if bound_plan is None:
            return {'plan': plan, 'route': 'deliver_and_read', 'response': {'markdown':
                    'No hay un destino aprobado inequívoco para ese paso. Indica una fuente registrada para este objetivo o solicita registrar la página.'}}
        return {'plan': bound_plan, 'plan_sources': available, 'route': 'admit_step'}

    def admit_step(self, state):
        plan, records = state['plan'], self.records()
        scope = resolve_source_scope(records, state['topic_id'], plan['source_key'], plan['target_id'])
        choices = state.get('plan_sources', state.get('catalog', []))
        if choices and plan['source_key'] not in {row['source_key'] for row in choices}:
            raise ValueError('Registered research base must be consulted before discovery')
        choice = next((row for row in choices if row['source_key'] == plan['source_key']), None)
        if choice and choice['targets'] and plan['target_id'] not in {row['target_id'] for row in choice['targets']}:
            raise ValueError('Select one of the remaining approved base targets')
        try:
            call = compile_call(scope['connector_revision'], plan['arguments'], state['window'], self.step_cap,
                                approved_urls=scope['urls'], approved_finance=scope['financial'], usage_cost_verified=self.usage_verified)
        except ValueError:
            if scope['financial'] is None:
                raise
            return {'route': 'deliver_and_read', 'response': {'markdown':
                    'No pude validar la selección financiera y su periodo. Indica los destinos registrados y las fechas o años exactos que deseas comparar.'}}
        try:
            tool = self.paid.reserve(state['run_id'], plan['source_key'], plan['target_id'], call, self.step_cap,
                                     plan['purpose'], authorization_hash=scope['authorization_hash'])
        except AdmissionBlocked as error:
            return {'reason': error.reason, 'route': 'draft_report'}
        previous = self.records().get('ai_mi_tool_runs', tool)
        if previous['state'] in {'succeeded', 'failed', 'aborted', 'timed_out'}:
            return {'reason': 'unresolved', 'route': 'draft_report'}
        return {'tool_id': tool, 'call': call, 'route': 'execute_step'}

    def scheduled_input(self, run_id):
        run = self.records().get('ai_mi_research_runs', run_id)
        if not run or not run['schedule_id']:
            raise ValueError('An owned scheduled run is required')
        return {'request_id': digest(['scheduled', run_id]), 'question': json.loads(run['brief_json'])['objective'],
                'schedule_run_id': run_id, 'pending': None}

    async def execute_step(self, state):
        try:
            result = await execute_selected(self.paid, state['tool_id'], state['call'], self.provider.start)
        except RuntimeError:
            return {'reason': 'uncertain', 'route': 'draft_report'}
        route = 'normalize_evidence' if result.get('status') == 'SUCCEEDED' else 'reconcile_step'
        return {'provider': result, 'route': route}

    async def reconcile_step(self, state):
        value, _ = self.journal.read()
        operation = value['operations'][state['tool_id']]
        if not operation.get('provider_run_id'):
            return {'route': 'deliver_and_read', 'response': {'markdown': 'El inicio anterior requiere conciliación; no se iniciará otra captura.'}}
        result = await self.provider.read(operation['provider_run_id'])
        status = {'SUCCEEDED': 'succeeded', 'FAILED': 'failed', 'ABORTED': 'aborted', 'TIMED-OUT': 'timed_out', 'RUNNING': 'running', 'READY': 'running'}[result['status']]
        self.paid.record_provider(state['tool_id'], operation['permit'], operation['provider_run_id'], status,
                                  {key: result[key] for key in ('runId', 'status')})
        if status in {'succeeded', 'failed', 'aborted', 'timed_out'} and result.get('spent_microusd') is not None:
            self.paid.record_cost(state['tool_id'], result['runId'], result['spent_microusd'])
        if status == 'succeeded':
            return {'provider': result, 'route': 'normalize_evidence'}
        if status == 'running':
            raise ProviderPending('The provider is still running; resume this exact request')
        return {'route': 'draft_report', 'reason': 'unresolved'}

    async def normalize_evidence(self, state):
        observations, cost = await self.provider.observations(state['provider'], state['call'])
        if cost is not None:
            self.paid.record_cost(state['tool_id'], state['provider']['runId'], cost)
        self.research.add_evidence(state['run_id'], state['tool_id'], observations)
        return {'evidence': self.research.evidence(state['run_id'])}

    async def assess_evidence(self, state):
        assessment = await self.ask('assess_evidence', EvidenceAssessment, {'question': state['question'], 'topic': state['intake']['topic'], 'window': state['window'], 'evidence': state['evidence']})
        self.research.assess(state['run_id'], assessment['assessments'], {
            'continue': assessment['continue_research'], 'reason': assessment['reason'], 'gap': assessment['gap']})
        return {'assessment': assessment}

    def close_limit(self, run_id):
        records = self.records()
        run = records.get('ai_mi_research_runs', run_id)
        timing = json.loads(run['started_finished_at_json'])
        policy = json.loads(run['completion_policy_json'])
        if (self.clock() - datetime.fromisoformat(timing['started_at'])).total_seconds() >= policy['max_elapsed_seconds']:
            return 'time'
        if sum(row['run_id'] == run_id for row in records.rows('ai_mi_tool_runs')) >= policy['max_steps']:
            return 'step_limit'
        return None

    def decide_next(self, state):
        limit = self.close_limit(state['run_id'])
        more = state['assessment']['continue_research'] and bool(state['assessment']['gap']) and not limit
        reason = 'no_data' if not state['evidence'] else ('unresolved' if state['assessment']['gap'] else 'sufficient')
        return {'route': 'plan_research' if more else 'draft_report', 'reason': limit or reason}

    async def draft_report(self, state):
        evidence = self.research.evidence(state['run_id'])
        if not evidence:
            return {'draft': {'answer_markdown': 'No se obtuvo evidencia verificable para responder. La ausencia de resultados no demuestra que el problema no exista.',
                             'cited_evidence_ids': [], 'findings': [], 'limitations': ['Sin evidencia recuperada; cierre: ' + state['reason']], 'chart_request': None}}
        draft = await self.ask('draft_report', ReportDraft, {'question': state['question'], 'topic': state['intake']['topic'], 'window': state['window'], 'evidence': evidence, 'close_reason': state['reason'],
            'remaining_gap': state['assessment'].get('gap', '')})
        from .workflow_actions import validate_report_links
        validate_report_links(draft, evidence)
        return {'draft': draft}

    def project_chart(self, state):
        from .workflow_actions import chart_projection
        return {'chart': chart_projection(self.records(), state['draft'], state['run_id'])}

    def publish_result(self, state):
        draft = state['draft']
        report_id = self.research.publish(state['run_id'], draft['answer_markdown'], draft['cited_evidence_ids'],
            draft['findings'], draft['limitations'], {'window': state['window'], 'scope': 'selected_sources_only',
            'sources': sorted({item['provenance'].get('source', 'unknown') for item in self.research.evidence(state['run_id'])})}, state['reason'], state['chart'])
        report = self.records().get('ai_mi_reports', report_id)
        return {'last_report_id': report_id, 'response': self.report_response(report)}

    @staticmethod
    def report_response(report):
        return {'report_id': report['report_id'], 'markdown': report['markdown'],
                'chart': json.loads(report['chart_spec_json']) if report['chart_spec_json'] else None,
                'published_at': report['published_at'], 'reused': False}

    async def deliver(self, state):
        if self.project:
            await self.project(self.records())
        return {'response': state['response'], 'evidence': [], 'provider': {}, 'call': {}, 'catalog': []}
