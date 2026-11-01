"""Durable admission of a single selected provider operation, never a broadcast."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from .repository import Records, digest, encoded
from .connectors import PROFILES


class AdmissionBlocked(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        messages = {'uncertain': 'An uncertain provider start must be reconciled first',
                    'budget': 'Approved research budget exhausted', 'step_limit': 'Research step limit reached',
                    'time': 'Research time limit reached'}
        super().__init__(messages[reason])


class ProviderPending(RuntimeError):
    pass


class PaidOperations:
    def __init__(self, journal):
        self.journal = journal

    def authorize(self, run_id, source_key, target_id):
        value, _ = self.journal.read()
        records = Records(self.journal.scope, value['tables'])
        run = records.get('ai_mi_research_runs', run_id)
        if not run:
            raise ValueError('Research run is not admitted')
        return resolve_source_scope(records, run['topic_id'], source_key, target_id)

    def reserve(self, run_id, source_key, target_id, arguments, cap_microusd, purpose, *, authorization_hash):
        if type(cap_microusd) is not int or cap_microusd <= 0:
            raise ValueError('A positive integer reservation is required')
        if not isinstance(purpose, str) or not 1 <= len(purpose) <= 1000:
            raise ValueError('A bounded step purpose is required')
        request_hash = digest({'source_key': source_key, 'target_id': target_id, 'arguments': arguments})
        tool_id = self.journal.scope.key(digest([run_id, request_hash]))

        def change(records, state):
            run = records.get('ai_mi_research_runs', run_id)
            if not run or run['state'] != 'running':
                raise ValueError('Research run is not admitted')
            previous = records.get('ai_mi_tool_runs', tool_id)
            if previous:
                if previous['reserved_microusd'] != cap_microusd:
                    raise ValueError('An existing reservation cannot change')
                return tool_id
            if any(op['state'] in {'uncertain', 'submitted'} for op in state['operations'].values()):
                raise AdmissionBlocked('uncertain')
            _admit_scope_and_limits(records, run, source_key, target_id, authorization_hash)
            used = sum(op.get('spent_microusd') if op.get('spent_microusd') is not None
                       else op['reserved_microusd'] for op in state['operations'].values())
            run_used = sum(op.get('spent_microusd') if op.get('spent_microusd') is not None
                           else op['reserved_microusd'] for op in state['operations'].values()
                           if op['run_id'] == run_id)
            if used + cap_microusd > state['budget_microusd'] or run_used + cap_microusd > (run['reserved_microusd'] or 0):
                raise AdmissionBlocked('budget')
            records.upsert('ai_mi_tool_runs', {'tool_run_id': tool_id, 'run_id': run_id,
                'source_key': source_key, 'target_id': target_id, 'request_hash': request_hash,
                'state': 'reserved', 'reserved_microusd': cap_microusd,
                'step_purpose_json': encoded({'version': 1, 'purpose': purpose}).decode()})
            state['operations'][tool_id] = {'run_id': run_id, 'request_hash': request_hash,
                'reserved_microusd': cap_microusd, 'spent_microusd': None, 'state': 'reserved',
                'authorization_hash': authorization_hash}
            return tool_id

        return self.journal.transact(change)

    def claim_start(self, tool_id):
        # A retry may observe a prior start, but can never obtain its permit.
        permit = str(uuid4())

        def change(records, state):
            operation = state['operations'][tool_id]
            if operation['state'] != 'reserved':
                return None
            if any(key != tool_id and op['state'] in {'submitted', 'uncertain'}
                   for key, op in state['operations'].items()):
                raise AdmissionBlocked('uncertain')
            row = records.get('ai_mi_tool_runs', tool_id)
            run = records.get('ai_mi_research_runs', row['run_id'])
            if not run or run['state'] != 'running':
                raise ValueError('Research run is no longer admitted')
            _admit_scope_and_limits(records, run, row['source_key'], row['target_id'],
                                    operation.get('authorization_hash'), existing_tool_id=tool_id)
            operation.update(state='submitted', permit=permit)
            records.upsert('ai_mi_tool_runs', {**row, 'state': 'submitted'})
            return permit

        return self.journal.transact(change)

    def record_provider(self, tool_id, permit, provider_run_id, status, receipt):
        if not isinstance(provider_run_id, str) or not 1 <= len(provider_run_id) <= 256:
            raise ValueError('A provider run identity is required')
        if status not in {'running', 'succeeded', 'failed', 'aborted', 'timed_out'}:
            raise ValueError('Unknown provider state')
        if len(encoded(receipt)) > 32768:
            raise ValueError('Provider receipt exceeds its metadata limit')

        def change(records, state):
            operation = state['operations'][tool_id]
            if operation.get('permit') != permit or operation['state'] == 'reserved':
                raise ValueError('Provider result belongs to another admission')
            if operation.get('provider_run_id') not in (None, provider_run_id):
                raise ValueError('Provider run identity changed')
            if operation.get('cost_overrun'):
                raise ValueError('A provider cost overrun requires operator reconciliation')
            if operation['state'] in {'succeeded', 'failed', 'aborted', 'timed_out'} and operation['state'] != status:
                raise ValueError('A terminal provider state cannot be replaced')
            operation.update(state=status, provider_run_id=provider_run_id)
            row = records.get('ai_mi_tool_runs', tool_id)
            records.upsert('ai_mi_tool_runs', {**row, 'state': status, 'provider_run_id': provider_run_id,
                'provider_receipt_json': encoded(receipt).decode()})

        self.journal.transact(change)

    def mark_uncertain(self, tool_id, permit):
        def change(records, state):
            operation = state['operations'][tool_id]
            if operation.get('permit') != permit:
                raise ValueError('Provider failure belongs to another admission')
            if operation['state'] != 'submitted':
                return
            operation['state'] = 'uncertain'
            row = records.get('ai_mi_tool_runs', tool_id)
            records.upsert('ai_mi_tool_runs', {**row, 'state': 'uncertain'})

        self.journal.transact(change)

    def record_cost(self, tool_id, provider_run_id, spent_microusd):
        if type(spent_microusd) is not int or spent_microusd < 0:
            raise ValueError('A measured nonnegative cost is required')

        def change(records, state):
            operation = state['operations'][tool_id]
            if operation.get('provider_run_id') != provider_run_id or operation['state'] not in {'succeeded', 'failed', 'aborted', 'timed_out'}:
                raise ValueError('Only terminal, identified operations have a final cost')
            if operation.get('spent_microusd') not in (None, spent_microusd):
                raise ValueError('Final provider cost changed; reconcile manually')
            operation['spent_microusd'] = spent_microusd
            if spent_microusd > operation['reserved_microusd']:
                operation['state'] = 'uncertain'
                operation['cost_overrun'] = True
            row = records.get('ai_mi_tool_runs', tool_id)
            records.upsert('ai_mi_tool_runs', {**row, 'spent_microusd': spent_microusd,
                'state': operation['state']})

        self.journal.transact(change)


async def execute_selected(ledger, tool_id, arguments, call_actor):
    """Transport must perform one invocation without automatic paid retries."""
    value, _ = ledger.journal.read()
    row = value['tables']['ai_mi_tool_runs'][tool_id]
    if digest({'source_key': row['source_key'], 'target_id': row['target_id'], 'arguments': arguments}) != row['request_hash']:
        raise ValueError('Provider arguments differ from the admitted request')
    permit = ledger.claim_start(tool_id)
    if permit is None:
        return {'state': 'reconcile', 'tool_run_id': tool_id}
    try:
        result = await call_actor(arguments)
        status = {'READY': 'running', 'RUNNING': 'running', 'SUCCEEDED': 'succeeded',
                  'FAILED': 'failed', 'ABORTED': 'aborted', 'TIMED-OUT': 'timed_out'}[result['status']]
        # Store only selected metadata; content belongs in documents/evidence.
        receipt = {key: result[key] for key in ('runId', 'actorId', 'status', 'startedAt', 'finishedAt') if key in result}
        ledger.record_provider(tool_id, permit, result['runId'], status, receipt)
        if status in {'succeeded', 'failed', 'aborted', 'timed_out'} and result.get('spent_microusd') is not None:
            ledger.record_cost(tool_id, result['runId'], result['spent_microusd'])
        return result
    except Exception:
        ledger.mark_uncertain(tool_id, permit)
        raise RuntimeError('Provider outcome is uncertain; reconcile the recorded operation before another start') from None


def resolve_source_scope(records, topic_id, source_key, target_id):
    """Resolve the same approved scope at planning, reservation and paid start."""
    source = records.get('ai_mi_sources', source_key)
    if not source or source['status'] != 'active' or source['connector_revision'] not in PROFILES:
        raise ValueError('The selected source has no active reviewed connector')
    policy = json.loads(source['allowed_scope_json'] or '{}')
    if source['connector_revision'] == 'x_search' and not policy.get('allow_discovery'):
        raise ValueError('Global X search needs explicit discovery approval; a registered account is insufficient')
    urls, target, link_scope = policy.get('urls', []), None, None
    if target_id:
        target = records.get('ai_mi_source_targets', target_id)
        if not target or target['source_key'] != source_key or target['status'] != 'active':
            raise ValueError('Target is outside the selected approved source')
        link = next((link for link in records.rows('ai_mi_topic_sources')
                     if link['topic_id'] == topic_id and link['target_id'] == target_id and link['status'] == 'active'), None)
        if not link:
            raise ValueError('Target approval for this topic was revoked')
        link_scope = json.loads(link['approved_scope_json'])
        urls = link_scope.get('urls', [])
    elif not policy.get('allow_discovery'):
        raise ValueError('Unregistered searches are outside the approved scope')
    finance = _financial_authority(source['connector_revision'], policy, target, link_scope)
    authorization = {'source_key': source_key, 'profile': PROFILES[source['connector_revision']],
        'connector_revision': source['connector_revision'], 'policy': policy, 'topic_id': topic_id,
        'target_id': target_id, 'target_url': target['canonical_url_or_id'] if target else None,
        'approved_scope': link_scope, **finance}
    return {'connector_revision': source['connector_revision'], 'urls': urls,
            'financial': finance.get('financial_scope'), 'authorization_hash': digest(authorization)}


def _financial_authority(profile, policy, target, link_scope):
    """Additional authority fields; preserve the existing nonfinancial hash contract."""
    if profile not in {'yahoo_finance', 'world_bank'}:
        return {}
    if not target or not link_scope or policy.get('allow_discovery'):
        raise ValueError('Financial research requires an approved topic target without discovery')
    target_scope = json.loads(target['allowed_scope_json'] or '{}')
    keys = ('tickers',) if profile == 'yahoo_finance' else ('countries', 'indicator_codes')
    scopes = (policy, target_scope, link_scope)
    result = {}
    for key in keys:
        values = [scope.get(key) for scope in scopes]
        if any(not isinstance(value, list) or not value or len(value) > 50
               or any(not isinstance(item, str) or not 1 <= len(item) <= 100 for item in value) for value in values):
            raise ValueError('Financial source, target and topic need explicit bounded allowlists')
        result[key] = sorted(set(values[0]).intersection(*map(set, values[1:])))
        if not result[key]:
            raise ValueError('Financial selections no longer share an approved scope')
    return {'financial_scope': result}


def _admit_scope_and_limits(records, run, source_key, target_id, authorization_hash, *, existing_tool_id=None):
    current = resolve_source_scope(records, run['topic_id'], source_key, target_id)
    if current['authorization_hash'] != authorization_hash:
        raise ValueError('Approved source scope changed; compile and authorize again before capture')
    _admit_schedule(records, run, target_id)
    policy = json.loads(run['completion_policy_json'])
    if sum(row['run_id'] == run['run_id'] and row['tool_run_id'] != existing_tool_id
           for row in records.rows('ai_mi_tool_runs')) >= policy['max_steps']:
        raise AdmissionBlocked('step_limit')
    started = datetime.fromisoformat(json.loads(run['started_finished_at_json'])['started_at'])
    if (datetime.now(timezone.utc) - started).total_seconds() >= policy['max_elapsed_seconds']:
        raise AdmissionBlocked('time')


def _admit_schedule(records, run, target_id):
    if run['schedule_id']:
        schedule = records.get('ai_mi_schedules', run['schedule_id'])
        revision = json.loads(run['brief_json']).get('schedule_revision')
        if not schedule or schedule['status'] != 'active' or schedule['revision'] != revision:
            raise ValueError('Schedule approval was changed or revoked')
        plan = json.loads(schedule['approved_plan_budget_json'])
        if target_id not in plan['target_ids'] or run['reserved_microusd'] != plan['cap_microusd']:
            raise ValueError('Scheduled admission differs from its approved plan')
