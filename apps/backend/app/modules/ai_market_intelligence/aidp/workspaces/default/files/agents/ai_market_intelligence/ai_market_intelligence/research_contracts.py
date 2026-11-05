"""Versioned current domain objects; no relational business model or execution authority."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import Output, FinancialMetrics, Finding, FactualReview, Short
from .runtime_core import public_url
from .media import MediaAsset


MarketSource = Literal['web', 'x', 'youtube', 'facebook', 'instagram', 'market_data', 'world_bank', 'financial_document']
MarketRole = Literal['coordinator', 'market_analysis', 'financial_analysis', 'analysis_visualization']
Market_AGGREGATE_REFERENCES = 8 * 2 * 50


def market_moment(value):
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError('An explicit timezone is required')
    return moment


class MarketBrief(Output):
    intent: Literal['research','reuse_report','schedule_proposal'] = 'research'
    entity: Short | None
    market: Short | None
    search_country_code: Annotated[str,Field(pattern='^[A-Z]{2}$')] | None = Field(default=None,
        description='Two-letter Google search region for a resolved market; null retains the configured default. Not a publication geofence.')
    objective: Short | None = Field(description='Faithful objective of the user request and supplied conversation context. Preserve requested objects, alternatives and explicit restrictions; do not add ownership, exclusivity, source requirements or other inferred scope restrictions. Treat unverified premises as questions to investigate.')
    start: Short | None = Field(description='ISO timestamp with timezone, or a calendar date interpreted as the start of that UTC day.')
    end: Short | None = Field(description='ISO timestamp with timezone, or a calendar date interpreted as the inclusive end of that UTC day.')
    sources: Annotated[list[MarketSource], Field(max_length=8)]
    clarification: Short | None
    assumptions: Annotated[list[Short], Field(max_length=8)]
    retrieval_mode: Literal['documentary','conversation','financial'] = 'conversation'

    @field_validator('start','end')
    @classmethod
    def normalized_boundary(cls,value,info):
        if value is None:
            return None
        if len(value)==10:
            moment=datetime.combine(date.fromisoformat(value),time.min if info.field_name=='start' else time.max,
                                    tzinfo=timezone.utc)
        else:
            moment=market_moment(value).astimezone(timezone.utc)
        return moment.isoformat()

    @model_validator(mode='after')
    def bounded_scope(self):
        if len(self.sources) != len(set(self.sources)):
            raise ValueError('Source selection must be unique')
        if self.clarification is None and self.intent=='research':
            if not all((self.entity, self.objective)):
                raise ValueError('Material scope must be resolved before planning')
            if self.retrieval_mode=='conversation':
                if not self.start or not self.end:
                    raise ValueError('The conversation publication window must be resolved')
                if market_moment(self.start) > market_moment(self.end):
                    raise ValueError('Research window is reversed')
        return self


class MarketReportChoice(Output):
    request_id: Short | None


class MarketScheduleDraft(Output):
    entity: Short | None
    objective: Short | None
    sources: Annotated[list[MarketSource], Field(max_length=8)]
    frequency: Short | None
    timezone: Short | None
    destination: Short | None


class MarketTask(Output):
    task_id: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')]
    source: MarketSource
    role: MarketRole
    query: Annotated[str, Field(max_length=500)]
    urls: Annotated[list[Short], Field(max_length=3)]
    purpose: Short

    @field_validator('urls')
    @classmethod
    def bounded_urls(cls, values):
        result = [public_url(value) for value in values]
        if len(result) != len(set(result)):
            raise ValueError('Targets must be unique')
        return result


class MarketPlan(Output):
    tasks: Annotated[list[MarketTask], Field(min_length=1, max_length=8)]

    @model_validator(mode='after')
    def unique_routes(self):
        if len({task.task_id for task in self.tasks}) != len(self.tasks):
            raise ValueError('Task IDs must be unique')
        if len({task.source for task in self.tasks}) != len(self.tasks):
            raise ValueError('A source receives one initial task, not a repeated global plan')
        return self


class MarketKeywordGroups(Output):
    groups: Annotated[list[Annotated[list[Annotated[str, Field(min_length=1,max_length=40)]],
                                Field(min_length=1,max_length=3)]], Field(min_length=1,max_length=3)]


class MarketSourceSelection(Output):
    candidate_ids: Annotated[list[Short], Field(max_length=2)]

    @field_validator('candidate_ids')
    @classmethod
    def unique_candidates(cls,values):
        if len(values)!=len(set(values)):
            raise ValueError('A discovery candidate cannot be selected twice')
        return values


class MarketEvidence(Output):
    evidence_id: Short
    source: MarketSource
    discovery_channel: MarketSource
    url: Short
    requested_url: Short | None = None
    author_url: Short | None = None
    author_name: Short | None = None
    source_title: Annotated[str, Field(max_length=1000)] | None = None
    source_description: Annotated[str, Field(max_length=12000)] | None = None
    media: Annotated[list[MediaAsset], Field(max_length=8)] = Field(default_factory=list)
    media_omitted_count: Annotated[int, Field(ge=0)] = 0
    media_content_analyzed: Literal[False] = False
    source_relation: Literal['owner','collaboration','requested_destination','unknown'] = 'unknown'
    text: Annotated[str, Field(min_length=1, max_length=12000)]
    published_at: Short | None
    data_last_updated: Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')] | None = None
    observed_at: Short
    receipt_id: Short
    content_basis: Literal['page_text', 'pdf_text', 'post_text', 'media_metadata', 'title_and_description', 'search_title_and_snippet', 'financial_series']
    locator: Short | None = None
    document_sha256: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')] | None = None
    financial: FinancialMetrics | None = None
    limitations: Annotated[list[Short], Field(max_length=10)] = Field(default_factory=list)

    @field_validator('url','requested_url','author_url')
    @classmethod
    def public_source(cls, value):
        return public_url(value) if value is not None else None

    @field_validator('published_at', 'observed_at')
    @classmethod
    def dated(cls, value):
        if value is not None:
            market_moment(value)
        return value

    @field_validator('data_last_updated')
    @classmethod
    def data_date(cls,value):
        return date.fromisoformat(value).isoformat() if value is not None else None


class MarketCollection(Output):
    status: Literal['complete', 'partial', 'unavailable', 'pending']
    evidence: Annotated[list[MarketEvidence], Field(max_length=50)]
    limitations: Annotated[list[Short], Field(max_length=12)]
    operation_id: Short | None


class MarketFinding(Finding):
    supports: Annotated[list[Short],
                        Field(min_length=1, max_length=50, description='Copy evidence_id values from the supplied evidence; never quotes or claims.')]
    refutes: Annotated[list[Short],
                       Field(max_length=50, description='Copy contradictory evidence_id values, or return an empty list.')]


class MarketFinancialFact(Output):
    evidence_id: Short
    entity: Short
    basis: Literal['individual','consolidated','unknown']
    metric: Short
    period: Annotated[str, Field(pattern=r'^\d{4}$')]
    currency: Annotated[str, Field(pattern=r'^[A-Z]{3}$')] | None
    unit: Literal['currency', 'count', 'percent', 'ratio']
    scale: Literal[1,1000,1000000,1000000000]
    amount_text: Annotated[str, Field(min_length=1,max_length=1000)]
    value_kind: Literal['exact','reported_text'] = Field(default='exact',
        description='Use reported_text for an attributed financial statement, including bounds, approximations or ranges. The server retains its whole source paragraph; it is never a numeric point. Exact amounts are limited to50characters.')
    decimal_separator: Literal['.',',']
    quoted_text: Annotated[str, Field(min_length=1,max_length=3000)]

    @model_validator(mode='after')
    def measurement_unit(self):
        if self.value_kind=='exact' and len(self.amount_text)>50:
            raise ValueError('Exact financial tokens are limited to fifty characters')
        if self.unit != 'currency' and self.currency is not None:
            raise ValueError('Only monetary amounts can declare a currency')
        if self.unit in {'percent', 'ratio'} and self.scale != 1:
            raise ValueError('Ratios and percentages require their literal unscaled value')
        return self


class MarketAnalysis(Output):
    findings: Annotated[list[MarketFinding], Field(max_length=12)]
    refinement_query: Short | None
    gap: Short | None
    financial_facts: Annotated[list[MarketFinancialFact], Field(max_length=20)] = Field(default_factory=list)

    @model_validator(mode='after')
    def justified_refinement(self):
        if self.refinement_query is not None and not self.gap:
            raise ValueError('A refinement requires a concrete gap')
        return self


class MarketPublicBlock(Output):
    text: Annotated[str, Field(min_length=1, max_length=20000)]
    evidence_ids: Annotated[list[Short], Field(max_length=50)]
    limitations: Annotated[list[Short], Field(max_length=12)]

    @field_validator('text')
    @classmethod
    def public_markdown(cls, value):
        if '<' in value or '```' in value:
            raise ValueError('Public prose cannot contain raw HTML or code')
        return value


class MarketRepairUnit(MarketPublicBlock):
    unit_id: Literal['u1','u2','u3','u4']
    text: Annotated[str, Field(min_length=1,max_length=4000)]
    evidence_ids: Annotated[list[Short], Field(min_length=1,max_length=8)]
    limitations: Annotated[list[Short], Field(max_length=2)]


class MarketRepairBundle(Output):
    units: Annotated[list[MarketRepairUnit], Field(max_length=4)]


class MarketUnitReview(FactualReview):
    unit_id: Literal['u1','u2','u3','u4']


class MarketRepairReview(Output):
    reviews: Annotated[list[MarketUnitReview], Field(max_length=4)]


class MarketArtifact(Output):
    artifact_id: Short
    kind: Literal['bar', 'line', 'table']
    title: Short
    x_label: Short
    y_label: Short
    unit: Short
    currency: Short | None
    rows: Annotated[list[dict], Field(min_length=1, max_length=50)]
    evidence_ids: Annotated[list[Short], Field(min_length=1, max_length=Market_AGGREGATE_REFERENCES)]
    caption: Short
    status: Literal['provisional', 'final']


class MarketEvent(Output):
    schema_version: Literal['1.0'] = '1.0'
    event_id: Short
    sequence: Annotated[int, Field(ge=1)]
    emitted_at: Short
    run_id: Short
    turn_id: Short
    research_id: Short
    revision: Short
    type: Literal['draft.upsert', 'draft.compacted', 'draft.corrected', 'artifact.upsert',
                  'source.coverage', 'clarification.required', 'run.completed', 'run.failed', 'run.cancelled']
    block_id: Short | None
    block_revision: Annotated[int, Field(ge=0)]
    supersedes: Annotated[list[Short], Field(max_length=50)]
    source_key: MarketSource | None
    source_keys: Annotated[list[MarketSource], Field(max_length=8)]
    discovery_channel: MarketSource | None
    role_key: MarketRole
    text: Annotated[str, Field(max_length=20000)]
    evidence_ids: Annotated[list[Short], Field(max_length=Market_AGGREGATE_REFERENCES)]
    artifact_ids: Annotated[list[Short], Field(max_length=Market_AGGREGATE_REFERENCES)]
    status: Literal['provisional', 'final', 'partial', 'pending', 'failed', 'cancelled']
    coverage: dict
    limitations: Annotated[list[Short], Field(max_length=20)]
