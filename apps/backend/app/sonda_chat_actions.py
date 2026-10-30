"""Draft Chat Actions 1.0.0 contract; no runtime, route or capture activation.

The existing Sonda 1.0.2 exchange remains unchanged. Huginn owns authorization,
quotes, decisions, capture and operation persistence; models only propose intents.
"""
from __future__ import annotations

from datetime import timezone
import ipaddress
from typing import Annotated, Literal, Union
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from pydantic import AfterValidator, AwareDatetime, Field, JsonValue, StrictBool, TypeAdapter, model_validator

from app.sonda_contract import ChatContext, Code, Count, Domain, ExecutionMessage, Hash, Name, Network, ObservationsMessage, ReceiptMessage, Record, Revision, StreamEntry, Url, canonical_url, encoded, no_duplicate_keys, parse, reject_secrets, sha256

ACTIONS_VERSION = '1.0.0'
ACTIONS_MAX_TTL_SECONDS = 900
ACTION_MAX_BYTES = 1024 * 1024


def action_utc(value):
    return value.astimezone(timezone.utc)


ActionTime = Annotated[AwareDatetime, AfterValidator(action_utc)]

class ActionPin(Record):
    capability_version: Literal['1.0.0']
    capability_sha256: Hash
    core_contract_version: Literal['1.0.2']
    core_contract_sha256: Hash


class ActionScope(ActionPin):
    registry_id: UUID
    generation_id: UUID


def action_public_target(target,domains):
    canonical=canonical_url(target)
    parsed=urlsplit(canonical)
    host=parsed.hostname
    if host not in domains or host in ('localhost','localhost.localdomain') or host.endswith('.local'):
        raise ValueError('Target must belong to an explicitly approved public domain')
    if parsed.port not in (None,80,443):
        raise ValueError('Chat web targets use standard HTTP(S) ports')
    try:
        address=ipaddress.ip_address(host)
    except ValueError:
        return canonical  # The broker still resolves DNS and checks every redirect.
    if not address.is_global:
        raise ValueError('Private or reserved address targets are not permitted')
    return canonical


class CaptureOption(Record):
    option_id: Hash
    registry_id: UUID
    generation_id: UUID
    turn_id: UUID
    capture_config_revision: Revision
    source_id: UUID
    source_revision: Revision
    query_id: UUID
    query_revision: Revision
    label: Name
    mode: Literal['posts','comments','pages','indicators']
    target: Annotated[str,Field(strict=True,min_length=1,max_length=2000)]
    target_override_allowed: StrictBool
    allowed_domains: Annotated[list[Domain],Field(max_length=30)]
    max_items: Annotated[int,Field(strict=True,ge=1,le=100)]
    max_cost_microusd: Count

    @model_validator(mode='after')
    def reviewed_target(self):
        if self.mode in ('pages','indicators'):
            action_public_target(self.target,self.allowed_domains)
        elif self.target_override_allowed:
            raise ValueError('Social captures retain the reviewed account/search target')
        if len(self.allowed_domains)!=len(set(self.allowed_domains)):
            raise ValueError('Allowed domains must be distinct')
        return self

    @model_validator(mode='after')
    def catalog_fingerprint(self):
        if self.option_id!=sha256(encoded(self.model_dump(mode='json',exclude={'option_id'}))):
            raise ValueError('A capture option must retain its exact server catalog fingerprint')
        return self


class ActionChatInput(ActionPin):
    kind: Literal['chat_input']
    version: Literal[3]
    turn_id: UUID
    created_at: ActionTime
    expires_at: ActionTime
    question: Annotated[str, Field(strict=True, min_length=1, max_length=500)]
    search_context: ChatContext
    capture_config_revision: Revision
    capture_options_total: Count
    capture_options: Annotated[list[CaptureOption],Field(max_length=20)]

    @model_validator(mode='after')
    def turn_window(self):
        if not 0 < (self.expires_at-self.created_at).total_seconds() <= ACTIONS_MAX_TTL_SECONDS:
            raise ValueError('A turn requires an immutable window of at most fifteen minutes')
        return self

    @model_validator(mode='after')
    def complete_capture_catalog(self):
        if self.capture_options_total!=len(self.capture_options):
            raise ValueError('Capture options cannot be silently truncated; refine the selection')
        if len({option.query_id for option in self.capture_options})!=len(self.capture_options):
            raise ValueError('Capture options require distinct reviewed queries')
        scope=(self.search_context.registry_id,self.search_context.generation_id,self.turn_id,self.capture_config_revision)
        if any((option.registry_id,option.generation_id,option.turn_id,option.capture_config_revision)!=scope
                for option in self.capture_options):
            raise ValueError('Capture catalog belongs to another persisted server turn')
        return self


