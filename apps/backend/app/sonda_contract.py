"""Authoritative Sonda v1 exchange types; schema/fixtures are exported from here.

This proposal is not connected to the existing 1.1.1 installer or ingestion.
"""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from datetime import datetime, timezone
from typing import Annotated, Literal, Union
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field,
                      JsonValue, StrictBool, StrictStr, TypeAdapter, model_validator)

VERSION = '1.0.2'
MAX_BYTES = 4 * 1024 * 1024
INBOUND_KINDS = ('configuration', 'budget_round', 'observations', 'execution_result')
OUTBOUND_KINDS = ('investigation_request', 'report', 'proposal', 'processing_receipt')
Hash = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')]
Count = Annotated[int, Field(strict=True, ge=0, le=9007199254740991)]
Revision = Annotated[int, Field(strict=True, ge=1, le=9007199254740991)]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=20000)]
Name = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Code = Annotated[str, Field(strict=True, pattern=r'^[a-z][a-z0-9_.-]{0,99}$')]
Domain = Annotated[str, Field(strict=True, max_length=253, pattern=r'^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')]
Score = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
Network = Literal['instagram', 'facebook', 'tiktok', 'x', 'linkedin', 'youtube']


def public_reference(value: str) -> str:
    url = urlsplit(value)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
        raise ValueError('A reference must be an HTTP(S) URL without credentials')
    return value


Url = Annotated[str, Field(strict=True, max_length=2048), AfterValidator(public_reference)]


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Approval(Record):
    status: Literal['pending', 'approved', 'rejected']
    decided_by: Name | None = None
    decided_at: AwareDatetime | None = None

    @model_validator(mode='after')
    def decision(self):
        decided = self.decided_by is not None and self.decided_at is not None
        if (self.status != 'pending') != decided or (self.status == 'pending' and (self.decided_by or self.decided_at)):
            raise ValueError('A decision requires its operator and timestamp; pending has neither')
        return self


class Managed(Record):
    revision: Revision
    origin: Literal['operator', 'agent']
    author_id: Name
    proposal_id: UUID | None = None
    approval: Approval
    status: Literal['draft', 'active', 'paused', 'archived']

    @model_validator(mode='after')
    def active_approval(self):
        if self.status == 'active' and self.approval.status != 'approved':
            raise ValueError('Only an operator-approved record may be active')
        return self


class Connector(Record):
    connector_id: Code
    revision: Revision
    actor_id: Name
    actor_build: Name
    operation: Literal['account_posts', 'search_posts', 'comments', 'pages', 'indicators']
    input_schema_sha256: Hash
    output_mapping_version: Name
    modes: Annotated[list[Literal['posts', 'comments', 'pages', 'indicators']], Field(min_length=1, max_length=4)]
    research_fields: Annotated[list[Code], Field(max_length=30)]
    reviewed_by: Name
    reviewed_at: AwareDatetime
    max_cost_microusd_per_item: Count | None

    @model_validator(mode='after')
    def operation_mode(self):
        expected = 'posts' if self.operation in ('account_posts', 'search_posts') else self.operation
        if self.modes != [expected]:
            raise ValueError('A reviewed connector has one effective operation/mode')
        return self


class Source(Managed):
    source_id: UUID
    label: Name
    connector_id: Code
    connector_revision: Revision
    category: Literal['social', 'news', 'public_web', 'economics']
    platform: Network | None = None
    allowed_domains: Annotated[list[Domain], Field(max_length=100)] = Field(default_factory=list)

    @model_validator(mode='after')
    def domains(self):
        if len(set(self.allowed_domains)) != len(self.allowed_domains):
            raise ValueError('Domain allowlist entries must be distinct')
        if self.category != 'social' and not self.allowed_domains:
            raise ValueError('A public-web source requires explicitly approved domains')
        return self


class Query(Managed):
    query_id: UUID
    source_id: UUID
    label: Name
    mode: Literal['posts', 'comments', 'pages', 'indicators']
    target: Annotated[str, Field(strict=True, min_length=1, max_length=2000)]
    parameters: dict[Code, JsonValue]
    max_items: Annotated[int, Field(strict=True, ge=1, le=100000)]
    max_charge_microusd: Count
    research_allowed: StrictBool


class LabelledExample(Record):
    text: Annotated[str, Field(strict=True, min_length=1, max_length=4000)]
    relevant: StrictBool


