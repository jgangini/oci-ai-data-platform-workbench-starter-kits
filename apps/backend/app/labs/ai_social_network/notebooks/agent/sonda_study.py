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


def case_projection(configuration, work, previous, execution_results):
    current, work = dto(ConfigMessage, configuration), dto(CaseWork, work)
    binding, key = scope(current), work['investigation_id']
    if work['scope'] != binding or work['config_revision'] > current['payload']['revision']:
        raise ValueError('Case work is outside the current generation/configuration')
    criterion = work['criterion']
    start = canonical_utc(work['window_start'])
    if not approved(criterion) or case_key(criterion, start) != key:
        raise ValueError('Case identity must bind its approved original criterion and UTC window')
    seconds = criterion['case_window_hours'] * 3600
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if (instant(start) - epoch).total_seconds() % seconds:
        raise ValueError('Case window start is not aligned with its approved duration')
    work['window_start'] = start
    previous = dto(CaseState, previous) if previous else None
    if previous and (previous['scope'] != binding or previous['investigation_id'] != key
            or previous['version'] != previous['predecessor_revision'] + 1):
        raise ValueError('Operational state belongs to another case/generation')
    if previous and previous['report_revision'] > work['report_revision']:
        raise ValueError('Gold report revision cannot roll back operational state')
    request_ids = [row['request']['request_id'] for row in work['requests']]
    if len(request_ids) != len(set(request_ids)) or any(row['request']['investigation_id'] != key
            or row['request']['criterion_id'] != criterion['criterion_id']
            or row['request']['criterion_revision'] != criterion['revision']
            or row['request']['config_revision'] > current['payload']['revision'] for row in work['requests']):
        raise ValueError('Case request history is duplicated or belongs to another case')
    if previous and not set(previous['request_ids']) <= set(request_ids):
        raise ValueError('The runner must hydrate the complete committed request history')
    if len(execution_results) > 20:
        raise ValueError('Case execution results exceed their bounded projection')
    for value in execution_results:
        message = dto(ExecutionMessage, value)
        result = message['payload']
        request = next((row for row in work['requests'] if row['request']['request_id'] == result['request_id']), None)
        if scope(message) != binding or request is None or any(result[field] != request['request'][field]
                for field in ('config_revision', 'query_id', 'query_revision', 'budget_round_id')):
            raise ValueError('Execution does not belong to this committed case request')
        if result['status'] != request['status']:
            raise ValueError('Reconcile execution results in Gold before loading case work')
    if work['budget_round'] and scope(work['budget_round']) != binding:
        raise ValueError('Budget belongs to another generation')
    if work['phase'] == 'evaluate':
        if work['facts'] is not None or work['evaluations'] or work['evidence'] or work['indicators']:
            raise ValueError('Evaluate work cannot report pre-commit aggregate facts')
        for row in work['pending_inputs']:
            if (not work['config_revision'] <= row['config_revision'] <= current['payload']['revision']
                    or row['source_id'] not in criterion['source_ids']
                    or criterion['query_ids'] and row['query_id'] not in criterion['query_ids']):
                raise ValueError('Pending input is outside the captured criterion scope')
    else:
        facts = work['facts']
        if work['pending_inputs'] or facts is None:
            raise ValueError('Report work requires fresh committed Gold facts, without pending inputs')
        if (facts['distinct_observations'] < facts['distinct_documents'] or facts['missing_evaluations'] > facts['distinct_documents']
                or facts['relevant_documents'] is not None and facts['relevant_documents'] > facts['distinct_documents'] - facts['missing_evaluations']):
            raise ValueError('Gold case aggregate counts are inconsistent')
        if facts['distinct_documents'] and facts['missing_evaluations'] == facts['distinct_documents'] and facts['relevant_documents'] is not None:
            raise ValueError('Entirely unknown relevance must remain NULL')
        refs = {row['evidence_id'] for row in work['evidence']}
        if len(refs) != len(work['evidence']) or any(row['evidence_id'] != digest([row['observation_key'], row['quote']]) for row in work['evidence']):
            raise ValueError('Gold evidence identity is duplicated or inconsistent')
        if any(not set(row['evidence_ids']) <= refs for row in work['indicators']):
            raise ValueError('Gold indicator references unavailable case evidence')
        for value in work['evaluations']:
            if (value['criterion_id'] != criterion['criterion_id'] or value['criterion_revision'] != criterion['revision']
                    or value['rule_sha256'] != digest(criterion) or value['threshold'] != criterion['threshold']):
                raise ValueError('Hydrated evaluation belongs to another criterion revision')
    return current['payload'], work, previous


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
    if len(encoded(payload)) > 98304:
        raise ValueError('Case inference context exceeds its byte budget')
    return await model(role, copy.deepcopy(payload))


