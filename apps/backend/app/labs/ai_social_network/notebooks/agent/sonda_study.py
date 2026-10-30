"""Bounded production Sonda investigator; transport and Gold persistence are adapters.

study_case evaluates one pending batch, then the runner commits it and reloads
report work with full-population Gold facts. Model callbacks are inference only.
Configuration approval, capture provenance, immutable revisions and replay keys
remain authoritative in Gold; no cumulative generation journal is kept here.
The caller commits output DTOs and compact CAS state together before ack.
"""
from __future__ import annotations

import copy
import ipaddress
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, TypedDict
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from pydantic import Field

from app.sonda_contract import (BudgetMessage, ConfigMessage, Count, Criterion, Evaluation,
    Evidence, ExecutionMessage, Hash, Indicator, InvestigationRequest, Name, Observation, Proposal,
    Record, Report, Revision, Score, StrictBool, canonical_url, canonical_utc,
    encoded, evaluation_fingerprint, reject_secrets, sha256)
from app.sonda_source_limits import SourceLimitsSnapshot


class Identity(Record):
    model_id: Name
    model_revision: Name
    prompt_version: Name


class Verdict(Record):
    score: Score
    reason: Annotated[str, Field(strict=True, min_length=1, max_length=2000)]
    quotes: Annotated[list[Annotated[str, Field(strict=True, min_length=1, max_length=4000)]], Field(min_length=1, max_length=20)]


class ResearchStep(Record):
    step: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    query_id: UUID
    purpose: Annotated[str, Field(strict=True, min_length=1, max_length=2000)]
    target_override: Annotated[str, Field(strict=True, min_length=1, max_length=2000)] | None = None
    max_items: Annotated[int, Field(strict=True, ge=1, le=100000)]


class Plan(Record):
    requests: Annotated[list[ResearchStep], Field(max_length=20)] = Field(default_factory=list)
    propose: StrictBool = False


class Prose(Record):
    title: Name
    summary: Annotated[str, Field(strict=True, min_length=1, max_length=1800)]


class CaseGraphState(TypedDict):
    output: dict


def dto(cls, value):
    raw = value.model_dump(mode='json') if hasattr(value, 'model_dump') else value
    reject_secrets(raw)
    return cls.model_validate(raw).model_dump(mode='json')