class Criterion(Managed):
    criterion_id: UUID
    label: Name
    source_ids: Annotated[list[UUID], Field(min_length=1, max_length=10000)]
    query_ids: Annotated[list[UUID], Field(max_length=10000)] = Field(default_factory=list)
    description: Annotated[str, Field(strict=True, min_length=1, max_length=4000)]
    threshold: Score = 0.80
    allow_research: StrictBool
    examples: Annotated[list[LabelledExample], Field(max_length=40)]
    case_scope_key: Name
    case_window_hours: Annotated[int, Field(strict=True, ge=1, le=720)]


class Policy(Record):
    interval_seconds: Annotated[int, Field(strict=True, ge=60, le=604800)]
    max_concurrency: Annotated[int, Field(strict=True, ge=5, le=5)] = 5
    max_items_per_round: Annotated[int, Field(strict=True, ge=1, le=1000000)]
    round_budget_microusd: Count
    research_reserve_microusd: Count
    max_research_requests_per_case: Annotated[int, Field(strict=True, ge=0, le=20)]

    @model_validator(mode='after')
    def reserve(self):
        if self.research_reserve_microusd > self.round_budget_microusd:
            raise ValueError('Research reserve must fit the round budget')
        return self


class ProposalDecision(Record):
    proposal_id: UUID
    decision: Literal['approved', 'adjusted', 'rejected']
    operator_id: Name
    decided_at: AwareDatetime
    config_revision: Revision
    reason: Annotated[str, Field(strict=True, min_length=1, max_length=2000)] | None = None


def unique(rows, key):
    values = [getattr(row, key) for row in rows]
    if len(set(values)) != len(values):
        raise ValueError('Duplicate configuration identity')


class Configuration(Record):
    revision: Revision
    previous_revision: Count
    catalog_version: Name
    catalog_sha256: Hash
    connectors: Annotated[list[Connector], Field(min_length=1, max_length=1000)]
    sources: Annotated[list[Source], Field(max_length=10000)]
    queries: Annotated[list[Query], Field(max_length=10000)]
    criteria: Annotated[list[Criterion], Field(max_length=10000)]
    policy: Policy
    proposal_decisions: Annotated[list[ProposalDecision], Field(max_length=10000)] = Field(default_factory=list)

    @model_validator(mode='after')
    def references(self):
        if self.previous_revision != self.revision - 1:
            raise ValueError('A configuration revision must extend its predecessor')
        for rows, key in ((self.connectors, 'connector_id'), (self.sources, 'source_id'),
                          (self.queries, 'query_id'), (self.criteria, 'criterion_id')):
            unique(rows, key)
        if self.catalog_sha256 != catalog_fingerprint([item.model_dump(mode='json') for item in self.connectors]):
            raise ValueError('Reviewed connector catalog fingerprint does not match')
        connectors = {r.connector_id: r for r in self.connectors}
        sources = {r.source_id: r for r in self.sources}
        queries = {r.query_id: r for r in self.queries}
        for source in self.sources:
            connector = connectors.get(source.connector_id)
            if connector is None or connector.revision != source.connector_revision:
                raise ValueError('A source requires its reviewed connector revision')
        for query in self.queries:
            source = sources.get(query.source_id)
            if source is None or query.mode not in connectors[source.connector_id].modes:
                raise ValueError('A query requires a source with that approved capability')
            if query.status == 'active' and source.status != 'active':
                raise ValueError('An active query requires an active source')
        for criterion in self.criteria:
            if len(set(criterion.source_ids)) != len(criterion.source_ids) or any(s not in sources for s in criterion.source_ids):
                raise ValueError('A criterion requires distinct source references')
            if len(set(criterion.query_ids)) != len(criterion.query_ids) or any(q not in queries or queries[q].source_id not in criterion.source_ids for q in criterion.query_ids):
                raise ValueError('Criterion query references must belong to its sources')
        unique(self.proposal_decisions, 'proposal_id')
        for decision in self.proposal_decisions:
            if decision.config_revision != self.revision:
                raise ValueError('A proposal decision belongs to its configuration revision')
            approved = [record for record in self.queries + self.criteria
                        if record.proposal_id == decision.proposal_id and record.approval.status == 'approved']
            if decision.decision == 'rejected':
                if approved:
                    raise ValueError('A rejected proposal cannot approve a record')
            elif len(approved) != 1 or approved[0].approval.decided_by != decision.operator_id or approved[0].approval.decided_at != decision.decided_at:
                raise ValueError('An approved or adjusted proposal requires its operator-approved record')
        return self


