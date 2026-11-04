"""Model outputs propose decisions; these types grant no permissions or effects."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Short = Annotated[str, Field(min_length=1, max_length=500)]
Reason = Annotated[str, Field(max_length=2000)]
Keys = Annotated[list[Short], Field(max_length=50)]
ROLLING_DAYS_PATTERN = r'last_([1-9]|[12][0-9]|3[01])_days'


class Output(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class TopicScope(Output):
    entity: Short
    market: Short
    objective: Short


class SearchContext(Output):
    country_code: Annotated[str, Field(pattern=r'^[A-Z]{2}$')]
    language_code: Annotated[str, Field(pattern=r'^[a-z]{2}(?:-[A-Z]{2})?$')]


RevisionHash = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]


class RuntimeBinding(Output):
    version: Literal[1]
    implementation_sha256: RevisionHash
    deployed_file_sha256: RevisionHash
    runtime_config_sha256: RevisionHash
    prompts_sha256: RevisionHash
    policy_sha256: RevisionHash
    model_sha256: RevisionHash
    model_id: Short
    dependencies: dict[Short, Short | None]


class ClarificationAnswer(Output):
    request_id: Annotated[str, Field(min_length=1, max_length=128)]
    text: Short


Clarifications = Annotated[list[ClarificationAnswer], Field(max_length=3)]


class ResearchRequest(Output):
    version: Literal[1]
    request_id: Annotated[str, Field(min_length=1, max_length=128)]
    question: Short
    scope_key: Short
    answers: Clarifications

    @field_validator('version', mode='before')
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError('Research request version must be an integer')
        return value


class PendingClarification(ResearchRequest):
    topic: TopicScope | None
    missing_fields: Annotated[list[Short], Field(max_length=5)]
    user_question: Short | None
    selected_source_key: Short | None = None
    prepared_run_id: Short | None = None
    prepared_window_spec: Short | None = None
    search_context: SearchContext | None = None


class IntakeDecision(Output):
    intent: Literal['research', 'followup', 'source_request', 'source_decision', 'list_sources',
                    'schedule_request', 'schedule_decision', 'read_report', 'cancel']
    canonical_question: Short
    topic: TopicScope | None
    window_spec: Annotated[str, Field(max_length=32,
        pattern=rf'^(calendar_today|rolling_24h|{ROLLING_DAYS_PATTERN})$')]
    missing_fields: Annotated[list[Short], Field(max_length=5)]
    user_question: Short | None
    selected_source_key: Short | None = None
    clarification_action: Literal['continue', 'replace', 'cancel'] | None = None
    search_context: SearchContext | None = None


class FinancialArguments(Output):
    tickers: Annotated[list[Short], Field(max_length=3)]
    countries: Annotated[list[Short], Field(max_length=3)]
    indicator_codes: Annotated[list[Short], Field(max_length=3)]
    start_date: Short | None
    end_date: Short | None
    year_from: Annotated[int, Field(ge=1960, le=2100)] | None
    year_to: Annotated[int, Field(ge=1960, le=2100)] | None


class StepArguments(Output):
    query: Annotated[str, Field(max_length=500)]
    urls: Annotated[list[Short], Field(max_length=10)]
    max_items: Annotated[int, Field(ge=1, le=50)]
    sort: Literal['Latest', 'Top']
    financial: FinancialArguments | None = None
    search_mode: Literal['research', 'navigation'] = 'research'


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
    relevance: Literal['relevant', 'uncertain', 'irrelevant'] = 'uncertain'


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


class FactualIssue(Output):
    claim: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_id: Short
    quoted_text: Annotated[str, Field(min_length=1, max_length=2000)]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class FactualReview(Output):
    accepted: bool
    issues: Annotated[list[FactualIssue], Field(max_length=10)]

    @model_validator(mode='after')
    def consistent_verdict(self):
        if self.accepted != (not self.issues):
            raise ValueError('Factual verdict must agree with its issues')
        return self


class ChartRequest(Output):
    kind: Literal['bar', 'line', 'table']
    metric: Literal['observations', 'likes', 'close', 'value']
    group_by: Literal['source', 'date']


class FinancialPoint(Output):
    period: Annotated[str, Field(pattern=r'^\d{4}(?:-\d{2}-\d{2})?$')]
    value: Annotated[float, Field(allow_inf_nan=False)] | None


class FinancialMetrics(Output):
    metric: Literal['close', 'value']
    entity: Short
    indicator: Short | None
    unit: Short
    currency: Short | None
    points: Annotated[list[FinancialPoint], Field(min_length=1, max_length=50)]


class ReportContent(Output):
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


class ResearchConclusion(Output):
    status: Literal['answered', 'partial', 'unresolved']
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    material_gaps: Annotated[list[Short], Field(max_length=5)]

    @model_validator(mode='after')
    def consistent_resolution(self):
        if (self.status == 'answered') == bool(self.material_gaps):
            raise ValueError('An answered question has no material gaps; partial or unresolved requires one')
        return self


class ReportDraft(ReportContent):
    decision: ResearchConclusion


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
    target_ids: Annotated[list[Short], Field(max_length=10)] = Field(default_factory=list)
    source_keys: Annotated[list[Short], Field(max_length=10)] = Field(default_factory=list)
    cap_microusd: Annotated[int, Field(gt=0)]

    @field_validator('target_ids', 'source_keys')
    @classmethod
    def unique_selections(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('Scheduled source and target selections must be distinct')
        return value

    @model_validator(mode='after')
    def selected_scope(self):
        if not self.target_ids and not self.source_keys:
            raise ValueError('Select at least one approved target or public source')
        return self


class ScheduleDraft(Output):
    weekdays: list[Annotated[int, Field(ge=1, le=7)]] | None = None
    hour: Annotated[int, Field(ge=0, le=23)] | None = None
    minute: Annotated[int, Field(ge=0, le=59)] | None = None
    timezone: Short | None = None
    window_days: Annotated[int, Field(ge=1, le=31)] | None = None
    target_ids: Annotated[list[Short], Field(max_length=10)] | None = None
    source_keys: Annotated[list[Short], Field(max_length=10)] | None = None
    cap_microusd: Annotated[int, Field(gt=0)] | None = None


class ConfirmationDecision(Output):
    action: Literal['ask_missing', 'present_summary', 'accept', 'reject', 'change', 'cancel']
    proposal_id: Short | None
    recurrence: Recurrence | None
    research_plan: ScheduleResearch | None
    missing_fields: Annotated[list[Short], Field(max_length=10)]
    user_question: Short | None
    draft: ScheduleDraft | None = None