def case_research(current, work, plan, timestamp):
    criterion, key = work['criterion'], work['investigation_id']
    requests, limits = [], []
    existing = {row['request']['request_id'] for row in work['requests']}
    permitted = {row['query_id']: row for row in eligible_queries(current, criterion)
                 if row['query_id'] in work['approved_query_ids']}
    budget = work['budget_round']['payload'] if work['budget_round'] else None
    valid_budget = budget and budget['status'] == 'open' and budget['config_revision'] == current['revision'] and instant(budget['opened_at']) <= timestamp < instant(budget['expires_at']) and all(budget[field] is not None for field in ('remaining_microusd', 'research_remaining_microusd', 'remaining_items'))
    cost_used = items_used = 0
    outstanding = [row['request'] for row in work['requests'] if row['status'] in ('pending', 'uncertain')]
    refs = [row['evidence_id'] for row in work['evidence']]
    for step in plan['requests']:
        query = permitted.get(step['query_id'])
        if query is None:
            limits.append('Una consulta propuesta no tiene autorización vigente para investigar.')
            continue
        source = next(row for row in current['sources'] if row['source_id'] == query['source_id'])
        connector = next(row for row in current['connectors'] if row['connector_id'] == source['connector_id'])
        if not target_allowed(step['target_override'], source, query, connector):
            limits.append('El destino propuesto no pertenece al alcance aprobado.')
            continue
        target = step['target_override'] or query['target']
        if query['mode'] in ('pages', 'indicators'):
            target = canonical_url(target)
        request_id = str(uuid5(UUID(work['scope']['generation_id']), encoded([key, step['step'].strip(), query['query_id'], query['revision'], target]).decode()))
        if request_id in existing:
            continue
        if len(existing) >= current['policy']['max_research_requests_per_case']:
            limits.append('Se alcanzó el máximo de solicitudes de este caso.')
            break
        price = connector['max_cost_microusd_per_item']
        if not valid_budget or price is None or not refs:
            limits.append('No hay ronda vigente, precio conocido o evidencia verificable; no se solicita gasto.')
            continue
        committed_cost = sum(row['max_cost_microusd'] for row in outstanding if row['budget_round_id'] == budget['budget_round_id'])
        committed_items = sum(row['max_items'] for row in outstanding if row['budget_round_id'] == budget['budget_round_id'])
        available = min(budget['remaining_microusd'], budget['research_remaining_microusd'], current['policy']['round_budget_microusd'], current['policy']['research_reserve_microusd']) - committed_cost - cost_used
        cap = min(available, query['max_charge_microusd'])
        items = min(step['max_items'], query['max_items'], current['policy']['max_items_per_round'], budget['remaining_items'] - committed_items - items_used)
        if price:
            items = min(items, cap // price)
        if items <= 0 or cap < 0:
            limits.append('El saldo conocido no permite otra solicitud.')
            continue
        request = dto(InvestigationRequest, {'request_id':request_id, 'investigation_id':key,
            'config_revision':current['revision'], 'criterion_id':criterion['criterion_id'], 'criterion_revision':criterion['revision'],
            'query_id':query['query_id'], 'query_revision':query['revision'], 'budget_round_id':budget['budget_round_id'],
            'purpose':step['purpose'], 'target_override':target if step['target_override'] is not None else None,
            'max_items':items, 'max_cost_microusd':price * items, 'expires_at':canonical_utc(budget['expires_at']), 'evidence_ids':refs})
        requests.append(request)
        existing.add(request_id)
        cost_used += request['max_cost_microusd']
        items_used += items
    return requests, limits


def case_proposals(response, current, work, identity):
    if not isinstance(response, dict) or set(response) != {'changes'} or not isinstance(response['changes'], list) or len(response['changes']) > 5:
        raise ValueError('Invalid proposal response')
    values = []
    for change in response['changes']:
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
        managed = ('revision', 'origin', 'author_id', 'proposal_id', 'approval', 'status')
        if existing and change['operation'] == 'update' and {k:v for k,v in record.items() if k not in managed} == {k:v for k,v in existing.items() if k not in managed}:
            continue
        if existing is None:
            semantic = {name:value for name,value in record.items() if name not in (field, *managed)}
            record[field] = str(uuid5(UUID(work['scope']['generation_id']), encoded([work['investigation_id'], kind, semantic]).decode()))
        record.update(origin='agent', author_id=identity['model_id'], proposal_id=None,
            approval={'status':'pending', 'decided_by':None, 'decided_at':None}, status='draft', revision=existing['revision']+1 if existing else 1)
        proposal_id = str(uuid5(UUID(work['scope']['generation_id']), encoded([work['investigation_id'], change['operation'], kind, record, change['rationale']]).decode()))
        record['proposal_id'] = proposal_id
        if kind == 'query':
            source = next((row for row in current['sources'] if row['source_id'] == record.get('source_id') and approved(row)), None)
            connector = next((row for row in current['connectors'] if source and row['connector_id'] == source['connector_id']), None)
            if not connector or record.get('mode') not in connector['modes']:
                raise ValueError('A proposed query requires a known source capability')
        else:
            query_sources = {row['query_id']:row['source_id'] for row in current['queries']}
            if not set(record['source_ids']) <= {row['source_id'] for row in current['sources']} or any(q not in query_sources or query_sources[q] not in record['source_ids'] for q in record['query_ids']):
                raise ValueError('A criterion proposal requires bound source/query references')
        values.append(dto(Proposal, {'proposal_id':proposal_id, 'investigation_id':work['investigation_id'],
            'base_config_revision':current['revision'], **change, 'change':{'target_kind':kind, 'record':record},
            'evidence_ids':[row['evidence_id'] for row in work['evidence']]}))
    return values


async def study_case(configuration, work, prior_operational_state, execution_results, *, model, now):
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

    async def report(_):
        criterion, facts = work['criterion'], work['facts']
        limits = list(work['limitations'])
        waiting = [row for row in work['requests'] if row['status'] in ('pending', 'uncertain') or row['status'] == 'completed' and not row['observations_committed']]
        exhausted = any(row['status'] == 'budget_exhausted' for row in work['requests'])
        allowed = [row for row in eligible_queries(current, criterion) if row['query_id'] in work['approved_query_ids']]
        quotes = context_rows(work['evidence'], 24576)
        # Facts remain full-population; display/reference metadata is unnecessary for inference.
        indicators = context_rows([{k:v for k,v in row.items() if k != 'evidence_ids'} for row in work['indicators']], 24576)
        verdicts = context_rows([{k:v for k,v in row.items() if k != 'evidence'} for row in work['evaluations']], 16384)
        propose = False
        if waiting:
            limits.append('Hay solicitudes pendientes o inciertas; se espera su reconciliación.')
        elif exhausted:
            limits.append('El presupuesto se agotó; se informa con la evidencia disponible.')
        elif facts['relevant_documents'] and work['evidence']:
            try:
                plan = dto(Plan, await case_infer(model, 'plan', {'investigation_id':work['investigation_id'],
                    'criterion':rule_context(criterion), 'facts':facts, 'evaluations':verdicts,
                    'evidence':quotes, 'approved_queries':allowed, 'limitations':limits}))
                output['investigation_requests'], reasons = case_research(current, work, plan, timestamp)
                limits.extend(reasons)
                propose = plan['propose']
            except Exception:
                limits.append('No se obtuvo un plan de investigación válido.')
        if facts['missing_evaluations']:
            limits.append('Hay evaluaciones desconocidas; no equivalen a resultados negativos.')
        if facts['distinct_observations'] > len(work['evaluations']):
            limits.append('Las cifras cubren el caso completo; las citas y evaluaciones mostradas son una selección acotada.')
        if len(quotes) < len(work['evidence']) or len(indicators) < len(work['indicators']) or len(verdicts) < len(work['evaluations']):
            limits.append('El contexto del modelo usa una selección acotada por tamaño; los totales provienen de Gold.')
        if facts['distinct_documents'] and not work['evidence']:
            limits.append('No hay citas disponibles en esta proyección; las cifras provienen de los agregados publicados.')
        limits = list(dict.fromkeys(limits))[:30]
        try:
            prose = dto(Prose, await case_infer(model, 'report', {'criterion':rule_context(criterion), 'facts':facts,
                'indicators':indicators, 'evidence':quotes, 'limitations':limits}))
            numbers = set(re.findall(r'-?\d+(?:[.,]\d+)?', encoded([facts, [row['value'] for row in work['indicators']], [row['quote'] for row in work['evidence']]]).decode()))
            if not set(re.findall(r'-?\d+(?:[.,]\d+)?', prose['title'] + ' ' + prose['summary'])) <= numbers:
                raise ValueError('Report prose introduced an unsupported number')
        except Exception:
            prose = {'title':criterion['label'], 'summary':'Informe basado en las observaciones y citas disponibles.'}
            limits.append('La prosa del modelo no estaba disponible o no se pudo verificar.')
        count = len(waiting) + len(output['investigation_requests'])
        value = dto(Report, {'report_id':digest([work['investigation_id'], 'report']), 'investigation_id':work['investigation_id'],
            'revision':work['report_revision']+1, 'config_revision':work['config_revision'],
            'criterion_id':criterion['criterion_id'], 'criterion_revision':criterion['revision'], **prose,
            'status':'awaiting_evidence' if count else 'failed' if facts['distinct_documents'] and facts['missing_evaluations'] == facts['distinct_documents'] else 'completed_with_limits' if limits else 'completed',
            'indicators':work['indicators'], 'evidence':work['evidence'], 'evaluations':work['evaluations'],
            'coverage':{'distinct_documents':facts['distinct_documents'], 'distinct_observations':facts['distinct_observations'],
                'query_ids':work['query_ids'], 'pending_requests':count,
                'failed_requests':sum(row['status'] in ('failed', 'cancelled') for row in work['requests']),
                'exhausted_budget':exhausted, 'limitations':list(dict.fromkeys(limits))[:30]}, **identity,
            'created_at':canonical_utc(timestamp.isoformat())})
        output['reports'].append(value)
        state['report_revision'] = value['revision']
        state['request_ids'].extend(row['request_id'] for row in output['investigation_requests'])
        if propose and work['evidence']:
            source_ids = {row['source_id'] for row in allowed}
            sources = [row for row in current['sources'] if row['source_id'] in source_ids]
            live_criterion = next((row for row in current['criteria'] if row['criterion_id'] == criterion['criterion_id']), None)
            configuration = {'sources':sources, 'queries':allowed, 'criteria':[live_criterion] if live_criterion else [],
                'connectors':[row for row in current['connectors'] if row['connector_id'] in {s['connector_id'] for s in sources}]}
            try:
                summary = {k:value[k] for k in ('report_id', 'investigation_id', 'title', 'summary', 'coverage')}
                response = await case_infer(model, 'propose', {'report':{**summary, 'indicators':indicators, 'evidence':quotes},
                    'configuration':configuration})
                candidates = case_proposals(response, {**configuration, 'revision':current['revision']}, work, identity)
                decisions = {row['proposal_id'] for row in current['proposal_decisions']}
                output['proposals'] = [row for row in candidates if row['proposal_id'] not in set(state['proposal_ids']) | decisions]
                # Bounded operational IDs; immutable Gold proposals own older dedup.
                state['proposal_ids'] = [row['proposal_id'] for row in candidates]
            except Exception:
                pass  # A failed proposal cannot activate configuration or invalidate a factual report.
        return {}

    graph = StateGraph(CaseGraphState)
    graph.add_node('evaluate', evaluate)
    graph.add_node('report', report)
    graph.add_edge(START, work['phase'])
    graph.add_edge('evaluate', END)
    graph.add_edge('report', END)
    await graph.compile().ainvoke({'output':output})
    return {**output, 'state':dto(CaseState, state)}