class Metrics(Record):
    likes: Count | None = None
    comments: Count | None = None
    shares: Count | None = None
    views: Count | None = None


class MappedIndicator(Record):
    key: Code
    label: Name
    value: Annotated[str, Field(strict=True, max_length=40, pattern=r'^-?(?:0|[1-9][0-9]{0,25})(?:\.[0-9]{1,12})?$')] | None
    unit: Name
    period_start: AwareDatetime | None = None
    period_end: AwareDatetime | None = None


class Indicator(MappedIndicator):
    evidence_ids: Annotated[list[Hash], Field(min_length=1, max_length=100)]
    method: Literal['count_distinct_documents', 'reported_metric_sum', 'source_mapped_value', 'ratio']
    population_count: Count
    missing_count: Count


class Observation(Record):
    document_key: Hash
    observation_key: Hash
    content_sha256: Hash
    identity_version: Literal[1] = 1
    document_type: Literal['post', 'comment', 'page', 'indicator']
    platform: Network | None = None
    provider_id: Annotated[str, Field(strict=True, min_length=1, max_length=500)] | None = None
    parent_document_key: Hash | None = None
    url: Url
    title: Annotated[str, Field(strict=True, max_length=500)] | None = None
    text: Annotated[str, Field(strict=True, max_length=20000)]
    author: Name | None = None
    published_at: AwareDatetime | None
    observed_at: AwareDatetime
    metrics: Metrics
    indicators: Annotated[list[MappedIndicator], Field(max_length=100)]
    capture_scope: Literal['requested_results', 'top_comments', 'partial_comments', 'page_snapshot']

    @model_validator(mode='after')
    def parent(self):
        if (self.document_type == 'comment') != (self.parent_document_key is not None):
            raise ValueError('Only comments require a parent document key')
        if self.document_type in ('post', 'comment') and (not self.platform or not self.provider_id):
            raise ValueError('Social identity requires platform and provider ID')
        expected = observation_fingerprints(self.model_dump(mode='json'))
        if any(getattr(self, key) != value for key, value in expected.items()):
            raise ValueError('Observation identity or business fingerprint does not match')
        return self


class ObservationBatch(Record):
    config_revision: Revision
    source_id: UUID
    source_revision: Revision
    query_id: UUID
    query_revision: Revision
    execution_id: UUID
    budget_round_id: UUID
    provider_run_id: Name
    initiated_by: Literal['operator', 'schedule', 'agent']
    observations: Annotated[list[Observation], Field(min_length=1, max_length=100)]


class ExecutionResult(Record):
    execution_id: UUID
    budget_round_id: UUID
    request_id: UUID | None
    config_revision: Revision
    query_id: UUID
    query_revision: Revision
    status: Literal['completed', 'budget_exhausted', 'failed', 'uncertain', 'cancelled']
    observation_message_ids: Annotated[list[UUID], Field(max_length=10000)]
    record_count: Count
    spent_microusd: Count | None
    coverage: Literal['requested_scope', 'partial', 'unknown']
    explanation: Annotated[str, Field(strict=True, min_length=1, max_length=2000)]


class BudgetRound(Record):
    budget_round_id: UUID
    revision: Revision
    config_revision: Revision
    opened_at: AwareDatetime
    expires_at: AwareDatetime
    status: Literal['open', 'closed']
    remaining_microusd: Count | None
    research_remaining_microusd: Count | None
    remaining_items: Count | None

    @model_validator(mode='after')
    def bounds(self):
        if self.expires_at <= self.opened_at:
            raise ValueError('A budget round requires an explicit positive lifetime')
        if self.remaining_microusd is not None and self.research_remaining_microusd is not None and self.research_remaining_microusd > self.remaining_microusd:
            raise ValueError('Research availability cannot exceed total availability')
        return self


class Evidence(Record):
    evidence_id: Hash
    observation_key: Hash
    document_key: Hash
    url: Url
    quote: Annotated[str, Field(strict=True, min_length=1, max_length=4000)]
    captured_at: AwareDatetime

    @model_validator(mode='after')
    def identity(self):
        if self.evidence_id != sha256(encoded([self.observation_key, self.quote])):
            raise ValueError('Evidence fingerprint does not match')
        return self