def capture_intent_id(registry_id, generation_id, turn_id):
    """One immutable intent per trusted server turn, regardless of model replanning."""
    return uuid5(UUID(str(generation_id)), 'chat-capture/v1/'+str(UUID(str(registry_id)))+'/'+str(UUID(str(turn_id))))


class CaptureIntent(ActionScope):
    kind: Literal['capture_intent']
    intent_id: UUID
    turn_id: UUID
    capture_option_id: Hash
    config_revision: Revision
    source_id: UUID
    source_revision: Revision
    query_id: UUID
    query_revision: Revision
    target_override: Url | None
    purpose: Annotated[str, Field(strict=True, min_length=1, max_length=500)]
    max_items: Annotated[int, Field(strict=True, ge=1, le=100)]
    max_cost_microusd: Count
    expires_at: ActionTime

    @model_validator(mode='after')
    def immutable_identity(self):
        if self.intent_id != capture_intent_id(self.registry_id,self.generation_id,self.turn_id):
            raise ValueError('Capture identity must belong to its server turn')
        if not self.purpose.strip():
            raise ValueError('Capture purpose must not be blank')
        return self


class CaptureQuote(ActionScope):
    kind: Literal['capture_quote']
    quote_id: UUID
    intent_id: UUID
    intent_sha256: Hash
    revision: Revision
    config_revision: Revision
    source_revision: Revision
    query_revision: Revision
    connector_revision: Revision
    budget_round_id: UUID
    budget_round_revision: Revision
    max_items: Annotated[int, Field(strict=True, ge=1, le=100)]
    max_cost_microusd: Count
    expires_at: ActionTime


class CaptureDecision(ActionScope):
    kind: Literal['capture_decision']
    decision_id: UUID
    intent_id: UUID
    quote_id: UUID
    quote_sha256: Hash
    expected_operation_revision: Revision
    decision: Literal['approve', 'reject']


class CaptureVersion(Record):
    observation_key: Hash
    document_key: Hash
    content_sha256: Hash

    @model_validator(mode='after')
    def content_identity(self):
        if self.observation_key!=sha256(encoded([1,self.document_key,self.content_sha256])):
            raise ValueError('Observation identity must bind its exact document and content version')
        return self


class CaptureMember(CaptureVersion):
    observation_message_ids: Annotated[list[UUID],Field(min_length=1,max_length=100)]


class CaptureSnapshot(Record):
    """Writer-only query output from executions/discoveries/history VERSION AS OF."""
    registry_id: UUID
    generation_id: UUID
    execution_id: UUID
    publication_id: Hash
    serving_versions: Annotated[dict[Code,Count],Field(min_length=4,max_length=26)]
    execution_payload_sha256: Hash
    observations: Annotated[list[CaptureVersion],Field(max_length=1000)]


