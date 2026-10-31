"""Human-confirmed schedules and source targets; recurrence is a product rule."""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .repository import Records, digest, encoded, json_text, utc_now
from .contracts import Recurrence, ScheduleResearch


def next_occurrence(plan, after):
    plan = Recurrence.model_validate(plan).model_dump()
    days = plan['weekdays']
    zone = ZoneInfo(plan['timezone'])
    if after.tzinfo is None:
        raise ValueError('Schedule clock must be timezone-aware')
    local = after.astimezone(zone)
    for offset in range(9):
        day = local.date() + timedelta(days=offset)
        if day.isoweekday() not in days:
            continue
        moment = datetime.combine(day, time(plan['hour'], plan['minute']), zone)
        utc = moment.astimezone(timezone.utc)
        # Skip nonexistent DST wall times; choose fold=0 once for overlaps.
        if utc > after and utc.astimezone(zone).replace(tzinfo=None) == moment.replace(tzinfo=None):
            return utc.isoformat()
    raise ValueError('No valid occurrence in the next nine days')


class Proposals:
    def __init__(self, journal):
        self.journal, self.scope = journal, journal.scope

    def recurring_questions(self, topic_id, now=None):
        clock = now or datetime.now(timezone.utc)
        value, _ = self.journal.read()
        records = Records(self.scope, value['tables'])
        matching = [row['question_id'] for row in records.rows('ai_mi_questions')
                    if row['topic_id'] == topic_id and row['question_kind'] == 'conversation_research'
                    and clock - timedelta(days=30) <= datetime.fromisoformat(row['asked_at']) <= clock]
        return sorted(set(matching))

    def propose_schedule(self, topic_id, *, direct=False, now=None):
        clock = now or datetime.now(timezone.utc)
        questions = self.recurring_questions(topic_id, clock)
        if not direct and len(questions) < 3:
            return None
        proposal_id = self.scope.key(digest(['schedule-proposal', topic_id]))

        def change(records, state):
            if any(row['topic_id'] == topic_id and row['status'] == 'active' for row in records.rows('ai_mi_schedules')):
                return None
            prior = records.get('ai_mi_schedule_proposals', proposal_id)
            if prior and (prior['state'] in {'pending', 'awaiting_confirmation', 'approved'}
                          or prior['suppress_until'] and datetime.fromisoformat(prior['suppress_until']) > clock):
                return proposal_id if prior['state'] in {'pending', 'awaiting_confirmation'} else None
            records.upsert('ai_mi_schedule_proposals', {'proposal_id': proposal_id, 'topic_id': topic_id,
                'state': 'pending', 'trigger_question_ids_json': encoded(questions).decode(),
                'proposed_plan_json': json_text({'revision': 1, 'plan': None, 'presented_hash': None})})
            return proposal_id

        return self.journal.transact(change)

    def present_schedule(self, proposal_id, recurrence, research_plan):
        next_occurrence(recurrence, datetime.now(timezone.utc))
        research_plan = ScheduleResearch.model_validate(research_plan).model_dump()
        if research_plan['cap_microusd'] > self.journal.budget:
            raise ValueError('Scheduled budget exceeds the admitted budget')

        def change(records, state):
            row = records.get('ai_mi_schedule_proposals', proposal_id)
            if not row or row['state'] not in {'pending', 'awaiting_confirmation'}:
                raise ValueError('No pending schedule proposal')
            for target_id in research_plan['target_ids']:
                target = records.get('ai_mi_source_targets', target_id)
                links = records.rows('ai_mi_topic_sources')
                if not target or target['status'] != 'active' or not any(link['topic_id'] == row['topic_id'] and link['target_id'] == target_id and link['status'] == 'active' for link in links):
                    raise ValueError('Schedule target is outside the approved topic base')
            previous = json.loads(row['proposed_plan_json'])
            plan = {'recurrence': recurrence, 'research': research_plan}
            summary_hash = digest(plan)
            records.upsert('ai_mi_schedule_proposals', {**row, 'state': 'awaiting_confirmation',
                'proposed_plan_json': json_text({'revision': previous['revision'] + 1,
                    'plan': plan, 'presented_hash': summary_hash})})
            return summary_hash

        return self.journal.transact(change)

    def save_schedule_draft(self, proposal_id, draft):
        from .contracts import ScheduleDraft
        validated = ScheduleDraft.model_validate(draft).model_dump(exclude_none=True)
        def change(records, state):
            row = records.get('ai_mi_schedule_proposals', proposal_id)
            if not row or row['state'] not in {'pending', 'awaiting_confirmation'}:
                raise ValueError('No pending schedule proposal')
            plan = json.loads(row['proposed_plan_json'])
            plan.update(draft=validated, presented_hash=None)
            records.upsert('ai_mi_schedule_proposals', {**row, 'state': 'pending', 'proposed_plan_json': json_text(plan)})
        self.journal.transact(change)

    def decide_schedule(self, proposal_id, presented_hash, accept, user_request_id, now=None):
        clock = now or datetime.now(timezone.utc)
        if type(accept) is not bool or not isinstance(user_request_id, str) or not 1 <= len(user_request_id) <= 128:
            raise ValueError('An explicit authenticated user action is required')

        def change(records, state):
            row = records.get('ai_mi_schedule_proposals', proposal_id)
            if not row:
                raise ValueError('Unknown schedule proposal')
            proposal = json.loads(row['proposed_plan_json'])
            if accept and (row['state'] != 'awaiting_confirmation' or proposal['presented_hash'] != presented_hash):
                raise ValueError('Only the exact presented, complete plan can be approved')
            if not accept and row['state'] not in {'pending', 'awaiting_confirmation'}:
                raise ValueError('Proposal is no longer pending')
            approval = {'actor_id': self.scope.owner_id, 'request_id': user_request_id,
                        'at': clock.isoformat(), 'presented_hash': presented_hash, 'accepted': accept}
            records.upsert('ai_mi_schedule_proposals', {**row, 'state': 'approved' if accept else 'rejected',
                'decision_json': json_text(approval), 'suppress_until': None if accept else (clock + timedelta(days=30)).isoformat()})
            if not accept:
                return None
            plan = proposal['plan']
            schedule_id = self.scope.key(digest(['schedule', proposal_id]))
            records.upsert('ai_mi_schedules', {'schedule_id': schedule_id, 'topic_id': row['topic_id'],
                'proposal_id': proposal_id, 'revision': proposal['revision'], 'status': 'active',
                'recurrence_timezone_json': json_text(plan['recurrence']),
                'approved_plan_budget_json': json_text(plan['research']), 'approval_json': json_text(approval),
                'next_due_at': next_occurrence(plan['recurrence'], clock)})
            return schedule_id

        return self.journal.transact(change)

    def read_notification(self, notification_id):
        def change(records, state):
            row = records.get('ai_mi_notifications', notification_id)
            if not row:
                raise ValueError('Unknown notification for this recipient')
            records.upsert('ai_mi_notifications', {**row, 'read_at': row['read_at'] or utc_now()})
            return records.get('ai_mi_reports', row['report_id'])

        return self.journal.transact(change)

    def claim_due(self, schedule_id, now=None, *, resume_pending=False):
        clock = now or datetime.now(timezone.utc)
        def change(records, state):
            schedule = records.get('ai_mi_schedules', schedule_id)
            if not schedule or schedule['status'] != 'active' or not schedule['next_due_at']:
                return None
            pending = [row for row in records.rows('ai_mi_research_runs') if row['schedule_id'] == schedule_id and row['state'] == 'running']
            if len(pending) > 1:
                raise RuntimeError('Multiple unfinished scheduled runs require reconciliation')
            if pending:
                return pending[0]['run_id'] if resume_pending else None
            due = datetime.fromisoformat(schedule['next_due_at'])
            if due > clock:
                return None
            occurrence = digest([schedule_id, schedule['revision'], due.isoformat()])
            run_id = self.scope.key(digest(['scheduled-run', occurrence]))
            plan = json.loads(schedule['approved_plan_budget_json'])
            window = {'start': (clock - timedelta(days=plan['window_days'])).isoformat(), 'end': clock.isoformat()}
            records.upsert('ai_mi_research_runs', {'run_id': run_id, 'schedule_id': schedule_id,
                'occurrence_key': occurrence, 'topic_id': schedule['topic_id'], 'state': 'running',
                'reserved_microusd': plan['cap_microusd'], 'plan_window_json': json_text(window),
                'brief_json': json_text({'objective': 'Investigación del tema y fuentes aprobados en la programación', 'schedule_revision': schedule['revision']}),
                'completion_policy_json': json_text({'max_steps': 4, 'max_elapsed_seconds': 300}),
                'decision_log_json': json_text({'decisions': []}),
                'started_finished_at_json': json_text({'started_at': clock.isoformat(), 'finished_at': None})})
            recurrence = json.loads(schedule['recurrence_timezone_json'])
            recurrence.pop('version', None)
            records.upsert('ai_mi_schedules', {**schedule, 'next_due_at': next_occurrence(recurrence, clock)})
            return run_id
        return self.journal.transact(change)