class Evaluation(Record):
    evaluation_key: Hash
    observation_key: Hash
    criterion_id: UUID
    criterion_revision: Revision
    config_revision: Revision
    model_id: Name
    model_revision: Name
    prompt_version: Name
    rule_sha256: Hash
    score: Score | None
    threshold: Score
    decision: Literal['relevant', 'not_relevant', 'error']
    reason: Annotated[str, Field(strict=True, min_length=1, max_length=2000)]
    evidence: Annotated[list[Evidence], Field(max_length=20)]
    evaluated_at: AwareDatetime

    @model_validator(mode='after')
    def verdict(self):
        if self.evaluation_key != evaluation_fingerprint(self.model_dump(mode='json')):
            raise ValueError('Evaluation fingerprint must bind observation, rule and model/prompt versions')
        if self.decision == 'error':
            if self.score is not None:
                raise ValueError('A failed evaluation has no score')
        elif self.score is None or (self.score >= self.threshold) != (self.decision == 'relevant') or not self.evidence:
            raise ValueError('A semantic verdict must match its threshold and quote evidence')
        if any(item.observation_key != self.observation_key for item in self.evidence):
            raise ValueError('Evaluation evidence must refer to its observation')
        return self


class InvestigationRequest(Record):
    request_id: UUID
    investigation_id: Hash
    config_revision: Revision
    criterion_id: UUID
    criterion_revision: Revision
    query_id: UUID
    query_revision: Revision
    budget_round_id: UUID
    purpose: Annotated[str, Field(strict=True, min_length=1, max_length=2000)]
    target_override: Annotated[str, Field(strict=True, min_length=1, max_length=2000)] | None
    max_items: Annotated[int, Field(strict=True, ge=1, le=100000)]
    max_cost_microusd: Count
    expires_at: AwareDatetime
    evidence_ids: Annotated[list[Hash], Field(min_length=1, max_length=100)]


class Coverage(Record):
    distinct_documents: Count
    distinct_observations: Count
    query_ids: Annotated[list[UUID], Field(max_length=10000)]
    pending_requests: Count
    failed_requests: Count
    exhausted_budget: StrictBool
    limitations: Annotated[list[Annotated[str, Field(strict=True, min_length=1, max_length=1000)]], Field(max_length=30)]


class Report(Record):
    report_id: Hash
    investigation_id: Hash
    revision: Revision
    config_revision: Revision
    criterion_id: UUID
    criterion_revision: Revision
    status: Literal['awaiting_evidence', 'completed', 'completed_with_limits', 'failed']
    title: Name
    summary: Annotated[str, Field(strict=True, min_length=1, max_length=8000)]
    indicators: Annotated[list[Indicator], Field(max_length=100)]
    evidence: Annotated[list[Evidence], Field(max_length=500)]
    evaluations: Annotated[list[Evaluation], Field(max_length=100)]
    coverage: Coverage
    model_id: Name
    model_revision: Name
    prompt_version: Name
    created_at: AwareDatetime

    @model_validator(mode='after')
    def indicator_refs(self):
        refs = {e.evidence_id for e in self.evidence}
        if any(not set(i.evidence_ids) <= refs for i in self.indicators):
            raise ValueError('An indicator may only reference report evidence')
        return self


class CriterionChange(Record):
    target_kind: Literal['criterion']
    record: Criterion


class QueryChange(Record):
    target_kind: Literal['query']
    record: Query


class Proposal(Record):
    proposal_id: UUID
    investigation_id: Hash
    base_config_revision: Revision
    operation: Literal['create', 'update', 'pause']
    change: Annotated[Union[CriterionChange, QueryChange], Field(discriminator='target_kind')]
    state: Literal['pending'] = 'pending'
    rationale: Annotated[str, Field(strict=True, min_length=1, max_length=4000)]
    evidence_ids: Annotated[list[Hash], Field(min_length=1, max_length=100)]

    @model_validator(mode='after')
    def pending_agent_change(self):
        record = self.change.record
        if record.origin != 'agent' or record.approval.status != 'pending' or record.status == 'active':
            raise ValueError('An agent proposal cannot approve or activate its change')
        if record.proposal_id != self.proposal_id:
            raise ValueError('The proposed record must retain its proposal provenance')
        return self


class ProcessingReceipt(Record):
    source_sequence: Revision
    source_message_id: UUID
    status: Literal['applied', 'rejected']
    code: Code
    explanation: Annotated[str, Field(strict=True, min_length=1, max_length=2000)]


class Scope(Record):
    contract_version: Literal['1.0.2'] = VERSION
    contract_sha256: Hash
    registry_id: UUID
    generation_id: UUID


