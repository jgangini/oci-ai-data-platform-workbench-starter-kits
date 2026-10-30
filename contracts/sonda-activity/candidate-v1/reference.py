"""Unaccepted Activity v1 candidate: pure codecs, no transport or activation.

Authentication, scope authorization, producer authority, replay and sequencing
are broker responsibilities; accepting this shape does not establish those.
"""
import json
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, TypeAdapter, model_validator


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Scope(Closed):
    registry_id: UUID
    generation_id: UUID


class ResultRef(Closed):
    result_id: UUID
    sha256: Annotated[str, Field(strict=True, pattern=r'^[a-f0-9]{64}$')]
    byte_length: Annotated[int, Field(strict=True, ge=1, le=1048576)]


class CausalParent(Closed):
    run_id: UUID
    attempt_id: UUID
    span_id: UUID
    event_id: UUID


class Run(Closed):
    run_id: UUID
    request_id: UUID
    attempt_id: UUID
    work_id: UUID | None
    investigation_id: Annotated[str, Field(strict=True, pattern=r'^[a-f0-9]{64}$')] | None
    execution_kind: Literal['native_invocation', 'provider_execution', 'gold_publication']
    component: Literal['native_agent', 'huginn_provider_broker', 'aidp_gold_runner']
    agent_id: UUID | None
    agent_name: Annotated[str, Field(strict=True, min_length=1, max_length=200)] | None
    execution_ref: Annotated[str, Field(strict=True, min_length=1, max_length=200)] | None = None
    causal_parent: CausalParent | None = None
    core_request_id: UUID | None = None
    core_message_ids: Annotated[list[UUID], Field(max_length=100)] = Field(default_factory=list)
    source: Literal['huginn_chat', 'autonomous_study']
    created_at: AwareDatetime
    synthetic: StrictBool

    @model_validator(mode='after')
    def autonomous_identity(self):
        if self.source == 'autonomous_study' and (self.work_id is None or self.investigation_id is None):
            raise ValueError('Autonomous runs require both native work and investigation identities')
        return self

    @model_validator(mode='after')
    def executor_identity(self):
        components = {'native_invocation':'native_agent', 'provider_execution':'huginn_provider_broker',
            'gold_publication':'aidp_gold_runner'}
        if self.component != components[self.execution_kind]:
            raise ValueError('Run component does not match its execution kind')
        if self.execution_kind == 'native_invocation':
            if self.agent_id is None or self.agent_name is None:
                raise ValueError('Native invocations require their real agent identity')
        elif self.agent_id is not None or self.agent_name is not None:
            raise ValueError('Broker and Gold executions cannot claim a native agent identity')
        return self

    @model_validator(mode='after')
    def causal_identity(self):
        if self.causal_parent and (self.causal_parent.run_id == self.run_id or self.causal_parent.attempt_id == self.attempt_id):
            raise ValueError('A causal parent must identify another execution attempt')
        if len(self.core_message_ids) != len(set(self.core_message_ids)):
            raise ValueError('Core message references must be unique')
        return self


class Event(Closed):
    event_id: UUID
    run_id: UUID
    attempt_id: UUID
    producer_stream_id: UUID
    producer_sequence: Annotated[int, Field(strict=True, ge=1, le=10000)]
    ingestion_sequence: Annotated[int, Field(strict=True, ge=1, le=9007199254740991)]
    authority: Literal['native_agent', 'provider_broker', 'gold_writer', 'activity_broker']
    emitted_at: AwareDatetime
    received_at: AwareDatetime
    span_id: UUID
    parent_span_id: UUID | None
    causation_id: UUID | None


Summary = Annotated[str, Field(strict=True, min_length=1, max_length=1024)]


class Stage(Event):
    type: Literal['stage']
    summary: Summary
    effect: Literal['requested', 'prepared', 'queued', 'executed', 'committed'] | None
    status: Literal['progress', 'degraded', 'uncertain'] = 'progress'

    @model_validator(mode='after')
    def neutral_status(self):
        if self.status != 'progress' and (self.authority != 'activity_broker' or self.effect is not None):
            raise ValueError('Observation degradation and uncertainty require the neutral broker without a business effect')
        return self


