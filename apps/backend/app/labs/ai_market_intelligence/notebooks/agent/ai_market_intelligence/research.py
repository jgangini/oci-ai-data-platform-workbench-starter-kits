"""Scoped research records, evidence versions and referenced result publication."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from pydantic import TypeAdapter

from .repository import Records, digest, encoded, json_text, utc_now
from .catalog import public_url
from .contracts import ClarificationAnswer, Clarifications, IntakeDecision, Keys, PendingClarification, ResearchRequest, ReportContent, SearchContext, RuntimeBinding


class PreparedClarificationPending(ValueError):
    pass


class PreparedClarificationIneligible(ValueError):
    pass


class ResearchRevisionMismatch(RuntimeError):
    pass


def runtime_bound_brief(brief, binding):
    return {**brief, **({'runtime_binding': binding} if binding is not None else {})}


def require_runtime_revision(run, binding, *, continuing=False):
    if binding is not None and (run['state'] == 'running' or continuing):
        saved = json.loads(run['brief_json']).get('runtime_binding')
        if saved != binding:
            raise ResearchRevisionMismatch('La investigación pendiente pertenece a otra versión o no conserva su versión. '
                'El trabajo guardado se mantiene. Pide al administrador retomar la versión original o abre una nueva conversación con la solicitud completa; '
                'no continué la búsqueda ni repetí capturas.')


def research_search_context(run):
    initial = json.loads(run['brief_json']).get('search_context')
    accepted = json.loads(run['decision_log_json']).get('search_context')
    if initial is not None and accepted is not None and initial != accepted:
        raise ValueError('The accepted search context differs from the initial framing')
    value = accepted if accepted is not None else initial
    return None if value is None else SearchContext.model_validate(value).model_dump(mode='json')


def _validate_run_replay(previous, answers, search_context):
    if answers is not None and json.loads(previous['brief_json']).get('clarifications', []) != answers:
        raise ValueError('Research clarification audit is immutable')
    if search_context is not None and research_search_context(previous) not in (None, search_context):
        raise ValueError('Research search framing is immutable')


def _clarification_clock(now=None):
    now = _reuse_utc(utc_now()) if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError('Clarification clock must be an explicit UTC datetime')
    return now


def run_is_paused(run):
    return _run_timing(run).get('clarification_paused_at') is not None


def _run_timing(run):
    value = json.loads(run['started_finished_at_json'])
    if not isinstance(value, dict):
        raise ValueError('Invalid research timing record')
    return value


def run_elapsed_seconds(run, now=None):
    now = _clarification_clock(now)
    timing = _run_timing(run)
    started = _reuse_utc(timing.get('started_at'))
    if started is None:
        raise ValueError('Research start must be an explicit UTC timestamp')
    elapsed = now - started
    if 'clarification_wait_microseconds' not in timing and 'clarification_paused_at' not in timing:
        return elapsed.total_seconds()
    wait = timing.get('clarification_wait_microseconds', 0)
    if type(wait) is not int or wait < 0 or wait > elapsed // timedelta(microseconds=1):
        raise ValueError('Invalid accumulated clarification wait')
    excluded = timedelta(microseconds=wait)
    if timing.get('clarification_paused_at') is not None:
        paused = _reuse_utc(timing['clarification_paused_at'])
        if paused is None or not started <= paused <= now:
            raise ValueError('Invalid clarification pause timestamp')
        excluded += now - paused
    if excluded > elapsed:
        raise ValueError('Clarification wait exceeds elapsed time')
    return (elapsed - excluded).total_seconds()


def _clarification_answers(value):
    answers = [answer.model_dump(mode='json') for answer in TypeAdapter(Clarifications).validate_python(value, strict=True)]
    if len({answer['request_id'] for answer in answers}) != len(answers):
        raise ValueError('Clarification request identities must be distinct')
    return answers


def _prepared_zero_effects(records, state, run):
    if run['schedule_id']:
        raise PreparedClarificationIneligible('Scheduled research cannot wait for clarification')
    if any(row['run_id'] == run['run_id'] for name in ('ai_mi_tool_runs', 'ai_mi_run_evidence', 'ai_mi_reports') for row in records.rows(name)):
        raise PreparedClarificationIneligible('Research with operations, evidence or reports cannot pause')
    if any(operation.get('run_id') == run['run_id'] for operation in state['operations'].values()):
        raise PreparedClarificationIneligible('Research with an admitted operation cannot pause')


def _prepared_record(run):
    log = json.loads(run['decision_log_json'])
    if not isinstance(log, dict) or not isinstance(log.get('decisions'), list):
        raise ValueError('Invalid research decision log')
    record = log.get('prepared_clarification')
    if record is not None:
        expected = {'version', 'state', 'pending', 'answers', 'events', 'last_resume', 'end_action'}
        if (not isinstance(record, dict) or set(record) not in (expected, expected | {'last_end'}) or type(record['version']) is not int
                or record['version'] != 1 or not isinstance(record['state'], str)
                or record['state'] not in {'waiting', 'resumed', 'ended'}
                or not isinstance(record['events'], list) or len(record['events']) > 8):
            raise ValueError('Invalid prepared clarification audit')
        _clarification_answers(record['answers'])
        if _prepared_events(record['events']) != record['answers']:
            raise ValueError('Prepared answers do not match the immutable event audit')
        _checked_resume_record(record['last_resume'])
        _clarification_end_context(record.get('last_end'))
        _prepared_record_state(record)
    return log, record


def _prepared_events(events):
    answers = []
    waiting, ended, previous_at = False, False, None
    for event in events:
        if not isinstance(event, dict) or event.get('action') not in ('pause', 'continue', 'cancel', 'replace', 'limit'):
            raise ValueError('Invalid prepared clarification event')
        action = event['action']
        extra = {'pending'} if action == 'pause' else {'answer'} if action == 'continue' else set()
        timestamp = _reuse_utc(event.get('at'))
        if set(event) != {'action', 'at'} | extra or timestamp is None:
            raise ValueError('Invalid prepared clarification event shape or timestamp')
        if ended or (previous_at is not None and timestamp < previous_at) or (action == 'pause') == waiting:
            raise ValueError('Invalid prepared clarification transition')
        if action == 'pause':
            PendingClarification.model_validate(event['pending'])
        if action == 'continue':
            answers.append(ClarificationAnswer.model_validate(event['answer']).model_dump(mode='json'))
        waiting, ended, previous_at = action == 'pause', action in ('cancel', 'replace', 'limit'), timestamp
    return answers


def _prepared_record_state(record):
    events = record['events']
    if not events:
        raise ValueError('Prepared clarification has no transition audit')
    expected = {'waiting': 'pause', 'resumed': 'continue', 'ended': record['end_action']}[record['state']]
    if events[-1]['action'] != expected:
        raise ValueError('Prepared clarification state differs from its last transition')
    latest_pending = next(event['pending'] for event in reversed(events) if event['action'] == 'pause')
    if record['pending'] != latest_pending:
        raise ValueError('Prepared clarification differs from its last saved question')
    if record['answers']:
        accepted = record['last_resume']
        if accepted is None or accepted['request']['answers'][-1] != record['answers'][-1]:
            raise ValueError('Prepared clarification lost its last accepted answer')


def _checked_resume_record(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'pending', 'request', 'intake'}:
        raise ValueError('Invalid accepted clarification audit')
    pending = PendingClarification.model_validate(value['pending']).model_dump(mode='json')
    request = ResearchRequest.model_validate(value['request']).model_dump(mode='json')
    _clarification_answers(request['answers'])
    original = {key: pending[key] for key in ResearchRequest.model_fields if key != 'answers'}
    if ({key: request[key] for key in original} != original or not request['answers']
            or request['answers'][:-1] != pending['answers']):
        raise ValueError('Accepted clarification differs from the original request')
    if value['intake'] is not None:
        IntakeDecision.model_validate(value['intake'])
    return value


def _clarification_end_context(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'request_id', 'question', 'intake'}:
        raise ValueError('Invalid ending clarification context')
    answer = ClarificationAnswer.model_validate({'request_id': value['request_id'], 'text': value['question']})
    if not answer.text.strip():
        raise ValueError('Ending clarification requires the literal user turn')
    intake = IntakeDecision.model_validate(value['intake']).model_dump(mode='json')
    return {'request_id': answer.request_id, 'question': answer.text, 'intake': intake}


def _ended_limit_replay(record, pending, request_id, text):
    accepted = record['last_resume']
    if record['end_action'] != 'limit' or accepted is None or accepted['pending'] != pending:
        return None
    if len(accepted['request']['answers']) != 3 or record['pending']['answers'] != accepted['request']['answers']:
        return None
    if accepted['request']['answers'][-1] != {'request_id': request_id, 'text': text}:
        return None
    context = record.get('last_end')
    if context and (context['request_id'], context['question']) != (request_id, text):
        return None
    return {'action': 'limit', 'intake': accepted['intake']}


def _finish_clarification_wait(run, now):
    run_elapsed_seconds(run, now)
    timing = _run_timing(run)
    paused = _reuse_utc(timing.get('clarification_paused_at'))
    if paused is None or now < paused:
        raise ValueError('Research has no matching clarification wait interval')
    timing['clarification_wait_microseconds'] += (now - paused) // timedelta(microseconds=1)
    timing['clarification_paused_at'] = None
    return timing


class Research:
    def __init__(self, journal, *, runtime_binding=None):
        self.journal, self.scope = journal, journal.scope
        self.runtime_binding = None if runtime_binding is None else RuntimeBinding.model_validate(runtime_binding).model_dump(mode='json')

    def check_runtime_revision(self, run_id, *, continuing=True):
        if self.runtime_binding is not None and run_id:
            value, _ = self.journal.read()
            run = Records(self.scope, value['tables']).get('ai_mi_research_runs', run_id)
            if run is None:
                raise ValueError('Unknown scoped research')
            require_runtime_revision(run, self.runtime_binding, continuing=continuing)

    def request_key(self, request_id, question):
        if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128
                or not isinstance(question, str) or not 1 <= len(question) <= 500 or not question.strip()):
            raise ValueError('A literal bounded research request is required')
        return self.scope.key(digest(['research-request', self.scope.__dict__, request_id, question]))

    def _bound_clarification(self, records, run_id, pending, *, attach=False):
        if records.scope != self.scope:
            raise ValueError('Clarification belongs to another authenticated scope')
        value = PendingClarification.model_validate(pending).model_dump(mode='json')
        if attach and value['prepared_run_id'] is None:
            value['prepared_run_id'] = run_id
        run = records.get('ai_mi_research_runs', run_id)
        if run is None:
            raise ValueError('Unknown scoped research')
        require_runtime_revision(run, self.runtime_binding)
        question = records.get('ai_mi_questions', run['question_id']) if run and run['question_id'] else None
        if not question or question['question_kind'] != 'conversation_research':
            raise PreparedClarificationIneligible('An owned conversational research is required')
        topic = json.loads(records.get('ai_mi_topics', run['topic_id'])['canonical_scope_json'])
        topic.pop('version', None)
        if (value['prepared_run_id'] != run_id or value['request_id'] != question['client_request_id']
                or value['question'] != question['question_text'] or value['topic'] != topic
                or value['scope_key'] != self.request_key(value['request_id'], value['question'])):
            raise ValueError('Prepared clarification differs from its original research request')
        answers = _clarification_answers(value['answers'])
        if any(answer['request_id'] == value['request_id'] or not answer['text'].strip() for answer in answers):
            raise ValueError('Invalid clarification answer identity or text')
        initial = _clarification_answers(json.loads(run['brief_json']).get('clarifications', []))
        return value, run, initial

    def _checked_pause(self, records, state, run_id, pending):
        value, run, initial = self._bound_clarification(records, run_id, pending)
        _prepared_zero_effects(records, state, run)
        _, record = _prepared_record(run)
        if run['state'] != 'running':
            raise PreparedClarificationIneligible('Research is no longer running')
        if (record is None or record['state'] != 'waiting' or record['pending'] != value
                or not run_is_paused(run) or value['answers'] != initial + record['answers']):
            raise ValueError('Prepared clarification is stale or not waiting')
        return value, run, record

    def paused_clarification(self, run_id):
        state, _ = self.journal.read()
        records = Records(self.scope, state['tables'])
        run = records.get('ai_mi_research_runs', run_id)
        if not run:
            raise ValueError('Unknown scoped research')
        _, record = _prepared_record(run)
        if record is None or record['state'] != 'waiting':
            if run_is_paused(run):
                raise ValueError('Research pause has no matching clarification audit')
            return None
        return self._checked_pause(records, state, run_id, record['pending'])[0]

    def check_clarification(self, run_id, pending):
        state, _ = self.journal.read()
        return self._checked_pause(Records(self.scope, state['tables']), state, run_id, pending)[0]

    def resumed_clarification(self, run_id, pending, request_id, text):
        state, _ = self.journal.read()
        value, run, _ = self._bound_clarification(Records(self.scope, state['tables']), run_id, pending)
        _, record = _prepared_record(run)
        if record is None or record['state'] == 'ended' or run['state'] == 'cancelled':
            return None
        accepted = record['last_resume']
        if accepted is None or accepted['intake'] is None or accepted['pending'] != value:
            return None
        if accepted['request']['answers'][-1] != {'request_id': request_id, 'text': text}:
            return None
        return {'request': accepted['request'], 'intake': accepted['intake']}

    def ended_clarification(self, run_id, pending, request_id, text):
        state, _ = self.journal.read()
        value, run, _ = self._bound_clarification(Records(self.scope, state['tables']), run_id, pending)
        _, record = _prepared_record(run)
        if record is None or record['state'] != 'ended' or run['state'] != 'cancelled' or run_is_paused(run):
            return None
        context = record.get('last_end')
        if context and record['pending'] == value and (context['request_id'], context['question']) == (request_id, text):
            return {'action': record['end_action'], 'intake': context['intake']}
        return _ended_limit_replay(record, value, request_id, text)

    def pause_clarification(self, run_id, pending, now=None):
        now = _clarification_clock(now)
        def change(records, state):
            value, run, initial = self._bound_clarification(records, run_id, pending, attach=True)
            _prepared_zero_effects(records, state, run)
            if run['state'] != 'running':
                raise PreparedClarificationIneligible('Research is no longer running')
            log, prior = _prepared_record(run)
            if value['answers'] != initial + (prior['answers'] if prior else []):
                raise ValueError('Prepared clarification changed its answer prefix')
            if prior and prior['state'] == 'waiting':
                return self._checked_pause(records, state, run_id, value)[0]
            if run_is_paused(run) or prior and prior['state'] == 'ended':
                raise ValueError('Prepared clarification cannot be paused in this state')
            run_elapsed_seconds(run, now)
            timing = json.loads(run['started_finished_at_json'])
            if now < _reuse_utc(timing['started_at']):
                raise ValueError('Clarification pause precedes research start')
            timing.update(clarification_paused_at=now.isoformat(),
                          clarification_wait_microseconds=timing.get('clarification_wait_microseconds', 0))
            record = prior or {'version': 1, 'answers': [], 'events': [], 'last_resume': None, 'last_end': None}
            record = {**record, 'state': 'waiting', 'pending': value, 'end_action': None,
                      'events': [*record['events'], {'action': 'pause', 'at': now.isoformat(), 'pending': value}]}
            if len(record['events']) > 8:
                raise ValueError('Prepared clarification audit limit reached')
            log['prepared_clarification'] = record
            records.upsert('ai_mi_research_runs', {**run, 'decision_log_json': json_text(log),
                           'started_finished_at_json': json_text(timing)})
            return value
        return self.journal.transact(change)

    def resume_clarification(self, run_id, pending, new_request, now=None, *, intake=None):
        now = _clarification_clock(now)
        request = ResearchRequest.model_validate(new_request).model_dump(mode='json')
        request['answers'] = _clarification_answers(request['answers'])
        intake = IntakeDecision.model_validate(intake).model_dump(mode='json') if intake is not None else None
        def change(records, state):
            value, run, initial = self._bound_clarification(records, run_id, pending)
            _prepared_zero_effects(records, state, run)
            log, record = _prepared_record(run)
            accepted = {'pending': value, 'request': request, 'intake': intake}
            if record and record['state'] == 'resumed' and record['last_resume'] == accepted:
                if run['state'] != 'running' or run_is_paused(run):
                    raise ValueError('Prepared resume no longer matches this research')
                return run_id
            value, run, record = self._checked_pause(records, state, run_id, value)
            original = {key: value[key] for key in ResearchRequest.model_fields if key != 'answers'}
            if ({key: request[key] for key in original} != original
                    or len(request['answers']) != len(value['answers']) + 1
                    or request['answers'][:-1] != value['answers']):
                raise ValueError('Resume needs exactly one new literal answer to the original request')
            added = request['answers'][-1]
            if added['request_id'] == value['request_id'] or not added['text'].strip():
                raise ValueError('Invalid resumed answer identity or text')
            timing = _finish_clarification_wait(run, now)
            log['prepared_clarification'] = {**record, 'state': 'resumed', 'last_resume': accepted,
                'answers': [*record['answers'], added],
                'events': [*record['events'], {'action': 'continue', 'at': now.isoformat(), 'answer': added}]}
            records.upsert('ai_mi_research_runs', {**run, 'decision_log_json': json_text(log),
                           'started_finished_at_json': json_text(timing)})
            return run_id
        return self.journal.transact(change)

    def end_clarification(self, run_id, pending, action, now=None, *, continuation=None):
        if not isinstance(action, str) or action not in {'cancel', 'replace', 'limit'}:
            raise ValueError('Unknown prepared clarification ending')
        now = _clarification_clock(now)
        continuation = _clarification_end_context(continuation)
        def change(records, state):
            value, run, _ = self._bound_clarification(records, run_id, pending)
            _prepared_zero_effects(records, state, run)
            log, record = _prepared_record(run)
            if (record and record['state'] == 'ended' and record['pending'] == value
                    and record['end_action'] == action and record.get('last_end') == continuation
                    and run['state'] == 'cancelled' and not run_is_paused(run)):
                return run_id
            value, run, record = self._checked_pause(records, state, run_id, value)
            timing = _finish_clarification_wait(run, now)
            timing['finished_at'] = now.isoformat()
            log['prepared_clarification'] = {**record, 'state': 'ended', 'end_action': action, 'last_end': continuation,
                'events': [*record['events'], {'action': action, 'at': now.isoformat()}]}
            records.upsert('ai_mi_research_runs', {**run, 'state': 'cancelled', 'termination_reason': 'unresolved',
                           'decision_log_json': json_text(log), 'started_finished_at_json': json_text(timing)})
            return run_id
        return self.journal.transact(change)

    def record_question(self, request_id, question, topic_scope, kind='conversation_research', now=None):
        if kind not in {'conversation_research', 'followup', 'administration'}:
            raise ValueError('Invalid question classification')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError('A stable client request identity is required')
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 500:
            raise ValueError('Question must contain 1–500 characters')
        if not isinstance(topic_scope, dict) or set(topic_scope) != {'entity', 'market', 'objective'}:
            raise ValueError('A canonical entity, market and objective are required')
        if any(not isinstance(value, str) or not 1 <= len(value.strip()) <= 500 for value in topic_scope.values()):
            raise ValueError('Topic scope is incomplete')
        question_id = self.scope.key(digest(['question', request_id]))
        topic_id = self.scope.key(digest(['topic', topic_scope]))
        timestamp = now or utc_now()

        def change(records, state):
            previous = records.get('ai_mi_questions', question_id)
            if previous:
                if (previous['question_text'], previous['topic_id'], previous['question_kind']) != (question, topic_id, kind):
                    raise ValueError('Client request identity was reused for a different question')
                return question_id, topic_id
            if not records.get('ai_mi_topics', topic_id):
                records.upsert('ai_mi_topics', {'topic_id': topic_id, 'canonical_scope_json': json_text(topic_scope),
                    'similarity_revision': 'canonical-scope-v1', 'created_at': timestamp})
            records.upsert('ai_mi_questions', {'question_id': question_id, 'topic_id': topic_id,
                'client_request_id': request_id, 'question_text': question, 'question_kind': kind,
                'asked_at': timestamp, 'match_reason_revision': 'canonical-scope-v1'})
            return question_id, topic_id

        return self.journal.transact(change)

    def bind_search_context(self, run_id, context):
        context = SearchContext.model_validate(context).model_dump(mode='json')
        def change(records, state):
            run = records.get('ai_mi_research_runs', run_id)
            if not run or run['state'] != 'running' or run_is_paused(run):
                raise ValueError('Search framing requires an owned active research run')
            previous = research_search_context(run)
            if previous is not None:
                if previous != context:
                    raise ValueError('The search country and language are immutable for this research')
                return context
            if any(row['run_id'] == run_id for row in records.rows('ai_mi_tool_runs')):
                raise ValueError('Search framing must be accepted before the first capture')
            log = json.loads(run['decision_log_json'])
            log['search_context'] = context
            records.upsert('ai_mi_research_runs', {**run, 'decision_log_json': encoded(log).decode()})
            return context
        return self.journal.transact(change)

    def prepare_run(self, question_id, window, brief, cap_microusd, *, max_steps=4, clarifications=None, search_context=None):
        start, end = _window(window)
        if type(cap_microusd) is not int or not 0 <= cap_microusd <= self.journal.budget:
            raise ValueError('Research cap is outside the bound budget')
        if type(max_steps) is not int or not 1 <= max_steps <= 10:
            raise ValueError('Research needs a bounded step count')
        if not isinstance(brief, str) or not 1 <= len(brief) <= 2000:
            raise ValueError('A research objective is required')
        answers = None
        if clarifications is not None:
            answers = _clarification_answers(clarifications)
        if search_context is not None:
            search_context = SearchContext.model_validate(search_context).model_dump(mode='json')
        run_id = self.scope.key(digest(['research', question_id]))

        def change(records, state):
            question = records.get('ai_mi_questions', question_id)
            if not question or question['question_kind'] != 'conversation_research':
                raise ValueError('A new research question is required')
            previous = records.get('ai_mi_research_runs', run_id)
            if previous:
                require_runtime_revision(previous, self.runtime_binding)
                _validate_run_replay(previous, answers, search_context)
                return run_id
            records.upsert('ai_mi_research_runs', {'run_id': run_id, 'question_id': question_id,
                'topic_id': question['topic_id'], 'state': 'running', 'reserved_microusd': cap_microusd,
                'plan_window_json': json_text({'start': start.isoformat(), 'end': end.isoformat()}),
                'brief_json': json_text(runtime_bound_brief({'objective': brief, **({'clarifications': answers} if answers else {}),
                    **({'search_context': search_context} if search_context is not None else {})}, self.runtime_binding)),
                'completion_policy_json': json_text({'max_steps': max_steps, 'max_elapsed_seconds': 300}),
                'started_finished_at_json': json_text({'started_at': utc_now(), 'finished_at': None}),
                'decision_log_json': json_text({'decisions': []})})
            return run_id

        return self.journal.transact(change)

    def add_evidence(self, run_id, tool_id, observations):
        if not isinstance(observations, list) or len(observations) > 50:
            raise ValueError('At most fifty normalized observations are admitted per tool')

        def change(records, state):
            tool = records.get('ai_mi_tool_runs', tool_id)
            run = records.get('ai_mi_research_runs', run_id)
            if not run or run['state'] != 'running' or not tool or tool['run_id'] != run_id or tool['state'] != 'succeeded':
                raise ValueError('Evidence must belong to a successful tool in the admitted run')
            evidence_ids = []
            start, end = _window({key: value for key, value in json.loads(run['plan_window_json']).items() if key in {'start', 'end'}})
            for observation in observations:
                if not publication_may_overlap(observation, start, end):
                    continue
                evidence_ids.append(_observation(records, self.scope, run_id, tool_id, observation))
            return evidence_ids

        return self.journal.transact(change)

    def evidence(self, run_id, *, records=None):
        if records is None:
            value, _ = self.journal.read()
            records = Records(self.scope, value['tables'])
        if records.scope != self.scope:
            raise ValueError('Evidence snapshot belongs to another authenticated scope')
        result = []
        for item in records.rows('ai_mi_run_evidence'):
            if item['run_id'] == run_id:
                document = records.get('ai_mi_documents', item['observation_key'])
                span = json.loads(item['quoted_span_json'])
                reused = item['tool_run_id'] is None and isinstance(span.get('reuse'), dict)
                capture = _capture_metadata(document, span)
                provenance = {key: value for key, value in capture['provenance'].items()
                              if key != 'provider_run_id'}
                if reused:
                    provenance.update(reused_publication_metadata=True, original_observed_at=capture['observed_at'])
                result.append({'evidence_id': item['evidence_key'], 'url': document['source_url'],
                    'published_at': document['published_at'], 'observed_at': capture['observed_at'],
                    'text': span['text'],
                    'metrics': {} if reused else json.loads(document['metrics_json']),
                    'assessment': json.loads(item['assessment_json']) if item['assessment_json'] else None,
                    'relevance': json.loads(item['relevance_json']) if item['relevance_json'] else None,
                    'provenance': provenance})
        return result

    def reference_urls(self, run_id, citations):
        citations = TypeAdapter(Keys).validate_python(citations, strict=True)
        if self.scope != self.journal.scope:
            raise ValueError('Reference lookup belongs to another authenticated scope')
        value, _ = self.journal.read()
        # Journal.read already validates the complete Records scope and references.
        tables = value['tables']
        if not isinstance(run_id, str) or run_id not in tables['ai_mi_research_runs']:
            raise ValueError('References require an existing owned research run')
        urls = []
        for key in citations:
            item = tables['ai_mi_run_evidence'].get(key)
            if not item or item['run_id'] != run_id:
                raise ValueError('Reference cites evidence outside this research')
            document = tables['ai_mi_documents'].get(item['observation_key'])
            if not document:
                raise ValueError('Reference has no scoped document')
            url = public_url(document['source_url'])
            if url not in urls:
                urls.append(url)
        return urls

    def reuse_publication_metadata(self, run_id, allowed_source_keys, *, scope_resolver, now):
        """Link recent, still-authorized YouTube metadata; never invent a capture."""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError('Publication reuse requires an explicit UTC clock')
        allowed = set(allowed_source_keys)
        def change(records, state):
            current = records.get('ai_mi_research_runs', run_id)
            if not current or current['state'] != 'running':
                raise ValueError('Publication reuse requires an owned running research')
            capacity = _reuse_capacity(records, current)
            if capacity == 0:
                return []
            window = {key: value for key, value in json.loads(current['plan_window_json']).items() if key in {'start', 'end'}}
            start, end = _window(window)
            candidates = []
            for item in records.rows('ai_mi_run_evidence'):
                original = records.get('ai_mi_research_runs', item['run_id'])
                if original['state'] != 'closed' or original['topic_id'] != current['topic_id']:
                    continue
                document = _reusable_publication(records, state, item, original, allowed, scope_resolver, start, end, now)
                if document:
                    candidates.append((document, item))
            candidates.sort(key=lambda pair: (-_reuse_utc(pair[0]['published_at']).timestamp(),
                                              -_reuse_utc(pair[0]['observed_at']).timestamp(),
                                              pair[0]['observation_key'], pair[1]['evidence_key']))
            selected = {}
            for document, item in candidates:
                selected.setdefault(document['observation_key'], (document, item))
            linked = []
            for document, item in selected.values():
                if len(linked) >= capacity:
                    break
                key = self.scope.key(digest([run_id, document['observation_key']]))
                if records.get('ai_mi_run_evidence', key):
                    continue
                records.upsert('ai_mi_run_evidence', {'evidence_key': key, 'run_id': run_id,
                    'tool_run_id': None, 'observation_key': document['observation_key'],
                    'quoted_span_json': json_text({'text': json.loads(item['quoted_span_json'])['text'],
                        'capture': {'observed_at': document['observed_at'],
                                    'provenance': json.loads(document['provenance_json'])},
                        'reuse': {'policy': 'youtube_publication_metadata_24h_v1',
                                  'original_run_id': item['run_id'], 'original_evidence_key': item['evidence_key'],
                                  'original_tool_run_id': item['tool_run_id'], 'original_observed_at': document['observed_at']}}),
                    'relevance_json': json_text({'state': 'not_evaluated'})})
                linked.append(key)
            return linked
        return self.journal.transact(change)

    def compare(self, run_id, *, scope_resolver=None):
        """Freeze a comparison of captured content, never a claim about market change."""
        def change(records, state):
            current = records.get('ai_mi_research_runs', run_id)
            if not current:
                raise ValueError('Comparison requires an owned research run')
            if current['comparison_context_json']:
                return json.loads(current['comparison_context_json'])
            started = datetime.fromisoformat(json.loads(current['started_finished_at_json'])['started_at'])
            candidates = []
            for row in records.rows('ai_mi_research_runs'):
                if row['run_id'] == run_id or row['topic_id'] != current['topic_id'] or row['state'] != 'closed':
                    continue
                finished = json.loads(row['started_finished_at_json']).get('finished_at')
                if finished and datetime.fromisoformat(finished) < started:
                    candidates.append((datetime.fromisoformat(finished), row['run_id'], row))
            baseline = max(candidates, key=lambda item: item[:2])[2] if candidates else None
            context = _comparison_context(records, state, current, baseline, scope_resolver)
            records.upsert('ai_mi_research_runs', {**current, 'comparison_context_json': json_text(context)})
            return context
        return self.journal.transact(change)

    def assess(self, run_id, assessments, decision):
        # Up to ten admitted steps, each with at most fifty observations.
        if not isinstance(assessments, list) or len(assessments) > 500:
            raise ValueError('Invalid evidence assessment count')
        if not isinstance(decision, dict) or set(decision) != {'continue', 'reason', 'gap'}:
            raise ValueError('A bounded continuation decision is required')
        if type(decision['continue']) is not bool or any(not isinstance(decision[k], str) or len(decision[k]) > 2000 for k in ('reason', 'gap')):
            raise ValueError('Invalid continuation decision')

        def change(records, state):
            run = records.get('ai_mi_research_runs', run_id)
            if not run or run['state'] != 'running':
                raise ValueError('Research is not running')
            for assessment in assessments:
                _assessment(records, run_id, assessment)
            log = json.loads(run['decision_log_json'])
            if len(log['decisions']) >= 10:
                raise RuntimeError('Research decision limit reached')
            log['decisions'].append({**decision, 'at': utc_now()})
            records.upsert('ai_mi_research_runs', {**run, 'decision_log_json': encoded(log).decode()})

        self.journal.transact(change)

    def publish(self, run_id, markdown, citations, findings, limitations, coverage, reason, chart=None):
        draft = ReportContent.model_validate({'answer_markdown': markdown, 'cited_evidence_ids': citations,
            'findings': findings, 'limitations': limitations, 'chart_request': None})
        markdown, citations = draft.answer_markdown, draft.cited_evidence_ids
        findings, limitations = [finding.model_dump() for finding in draft.findings], draft.limitations
        if reason not in {'sufficient', 'no_data', 'budget', 'time', 'step_limit', 'uncertain', 'unresolved'}:
            raise ValueError('A documented close reason is required')
        report_id = self.scope.key(digest(['report', run_id, 1]))

        def change(records, state):
            run = records.get('ai_mi_research_runs', run_id)
            if not run:
                raise ValueError('Unknown scoped research')
            prior = records.get('ai_mi_reports', report_id)
            if prior:
                if prior['markdown'] != markdown or json.loads(prior['cited_evidence_ids_json']) != citations:
                    raise ValueError('A published report is immutable')
                return report_id
            if run_is_paused(run):
                raise PreparedClarificationPending('Prepared research is paused for clarification')
            if run['state'] != 'running':
                raise ValueError('Research is already closed')
            for key in citations:
                evidence = records.get('ai_mi_run_evidence', key)
                if not evidence or evidence['run_id'] != run_id:
                    raise ValueError('Report cites evidence outside this research')
            validate_findings(findings, set(citations))
            records.upsert('ai_mi_reports', {'report_id': report_id, 'run_id': run_id, 'revision': 1,
                'status': 'published', 'markdown': markdown, 'published_at': utc_now(),
                'cited_evidence_ids_json': encoded(citations).decode(), 'findings_json': encoded(findings).decode(),
                'limitations_json': encoded(limitations).decode(), 'coverage_json': encoded(coverage).decode(),
                'chart_spec_json': encoded(chart).decode() if chart else None,
                'chart_decision_json': json_text({'included': bool(chart), 'reason': 'verified_metrics' if chart else 'not_required'})})
            timing = json.loads(run['started_finished_at_json'])
            timing['finished_at'] = utc_now()
            records.upsert('ai_mi_research_runs', {**run, 'state': 'closed', 'termination_reason': reason,
                'started_finished_at_json': encoded(timing).decode()})
            if run['schedule_id']:
                delivery_key = digest([report_id, self.scope.owner_id])
                records.upsert('ai_mi_notifications', {'notification_id': self.scope.key(delivery_key),
                    'report_id': report_id, 'delivery_key': delivery_key, 'created_at': utc_now()})
            return report_id

        return self.journal.transact(change)


def _reuse_capacity(records, run):
    evidence = [row for row in records.rows('ai_mi_run_evidence') if row['run_id'] == run['run_id']]
    reused = sum(row['tool_run_id'] is None and isinstance(json.loads(row['quoted_span_json']).get('reuse'), dict)
                 for row in evidence)
    admitted = sum(row['run_id'] == run['run_id'] for row in records.rows('ai_mi_tool_runs'))
    steps = json.loads(run['completion_policy_json'])['max_steps']
    future_slots = 50 * max(0, steps - admitted)
    return max(0, min(50 - reused, 500 - len(evidence) - future_slots))


def _reuse_utc(value):
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None and moment.utcoffset() == timedelta(0) else None


def _fresh_capture(value):
    observed = _reuse_utc(value['observed_at'])
    if observed is None or not isinstance(value['provenance'], dict):
        raise ValueError('Observation capture metadata requires an explicit UTC time and provenance')
    return {'observed_at': observed.isoformat(), 'provenance': value['provenance']}


def _capture_metadata(document, span):
    if 'capture' not in span:
        return {'observed_at': document['observed_at'], 'provenance': json.loads(document['provenance_json'])}
    capture = span['capture']
    if not isinstance(capture, dict) or set(capture) != {'observed_at', 'provenance'}:
        raise ValueError('Invalid evidence capture metadata')
    return _fresh_capture(capture)


def _newer_capture(prior, document, tool_id, capture):
    if prior['tool_run_id'] == tool_id:
        return False
    span = json.loads(prior['quoted_span_json'])
    if prior['tool_run_id'] is None and isinstance(span.get('reuse'), dict):
        return True
    previous = _reuse_utc(_capture_metadata(document, span)['observed_at'])
    return previous is None or _reuse_utc(capture['observed_at']) > previous


def _reuse_authorized(records, state, item, original, allowed, scope_resolver):
    tool = records.get('ai_mi_tool_runs', item['tool_run_id']) if item['tool_run_id'] else None
    if not tool:
        return False
    if (tool['run_id'], tool['state']) != (original['run_id'], 'succeeded') or tool['source_key'] not in allowed:
        return False
    operation = state['operations'].get(tool['tool_run_id'], {})
    if (operation.get('state'), operation.get('run_id')) != ('succeeded', original['run_id']):
        return False
    try:
        authority = scope_resolver(records, original['topic_id'], tool['source_key'], tool['target_id'])
    except ValueError:
        return False
    return authority['connector_revision'] == 'youtube_search' and operation.get('authorization_hash') == authority['authorization_hash']


def _reusable_publication(records, state, item, original, allowed, scope_resolver, start, end, now):
    if not _reuse_authorized(records, state, item, original, allowed, scope_resolver):
        return None
    document = records.get('ai_mi_documents', item['observation_key'])
    try:
        capture = _capture_metadata(document, json.loads(item['quoted_span_json']))
    except ValueError:
        return None
    published, observed = _reuse_utc(document['published_at']), _reuse_utc(capture['observed_at'])
    if published is None or observed is None:
        return None
    if not start <= published < end or published > now or not now - timedelta(days=1) <= observed <= now:
        return None
    return {**document, 'observed_at': capture['observed_at'], 'provenance_json': encoded(capture['provenance']).decode()}


def _captured_scope(records, state, run_id, scope_resolver):
    signature = set()
    tools = [row for row in records.rows('ai_mi_tool_runs') if row['run_id'] == run_id]
    if not tools or scope_resolver is None:
        return None
    for tool in tools:
        operation = state['operations'].get(tool['tool_run_id'], {})
        if tool['state'] != 'succeeded' or operation.get('state') != 'succeeded' or not tool['target_id']:
            return None
        try:
            scope = scope_resolver(records, records.get('ai_mi_research_runs', run_id)['topic_id'],
                                   tool['source_key'], tool['target_id'])
        except ValueError:
            return None
        if scope['connector_revision'] != 'website' or operation.get('authorization_hash') != scope['authorization_hash']:
            return None
        signature.add((tool['source_key'], tool['target_id'], scope['connector_revision'], scope['authorization_hash']))
    return signature


def _captured_documents(records, run_id):
    documents = {}
    for evidence in records.rows('ai_mi_run_evidence'):
        if evidence['run_id'] != run_id:
            continue
        document = records.get('ai_mi_documents', evidence['observation_key'])
        tool = records.get('ai_mi_tool_runs', evidence['tool_run_id'])
        target = records.get('ai_mi_source_targets', tool['target_id']) if tool else None
        if not target or document['source_url'] != target['canonical_url_or_id']:
            return None
        text = json.loads(evidence['quoted_span_json'])['text']
        documents.setdefault(document['source_url'], set()).add(digest(text))
    return documents


def _comparison_context(records, state, current, baseline, scope_resolver):
    context = {'version': 1, 'basis': 'captured_text', 'status': 'no_baseline', 'reason': 'no_prior_closed_run_for_topic',
        'baseline_run_id': baseline['run_id'] if baseline else None,
        'current_window': json.loads(current['plan_window_json']),
        'baseline_window': json.loads(baseline['plan_window_json']) if baseline else None,
        'newly_observed': [], 'changed': [], 'unchanged': [],
        'limitations': [
            'Solo compara contenido capturado en las URLs autorizadas, no todo el mercado ni toda la página.',
            'Una URL observada por primera vez no demuestra una publicación nueva.',
            'La ausencia de una URL en la captura actual no demuestra desaparición.',
            'Las frecuencias de captura no prueban tendencias de mercado.',
            'request_hash no acredita búsquedas equivalentes; búsquedas abiertas y series financieras no son comparables aquí.']}
    if not baseline:
        return context
    context.update(status='not_comparable', reason='capture_scope_not_verifiable')
    current_scope = _captured_scope(records, state, current['run_id'], scope_resolver)
    baseline_scope = _captured_scope(records, state, baseline['run_id'], scope_resolver)
    if not current_scope or not baseline_scope:
        return context
    if current_scope != baseline_scope:
        context['reason'] = 'source_target_or_profile_scope_changed'
        return context
    latest = _captured_documents(records, current['run_id'])
    previous = _captured_documents(records, baseline['run_id'])
    if latest is None or previous is None:
        context['reason'] = 'evidence_does_not_match_fixed_url_scope'
        return context
    context.update(status='comparable', reason='same_verified_fixed_url_capture_scope',
        coverage={'fixed_targets': len(current_scope), 'current_observed_urls': len(latest),
                  'baseline_observed_urls': len(previous)},
        newly_observed=sorted(latest.keys() - previous.keys()),
        changed=sorted(url for url in latest.keys() & previous.keys() if latest[url] != previous[url]),
        unchanged=sorted(url for url in latest.keys() & previous.keys() if latest[url] == previous[url]))
    return context


def publication_may_overlap(observation, start, end):
    published = observation.get('published_at')
    if published:
        moment = datetime.fromisoformat(published.replace('Z', '+00:00'))
        if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
            raise ValueError('Observation publication time must be UTC')
        return start <= moment < end
    provenance = observation.get('provenance') or {}
    if provenance.get('publication_date_precision') == 'day':
        day = datetime.strptime(provenance['publication_date_raw'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
        # Bounds cover possible timezones; they never become a publication timestamp.
        earliest, latest = day - timedelta(hours=14), day + timedelta(days=1, hours=14)
        return earliest < end and latest > start
    return True


def _window(value):
    if not isinstance(value, dict) or set(value) != {'start', 'end'}:
        raise ValueError('An explicit UTC research window is required')
    start, end = (datetime.fromisoformat(value[key].replace('Z', '+00:00')) for key in ('start', 'end'))
    if any(moment.tzinfo is None or moment.utcoffset() != timedelta(0) for moment in (start, end)):
        raise ValueError('Research window must be UTC')
    if not timedelta(0) < end - start <= timedelta(days=31):
        raise ValueError('Research window must span at most thirty-one days')
    return start, end


def _observation(records, scope, run_id, tool_id, value):
    expected = {'url', 'text', 'published_at', 'observed_at', 'provenance', 'metrics'}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('Invalid normalized observation')
    url = public_url(value['url'])
    if not isinstance(value['text'], str) or not 1 <= len(value['text']) <= 12000:
        raise ValueError('Observation text must be bounded')
    if not isinstance(value['provenance'], dict) or not isinstance(value['metrics'], dict):
        raise ValueError('Observation provenance and metrics must be objects')
    capture = _fresh_capture(value)
    content_hash = digest({'text': value['text'], 'published_at': value['published_at'], 'metrics': value['metrics']})
    document_key = scope.key(digest(['document', url]))
    observation_key = scope.key(digest([document_key, content_hash]))
    evidence_key = scope.key(digest([run_id, observation_key]))
    existing = records.get('ai_mi_documents', observation_key)
    records.upsert('ai_mi_documents', existing or {'observation_key': observation_key,
        'document_key': document_key, 'content_hash': content_hash, 'source_url': url,
        'published_at': value['published_at'], 'observed_at': capture['observed_at'],
        'content_ref': 'journal:' + observation_key, 'metrics_json': encoded(value['metrics']).decode(),
        'provenance_json': encoded(value['provenance']).decode()})
    prior = records.get('ai_mi_run_evidence', evidence_key)
    if prior and _newer_capture(prior, existing, tool_id, capture):
        records.upsert('ai_mi_run_evidence', {**prior, 'tool_run_id': tool_id,
            'quoted_span_json': json_text({'text': value['text'], 'capture': capture}),
            'relevance_json': json_text({'state': 'not_evaluated'}), 'assessment_json': None})
    elif not prior:
        records.upsert('ai_mi_run_evidence', {'evidence_key': evidence_key, 'run_id': run_id,
            'tool_run_id': tool_id, 'observation_key': observation_key,
            'quoted_span_json': json_text({'text': value['text'], 'capture': capture}),
            'relevance_json': json_text({'state': 'not_evaluated'})})
    return evidence_key


def _assessment(records, run_id, value):
    required = {'evidence_id', 'stance', 'reason', 'independence'}
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - required - {'relevance'}:
        raise ValueError('Invalid evidence assessment')
    if value['stance'] not in {'supports', 'refutes', 'context', 'unclear'} or value['independence'] not in {'known', 'unknown', 'duplicate'}:
        raise ValueError('Invalid evidence stance or independence')
    if not isinstance(value['reason'], str) or not 1 <= len(value['reason']) <= 2000:
        raise ValueError('An assessment needs a reason')
    relevance = value.get('relevance', 'uncertain')
    if relevance not in {'relevant', 'uncertain', 'irrelevant'}:
        raise ValueError('Invalid relevance assessment')
    row = records.get('ai_mi_run_evidence', value['evidence_id'])
    if not row or row['run_id'] != run_id:
        raise ValueError('Assessment references evidence outside this research')
    records.upsert('ai_mi_run_evidence', {**row,
        'assessment_json': json_text({key: value[key] for key in required}),
        'relevance_json': json_text({'state': relevance, 'reason': value['reason']})})


def validate_findings(findings, citations):
    if not isinstance(findings, list) or len(findings) > 20:
        raise ValueError('Invalid finding count')
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {'claim', 'kind', 'supports', 'refutes', 'limitations'}:
            raise ValueError('Invalid finding structure')
        if finding['kind'] not in {'observation', 'opinion', 'inference', 'hypothesis'}:
            raise ValueError('Invalid finding kind')
        for name in ('supports', 'refutes'):
            if not isinstance(finding[name], list) or not set(finding[name]).issubset(citations):
                raise ValueError('Finding references unverified evidence')
        if not finding['supports'] and not finding['refutes']:
            raise ValueError('A finding requires evidence; unknowns belong in limitations')