class Message(Scope):
    message_id: UUID
    created_at: AwareDatetime
    correlation_id: UUID | None = None


class ConfigMessage(Message):
    kind: Literal['configuration']
    direction: Literal['huginn_to_aidp']
    payload: Configuration


class ObservationsMessage(Message):
    kind: Literal['observations']
    direction: Literal['huginn_to_aidp']
    payload: ObservationBatch


class ExecutionMessage(Message):
    kind: Literal['execution_result']
    direction: Literal['huginn_to_aidp']
    payload: ExecutionResult


class BudgetMessage(Message):
    kind: Literal['budget_round']
    direction: Literal['huginn_to_aidp']
    payload: BudgetRound


class InvestigationMessage(Message):
    kind: Literal['investigation_request']
    direction: Literal['aidp_to_huginn']
    payload: InvestigationRequest


class ReportMessage(Message):
    kind: Literal['report']
    direction: Literal['aidp_to_huginn']
    payload: Report


class ProposalMessage(Message):
    kind: Literal['proposal']
    direction: Literal['aidp_to_huginn']
    payload: Proposal


class ReceiptMessage(Message):
    kind: Literal['processing_receipt']
    direction: Literal['aidp_to_huginn']
    payload: ProcessingReceipt


Direction = Literal['huginn_to_aidp', 'aidp_to_huginn']


class StreamEntry(Scope):
    kind: Literal['stream_entry']
    direction: Direction
    sequence: Revision
    message_id: UUID
    message_kind: Literal['configuration', 'budget_round', 'observations', 'execution_result', 'investigation_request', 'report', 'proposal', 'processing_receipt']
    message_sha256: Hash
    message_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_BYTES)]
    previous_entry_sha256: Hash | None

    @model_validator(mode='after')
    def predecessor(self):
        if (self.sequence == 1) != (self.previous_entry_sha256 is None):
            raise ValueError('Only the first stream entry has no predecessor')
        inbound = self.message_kind in INBOUND_KINDS
        if inbound != (self.direction == 'huginn_to_aidp'):
            raise ValueError('Message kind does not belong to this stream direction')
        return self


class StreamHead(Scope):
    kind: Literal['stream_head']
    direction: Direction
    last_sequence: Count
    last_entry_sha256: Hash | None
    updated_at: AwareDatetime

    @model_validator(mode='after')
    def cursor(self):
        if (self.last_sequence == 0) != (self.last_entry_sha256 is None):
            raise ValueError('An empty stream has no last-entry hash')
        return self


class ConsumerAck(Scope):
    kind: Literal['consumer_ack']
    direction: Direction
    last_processed_sequence: Count
    last_entry_sha256: Hash | None
    updated_at: AwareDatetime

    @model_validator(mode='after')
    def cursor(self):
        if (self.last_processed_sequence == 0) != (self.last_entry_sha256 is None):
            raise ValueError('An empty acknowledgement has no last-entry hash')
        return self


Document = Annotated[Union[ConfigMessage, BudgetMessage, ObservationsMessage, ExecutionMessage,
    InvestigationMessage, ReportMessage, ProposalMessage, ReceiptMessage,
    StreamEntry, StreamHead, ConsumerAck], Field(discriminator='kind')]
ADAPTER = TypeAdapter(Document)


class ChatContext(Record):
    registry_id: UUID
    generation_id: UUID
    config_revision: Revision
    source_ids: Annotated[list[UUID], Field(max_length=1000)]
    query_ids: Annotated[list[UUID], Field(max_length=1000)]


class ChatInput(Record):
    version: Literal[2]
    question: Annotated[str, Field(strict=True, min_length=1, max_length=500)]
    search_context: ChatContext


def schema():
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            '$id': 'urn:ai-social-network:sonda:' + VERSION, **ADAPTER.json_schema()}