class CapturePublication(ActionScope):
    """Writer-issued immutable proof; validating shape never authenticates its issuer."""
    kind: Literal['capture_publication']
    publication_id: Hash
    published_at: ActionTime
    capture_config_revision: Revision
    publication_config_revision: Revision
    serving_versions: Annotated[dict[Code,Count], Field(min_length=4,max_length=26)]
    publication_message_ids: Annotated[list[Name], Field(min_length=1,max_length=10000)]
    execution_id: UUID
    intent_id: UUID | None
    execution_message_id: UUID
    execution_payload_sha256: Hash
    execution_source_sequence: Revision
    execution_entry_sha256: Hash
    execution_previous_entry_sha256: Hash | None
    observation_message_ids: Annotated[list[UUID], Field(max_length=100)]
    processing_receipt_ids: Annotated[list[UUID], Field(min_length=1, max_length=101)]
    record_count: Count
    members: Annotated[list[CaptureMember],Field(max_length=1000)]

    @model_validator(mode='after')
    def observed_population(self):
        if self.publication_config_revision<self.capture_config_revision:
            raise ValueError('A publication cannot precede its capture configuration')
        if bool(self.record_count) != bool(self.observation_message_ids):
            raise ValueError('A positive population needs observations; zero does not invent them')
        for values in (self.observation_message_ids,self.processing_receipt_ids,self.publication_message_ids):
            if len(values) != len(set(values)):
                raise ValueError('Publication references must be distinct')
        if str(self.execution_message_id) not in self.publication_message_ids:
            raise ValueError('Execution completion must belong to this exact publication')
        tables={'ai_social_document_current','ai_social_observations','ai_social_discoveries','ai_social_executions'}
        if not tables.issubset(self.serving_versions):
            raise ValueError('Publication must pin document and capture provenance versions')
        scope={'registry_id':str(self.registry_id),'generation_id':str(self.generation_id)}
        if self.publication_id!=sha256(encoded([scope,self.publication_config_revision,self.serving_versions])):
            raise ValueError('Publication hash does not match its scope, revision and serving versions')
        if (self.execution_source_sequence==1)!=(self.execution_previous_entry_sha256 is None):
            raise ValueError('Stream sequence one alone has no predecessor')
        return self

    @model_validator(mode='after')
    def exact_capture_members(self):
        if len({row.observation_key for row in self.members})!=len(self.members):
            raise ValueError('Capture members are distinct observed content versions')
        messages={message for row in self.members for message in row.observation_message_ids}
        if messages!=set(self.observation_message_ids):
            raise ValueError('Member provenance must cover the exact observation messages')
        if any(len(row.observation_message_ids)!=len(set(row.observation_message_ids)) for row in self.members):
            raise ValueError('Member message references must be distinct')
        return self


class CaptureStatus(ActionScope):
    kind: Literal['capture_status']
    intent_id: UUID
    revision: Revision
    updated_at: ActionTime
    state: Literal['proposed','awaiting_confirmation','starting','running','captured',
        'awaiting_gold','ready','rejected','expired','cancelled','failed','uncertain']
    quote: CaptureQuote | None
    execution_id: UUID | None
    spent_microusd: Count | None
    failure_code: Code | None
    publication: CapturePublication | None

    @model_validator(mode='after')
    def progress_state(self):
        if self.state in ('starting','running','captured','awaiting_gold','ready','uncertain') and self.execution_id is None:
            raise ValueError('An attempted start retains its execution identity')
        if self.state=='awaiting_confirmation' and self.quote is None:
            raise ValueError('Confirmation requires a current quote')
        if (self.state=='ready') != (self.publication is not None):
            raise ValueError('Only committed Gold has a ready publication')
        if self.state in ('failed','uncertain') and self.failure_code is None:
            raise ValueError('Failure has a reviewed code, not a provider body')
        return self

    @model_validator(mode='after')
    def quote_scope(self):
        if self.quote and (self.quote.intent_id!=self.intent_id or any(
                getattr(self.quote,key)!=getattr(self,key) for key in ActionScope.model_fields)):
            raise ValueError('Quote belongs to another operation or negotiated scope')
        return self

    @model_validator(mode='after')
    def publication_scope(self):
        if self.publication and (self.publication.execution_id!=self.execution_id
                or self.publication.intent_id not in (None,self.intent_id) or any(
                    getattr(self.publication,key)!=getattr(self,key) for key in ActionScope.model_fields)):
            raise ValueError('Publication belongs to another execution, intent or negotiated scope')
        return self


class GoldPublication(Record):
    module_id: Literal['ai_social_network']
    publication_id: Hash
    published_at: ActionTime
    registry_id: UUID
    generation_id: UUID
    config_revision: Revision
    pending_files: Count | None
    failed_files: Count | None
    current_version: Count
    history_version: Count
    daily_version: Count


