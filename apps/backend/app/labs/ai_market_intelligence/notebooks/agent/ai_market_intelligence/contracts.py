"""Model outputs propose decisions; these types grant no permissions or effects."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Short = Annotated[str, Field(min_length=1, max_length=500)]
Reason = Annotated[str, Field(max_length=2000)]
Keys = Annotated[list[Short], Field(max_length=50)]


class Output(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class TopicScope(Output):
    entity: Short
    market: Short
    objective: Short


class IntakeDecision(Output):
    intent: Literal['research', 'followup', 'source_request', 'source_decision',
                    'schedule_request', 'schedule_decision', 'read_report', 'cancel']
    canonical_question: Short
    topic: TopicScope | None
    window_spec: Literal['last_7_days', 'rolling_24h', 'calendar_today']
    missing_fields: Annotated[list[Short], Field(max_length=5)]
    user_question: Short | None
    selected_source_key: Short | None = None


class StepArguments(Output):
    query: Annotated[str, Field(max_length=500)]
    urls: Annotated[list[Short], Field(max_length=10)]
    max_items: Annotated[int, Field(ge=1, le=50)]
    sort: Literal['Latest', 'Top']


class ResearchPlan(Output):
    next_action: Literal['execute', 'reuse', 'clarify', 'close', 'request_extension']
    source_key: Short | None
    target_id: Short | None
    arguments: StepArguments | None
    purpose: Reason
    expected_contribution: Reason
    user_question: Short | None


class Assessment(Output):
    evidence_id: Short
    stance: Literal['supports', 'refutes', 'context', 'unclear']
    reason: Reason
    independence: Literal['known', 'unknown', 'duplicate']


class EvidenceAssessment(Output):
    assessments: Annotated[list[Assessment], Field(max_length=50)]
    continue_research: bool
    reason: Reason
    gap: Reason


class Finding(Output):
    claim: Reason
    kind: Literal['observation', 'opinion', 'inference', 'hypothesis']
    supports: Keys
    refutes: Keys
    limitations: Annotated[list[Reason], Field(max_length=10)]


class ChartRequest(Output):
    kind: Literal['bar', 'line', 'table']
    metric: Literal['observations', 'likes', 'close', 'value']
    group_by: Literal['source', 'date']


class ReportDraft(Output):
    answer_markdown: Annotated[str, Field(min_length=1, max_length=20000)]
    cited_evidence_ids: Keys
    findings: Annotated[list[Finding], Field(max_length=20)]
    limitations: Annotated[list[Reason], Field(max_length=20)]
    chart_request: ChartRequest | None

    @field_validator('answer_markdown')
    @classmethod
    def plain_markdown(cls, value):
        if '<' in value or '```' in value:
            raise ValueError('A bounded Markdown answer without raw HTML/code is required')
        return value


class OpportunityDecision(Output):
    kind: Literal['none', 'source', 'schedule']
    source_key: Short | None
    target_url: Short | None
    evidence_ids: Keys
    reason: Reason
    user_question: Short | None


class Recurrence(Output):
    weekdays: Annotated[list[Annotated[int, Field(ge=1, le=7)]], Field(min_length=1, max_length=7)]
    hour: Annotated[int, Field(ge=0, le=23)]
    minute: Annotated[int, Field(ge=0, le=59)]
    timezone: Short

    @field_validator('weekdays')
    @classmethod
    def unique_weekdays(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('Schedule weekdays must be distinct ISO weekday numbers')
        return value


class ScheduleResearch(Output):
    window_days: Annotated[int, Field(ge=1, le=31)]
    target_ids: Annotated[list[Short], Field(min_length=1, max_length=10)]
    cap_microusd: Annotated[int, Field(gt=0)]


class ScheduleDraft(Output):
    weekdays: list[Annotated[int, Field(ge=1, le=7)]] | None = None
    hour: Annotated[int, Field(ge=0, le=23)] | None = None
    minute: Annotated[int, Field(ge=0, le=59)] | None = None
    timezone: Short | None = None
    window_days: Annotated[int, Field(ge=1, le=31)] | None = None
    target_ids: Annotated[list[Short], Field(max_length=10)] | None = None
    cap_microusd: Annotated[int, Field(gt=0)] | None = None


class ConfirmationDecision(Output):
    action: Literal['ask_missing', 'present_summary', 'accept', 'reject', 'change', 'cancel']
    proposal_id: Short | None
    recurrence: Recurrence | None
    research_plan: ScheduleResearch | None
    missing_fields: Annotated[list[Short], Field(max_length=10)]
    user_question: Short | None
    draft: ScheduleDraft | None = None