def chat_schema():
    return {'$schema':'https://json-schema.org/draft/2020-12/schema',
            '$id':'urn:ai-social-network:sonda:chat:2', **ChatInput.model_json_schema()}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def reject_secrets(value, depth=0):
    if depth > 16:
        raise ValueError('Payload nesting exceeds its limit')
    if isinstance(value, dict):
        if len(value) > 10000 or any(k.lower() in {'token', 'api_key', 'password', 'private_key', 'authorization', 'secret'} for k in value):
            raise ValueError('Credentials are forbidden in the exchange')
        for child in value.values():
            reject_secrets(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            reject_secrets(child, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Nonfinite JSON number')


def parse(raw: bytes, *, registry_id: UUID, generation_id: UUID, contract_sha256: str):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_BYTES:
        raise ValueError('Exchange document exceeds its byte limit')
    value = json.loads(raw, object_pairs_hook=no_duplicate_keys,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON number')))
    reject_secrets(value)
    document = ADAPTER.validate_json(raw)
    if (document.registry_id, document.generation_id, document.contract_sha256) != (registry_id, generation_id, contract_sha256):
        raise ValueError('Exchange scope or pinned contract does not match')
    return document


def sha256(raw: bytes):
    return hashlib.sha256(raw).hexdigest()


def canonical_url(value):
    parsed = urlsplit(public_reference(value))
    host = parsed.hostname.encode('idna').decode().lower()
    host = '[' + host + ']' if ':' in host else host
    port = parsed.port
    authority = host + (':' + str(port) if port and (parsed.scheme, port) not in (('http', 80), ('https', 443)) else '')
    return parsed.scheme.lower() + '://' + authority + (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')


def canonical_utc(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('A canonical timestamp requires a timezone')
    return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def observation_fingerprints(value):
    """Versioned portable identity; discovery/query/observation time are excluded."""
    def normalized(item):
        if isinstance(item, str):
            return unicodedata.normalize('NFC', item)
        if isinstance(item, dict):
            return {key: normalized(child) for key, child in item.items()}
        if isinstance(item, list):
            return [normalized(child) for child in item]
        return item

    kind = value['document_type']
    url = canonical_url(value['url'])
    identity = [1, kind, value.get('platform'), value.get('parent_document_key'), value.get('provider_id')] if kind in ('post', 'comment') else [1, 'url', url]
    document_key = sha256(encoded(identity))
    content = {key: value.get(key) for key in ('document_type', 'platform', 'provider_id', 'parent_document_key', 'title', 'text', 'author', 'capture_scope')}
    content.update(url=url, published_at=canonical_utc(value.get('published_at')),
        metrics={key: value.get('metrics', {}).get(key) for key in ('likes', 'comments', 'shares', 'views')},
        indicators=[{**item, 'period_start':canonical_utc(item.get('period_start')), 'period_end':canonical_utc(item.get('period_end'))}
                    for item in sorted(value.get('indicators', []), key=lambda item:item['key'])])
    content = {**normalized(content), 'url':url, 'provider_id':value.get('provider_id')}
    content_sha256 = sha256(encoded(content))
    return {'document_key': document_key, 'content_sha256': content_sha256,
        'observation_key': sha256(encoded([1, document_key, content_sha256]))}


def evaluation_fingerprint(value):
    return sha256(encoded([1, value['observation_key'], str(value['criterion_id']),
        value['criterion_revision'], value['rule_sha256'], value['model_id'],
        value['model_revision'], value['prompt_version']]))


def catalog_fingerprint(connectors):
    values = []
    for row in sorted(connectors, key=lambda item:item['connector_id']):
        values.append({**row,'reviewed_at':canonical_utc(row['reviewed_at'])})
    return sha256(encoded(values))


def stream_prefix(registry_id, generation_id, direction):
    if direction not in ('huginn_to_aidp', 'aidp_to_huginn'):
        raise ValueError('Unknown stream direction')
    return 'ai_social_network/exchange/v1/registries/' + str(UUID(str(registry_id))) + '/generations/' + str(UUID(str(generation_id))) + '/' + direction + '/'


def message_target(kind, message_id, registry_id, generation_id, direction, source_prefix):
    if kind not in INBOUND_KINDS + OUTBOUND_KINDS or (kind in INBOUND_KINDS) != (direction == 'huginn_to_aidp'):
        raise ValueError('Message kind does not belong to this direction')
    root = stream_prefix(registry_id, generation_id, direction)
    role = 'artifacts'
    if kind == 'observations':
        if (not isinstance(source_prefix,str) or source_prefix.startswith('/') or '\\' in source_prefix
            or any(part in ('', '.', '..') for part in source_prefix.rstrip('/').split('/'))
            or any(ord(char)<32 for char in source_prefix)):
            raise ValueError('Invalid bound source prefix')
        root = source_prefix.rstrip('/') + '/exchange/v1/' + root.removeprefix('ai_social_network/exchange/v1/')
        role = 'source'
    return {'bucket_role':role,'key':root + 'messages/' + str(UUID(str(message_id))) + '.json'}
