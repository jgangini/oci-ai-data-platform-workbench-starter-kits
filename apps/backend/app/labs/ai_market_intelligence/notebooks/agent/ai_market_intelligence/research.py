"""Scoped research records, evidence versions and referenced result publication."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .repository import Records, digest, encoded, json_text, utc_now
from .catalog import public_url
from .contracts import ReportDraft


class Research:
    def __init__(self, journal):
        self.journal, self.scope = journal, journal.scope

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

    def prepare_run(self, question_id, window, brief, cap_microusd, *, max_steps=4):
        start, end = _window(window)
        if type(cap_microusd) is not int or not 0 <= cap_microusd <= self.journal.budget:
            raise ValueError('Research cap is outside the bound budget')
        if type(max_steps) is not int or not 1 <= max_steps <= 10:
            raise ValueError('Research needs a bounded step count')
        if not isinstance(brief, str) or not 1 <= len(brief) <= 2000:
            raise ValueError('A research objective is required')
        run_id = self.scope.key(digest(['research', question_id]))

        def change(records, state):
            question = records.get('ai_mi_questions', question_id)
            if not question or question['question_kind'] != 'conversation_research':
                raise ValueError('A new research question is required')
            previous = records.get('ai_mi_research_runs', run_id)
            if previous:
                return run_id
            records.upsert('ai_mi_research_runs', {'run_id': run_id, 'question_id': question_id,
                'topic_id': question['topic_id'], 'state': 'running', 'reserved_microusd': cap_microusd,
                'plan_window_json': json_text({'start': start.isoformat(), 'end': end.isoformat()}),
                'brief_json': json_text({'objective': brief}),
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
                published = observation.get('published_at')
                if published:
                    moment = datetime.fromisoformat(published.replace('Z', '+00:00'))
                    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
                        raise ValueError('Observation publication time must be UTC')
                    if not start <= moment < end:
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
                result.append({'evidence_id': item['evidence_key'], 'url': document['source_url'],
                    'published_at': document['published_at'], 'observed_at': document['observed_at'],
                    'text': json.loads(item['quoted_span_json'])['text'],
                    'metrics': json.loads(document['metrics_json']),
                    'assessment': json.loads(item['assessment_json']) if item['assessment_json'] else None,
                    'provenance': {key: value for key, value in json.loads(document['provenance_json']).items()
                                   if key != 'provider_run_id'}})
        return result

    def assess(self, run_id, assessments, decision):
        if not isinstance(assessments, list) or len(assessments) > 50:
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
        draft = ReportDraft.model_validate({'answer_markdown': markdown, 'cited_evidence_ids': citations,
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
            if run['state'] != 'running':
                raise ValueError('Research is already closed')
            for key in citations:
                evidence = records.get('ai_mi_run_evidence', key)
                if not evidence or evidence['run_id'] != run_id:
                    raise ValueError('Report cites evidence outside this research')
            _findings(findings, set(citations))
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
    content_hash = digest({'text': value['text'], 'published_at': value['published_at'], 'metrics': value['metrics']})
    document_key = scope.key(digest(['document', url]))
    observation_key = scope.key(digest([document_key, content_hash]))
    evidence_key = scope.key(digest([run_id, observation_key]))
    existing = records.get('ai_mi_documents', observation_key)
    records.upsert('ai_mi_documents', existing or {'observation_key': observation_key,
        'document_key': document_key, 'content_hash': content_hash, 'source_url': url,
        'published_at': value['published_at'], 'observed_at': utc_now(),
        'content_ref': 'journal:' + observation_key, 'metrics_json': encoded(value['metrics']).decode(),
        'provenance_json': encoded(value['provenance']).decode()})
    if not records.get('ai_mi_run_evidence', evidence_key):
        records.upsert('ai_mi_run_evidence', {'evidence_key': evidence_key, 'run_id': run_id,
            'tool_run_id': tool_id, 'observation_key': observation_key,
            'quoted_span_json': json_text({'text': value['text']}),
            'relevance_json': json_text({'state': 'not_evaluated'})})
    return evidence_key


def _assessment(records, run_id, value):
    if not isinstance(value, dict) or set(value) != {'evidence_id', 'stance', 'reason', 'independence'}:
        raise ValueError('Invalid evidence assessment')
    if value['stance'] not in {'supports', 'refutes', 'context', 'unclear'} or value['independence'] not in {'known', 'unknown', 'duplicate'}:
        raise ValueError('Invalid evidence stance or independence')
    if not isinstance(value['reason'], str) or not 1 <= len(value['reason']) <= 2000:
        raise ValueError('An assessment needs a reason')
    row = records.get('ai_mi_run_evidence', value['evidence_id'])
    if not row or row['run_id'] != run_id:
        raise ValueError('Assessment references evidence outside this research')
    records.upsert('ai_mi_run_evidence', {**row, 'assessment_json': json_text(value)})


def _findings(findings, citations):
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