class GoldSource(Record):
    post_key: Hash
    comment_key: Hash | None
    document_key: Hash
    parent_document_key: Hash | None
    platform: Network | None
    url: Url
    published_at: ActionTime | None
    commented_at: ActionTime | None
    observed_at: ActionTime | None
    document_type: Literal['post','comment','page','indicator']
    capture_scope: Literal['requested_results','top_comments','partial_comments','page_snapshot'] | None


VisualInt=Annotated[int,Field(strict=True,ge=-9007199254740991,le=9007199254740991)]
VisualFloat=Annotated[float,Field(strict=True,allow_inf_nan=False,ge=-9007199254740991,le=9007199254740991)]
VisualCell=Annotated[str,Field(strict=True,max_length=2000)] | VisualInt | VisualFloat | None


class GoldVisualization(Record):
    version: Literal[1]
    type: Literal['table','bar','pie','line']
    title: Annotated[str,Field(strict=True,min_length=1,max_length=200)]
    columns: Annotated[list[Annotated[str,Field(strict=True,min_length=1,max_length=200)]],Field(min_length=1,max_length=12)]
    rows: Annotated[list[Annotated[list[VisualCell],Field(max_length=12)]],Field(max_length=50)]
    caption: Annotated[str,Field(strict=True,max_length=500)]

    @model_validator(mode='after')
    def rectangular(self):
        if any(len(row)!=len(self.columns) for row in self.rows):
            raise ValueError('Visualization rows must match their declared columns')
        return self


class GoldQuery(Record):
    tool: Literal['summary','ranking','trend','comments','search','history','details']
    args: Annotated[dict[Literal['date_from','date_to','text','post_key','entity','platform','platforms',
        'period','entity_scope','metric','limit','document_type'],JsonValue],Field(max_length=12)]


class ChatGoldData(Record):
    sources: Annotated[list[GoldSource],Field(max_length=8)]
    publication: GoldPublication
    search_context: ChatContext
    query_context: Annotated[list[GoldQuery],Field(max_length=4)]
    visualization: GoldVisualization | None
    visualization_error: Annotated[str,Field(strict=True,min_length=1,max_length=300)] | None = None

    @model_validator(mode='after')
    def publication_scope(self):
        if any(getattr(self.publication,key)!=getattr(self.search_context,key)
                for key in ('registry_id','generation_id','config_revision')):
            raise ValueError('Gold data must retain its published search scope')
        return self


class ActionChatResponse(ActionScope):
    kind: Literal['chat_response']
    version: Literal[3]
    turn_id: UUID
    capture_config_revision: Revision
    answer: Annotated[str,Field(strict=True,min_length=1,max_length=1800)]
    gold: ChatGoldData | None
    gold_unavailable_reason: Literal['no_publication','stale_publication','out_of_scope',
        'not_requested','query_failed','planning_failed'] | None
    actions: Annotated[list[CaptureIntent],Field(max_length=1)]

    @model_validator(mode='after')
    def proposed_only(self):
        for intent in self.actions:
            if intent.turn_id!=self.turn_id or intent.config_revision!=self.capture_config_revision or any(
                    getattr(intent,key)!=getattr(self,key) for key in ActionScope.model_fields):
                raise ValueError('An action must retain its turn and negotiated pins')
        return self

    @model_validator(mode='after')
    def gold_availability(self):
        if (self.gold is None)!=(self.gold_unavailable_reason is not None):
            raise ValueError('Missing Gold requires an explicit reason, never a fabricated empty result')
        if self.gold and any(getattr(self.gold.search_context,key)!=getattr(self,key)
                for key in ('registry_id','generation_id')):
            raise ValueError('Gold response belongs to another installation')
        if not self.answer.strip() or '```' in self.answer:
            raise ValueError('A chat response requires a plain-language answer')
        return self


ActionDocument = Annotated[Union[ActionChatInput,CaptureIntent,CaptureQuote,CaptureDecision,
    CapturePublication,CaptureStatus,ActionChatResponse], Field(discriminator='kind')]
ACTION_ADAPTER = TypeAdapter(ActionDocument)