def instant(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('Study time requires a timezone')
    return parsed.astimezone(timezone.utc)


def digest(value):
    return sha256(encoded(value))


def scope(message):
    return {key: message[key] for key in ('contract_version', 'contract_sha256', 'registry_id', 'generation_id')}


def approved(row):
    return row['status'] == 'active' and row['approval']['status'] == 'approved'


def case_key(criterion, observed_at):
    timestamp = instant(observed_at)
    seconds = criterion['case_window_hours'] * 3600
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    start = epoch + timedelta(seconds=((timestamp - epoch) // timedelta(seconds=seconds)) * seconds)
    return digest([criterion['criterion_id'], criterion['revision'], criterion['case_scope_key'], canonical_utc(start.isoformat())])


def evidence_for(observation, quote):
    if not quote.strip() or not any(quote in (observation.get(field) or '') for field in ('text', 'title')):
        raise ValueError('A quote must occur exactly in its observation')
    return dto(Evidence, {'evidence_id': digest([observation['observation_key'], quote]),
        'observation_key': observation['observation_key'], 'document_key': observation['document_key'],
        'url': observation['url'], 'quote': quote, 'captured_at': observation['observed_at']})


def target_allowed(target, source, query, connector):
    if target is not None and 'target' not in connector['research_fields']:
        return False
    if query['mode'] not in ('pages', 'indicators'):
        # Account/search Actor input semantics are owned by Huginn; do not invent an override.
        return target is None or target == query['target']
    try:
        parsed = urlsplit(canonical_url(target or query['target']))
        host = parsed.hostname
        if host not in source['allowed_domains'] or host in ('localhost', 'localhost.localdomain') or host.endswith('.local'):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            pass
        return True  # DNS/redirect and Actor-schema validation still belongs to Huginn.
    except (ValueError, UnicodeError):
        return False


def eligible_queries(config, criterion):
    sources = {row['source_id']: row for row in config['sources']}
    current = next((row for row in config['criteria'] if row['criterion_id'] == criterion['criterion_id']), None)
    if not current or not approved(current) or current['revision'] != criterion['revision'] or not current['allow_research']:
        return []
    return [row for row in config['queries'] if approved(row) and row['research_allowed']
            and approved(sources[row['source_id']]) and row['source_id'] in current['source_ids']
            and (not current['query_ids'] or row['query_id'] in current['query_ids'])]


class CaseFacts(Record):
    """Exact full-population Gold aggregates, never statistics of the hydrated sample."""
    distinct_documents: Count
    distinct_observations: Count
    relevant_documents: Count | None
    missing_evaluations: Count


class CaseInput(Record):
    observation: Observation
    config_revision: Revision
    source_id: UUID
    query_id: UUID


class CaseRequest(Record):
    request: InvestigationRequest
    status: Literal['pending', 'uncertain', 'completed', 'failed', 'cancelled', 'budget_exhausted']
    observation_message_ids: Annotated[list[UUID], Field(max_length=10000)] = Field(default_factory=list)
    observations_committed: StrictBool


class CaseWork(Record):
    """Trusted runner projection of one published Gold case; not a public wire DTO.

    Evaluate and commit pending inputs FIRST. Reload report work afterwards so its
    SQL facts include those committed verdicts. Gold owns replay keys, quotes and
    first-window membership, including research evidence arriving after that window.
    No caller may replace this projection with model or browser supplied values.
    """
    scope: dict[str, str]
    investigation_id: Hash
    criterion: Criterion
    config_revision: Revision
    window_start: Annotated[str, Field(strict=True, max_length=40)]
    phase: Literal['evaluate', 'report']
    input_revision: Hash
    pending_inputs: Annotated[list[CaseInput], Field(max_length=100)] = Field(default_factory=list)
    completed_evaluation_keys: Annotated[list[Hash], Field(max_length=100)] = Field(default_factory=list)
    evaluations: Annotated[list[Evaluation], Field(max_length=100)] = Field(default_factory=list)
    evidence: Annotated[list[Evidence], Field(max_length=100)] = Field(default_factory=list)
    facts: CaseFacts | None = None
    indicators: Annotated[list[Indicator], Field(max_length=100)] = Field(default_factory=list)
    query_ids: Annotated[list[UUID], Field(max_length=100)] = Field(default_factory=list)
    requests: Annotated[list[CaseRequest], Field(max_length=20)] = Field(default_factory=list)
    requests_complete: Literal[True]
    budget_round: BudgetMessage | None = None
    source_limits: SourceLimitsSnapshot | None = None
    report_revision: Count = 0
    approved_query_ids: Annotated[list[UUID], Field(max_length=20)] = Field(default_factory=list)
    limitations: Annotated[list[Annotated[str, Field(strict=True, min_length=1, max_length=1000)]], Field(max_length=20)] = Field(default_factory=list)


class CaseState(Record):
    scope: dict[str, str]
    investigation_id: Hash
    version: Count
    predecessor_revision: Count
    phase: Literal['evaluate', 'report']
    input_revision: Hash
    last_input_fingerprint: Hash
    report_revision: Count
    request_ids: Annotated[list[UUID], Field(max_length=20)] = Field(default_factory=list)
    proposal_ids: Annotated[list[UUID], Field(max_length=5)] = Field(default_factory=list)


def _case_study_case_identity(work, binding, current_revision):
    if work['scope'] != binding or work['config_revision'] > current_revision:
        raise ValueError('Case work is outside the current generation/configuration')
    criterion = work['criterion']
    start = canonical_utc(work['window_start'])
    if not approved(criterion) or case_key(criterion, start) != work['investigation_id']:
        raise ValueError('Case identity must bind its approved original criterion and UTC window')
    seconds = criterion['case_window_hours'] * 3600
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if (instant(start) - epoch).total_seconds() % seconds:
        raise ValueError('Case window start is not aligned with its approved duration')
    work['window_start'] = start


def _case_study_previous_state(previous, binding, work):
    previous = dto(CaseState, previous) if previous else None
    if previous and (previous['scope'] != binding or previous['investigation_id'] != work['investigation_id']
            or previous['version'] != previous['predecessor_revision'] + 1):
        raise ValueError('Operational state belongs to another case/generation')
    if previous and previous['report_revision'] > work['report_revision']:
        raise ValueError('Gold report revision cannot roll back operational state')
    return previous


def _case_study_request_history(work, previous, current_revision):
    requests = [row['request'] for row in work['requests']]
    request_ids = [row['request_id'] for row in requests]
    expected = (work['investigation_id'], work['criterion']['criterion_id'], work['criterion']['revision'])
    if len(request_ids) != len(set(request_ids)):
        raise ValueError('Case request history is duplicated or belongs to another case')
    for request in requests:
        identity = (request['investigation_id'], request['criterion_id'], request['criterion_revision'])
        if identity != expected or request['config_revision'] > current_revision:
            raise ValueError('Case request history is duplicated or belongs to another case')
    if previous and not set(previous['request_ids']) <= set(request_ids):
        raise ValueError('The runner must hydrate the complete committed request history')
    return {row['request']['request_id']: row for row in work['requests']}


def _case_study_execution_history(execution_results, requests, binding):
    if len(execution_results) > 20:
        raise ValueError('Case execution results exceed their bounded projection')
    for value in execution_results:
        message = dto(ExecutionMessage, value)
        result = message['payload']
        request = requests.get(result['request_id'])
        if scope(message) != binding or request is None or any(result[field] != request['request'][field]
                for field in ('config_revision', 'query_id', 'query_revision', 'budget_round_id')):
            raise ValueError('Execution does not belong to this committed case request')
        if result['status'] != request['status']:
            raise ValueError('Reconcile execution results in Gold before loading case work')


def _case_study_evaluate_projection(work, current_revision):
    if work['facts'] is not None or work['evaluations'] or work['evidence'] or work['indicators']:
        raise ValueError('Evaluate work cannot report pre-commit aggregate facts')
    criterion = work['criterion']
    for row in work['pending_inputs']:
        if (not work['config_revision'] <= row['config_revision'] <= current_revision
                or row['source_id'] not in criterion['source_ids']
                or criterion['query_ids'] and row['query_id'] not in criterion['query_ids']):
            raise ValueError('Pending input is outside the captured criterion scope')


def _case_study_report_counts(facts):
    if (facts['distinct_observations'] < facts['distinct_documents'] or facts['missing_evaluations'] > facts['distinct_documents']
            or facts['relevant_documents'] is not None and facts['relevant_documents'] > facts['distinct_documents'] - facts['missing_evaluations']):
        raise ValueError('Gold case aggregate counts are inconsistent')
    if facts['distinct_documents'] and facts['missing_evaluations'] == facts['distinct_documents'] and facts['relevant_documents'] is not None:
        raise ValueError('Entirely unknown relevance must remain NULL')


def _case_study_report_references(work):
    refs = {row['evidence_id'] for row in work['evidence']}
    if len(refs) != len(work['evidence']) or any(row['evidence_id'] != digest([row['observation_key'], row['quote']]) for row in work['evidence']):
        raise ValueError('Gold evidence identity is duplicated or inconsistent')
    if any(not set(row['evidence_ids']) <= refs for row in work['indicators']):
        raise ValueError('Gold indicator references unavailable case evidence')


def _case_study_report_projection(work):
    if work['pending_inputs'] or work['facts'] is None:
        raise ValueError('Report work requires fresh committed Gold facts, without pending inputs')
    _case_study_report_counts(work['facts'])
    _case_study_report_references(work)
    criterion = work['criterion']
    expected = (criterion['criterion_id'], criterion['revision'], digest(criterion), criterion['threshold'])
    for value in work['evaluations']:
        identity = (value['criterion_id'], value['criterion_revision'], value['rule_sha256'], value['threshold'])
        if identity != expected:
            raise ValueError('Hydrated evaluation belongs to another criterion revision')


def case_projection(configuration, work, previous, execution_results):
    current, work = dto(ConfigMessage, configuration), dto(CaseWork, work)
    binding, current_revision = scope(current), current['payload']['revision']
    _case_study_case_identity(work, binding, current_revision)
    previous = _case_study_previous_state(previous, binding, work)
    requests = _case_study_request_history(work, previous, current_revision)
    _case_study_execution_history(execution_results, requests, binding)
    if work['budget_round'] and scope(work['budget_round']) != binding:
        raise ValueError('Budget belongs to another generation')
    _case_study_source_limits(current['payload'], work, binding)
    if work['phase'] == 'evaluate':
        _case_study_evaluate_projection(work, current_revision)
    else:
        _case_study_report_projection(work)
    return current['payload'], work, previous


def _case_study_source_limits(configuration, work, binding):
    limits = work['source_limits']
    if limits is None:
        return
    if any(limits[key] != binding[key] for key in ('registry_id', 'generation_id')):
        raise ValueError('Source limits belong to another generation')
    if (limits['config_revision'] != configuration['revision']
            or limits['configuration_payload_sha256'] != digest(configuration)):
        raise ValueError('Source limits do not match the current configuration')
    if {row['source_id']: row['source_revision'] for row in limits['policies']} != {
            row['source_id']: row['revision'] for row in configuration['sources']}:
        raise ValueError('Source limits do not cover the exact source revisions')


def rule_context(criterion):
    return {key: criterion[key] for key in ('criterion_id', 'revision', 'label', 'description', 'threshold', 'examples', 'allow_research')}


def context_rows(rows, byte_limit):
    """Choose complete immutable records; never truncate a quote or invent its hash."""
    selected, size = [], 2
    for row in rows:
        width = len(encoded(row)) + 1
        if size + width > byte_limit:
            continue
        selected.append(row)
        size += width
    return selected


async def case_infer(model, role, payload):
    # A row-count cap alone does not bound long posts, examples or mapped values.
    context = payload.get('_tool_context') if role == 'plan' else None
    if context is not None and len(encoded(context)) > 4194304:
        raise ValueError('Trusted research tool configuration exceeds its byte budget')
    visible = {key: value for key, value in payload.items() if key != '_tool_context'} if context is not None else payload
    if len(encoded(visible)) > 98304:
        raise ValueError('Case inference context exceeds its byte budget')
    return await model(role, copy.deepcopy(payload))


def _case_study_open_budget(work, current_revision, timestamp):
    budget = work['budget_round']['payload'] if work['budget_round'] else None
    if (budget and budget['status'] == 'open' and budget['config_revision'] == current_revision
            and instant(budget['opened_at']) <= timestamp < instant(budget['expires_at'])
            and all(budget[field] is not None for field in ('remaining_microusd', 'research_remaining_microusd', 'remaining_items'))):
        return budget
    return None


def _case_study_research_target(current, work, step, query):
    source = next(row for row in current['sources'] if row['source_id'] == query['source_id'])
    connector = next(row for row in current['connectors'] if row['connector_id'] == source['connector_id'])
    if not target_allowed(step['target_override'], source, query, connector):
        return None
    target = step['target_override'] or query['target']
    if query['mode'] in ('pages', 'indicators'):
        target = canonical_url(target)
    request_id = str(uuid5(UUID(work['scope']['generation_id']), encoded([
        work['investigation_id'], step['step'].strip(), query['query_id'], query['revision'], target]).decode()))
    return connector, target, request_id


def _case_study_request_capacity(current, work, budget, query, connector, step, used):
    price = connector['max_cost_microusd_per_item']
    if budget is None or price is None or not work['evidence']:
        return None, 'No hay ronda vigente, precio conocido o evidencia verificable; no se solicita gasto.'
    outstanding = [row['request'] for row in work['requests'] if row['status'] in ('pending', 'uncertain')]
    committed_cost = sum(row['max_cost_microusd'] for row in outstanding if row['budget_round_id'] == budget['budget_round_id'])
    committed_items = sum(row['max_items'] for row in outstanding if row['budget_round_id'] == budget['budget_round_id'])
    available = min(budget['remaining_microusd'], budget['research_remaining_microusd'],
        current['policy']['round_budget_microusd'], current['policy']['research_reserve_microusd']) - committed_cost - used[0]
    cap = min(available, query['max_charge_microusd'])
    items = min(step['max_items'], query['max_items'], current['policy']['max_items_per_round'],
        budget['remaining_items'] - committed_items - used[1])
    if price:
        items = min(items, cap // price)
    if items <= 0 or cap < 0:
        return None, 'El saldo conocido no permite otra solicitud.'
    return (items, price * items), None


def case_research(current, work, plan, timestamp):
    criterion, key = work['criterion'], work['investigation_id']
    requests, limits = [], []
    existing = {row['request']['request_id'] for row in work['requests']}
    permitted = {row['query_id']: row for row in eligible_queries(current, criterion)
                 if row['query_id'] in work['approved_query_ids']}
    budget = _case_study_open_budget(work, current['revision'], timestamp)
    cost_used = items_used = 0
    source_used = {}
    refs = [row['evidence_id'] for row in work['evidence']]
    for step in plan['requests']:
        query = permitted.get(step['query_id'])
        if query is None:
            limits.append('Una consulta propuesta no tiene autorización vigente para investigar.')
            continue
        target_info = _case_study_research_target(current, work, step, query)
        if target_info is None:
            limits.append('El destino propuesto no pertenece al alcance aprobado.')
            continue
        connector, target, request_id = target_info
        if request_id in existing or _case_study_target_requested(work, query, target):
            continue
        if len(existing) >= current['policy']['max_research_requests_per_case']:
            limits.append('Se alcanzó el máximo de solicitudes de este caso.')
            break
        capacity, refusal = _case_study_request_capacity(current, work, budget, query, connector, step, (cost_used, items_used))
        if capacity is None:
            limits.append(refusal)
            continue
        items, cost = capacity
        capacity, refusal = _case_study_source_capacity(work, query, connector, items, cost, source_used)
        if capacity is None:
            limits.append(refusal)
            continue
        items, cost = capacity
        request = dto(InvestigationRequest, {'request_id':request_id, 'investigation_id':key,
            'config_revision':current['revision'], 'criterion_id':criterion['criterion_id'], 'criterion_revision':criterion['revision'],
            'query_id':query['query_id'], 'query_revision':query['revision'], 'budget_round_id':budget['budget_round_id'],
            'purpose':step['purpose'], 'target_override':target if step['target_override'] is not None else None,
            'max_items':items, 'max_cost_microusd':cost, 'expires_at':canonical_utc(budget['expires_at']), 'evidence_ids':refs})
        requests.append(request)
        existing.add(request_id)
        cost_used += request['max_cost_microusd']
        items_used += items
        spent, starts = source_used.get(query['source_id'], (0, 0))
        source_used[query['source_id']] = (spent + cost, starts + 1)
    return requests, limits


def _case_study_target_requested(work, query, target):
    """Preserve semantic dedup when upgrading earlier model-worded step IDs."""
    for row in work['requests']:
        request = row['request']
        if (request['query_id'], request['query_revision']) != (query['query_id'], query['revision']):
            continue
        previous = request['target_override'] or query['target']
        if query['mode'] in ('pages', 'indicators'):
            previous = canonical_url(previous)
        if previous == target:
            return True
    return False


def _case_study_source_capacity(work, query, connector, items, cost, source_used):
    """Static per-source bounds for this plan; Huginn owns live reservations."""
    snapshot = work.get('source_limits')
    if snapshot is None:
        return (items, cost), None
    policy = next((row for row in snapshot['policies'] if row['source_id'] == query['source_id']), None)
    if policy is None:
        return None, 'No está disponible la política de la fuente.'
    spent, starts = source_used.get(query['source_id'], (0, 0))
    if policy['max_starts_per_round'] is not None and starts >= policy['max_starts_per_round']:
        return None, 'Se alcanzó el límite de solicitudes de la fuente en este plan.'
    if policy['max_items_per_execution'] is not None:
        items = min(items, policy['max_items_per_execution'])
    price = connector['max_cost_microusd_per_item']
    if policy['max_cost_per_round_microusd'] is not None and price:
        items = min(items, max(0, policy['max_cost_per_round_microusd'] - spent) // price)
    if not items:
        return None, 'Los límites de la fuente no permiten esta solicitud.'
    return (items, items * price), None


_STUDY_PROPOSAL_MANAGED_FIELDS = ('revision', 'origin', 'author_id', 'proposal_id', 'approval', 'status')


def _case_study_record_content(record, excluded=()):
    managed = (*excluded, *_STUDY_PROPOSAL_MANAGED_FIELDS)
    return {key:value for key,value in record.items() if key not in managed}


def _case_study_checked_change(change, current):
    reject_secrets(change)
    if not isinstance(change, dict) or set(change) != {'operation', 'change', 'rationale'}:
        raise ValueError('Invalid proposal fields')
    record, kind = copy.deepcopy(change['change']['record']), change['change']['target_kind']
    if kind not in ('query', 'criterion'):
        raise ValueError('Only query/criterion proposals are supported')
    field = kind + '_id'
    existing = next((row for row in current['queries' if kind == 'query' else 'criteria'] if row[field] == record.get(field)), None)
    if change['operation'] != 'create' and existing is None or change['operation'] == 'create' and existing:
        raise ValueError('Proposal operation must match record identity')
    return record, kind, existing


def _case_study_draft_record(change, current, work, identity):
    record, kind, existing = _case_study_checked_change(change, current)
    if existing and change['operation'] == 'update' and _case_study_record_content(record) == _case_study_record_content(existing):
        return None
    if existing is None:
        field = kind + '_id'
        semantic = _case_study_record_content(record, (field,))
        record[field] = str(uuid5(UUID(work['scope']['generation_id']), encoded([work['investigation_id'], kind, semantic]).decode()))
    record.update(origin='agent', author_id=identity['model_id'], proposal_id=None,
        approval={'status':'pending', 'decided_by':None, 'decided_at':None}, status='draft', revision=existing['revision']+1 if existing else 1)
    proposal_id = str(uuid5(UUID(work['scope']['generation_id']), encoded([
        work['investigation_id'], change['operation'], kind, record, change['rationale']]).decode()))
    record['proposal_id'] = proposal_id
    return record, kind, proposal_id


def _case_study_query_proposal_scope(record, current):
    source = next((row for row in current['sources'] if row['source_id'] == record.get('source_id') and approved(row)), None)
    connector = next((row for row in current['connectors'] if source and row['connector_id'] == source['connector_id']), None)
    if not connector or record.get('mode') not in connector['modes']:
        raise ValueError('A proposed query requires a known source capability')


def _case_study_criterion_proposal_scope(record, current):
    query_sources = {row['query_id']:row['source_id'] for row in current['queries']}
    if not set(record['source_ids']) <= {row['source_id'] for row in current['sources']} or any(
            query not in query_sources or query_sources[query] not in record['source_ids'] for query in record['query_ids']):
        raise ValueError('A criterion proposal requires bound source/query references')


def case_proposals(response, current, work, identity):
    if not isinstance(response, dict) or set(response) != {'changes'} or not isinstance(response['changes'], list) or len(response['changes']) > 5:
        raise ValueError('Invalid proposal response')
    values = []
    checks = {'query':_case_study_query_proposal_scope, 'criterion':_case_study_criterion_proposal_scope}
    for change in response['changes']:
        prepared = _case_study_draft_record(change, current, work, identity)
        if prepared is None:
            continue
        record, kind, proposal_id = prepared
        checks[kind](record, current)
        values.append(dto(Proposal, {'proposal_id':proposal_id, 'investigation_id':work['investigation_id'],
            'base_config_revision':current['revision'], **change, 'change':{'target_kind':kind, 'record':record},
            'evidence_ids':[row['evidence_id'] for row in work['evidence']]}))
    return values


async def _case_study_evaluate(work, model, identity, timestamp, output):
    seen = set(work['completed_evaluation_keys'])
    criterion = work['criterion']
    for row in work['pending_inputs']:
        observation = row['observation']
        base = {'observation_key':observation['observation_key'], 'criterion_id':criterion['criterion_id'],
            'criterion_revision':criterion['revision'], 'config_revision':row['config_revision'], **identity,
            'rule_sha256':digest(criterion), 'threshold':criterion['threshold'], 'evaluated_at':canonical_utc(timestamp.isoformat())}
        key = evaluation_fingerprint(base)
        if key in seen:
            continue
        try:
            verdict = dto(Verdict, await case_infer(model, 'evaluate', {'criterion':rule_context(criterion), 'observation':observation}))
            value = {**base, 'evaluation_key':key, 'score':verdict['score'], 'reason':verdict['reason'],
                'decision':'relevant' if verdict['score'] >= criterion['threshold'] else 'not_relevant',
                'evidence':[evidence_for(observation, quote) for quote in dict.fromkeys(verdict['quotes'])]}
        except Exception:
            value = {**base, 'evaluation_key':key, 'score':None, 'decision':'error', 'evidence':[],
                'reason':'La evaluación no está disponible o no produjo citas verificables.'}
        output['evaluations'].append(dto(Evaluation, value))
        seen.add(key)
    return {}


def _case_study_report_context(current, work):
    limits = list(work['limitations'])
    waiting = [row for row in work['requests'] if row['status'] in ('pending', 'uncertain') or row['status'] == 'completed' and not row['observations_committed']]
    exhausted = any(row['status'] == 'budget_exhausted' for row in work['requests'])
    allowed = [row for row in eligible_queries(current, work['criterion']) if row['query_id'] in work['approved_query_ids']]
    quotes = context_rows(work['evidence'], 24576)
    # Facts remain full-population; display/reference metadata is unnecessary for inference.
    indicators = context_rows([{k:v for k,v in row.items() if k != 'evidence_ids'} for row in work['indicators']], 24576)
    verdicts = context_rows([{k:v for k,v in row.items() if k != 'evidence'} for row in work['evaluations']], 16384)
    return {'waiting':waiting, 'exhausted':exhausted, 'allowed':allowed, 'quotes':quotes,
        'indicators':indicators, 'verdicts':verdicts, 'limits':limits}


async def _case_study_plan_report(current, work, model, timestamp, context, output):
    limits = context['limits']
    if context['waiting']:
        limits.append('Hay solicitudes pendientes o inciertas; se espera su reconciliación.')
    elif context['exhausted']:
        limits.append('El presupuesto se agotó; se informa con la evidencia disponible.')
    elif work['facts']['relevant_documents'] and work['evidence']:
        try:
            plan = dto(Plan, await case_infer(model, 'plan', {'investigation_id':work['investigation_id'],
                'criterion':rule_context(work['criterion']), 'facts':work['facts'], 'evaluations':context['verdicts'],
                'evidence':context['quotes'], 'approved_queries':context['allowed'], 'limitations':limits,
                '_tool_context':{'configuration':current, 'scope':work['scope'],
                    'approved_query_ids':work['approved_query_ids'], 'source_limits':work['source_limits']}}))
            output['investigation_requests'], reasons = case_research(current, work, plan, timestamp)
            limits.extend(reasons)
            return plan['propose']
        except Exception:
            limits.append('No se obtuvo un plan de investigación válido.')
    return False


def _case_study_report_limits(work, context):
    facts, limits = work['facts'], context['limits']
    if facts['missing_evaluations']:
        limits.append('Hay evaluaciones desconocidas; no equivalen a resultados negativos.')
    if facts['distinct_observations'] > len(work['evaluations']):
        limits.append('Las cifras cubren el caso completo; las citas y evaluaciones mostradas son una selección acotada.')
    if (len(context['quotes']) < len(work['evidence']) or len(context['indicators']) < len(work['indicators'])
            or len(context['verdicts']) < len(work['evaluations'])):
        limits.append('El contexto del modelo usa una selección acotada por tamaño; los totales provienen de Gold.')
    if facts['distinct_documents'] and not work['evidence']:
        limits.append('No hay citas disponibles en esta proyección; las cifras provienen de los agregados publicados.')
    return list(dict.fromkeys(limits))[:30]


async def _case_study_report_prose(work, model, context):
    try:
        prose = dto(Prose, await case_infer(model, 'report', {'criterion':rule_context(work['criterion']), 'facts':work['facts'],
            'indicators':context['indicators'], 'evidence':context['quotes'], 'limitations':context['limits']}))
        numbers = set(re.findall(r'-?\d+(?:[.,]\d+)?', encoded([work['facts'], [row['value'] for row in work['indicators']], [row['quote'] for row in work['evidence']]]).decode()))
        if not set(re.findall(r'-?\d+(?:[.,]\d+)?', prose['title'] + ' ' + prose['summary'])) <= numbers:
            raise ValueError('Report prose introduced an unsupported number')
        return prose
    except Exception:
        context['limits'].append('La prosa del modelo no estaba disponible o no se pudo verificar.')
        return {'title':work['criterion']['label'], 'summary':'Informe basado en las observaciones y citas disponibles.'}


def _case_study_report_status(work, pending, limits):
    if pending:
        return 'awaiting_evidence'
    facts = work['facts']
    if facts['distinct_documents'] and facts['missing_evaluations'] == facts['distinct_documents']:
        return 'failed'
    return 'completed_with_limits' if limits else 'completed'


def _case_study_build_report(work, identity, timestamp, context, output, prose):
    criterion, facts = work['criterion'], work['facts']
    count = len(context['waiting']) + len(output['investigation_requests'])
    return dto(Report, {'report_id':digest([work['investigation_id'], 'report']), 'investigation_id':work['investigation_id'],
        'revision':work['report_revision']+1, 'config_revision':work['config_revision'],
        'criterion_id':criterion['criterion_id'], 'criterion_revision':criterion['revision'], **prose,
        'status':_case_study_report_status(work, count, context['limits']),
        'indicators':work['indicators'], 'evidence':work['evidence'], 'evaluations':work['evaluations'],
        'coverage':{'distinct_documents':facts['distinct_documents'], 'distinct_observations':facts['distinct_observations'],
            'query_ids':work['query_ids'], 'pending_requests':count,
            'failed_requests':sum(row['status'] in ('failed', 'cancelled') for row in work['requests']),
            'exhausted_budget':context['exhausted'], 'limitations':list(dict.fromkeys(context['limits']))[:30]}, **identity,
        'created_at':canonical_utc(timestamp.isoformat())})


def _case_study_proposal_configuration(current, work, allowed):
    source_ids = {row['source_id'] for row in allowed}
    sources = [row for row in current['sources'] if row['source_id'] in source_ids]
    live_criterion = next((row for row in current['criteria'] if row['criterion_id'] == work['criterion']['criterion_id']), None)
    return {'sources':sources, 'queries':allowed, 'criteria':[live_criterion] if live_criterion else [],
        'connectors':[row for row in current['connectors'] if row['connector_id'] in {source['connector_id'] for source in sources}]}


async def _case_study_propose_report(current, work, model, identity, state, context, output, report):
    configuration = _case_study_proposal_configuration(current, work, context['allowed'])
    try:
        summary = {key:report[key] for key in ('report_id', 'investigation_id', 'title', 'summary', 'coverage')}
        response = await case_infer(model, 'propose', {'report':{**summary, 'indicators':context['indicators'], 'evidence':context['quotes']},
            'configuration':configuration})
        candidates = case_proposals(response, {**configuration, 'revision':current['revision']}, work, identity)
        decisions = {row['proposal_id'] for row in current['proposal_decisions']}
        output['proposals'] = [row for row in candidates if row['proposal_id'] not in set(state['proposal_ids']) | decisions]
        # Bounded operational IDs; immutable Gold proposals own older dedup.
        state['proposal_ids'] = [row['proposal_id'] for row in candidates]
    except Exception:
        pass  # A failed proposal cannot activate configuration or invalidate a factual report.


async def _case_study_report(current, work, model, identity, timestamp, output, state):
    context = _case_study_report_context(current, work)
    propose = await _case_study_plan_report(current, work, model, timestamp, context, output)
    context['limits'] = _case_study_report_limits(work, context)
    prose = await _case_study_report_prose(work, model, context)
    report = _case_study_build_report(work, identity, timestamp, context, output, prose)
    output['reports'].append(report)
    state['report_revision'] = report['revision']
    state['request_ids'].extend(row['request_id'] for row in output['investigation_requests'])
    if propose and work['evidence']:
        await _case_study_propose_report(current, work, model, identity, state, context, output, report)
    return {}


async def study_case(configuration, work, prior_operational_state, execution_results, *, model, now, within_parent_graph=False):
    """Production entry: one bounded Gold case with compact CAS operational state.

    The runner claims work under its writer lease, hydrates committed Gold data and
    validates quote membership there, then atomically journals outputs/state before
    ack. Evaluate results must commit and refresh SQL facts before report phase.
    No all-generation journal, observation history or configuration is returned.
    """
    from langgraph.graph import END, START, StateGraph

    current, work, previous = case_projection(configuration, work, prior_operational_state, execution_results)
    identity, timestamp = dto(Identity, getattr(model, 'identity', None)), instant(now)
    # A committed report increment alone is not new case evidence/work.
    fingerprint = digest([{k:v for k,v in work.items() if k != 'report_revision'}, identity])
    output = {'evaluations':[], 'investigation_requests':[], 'reports':[], 'proposals':[]}
    version = previous['version'] if previous else 0
    state = {'scope':work['scope'], 'investigation_id':work['investigation_id'], 'version':version+1,
        'predecessor_revision':version, 'phase':work['phase'], 'input_revision':work['input_revision'],
        'last_input_fingerprint':fingerprint, 'report_revision':work['report_revision'],
        'request_ids':[row['request']['request_id'] for row in work['requests']],
        'proposal_ids':previous['proposal_ids'] if previous else []}
    if previous and previous['last_input_fingerprint'] == fingerprint:
        return {**output, 'state':previous}

    async def evaluate(_):
        return await _case_study_evaluate(work, model, identity, timestamp, output)

    async def report(_):
        return await _case_study_report(current, work, model, identity, timestamp, output, state)

    graph = StateGraph(CaseGraphState)
    graph.add_node('evaluate', evaluate)
    graph.add_node('report', report)
    graph.add_edge(START, work['phase'])
    graph.add_edge('evaluate', END)
    graph.add_edge('report', END)
    if within_parent_graph:
        await {'evaluate':evaluate,'report':report}[work['phase']]({})
    else:
        await graph.compile().ainvoke({'output':output})
    return {**output, 'state':dto(CaseState, state)}
