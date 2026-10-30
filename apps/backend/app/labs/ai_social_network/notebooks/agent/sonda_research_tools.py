"""Case-local research tools prepare bounded specifications without side effects.

No provider call, capture, reservation, SQL, outbox or second writer lives here.
The caller passes authenticated configuration/policies and commits accepted
case_research output through the existing Gold writer and Huginn broker.
"""
from __future__ import annotations

import copy
import json
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, TypeAdapter, create_model
from app.sonda_activity import current_activity

from app.sonda_contract import Configuration, Evidence, Hash, Record, Revision, canonical_url, encoded, reject_secrets, sha256
from app.sonda_source_limits import SourcePolicy
from app.labs.ai_social_network.notebooks.sonda_tools import tool_catalog_rows
from app.labs.ai_social_network.notebooks.agent.sonda_study import Plan, ResearchStep, approved, eligible_queries, target_allowed
from app.labs.ai_social_network.notebooks.agent.sonda_x_research import CaseResearchObservation, x_research_normalized_target
from app.labs.ai_social_network.notebooks.sonda_tools import x_research_capable

RESEARCH_TOOL_CALL_LIMIT = 20
RESEARCH_TOOL_SCHEMA_BYTES = 98304
RESEARCH_PREPARED_MESSAGE = 'Solicitud preparada para validación; todavía no está en cola ni se ha ejecutado.'
CONFIGURATION_REVIEW_TOOL = 'sonda_request_configuration_review'


class ResearchContext(Record):
    observations: Annotated[list[CaseResearchObservation],Field(max_length=100)] = Field(default_factory=list)
    evidence: Annotated[list[Evidence],Field(max_length=100)] = Field(default_factory=list)
    relevant_keys: Annotated[list[Hash],Field(max_length=100)] = Field(default_factory=list)


def _research_source_policies(current, policies):
    if policies is None:
        return {}
    policies = [SourcePolicy.model_validate(policy) for policy in policies]
    indexed = {str(policy.source_id): policy for policy in policies}
    expected = {source['source_id']: source['revision'] for source in current['sources']}
    if len(indexed) != len(policies) or {key: policy.source_revision for key, policy in indexed.items()} != expected:
        raise ValueError('Research tools require complete exact source policies')
    return indexed