def action_schema():
    return {'$schema':'https://json-schema.org/draft/2020-12/schema',
        '$id':'urn:ai-social-network:sonda:chat-actions:'+ACTIONS_VERSION,**ACTION_ADAPTER.json_schema()}


def action_fingerprint(value):
    return sha256(encoded(value.model_dump(mode='json')))


def parse_action(raw, *, capability_sha256, core_contract_sha256, registry_id, generation_id):
    """Validate a document against trusted installed pins and authenticated scope."""
    import json
    if not isinstance(raw,bytes) or len(raw)>ACTION_MAX_BYTES:
        raise ValueError('Bounded action document bytes are required')
    value=json.loads(raw,object_pairs_hook=no_duplicate_keys,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON number')))
    reject_secrets(value)
    document=ACTION_ADAPTER.validate_python(value)
    if document.capability_sha256!=capability_sha256 or document.core_contract_sha256!=core_contract_sha256:
        raise ValueError('Chat action capability or core pin differs from the installation')
    context=document.search_context if isinstance(document,ActionChatInput) else document
    if context.registry_id!=UUID(str(registry_id)) or context.generation_id!=UUID(str(generation_id)):
        raise ValueError('Chat action belongs to another installation scope')
    return document


def check_capture_intent(turn, intent, *, now):
    """Bind a proposal to the trusted server turn before broker policy evaluation."""
    if intent.turn_id!=turn.turn_id or any(getattr(intent,key)!=getattr(turn,key) for key in ActionPin.model_fields):
        raise ValueError('Intent belongs to another trusted turn or capability')
    context=turn.search_context
    if (intent.registry_id!=context.registry_id or intent.generation_id!=context.generation_id
            or intent.config_revision!=turn.capture_config_revision):
        raise ValueError('Intent differs from the authenticated search scope')
    if not turn.created_at<=now<intent.expires_at<=turn.expires_at:
        raise ValueError('Intent must remain within the original live turn')
    _check_capture_option(turn,intent)
    return intent


def check_chat_response(turn,response,*,now):
    if (response.turn_id!=turn.turn_id or response.capture_config_revision!=turn.capture_config_revision
            or any(getattr(response,key)!=getattr(turn,key) for key in ActionPin.model_fields)):
        raise ValueError('Response belongs to another exact server turn')
    context=turn.search_context
    if response.registry_id!=context.registry_id or response.generation_id!=context.generation_id:
        raise ValueError('Response belongs to another installation')
    if response.gold and response.gold.search_context!=context:
        raise ValueError('Gold response changed the requested reading scope')
    for intent in response.actions:
        check_capture_intent(turn,intent,now=now)
    return response


def _check_capture_option(turn,intent):
    option=next((row for row in turn.capture_options if row.option_id==intent.capture_option_id),None)
    if option is None:
        raise ValueError('No capture option is authorized for this proposal')
    fields=('source_id','source_revision','query_id','query_revision')
    if any(getattr(intent,key)!=getattr(option,key) for key in fields):
        raise ValueError('A model cannot replace the reviewed capture option')
    if intent.max_items>option.max_items or intent.max_cost_microusd>option.max_cost_microusd:
        raise ValueError('Capture proposal expands the server option limits')
    if intent.target_override is not None:
        if not option.target_override_allowed:
            raise ValueError('The capture option does not permit target overrides')
        if action_public_target(intent.target_override,option.allowed_domains)!=intent.target_override:
            raise ValueError('Target overrides must use their canonical public URL')


def check_capture_quote(intent, quote, *, now):
    """Structural correlation only; broker must also authorize against current state."""
    if quote.intent_id!=intent.intent_id or quote.intent_sha256!=action_fingerprint(intent):
        raise ValueError('Quote belongs to another immutable intent')
    fields=(*ActionScope.model_fields,'config_revision','source_revision','query_revision')
    if any(getattr(quote,key)!=getattr(intent,key) for key in fields):
        raise ValueError('Quote scope or approved revisions differ')
    if quote.max_items>intent.max_items or quote.max_cost_microusd>intent.max_cost_microusd:
        raise ValueError('Quote cannot expand the proposed limits')
    if not now<quote.expires_at<=intent.expires_at:
        raise ValueError('Quote must be live within the original intent expiry')
    return quote


def check_capture_decision(quote, decision, *, operation_revision, now):
    """An operator identity/permission and atomic revision check are still required."""
    if (decision.intent_id!=quote.intent_id or decision.quote_id!=quote.quote_id
            or decision.quote_sha256!=action_fingerprint(quote)):
        raise ValueError('Decision belongs to another exact quote')
    if any(getattr(decision,key)!=getattr(quote,key) for key in ActionScope.model_fields):
        raise ValueError('Decision belongs to another scope or capability')
    if decision.expected_operation_revision!=operation_revision or now>=quote.expires_at:
        raise ValueError('Stale quote or operation revision')
    return decision


def capture_receipt_path(registry_id,generation_id,execution_id):
    return ('ai_social_network/registries/'+str(UUID(str(registry_id)))+'/generations/'
        +str(UUID(str(generation_id)))+'/control/chat_actions/v1/capture_receipts/'
        +str(UUID(str(execution_id)))+'.json')


def parse_capture_receipt(raw, *, execution_id, previous_sha256=None, **scope):
    if previous_sha256 is not None and sha256(raw)!=previous_sha256:
        raise ValueError('An immutable capture receipt changed')
    receipt=parse_action(raw,**scope)
    if not isinstance(receipt,CapturePublication) or receipt.execution_id!=UUID(str(execution_id)):
        raise ValueError('Receipt belongs to another exact execution')
    return receipt


def check_capture_publication(receipt,execution_raw,entry,receipts,publication,*,observation_raw,snapshot):
    """Writer correlation against authenticated core transport and native Gold.

    The caller must load the protected entry and exact native publication. Client
    or model claims are not evidence; execution bytes are checked against the entry.
    """
    execution=parse(execution_raw,registry_id=receipt.registry_id,generation_id=receipt.generation_id,
        contract_sha256=receipt.core_contract_sha256)
    if (not isinstance(execution,ExecutionMessage) or not isinstance(entry,StreamEntry)
            or any(not isinstance(row,ReceiptMessage) for row in receipts)):
        raise ValueError('Verified core execution and transport evidence are required')
    core_scope=(receipt.registry_id,receipt.generation_id,receipt.core_contract_sha256)
    for document in (entry,*receipts):
        if (document.registry_id,document.generation_id,document.contract_sha256)!=core_scope:
            raise ValueError('Core evidence belongs to another installation')
    _check_completed_capture(receipt,execution)
    _check_execution_stream(receipt,execution,entry,execution_raw)
    _check_applied_capture_receipts(receipt,receipts)
    _check_capture_publication_row(receipt,publication)
    _check_capture_members(receipt,execution,observation_raw)
    _check_capture_snapshot(receipt,snapshot)
    return receipt


def _check_completed_capture(receipt,execution):
    result=execution.payload
    if (result.status!='completed' or result.request_id is not None or result.execution_id!=receipt.execution_id
            or execution.message_id!=receipt.execution_message_id or result.config_revision!=receipt.capture_config_revision
            or result.record_count!=receipt.record_count
            or set(result.observation_message_ids)!=set(receipt.observation_message_ids)):
        raise ValueError('Receipt must describe the exact completed operator capture')
    if sha256(encoded(result.model_dump(mode='json')))!=receipt.execution_payload_sha256:
        raise ValueError('Execution payload fingerprint differs from the committed capture')


def _check_execution_stream(receipt,execution,entry,execution_raw):
    if (entry.message_id!=execution.message_id or entry.message_kind!='execution_result'
            or entry.direction!='huginn_to_aidp' or entry.sequence!=receipt.execution_source_sequence
            or sha256(encoded(entry.model_dump(mode='json')))!=receipt.execution_entry_sha256
            or entry.previous_entry_sha256!=receipt.execution_previous_entry_sha256
            or sha256(execution_raw)!=entry.message_sha256 or len(execution_raw)!=entry.message_bytes):
        raise ValueError('Execution stream proof differs from the exact input bytes')


def _check_applied_capture_receipts(receipt,receipts):
    expected={receipt.execution_message_id,*receipt.observation_message_ids}
    if any(not isinstance(row,ReceiptMessage) or row.payload.status!='applied' for row in receipts):
        raise ValueError('Capture inputs require applied native processing receipts')
    if ({row.message_id for row in receipts}!=set(receipt.processing_receipt_ids)
            or len(receipts)!=len(expected) or {row.payload.source_message_id for row in receipts}!=expected):
        raise ValueError('Applied receipt identities do not cover the exact capture inputs')
    execution_receipt=next(row for row in receipts if row.payload.source_message_id==receipt.execution_message_id)
    if execution_receipt.payload.source_sequence!=receipt.execution_source_sequence:
        raise ValueError('Applied execution receipt has another transport sequence')
    if any(row.payload.source_sequence>=receipt.execution_source_sequence
            for row in receipts if row.payload.source_message_id!=receipt.execution_message_id):
        raise ValueError('Observation inputs must be applied before their capture completion')


def _check_capture_publication_row(receipt,publication):
    import json
    scope={'registry_id':str(receipt.registry_id),'generation_id':str(receipt.generation_id),
        'publication_id':receipt.publication_id,'config_revision':receipt.publication_config_revision}
    if any(publication.get(key)!=value for key,value in scope.items()):
        raise ValueError('Native publication identity differs')
    if (json.loads(publication['versions_json'],object_pairs_hook=no_duplicate_keys)!=receipt.serving_versions
            or set(json.loads(publication['message_ids_json']))!=set(receipt.publication_message_ids)
            or TypeAdapter(ActionTime).validate_python(publication['published_at'])!=receipt.published_at):
        raise ValueError('Native publication versions, inputs or time differ')


def _check_capture_members(receipt,execution,observation_raw):
    if len(observation_raw)!=len(receipt.observation_message_ids):
        raise ValueError('The exact protected observation inputs are required')
    versions={}
    ids=[]
    record_count=0
    for raw in observation_raw:
        message=parse(raw,registry_id=receipt.registry_id,generation_id=receipt.generation_id,
            contract_sha256=receipt.core_contract_sha256)
        _check_capture_observation_scope(message,execution)
        ids.append(message.message_id)
        record_count+=len(message.payload.observations)
        for item in message.payload.observations:
            key=(item.observation_key,item.document_key,item.content_sha256)
            versions.setdefault(key,set()).add(message.message_id)
    if len(ids)!=len(set(ids)) or set(ids)!=set(receipt.observation_message_ids):
        raise ValueError('Observation input identities differ from the capture')
    if record_count!=receipt.record_count:
        raise ValueError('Record count must count the normalized observations, not messages or current documents')
    expected={(row.observation_key,row.document_key,row.content_sha256):set(row.observation_message_ids)
        for row in receipt.members}
    if versions!=expected:
        raise ValueError('Member provenance differs from the protected observation content')


def _check_capture_observation_scope(message,execution):
    if not isinstance(message,ObservationsMessage) or message.payload.initiated_by!='operator':
        raise ValueError('A chat capture requires its confirmed operator observation messages')
    fields=('execution_id','config_revision','query_id','query_revision','budget_round_id')
    if any(getattr(message.payload,key)!=getattr(execution.payload,key) for key in fields):
        raise ValueError('Observation provenance belongs to another capture')


def _check_capture_snapshot(receipt,snapshot):
    if not isinstance(snapshot,CaptureSnapshot):
        raise ValueError('An exact native VERSION AS OF capture snapshot is required')
    fields=('registry_id','generation_id','execution_id','publication_id','serving_versions','execution_payload_sha256')
    if any(getattr(receipt,key)!=getattr(snapshot,key) for key in fields):
        raise ValueError('Capture snapshot differs from the exact committed scope or versions')
    actual={(row.observation_key,row.document_key,row.content_sha256) for row in snapshot.observations}
    expected={(row.observation_key,row.document_key,row.content_sha256) for row in receipt.members}
    if len(snapshot.observations)!=len(actual) or actual!=expected:
        raise ValueError('Capture content versions are missing or different in the final snapshot')