class ToolStart(Event):
    type: Literal['tool_start']
    tool_name: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    summary: Summary
    domain: Literal['query_read', 'research_preparation', 'provider_capture', 'publication']

    @model_validator(mode='after')
    def domain_role(self):
        roles = {'query_read':'native_agent', 'research_preparation':'native_agent',
            'provider_capture':'provider_broker', 'publication':'gold_writer'}
        if self.authority != roles[self.domain]:
            raise ValueError('Tool domain is not allowed for this producer role')
        return self


class ToolFinish(Event):
    type: Literal['tool_finish']
    summary: Summary
    effect: Literal['prepared', 'executed', 'committed']


class ToolError(Event):
    type: Literal['tool_error']
    error_code: Annotated[str, Field(strict=True, pattern=r'^[a-z][a-z0-9_]{0,63}$')]
    summary: Summary


class TextDelta(Event):
    type: Literal['text_delta']
    public_text_delta: Annotated[str, Field(strict=True, min_length=1, max_length=4096)]


class Terminal(Event):
    type: Literal['run_terminal']
    outcome: Literal['completed', 'failed', 'cancelled']
    summary: Summary
    final_ref: ResultRef | None

    @model_validator(mode='after')
    def completed_result(self):
        if self.authority != 'activity_broker':
            raise ValueError('Only the activity broker projects canonical run terminal events')
        if self.outcome == 'completed' and self.final_ref is None:
            raise ValueError('Completed runs require a validated public result reference')
        return self


ActivityEvent = Annotated[Union[Stage, ToolStart, ToolFinish, ToolError, TextDelta, Terminal],
                          Field(discriminator='type')]


class RunsResponse(Closed):
    schema_version: Literal['activity.v1.candidate.4']
    scope: Scope
    items: Annotated[list[Run], Field(max_length=100)]
    next_cursor: Annotated[str, Field(strict=True, min_length=1, max_length=2048)] | None


class EventsResponse(Closed):
    schema_version: Literal['activity.v1.candidate.4']
    scope: Scope
    run_id: UUID
    attempt_id: UUID
    events: Annotated[list[ActivityEvent], Field(max_length=100)]
    resume_cursor: Annotated[str, Field(strict=True, min_length=1, max_length=2048)]
    next_cursor: Annotated[str, Field(strict=True, min_length=1, max_length=2048)] | None

    @model_validator(mode='after')
    def same_run(self):
        if any(event.run_id != self.run_id or event.attempt_id != self.attempt_id for event in self.events):
            raise ValueError('Event belongs to another run or attempt')
        return self


RESPONSES = TypeAdapter(Union[RunsResponse, EventsResponse])


def validate_run_event(run, event):
    """Bind a projected event to an already authorized run; no authorization IO.

    Finish/effect adjudication additionally requires its registered start and
    actual receipt. Missing starts must remain pending in the caller's ledger.
    """
    run = Run.model_validate(run)
    event = TypeAdapter(ActivityEvent).validate_python(event)
    if (event.run_id, event.attempt_id) != (run.run_id, run.attempt_id):
        raise ValueError('Event does not belong to the authorized run attempt')
    if event.authority == 'activity_broker':
        if event.type not in ('stage', 'run_terminal'):
            raise ValueError('Neutral broker cannot claim a tool operation or model text')
        return event
    roles = {'native_agent':'native_agent', 'huginn_provider_broker':'provider_broker', 'aidp_gold_runner':'gold_writer'}
    if event.authority != roles[run.component]:
        raise ValueError('Event producer role does not match this run component')
    domains = {'native_invocation':{'query_read','research_preparation'},
        'provider_execution':{'provider_capture'}, 'gold_publication':{'publication'}}
    if isinstance(event, ToolStart) and event.domain not in domains[run.execution_kind]:
        raise ValueError('Tool domain does not match this execution kind')
    if isinstance(event, TextDelta) and run.execution_kind != 'native_invocation':
        raise ValueError('Public model text belongs only to a native invocation')
    return event


def parse(raw: bytes):
    if len(raw) > 1048576:
        raise ValueError('Activity batch exceeds its byte limit')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate activity JSON key')
            result[key] = value
        return result
    value = RESPONSES.validate_python(json.loads(raw, object_pairs_hook=unique))
    if isinstance(value, EventsResponse):
        for event in value.events:
            if len(event.model_dump_json().encode('utf-8')) > 16384:
                raise ValueError('Activity event exceeds its byte limit')
    return value