def _research_item_cap(current, query, connector, policy):
    price = connector['max_cost_microusd_per_item']
    if price is None:
        return 0
    items = min(query['max_items'], current['policy']['max_items_per_round'])
    cost = min(query['max_charge_microusd'], current['policy']['round_budget_microusd'],
        current['policy']['research_reserve_microusd'])
    if policy is not None:
        if policy.max_starts_per_round == 0:
            return 0
        if policy.max_items_per_execution is not None:
            items = min(items, policy.max_items_per_execution)
        if policy.max_cost_per_round_microusd is not None:
            cost = min(cost, policy.max_cost_per_round_microusd)
    return min(items, cost // price) if price else items


def _research_effective_target(override, source, query, connector, research_context=None):
    if not target_allowed(override, source, query, connector,research_context=research_context):
        raise ValueError('Research target is outside the reviewed source/query scope')
    target = override or query['target']
    if query['mode'] in ('pages', 'indicators'):
        target = canonical_url(target)
        if urlsplit(target).port not in (None, 80, 443):
            raise ValueError('Research targets require standard HTTP(S) ports')
    return target


def _research_argument_schema(name, cap, override_schema):
    fields = {'purpose': (Annotated[str, Field(strict=True, min_length=1, max_length=2000)], ...),
        'max_items': (Annotated[int, Field(strict=True, ge=1, le=cap)], ...)}
    if override_schema is not None:
        target=override_schema['anyOf'][0]
        fields['target_override'] = (Annotated[str, Field(strict=True, min_length=1,
            max_length=target['maxLength'],pattern=target['pattern'])] | None, None)
    return create_model('ResearchInput_'+name, __base__=Record, **fields)


def _build_research_tool(metadata, source, query, connector, cap, research_context):
    from langchain_core.tools import StructuredTool
    override_schema = json.loads(metadata['input_schema_json'])['properties'].get('target_override')
    schema = _research_argument_schema(metadata['tool_name'], cap, override_schema)

    def prepare(**arguments):
        arguments = schema.model_validate(arguments).model_dump(mode='json')
        reject_secrets(arguments)
        if not arguments['purpose'].strip():
            raise ValueError('Research purpose must contain meaningful text')
        override = arguments.get('target_override')
        target = _research_effective_target(override, source, query, connector,research_context)
        identity_target=x_research_normalized_target(target) if x_research_capable(source,query,connector) else target
        step = ResearchStep.model_validate({'step': 'tool-'+sha256(encoded([query['query_id'], identity_target])),
            'query_id': query['query_id'], 'purpose': arguments['purpose'],
            'max_items': arguments['max_items'], 'target_override': target if override is not None else None})
        return RESEARCH_PREPARED_MESSAGE, step.model_dump(mode='json')

    return StructuredTool.from_function(func=prepare, name=metadata['tool_name'],
        description=metadata['description']+'. Prepara investigación; no ejecuta capturas ni devuelve resultados nuevos.',
        args_schema=schema, infer_schema=False, response_format='content_and_artifact',
        metadata={'target_normalization':'x_nfc_lower'} if x_research_capable(source,query,connector) else None)


def _configuration_review_tool():
    from langchain_core.tools import StructuredTool
    schema = create_model('ResearchConfigurationReviewInput', __base__=Record,
        purpose=(Annotated[str, Field(strict=True, min_length=1, max_length=2000)], ...))

    def prepare_review(**arguments):
        arguments = schema.model_validate(arguments).model_dump(mode='json')
        if not arguments['purpose'].strip():
            raise ValueError('Configuration review purpose must contain meaningful text')
        return ('Revisión preparada; todavía no se ha propuesto ni aplicado ningún cambio.',
            {'request_configuration_review': True})

    return StructuredTool.from_function(func=prepare_review, name=CONFIGURATION_REVIEW_TOOL,
        description='Solicita la etapa existente de propuestas para revisión humana; no es una fuente de datos ni cambia la configuración.',
        args_schema=schema, infer_schema=False, response_format='content_and_artifact')


def build_research_tools(configuration, criterion, approved_query_ids, *, source_policies=None,
        include_configuration_review=False, research_context=None):
    """Build isolated closures from an authenticated case/configuration snapshot.

    None policies means the caller established the genuine legacy absence path;
    an expected-but-unavailable sidecar must be rejected before calling this.
    Criterion identity/revision selects the authoritative current criterion; any
    extra projected criterion fields never override approved configuration.
    """
    current = Configuration.model_validate(copy.deepcopy(configuration)).model_dump(mode='json')
    context = ResearchContext.model_validate(copy.deepcopy(research_context or {})).model_dump(mode='json')
    selected = {'criterion_id': str(UUID(str(criterion['criterion_id']))),
        'revision': TypeAdapter(Revision).validate_python(criterion['revision'])}
    approved_ids = {str(UUID(str(query_id))) for query_id in approved_query_ids}
    policies = _research_source_policies(current, source_policies)
    metadata = {row['query_id']: row for row in tool_catalog_rows(current, validated=True)}
    sources = {row['source_id']: row for row in current['sources']}
    connectors = {row['connector_id']: row for row in current['connectors']}
    tools = []
    for query in eligible_queries(current, selected):
        if query['query_id'] not in approved_ids:
            continue
        source = sources[query['source_id']]
        connector = connectors[source['connector_id']]
        cap = _research_item_cap(current, query, connector, policies.get(source['source_id']))
        if cap > 0:
            _research_effective_target(None, source, query, connector)
            tools.append(_build_research_tool(metadata[query['query_id']], source, query, connector, cap, context))
    current_criterion = next((row for row in current['criteria']
        if row['criterion_id'] == selected['criterion_id'] and row['revision'] == selected['revision']), None)
    if include_configuration_review and current_criterion is not None and approved(current_criterion):
        tools.append(_configuration_review_tool())
    return tools


def _research_tool_calls(response, tools):
    from langchain_core.messages import AIMessage, AIMessageChunk
    if not isinstance(response, AIMessage) or isinstance(response, AIMessageChunk) or response.invalid_tool_calls:
        raise ValueError('Research planner requires a complete valid AIMessage')
    calls = response.tool_calls
    if len(calls) > RESEARCH_TOOL_CALL_LIMIT:
        raise ValueError('Research planner exceeded its bounded tool call batch')
    seen = set()
    for call in calls:
        if not isinstance(call, dict) or call.get('type') != 'tool_call':
            raise ValueError('Research planner returned a malformed tool call')
        if call.get('name') not in tools or not isinstance(call.get('args'), dict):
            raise ValueError('Research planner selected an unavailable tool or malformed arguments')
        identity = call.get('id')
        if not isinstance(identity, str) or not 1 <= len(identity) <= 200 or identity in seen:
            raise ValueError('Research tool call identities must be distinct bounded strings')
        seen.add(identity)
    return calls


def _research_tool_artifact(call, result, prepared, *, normalizer=None):
    from langchain_core.messages import ToolMessage
    if not isinstance(result, ToolMessage) or result.tool_call_id != call['id']:
        raise ValueError('Research tool result does not match its invocation')
    if call['name'] == CONFIGURATION_REVIEW_TOOL:
        if result.artifact != {'request_configuration_review': True}:
            raise ValueError('Configuration review tool returned an invalid artifact')
        return True
    step = ResearchStep.model_validate(result.artifact).model_dump(mode='json')
    previous = prepared.get(step['step'])
    if previous is not None:
        left,right = dict(previous),dict(step)
        if normalizer and left['target_override'] is not None and right['target_override'] is not None:
            left['target_override'],right['target_override'] = normalizer(left['target_override']),normalizer(right['target_override'])
        if left != right:
            raise ValueError('Conflicting duplicate research invocations')
    else:
        prepared[step['step']] = step
    return False


async def plan_research_tools(invoker, messages, tools):
    """One checkpointed model selection followed by pure local tool validation.

    The caller owns its durable inference task. This adapter never retries the
    model or treats a prepared specification as a committed capture request.
    """
    from langchain_core.tools import StructuredTool
    if not tools:
        return Plan().model_dump(mode='json')
    if any(not isinstance(tool, StructuredTool) for tool in tools):
        raise ValueError('Research planner requires case-local StructuredTool instances')
    indexed = {tool.name: tool for tool in tools}
    if len(indexed) != len(tools):
        raise ValueError('Research tool names must be distinct within one case')
    definitions = [{'name': tool.name, 'description': tool.description,
        'schema': tool.args_schema.model_json_schema()} for tool in tools]
    if len(encoded(definitions)) > RESEARCH_TOOL_SCHEMA_BYTES:
        raise ValueError('Research tool definitions exceed the bounded inference schema window')
    response = await invoker.bind_tools(tools, parallel_tool_calls=False).ainvoke(messages)
    prepared = {}
    propose = False
    for call in _research_tool_calls(response, indexed):
        async def prepare_tool():
            result = await indexed[call['name']].ainvoke(call)
            normalizer = (x_research_normalized_target
                if (indexed[call['name']].metadata or {}).get('target_normalization')=='x_nfc_lower' else None)
            return _research_tool_artifact(call, result, prepared,normalizer=normalizer)
        activity=current_activity()
        if activity:
            async with activity.tool('research_preparation',call['name']):
                review=await prepare_tool()
        else:
            review=await prepare_tool()
        if review and propose:
            raise ValueError('Configuration review may be requested at most once')
        propose = propose or review
    return Plan(requests=list(prepared.values()), propose=propose).model_dump(mode='json')
