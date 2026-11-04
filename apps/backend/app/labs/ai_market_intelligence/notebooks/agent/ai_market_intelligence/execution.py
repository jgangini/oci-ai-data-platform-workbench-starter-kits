"""Durable admission of a single selected provider operation, never a broadcast."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from .runtime_core import ProviderPending, ProviderStatusUnavailable, ProviderReceiptUnverified, ProviderStartRejected
from .repository import Records, digest, encoded
from .connectors import PROFILES, ConnectorUnavailable, require_available
from .catalog import discovery_permitted, PUBLIC_SEARCH_PROFILES, public_url, requested_public_urls
from .contracts import ScheduleResearch
from .serp import serp_receipt_matches
from .research import PreparedClarificationPending, run_elapsed_seconds, run_is_paused, research_search_context, _capture_metadata, _window


class AdmissionBlocked(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        messages = {'uncertain': 'An uncertain provider start must be reconciled first',
                    'budget': 'Approved research budget exhausted', 'step_limit': 'Research step limit reached',
                    'time': 'Research time limit reached'}
        super().__init__(messages[reason])








class ScheduleScopeError(ValueError):
    pass


class SourceScopeError(ValueError):
    user_message = 'La fuente o el alcance autorizado cambiaron. Revisa la selección antes de continuar; no se inició otra captura.'




class TargetAlreadyInvestigated(RuntimeError):
    pass


class PaidOperations:
    def __init__(self, journal):
        self.journal = journal

    def authorize(self, run_id, source_key, target_id, *, requested_urls=None):
        value, _ = self.journal.read()
        records = Records(self.journal.scope, value['tables'])
        run = records.get('ai_mi_research_runs', run_id)
        if not run:
            raise ValueError('Research run is not admitted')
        return resolve_source_scope(records, run['topic_id'], source_key, target_id, run_id=run_id, requested_urls=requested_urls)

    def reserve(self, run_id, source_key, target_id, arguments, cap_microusd, purpose, *, authorization_hash):
        if type(cap_microusd) is not int or cap_microusd <= 0:
            raise ValueError('A positive integer reservation is required')
        if not isinstance(purpose, str) or not 1 <= len(purpose) <= 1000:
            raise ValueError('A bounded step purpose is required')
        request_hash = digest({'source_key': source_key, 'target_id': target_id, 'arguments': arguments})
        tool_id = self.journal.scope.key(digest([run_id, request_hash]))
        request_metadata = _request_metadata(arguments)
        requested_urls = request_metadata.get('requested_urls')

        def change(records, state):
            run = records.get('ai_mi_research_runs', run_id)
            if not run or run['state'] != 'running':
                raise ValueError('Research run is not admitted')
            if run_is_paused(run):
                raise PreparedClarificationPending('Prepared research is paused for clarification')
            previous = records.get('ai_mi_tool_runs', tool_id)
            if previous:
                if previous['reserved_microusd'] != cap_microusd:
                    raise ValueError('An existing reservation cannot change')
                return tool_id
            if any(op['state'] in {'uncertain', 'submitted'} for op in state['operations'].values()):
                raise AdmissionBlocked('uncertain')
            _admit_scope_and_limits(records, run, source_key, target_id, authorization_hash, requested_urls=requested_urls)
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
                'step_purpose_json': encoded({'version': 1, 'purpose': purpose, **request_metadata}).decode()})
            state['operations'][tool_id] = {'run_id': run_id, 'request_hash': request_hash,
                'reserved_microusd': cap_microusd, 'spent_microusd': None, 'state': 'reserved',
                'authorization_hash': authorization_hash}
            if requested_urls is not None:
                state['operations'][tool_id]['requested_urls'] = requested_urls
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
                                    operation.get('authorization_hash'), existing_tool_id=tool_id,
                                    requested_urls=operation.get('requested_urls'))
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
            if operation.get('cost_overrun') or operation.get('cost_discrepancy'):
                raise ValueError('A provider cost overrun or discrepancy requires operator reconciliation')
            if operation['state'] in {'succeeded', 'failed', 'aborted', 'timed_out'} and operation['state'] != status:
                raise ValueError('A terminal provider state cannot be replaced')
            operation.update(state=status, provider_run_id=provider_run_id)
            row = records.get('ai_mi_tool_runs', tool_id)
            updated_receipt = merge_provider_receipt(receipt, json.loads(row['provider_receipt_json'] or '{}'))
            records.upsert('ai_mi_tool_runs', {**row, 'state': status, 'provider_run_id': provider_run_id,
                'provider_receipt_json': encoded(updated_receipt).decode()})

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

    def reject_start(self, tool_id, permit, reason):
        if reason != 'build_not_found':
            raise ValueError('Only a verified pre-start rejection can release its reservation')
        def change(records, state):
            operation = state['operations'][tool_id]
            if operation.get('permit') != permit or operation.get('provider_run_id'):
                raise ValueError('A provider run or another permit cannot be marked as not started')
            if operation.get('start_rejection') == reason and operation['state'] == 'failed':
                return
            if operation['state'] not in {'submitted', 'uncertain'}:
                raise ValueError('No unresolved start to reject')
            operation.update(state='failed', start_rejection=reason, spent_microusd=0)
            row = records.get('ai_mi_tool_runs', tool_id)
            records.upsert('ai_mi_tool_runs', {**row, 'state': 'failed', 'spent_microusd': 0,
                'provider_receipt_json': encoded({'rejected_before_start': reason, 'run_created': False}).decode()})
        self.journal.transact(change)

    def record_retrieval(self, tool_id, provider_run_id, coverage):
        validate_retrieval_coverage(coverage)
        def change(records, state):
            operation = state['operations'].get(tool_id, {})
            row = records.get('ai_mi_tool_runs', tool_id)
            if (not row or operation.get('state') != 'succeeded' or row['state'] != 'succeeded'
                    or operation.get('provider_run_id') != provider_run_id or row['provider_run_id'] != provider_run_id):
                raise ValueError('Retrieval coverage requires the same successful provider run')
            receipt = json.loads(row['provider_receipt_json'])
            if 'retrieval_coverage' in receipt and receipt['retrieval_coverage'] != coverage:
                raise ValueError('Recorded retrieval coverage is immutable')
            receipt['retrieval_coverage'] = coverage
            if len(encoded(receipt)) > 32768:
                raise ValueError('Provider receipt exceeds its metadata limit')
            records.upsert('ai_mi_tool_runs', {**row, 'provider_receipt_json': encoded(receipt).decode()})
        self.journal.transact(change)

    def record_cost(self, tool_id, provider_run_id, spent_microusd):
        if type(spent_microusd) is not int or spent_microusd < 0:
            raise ValueError('A measured nonnegative cost is required')

        def change(records, state):
            operation = state['operations'][tool_id]
            if operation.get('provider_run_id') != provider_run_id or operation['state'] not in {'succeeded', 'failed', 'aborted', 'timed_out'}:
                raise ValueError('Only terminal, identified operations have a final cost')
            if operation.get('spent_microusd') not in (None, spent_microusd):
                operation['cost_discrepancy'] = {'previous_microusd': operation['spent_microusd'],
                    'observed_microusd': spent_microusd, 'provider_state': operation['state']}
                operation['state'] = 'uncertain'
                row = records.get('ai_mi_tool_runs', tool_id)
                records.upsert('ai_mi_tool_runs', {**row, 'state': 'uncertain'})
                return False
            operation['spent_microusd'] = spent_microusd
            if spent_microusd > operation['reserved_microusd']:
                operation['state'] = 'uncertain'
                operation['cost_overrun'] = True
            row = records.get('ai_mi_tool_runs', tool_id)
            records.upsert('ai_mi_tool_runs', {**row, 'spent_microusd': spent_microusd,
                'state': operation['state']})
            return True

        if not self.journal.transact(change):
            raise ProviderReceiptUnverified('Provider cost changed; the discrepancy is recorded and new captures are blocked pending reconciliation')


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
    except ProviderStartRejected as error:
        ledger.reject_start(tool_id, permit, error.code)
        raise
    except Exception:
        ledger.mark_uncertain(tool_id, permit)
        raise RuntimeError('Provider outcome is uncertain; reconcile the recorded operation before another start') from None


def resolve_source_scope(records, topic_id, source_key, target_id, *, run_id=None, requested_urls=None):
    """Resolve the same approved scope at planning, reservation and paid start."""
    try:
        return _resolved_source_scope(records, topic_id, source_key, target_id, run_id=run_id, requested_urls=requested_urls)
    except ValueError as error:
        raise SourceScopeError(str(error)) from None


def _active_source_policy(records, topic_id, source_key):
    source = records.get('ai_mi_sources', source_key)
    if not source or source['status'] != 'active' or source['connector_revision'] not in PROFILES:
        raise ValueError('The selected source has no active reviewed connector')
    policy = json.loads(source['allowed_scope_json'] or '{}')
    if not isinstance(policy, dict):
        raise ValueError('The approved source policy must be an object')
    if source['connector_revision'] in PUBLIC_SEARCH_PROFILES and not discovery_permitted(policy, topic_id):
        raise ValueError('Public search needs explicit discovery approval; a registered account is insufficient')
    return source, policy


def _approved_source_target(records, topic_id, source_key, target_id):
    target = records.get('ai_mi_source_targets', target_id)
    if not target or target['source_key'] != source_key or target['status'] != 'active':
        raise ValueError('Target is outside the selected approved source')
    link = next((link for link in records.rows('ai_mi_topic_sources')
                 if link['topic_id'] == topic_id and link['target_id'] == target_id and link['status'] == 'active'), None)
    if not link:
        raise ValueError('Target approval for this topic was revoked')
    link_scope = json.loads(link['approved_scope_json'])
    if not isinstance(link_scope, dict):
        raise ValueError('The approved target scope must be an object')
    return target, link_scope


def _resolved_source_scope(records, topic_id, source_key, target_id, *, run_id=None, requested_urls=None):
    source, policy = _active_source_policy(records, topic_id, source_key)
    urls, target, link_scope, pointed, discovered = policy.get('urls', []), None, None, None, None
    if target_id:
        target, link_scope = _approved_source_target(records, topic_id, source_key, target_id)
        urls = link_scope.get('urls', [])
    elif source['connector_revision'] == 'website':
        pointed, discovered = _reader_authority(records, topic_id, run_id, source, requested_urls)
        urls = pointed['urls'] if pointed else [item['url'] for item in discovered['evidence']]
    elif not discovery_permitted(policy, topic_id):
        raise ValueError('Unregistered searches are outside the approved scope')
    finance = _financial_authority(source['connector_revision'], policy, target, link_scope)
    authorization = {'source_key': source_key, 'profile': PROFILES[source['connector_revision']],
        'connector_revision': source['connector_revision'], 'policy': policy, 'topic_id': topic_id,
        'target_id': target_id, 'target_url': target['canonical_url_or_id'] if target else None,
        'approved_scope': link_scope, **finance}
    if pointed:
        authorization['pointed_request'] = pointed
    if discovered:
        authorization['discovered_request'] = discovered
    context = _search_authority_context(source, records, topic_id, run_id)
    if context is not None:
        authorization['search_context'] = context
    result = {'connector_revision': source['connector_revision'], 'urls': urls,
              'financial': finance.get('financial_scope'), 'authorization_hash': digest(authorization)}
    if pointed:
        result['pointed_urls'] = urls
    if discovered:
        result['discovered_urls'] = urls
    if context is not None:
        result['search_context'] = context
    return result


def _reader_authority(records, topic_id, run_id, source, requested_urls):
    run = records.get('ai_mi_research_runs', run_id) if run_id else None
    question = records.get('ai_mi_questions', run['question_id']) if run and run['question_id'] else None
    if question and requested_public_urls(question['question_text']):
        return _pointed_authority(records, topic_id, run_id), None
    return None, _discovered_read_authority(records, topic_id, run_id, source, requested_urls)


def _search_authority_context(source, records, topic_id, run_id):
    if source['connector_revision'] not in {'web_search_localized', 'web_search_serp'}:
        return None
    run = records.get('ai_mi_research_runs', run_id) if run_id else None
    if not run or run['topic_id'] != topic_id:
        raise ValueError('Localized search requires this research framing')
    context = research_search_context(run)
    if context is None:
        raise ValueError('Localized search needs an accepted country and language')
    return context


def _request_metadata(arguments):
    if arguments.get('actor') == PROFILES['web_search_serp']['actor']:
        return {'search_call': arguments, 'search_navigation': 'tbs' not in arguments.get('input', {})}
    if arguments.get('actor') == PROFILES['web_search_localized']['actor']:
        return {'search_navigation': True} if not {'dateFrom', 'dateTo'}.intersection(arguments.get('input', {})) else {}
    if arguments.get('actor') != PROFILES['website']['actor']:
        return {}
    values = arguments.get('input', {}).get('startUrls')
    if not isinstance(values, list) or not 1 <= len(values) <= 10:
        raise ValueError('Reader calls require explicit bounded URLs')
    return {'requested_urls': sorted({public_url(item['url']) for item in values})}


def _discovery_evidence(records, run):
    context = research_search_context(run)
    if context is None:
        return []
    window = json.loads(run['plan_window_json'])
    start, end = _window({key: window[key] for key in ('start', 'end')})
    requested = {'provider': 'google', 'countryCode': context['country_code'], 'languageCode': context['language_code'],
                 'dateFrom': start.date().isoformat(), 'dateTo': end.date().isoformat()}
    matches = []
    for evidence in records.rows('ai_mi_run_evidence'):
        proof = _discovery_proof(records, run, evidence, requested)
        if proof is not None:
            matches.append(proof)
    return matches


def _discovery_proof(records, run, evidence, requested):
    if evidence['run_id'] != run['run_id'] or not evidence['tool_run_id']:
        return None
    if json.loads(evidence['relevance_json'] or '{}').get('state') == 'irrelevant':
        return None
    tool = records.get('ai_mi_tool_runs', evidence['tool_run_id'])
    if not tool or tool['run_id'] != run['run_id'] or tool['state'] != 'succeeded':
        return None
    source = records.get('ai_mi_sources', tool['source_key'])
    if not source or source['status'] != 'active' or source['connector_revision'] not in {'web_search_localized', 'web_search_serp'}:
        return None
    policy = json.loads(source['allowed_scope_json'] or '{}')
    if not discovery_permitted(policy, run['topic_id']):
        return None
    document = records.get('ai_mi_documents', evidence['observation_key'])
    capture = _capture_metadata(document, json.loads(evidence['quoted_span_json']))
    profile = PROFILES[source['connector_revision']]
    if not _discovery_receipt_matches(capture['provenance'], tool, requested, profile):
        return None
    return {'url': public_url(document['source_url']), 'evidence_key': evidence['evidence_key'],
        'tool_run_id': tool['tool_run_id'], 'source_key': tool['source_key'], 'request_hash': tool['request_hash'],
        'provider_run_id': tool['provider_run_id'], 'profile': profile, 'policy': policy}


def _discovery_receipt_matches(provenance, tool, requested, profile):
    purpose = json.loads(tool['step_purpose_json'] or '{}')
    receipt = json.loads(tool['provider_receipt_json'] or '{}')
    if receipt.get('runId') != tool['provider_run_id'] or receipt.get('status') != 'SUCCEEDED':
        return False
    if profile['actor'] == PROFILES['web_search_serp']['actor']:
        return serp_receipt_matches(provenance, tool, purpose, requested, profile)
    if purpose.get('search_navigation') is True:
        requested = {**requested, 'dateFrom': None, 'dateTo': None}
    expected = {'content_basis': 'search_title_and_snippet', 'source': profile['actor'],
        'actor_build': profile['build'], 'provider_run_id': tool['provider_run_id'],
        'requested_search': requested, 'effective_search': requested}
    return all(provenance.get(key) == value for key, value in expected.items())


def _discovered_read_authority(records, topic_id, run_id, reader, requested_urls):
    run = records.get('ai_mi_research_runs', run_id) if run_id else None
    if (not run or run['state'] != 'running' or run['topic_id'] != topic_id or run['schedule_id']
            or reader['source_key'] != records.scope.key(digest(['source', 'website']))
            or json.loads(reader['allowed_scope_json'] or '{}') != {'version': 1, 'allow_discovery': False}):
        raise ValueError('Discovered pages require the active research and unchanged managed reader')
    if not isinstance(requested_urls, list) or not 1 <= len(requested_urls) <= 3:
        raise ValueError('Select one to three organic search results from this research')
    selected = {public_url(url) for url in requested_urls}
    proofs = {}
    for item in sorted(_discovery_evidence(records, run), key=lambda item: item['evidence_key']):
        if item['url'] in selected:
            proofs.setdefault(item['url'], item)
    if set(proofs) != selected:
        raise ValueError('Requested page has no current successful organic discovery in this research')
    return {'scope': records.scope.__dict__, 'run_id': run_id, 'topic_id': topic_id,
            'evidence': [proofs[url] for url in sorted(proofs)]}


def discovered_read_options(records, topic_id, run_id):
    try:
        return _discovered_read_options(records, topic_id, run_id)
    except ValueError as error:
        raise SourceScopeError(str(error)) from None


def _discovered_read_options(records, topic_id, run_id):
    run = records.get('ai_mi_research_runs', run_id)
    if not run or run['state'] != 'running' or run['topic_id'] != topic_id or run['schedule_id']:
        return None
    reader = records.get('ai_mi_sources', records.scope.key(digest(['source', 'website'])))
    if not reader or reader['status'] != 'active' or reader['connector_revision'] != 'website':
        return None
    visited = {url for row in records.rows('ai_mi_tool_runs') if row['run_id'] == run_id and row['source_key'] == reader['source_key']
               for url in json.loads(row['step_purpose_json'] or '{}').get('requested_urls', [])}
    urls = sorted({item['url'] for item in _discovery_evidence(records, run)} - visited)
    if not urls:
        return None
    _discovered_read_authority(records, topic_id, run_id, reader, urls[:3])
    return {'source_key': reader['source_key'], 'name': reader['name'], 'profile_id': 'website',
            'targets': [], 'discovered_urls': urls}


def _pointed_authority(records, topic_id, run_id):
    run = records.get('ai_mi_research_runs', run_id) if run_id else None
    if not run or (run['state'], run['topic_id']) != ('running', topic_id) or run['schedule_id']:
        raise ValueError('A direct URL requires an owned running conversational research')
    question = records.get('ai_mi_questions', run['question_id']) if run['question_id'] else None
    if not question or (question['question_kind'], question['topic_id']) != ('conversation_research', topic_id):
        raise ValueError('A direct URL requires its original research question')
    urls = sorted(requested_public_urls(question['question_text']))
    if not 1 <= len(urls) <= 10:
        raise ValueError('The original question must select between one and ten public HTTPS URLs')
    return {'run_id': run_id, 'question_id': question['question_id'], 'urls': urls}


def _financial_authority(profile, policy, target, link_scope):
    """Additional authority fields; preserve the existing nonfinancial hash contract."""
    if profile not in {'yahoo_finance', 'yahoo_history', 'world_bank'}:
        return {}
    if not target or not link_scope or policy.get('allow_discovery'):
        raise ValueError('Financial research requires an approved topic target without discovery')
    target_scope = json.loads(target['allowed_scope_json'] or '{}')
    keys = ('countries', 'indicator_codes') if profile == 'world_bank' else ('tickers',)
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


def _admit_scope_and_limits(records, run, source_key, target_id, authorization_hash, *, existing_tool_id=None, requested_urls=None):
    if run_is_paused(run):
        raise PreparedClarificationPending('Prepared research is paused for clarification')
    _admit_schedule(records, run, source_key, target_id)
    current = resolve_source_scope(records, run['topic_id'], source_key, target_id, run_id=run['run_id'], requested_urls=requested_urls)
    require_available(current['connector_revision'])
    if current['authorization_hash'] != authorization_hash:
        raise SourceScopeError('Approved source scope changed; compile and authorize again before capture')
    _admit_target_once(records, run['run_id'], target_id, existing_tool_id)
    if current.get('pointed_urls'):
        _admit_pointed_once(records, run['run_id'], source_key, existing_tool_id)
    if current.get('discovered_urls'):
        _admit_discovered_once(records, run['run_id'], source_key, current['discovered_urls'], existing_tool_id)
    policy = json.loads(run['completion_policy_json'])
    if sum(row['run_id'] == run['run_id'] and row['tool_run_id'] != existing_tool_id
           for row in records.rows('ai_mi_tool_runs')) >= policy['max_steps']:
        raise AdmissionBlocked('step_limit')
    if run_elapsed_seconds(run, datetime.now(timezone.utc)) >= policy['max_elapsed_seconds']:
        raise AdmissionBlocked('time')


def _admit_discovered_once(records, run_id, source_key, urls, existing_tool_id):
    prior = {url for row in records.rows('ai_mi_tool_runs')
             if row['run_id'] == run_id and row['source_key'] == source_key and row['tool_run_id'] != existing_tool_id
             for url in json.loads(row['step_purpose_json'] or '{}').get('requested_urls', [])}
    if prior.intersection(urls):
        raise TargetAlreadyInvestigated('Estas páginas ya se consultaron en la investigación; no se repetirá su captura.')


def _admit_target_once(records, run_id, target_id, existing_tool_id):
    if target_id is not None and any(row['run_id'] == run_id and row['target_id'] == target_id
            and row['tool_run_id'] != existing_tool_id for row in records.rows('ai_mi_tool_runs')):
        raise TargetAlreadyInvestigated('Este destino ya se consultó en esta investigación. No se repetirá la captura; el informe conservará las brechas pendientes.')


def _admit_pointed_once(records, run_id, source_key, existing_tool_id):
    if any((row['run_id'], row['source_key'], row['target_id']) == (run_id, source_key, None)
           and row['tool_run_id'] != existing_tool_id for row in records.rows('ai_mi_tool_runs')):
        raise TargetAlreadyInvestigated('Las páginas indicadas ya se consultaron en esta investigación. No se repetirá la captura puntual.')


def _schedule_selection_binding(records, topic_id, source_key, target_id, window_days):
    try:
        source = records.get('ai_mi_sources', source_key)
        if source and source['connector_revision'] in {'web_search_localized', 'web_search_serp'}:
            raise ScheduleScopeError('La búsqueda localizada y la lectura de sus resultados todavía necesitan un alcance programado verificable. Puedes realizar esta investigación a demanda; esta combinación aún no se programó.')
        scope = resolve_source_scope(records, topic_id, source_key, target_id)
        profile = scope['connector_revision']
        require_available(profile)
        if target_id is None:
            if profile not in PUBLIC_SEARCH_PROFILES:
                raise ValueError('A scheduled public capability needs a reviewed search profile')
        elif profile in PUBLIC_SEARCH_PROFILES:
            raise ValueError('A registered account cannot authorize an open scheduled search')
        elif profile == 'website':
            target = records.get('ai_mi_source_targets', target_id)
            if not scope['urls'] or target['canonical_url_or_id'] not in scope['urls']:
                raise ValueError('A scheduled website needs an approved public URL')
            for url in scope['urls']:
                public_url(url)
        if profile == 'youtube_search' and window_days > 7:
            raise ScheduleScopeError('La búsqueda de YouTube admite una ventana de hasta siete días; revisa el periodo antes de confirmar.')
        return scope['authorization_hash']
    except (ValueError, ConnectorUnavailable) as error:
        if isinstance(error, ScheduleScopeError):
            raise
        raise ScheduleScopeError('Las fuentes seleccionadas ya no tienen el alcance autorizado para esta programación. Revisa la selección antes de confirmar.') from None


def schedule_scope_bindings(records, topic_id, plan):
    fields = ('window_days', 'target_ids', 'source_keys', 'cap_microusd')
    try:
        research = ScheduleResearch.model_validate({key: plan[key] for key in fields if key in plan}).model_dump()
    except (ValueError, TypeError):
        raise ScheduleScopeError('La programación necesita fuentes, ventana y presupuesto válidos.') from None
    bindings = {}
    for target_id in research['target_ids']:
        target = records.get('ai_mi_source_targets', target_id)
        if not target:
            raise ScheduleScopeError('El destino seleccionado no pertenece a esta base de investigación.')
        bindings['target:' + target_id] = _schedule_selection_binding(records, topic_id, target['source_key'], target_id, research['window_days'])
    for source_key in research['source_keys']:
        bindings['source:' + source_key] = _schedule_selection_binding(records, topic_id, source_key, None, research['window_days'])
    return bindings


def validate_scheduled_research(records, topic_id, plan):
    if not isinstance(plan, dict) or type(plan.get('version', 1)) is not int or plan.get('version', 1) not in (1, 2):
        raise ScheduleScopeError('La versión del plan programado necesita revisión.')
    version = plan.get('version', 1)
    required = {'window_days', 'target_ids', 'cap_microusd'}
    if version == 2:
        required |= {'version', 'source_keys', 'source_authorizations'}
    if not required.issubset(plan) or set(plan) - required - {'version'}:
        raise ScheduleScopeError('El plan programado contiene campos incompletos o no autorizados.')
    current = schedule_scope_bindings(records, topic_id, plan)
    if version == 2 and plan['source_authorizations'] != current:
        raise ScheduleScopeError('Las fuentes o su versión cambiaron desde el resumen aprobado. Revisa la programación antes de ejecutarla.')
    return dict(plan)


def _admit_schedule(records, run, source_key, target_id):
    if run['schedule_id']:
        schedule = records.get('ai_mi_schedules', run['schedule_id'])
        revision = json.loads(run['brief_json']).get('schedule_revision')
        if not schedule or schedule['status'] != 'active' or schedule['revision'] != revision:
            raise ValueError('Schedule approval was changed or revoked')
        plan = validate_scheduled_research(records, run['topic_id'], json.loads(schedule['approved_plan_budget_json']))
        selected = source_key in plan.get('source_keys', []) if target_id is None else target_id in plan['target_ids']
        if not selected or run['reserved_microusd'] != plan['cap_microusd']:
            raise ValueError('Scheduled admission differs from its approved plan')


def merge_provider_receipt(receipt, saved):
    result = dict(receipt)
    if "retrieval_coverage" in saved:
        if "retrieval_coverage" in receipt and receipt["retrieval_coverage"] != saved["retrieval_coverage"]:
            raise ValueError("Recorded retrieval coverage is immutable")
        result["retrieval_coverage"] = saved["retrieval_coverage"]
    if len(encoded(result)) > 32768:
        raise ValueError("Provider receipt exceeds its metadata limit")
    return result


def validate_retrieval_coverage(coverage):
    if (not isinstance(coverage, dict) or set(coverage) != {'version', 'total_rows', 'accepted_rows', 'failed_pages'}
            or type(coverage['version']) is not int or coverage['version'] != 1
            or any(type(coverage[k]) is not int or not 0 <= coverage[k] <= 50 for k in ('total_rows', 'accepted_rows'))
            or not isinstance(coverage['failed_pages'], list)
            or coverage['accepted_rows'] + len(coverage['failed_pages']) != coverage['total_rows']):
        raise ValueError('Invalid retrieval coverage')
    for page in coverage["failed_pages"]:
        validate_failed_page(page)


def validate_failed_page(page):
    from .catalog import public_url
    if (not isinstance(page, dict) or set(page) != {'url', 'reason', 'http_status'}
            or page['reason'] not in {'http_error', 'empty_content', 'origin_changed'}
            or type(page['http_status']) is not int
            or not (400 <= page['http_status'] <= 599 if page['reason'] == 'http_error' else 200 <= page['http_status'] <= 299)
            or public_url(page['url']) != page['url']):
        raise ValueError('Invalid failed-page coverage')
