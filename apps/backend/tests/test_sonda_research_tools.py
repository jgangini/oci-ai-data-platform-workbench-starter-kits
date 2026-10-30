"""Real LangChain tool execution, fake model selection, no cloud/provider calls."""
import asyncio
import copy
import json
from pathlib import Path
from uuid import UUID

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
import pytest

from app import sonda_contract as core
from app.labs.ai_social_network.notebooks.agent import sonda_research_tools as research
from app.labs.ai_social_network.notebooks.agent.sonda_study import ResearchStep, rule_context
from app.labs.ai_social_network.notebooks.sonda_tools import tool_catalog_rows

ROOT = Path(__file__).resolve().parents[3]


def configuration():
    value = json.loads((ROOT/'contracts/sonda/v1/fixtures/01-configuration.json').read_text(encoding='utf-8'))['payload']
    return core.Configuration.model_validate(value).model_dump(mode='json')


def build(config=None, *, source_policies=None, approved=None, criterion=None, include_configuration_review=False):
    config = config or configuration()
    return research.build_research_tools(config, criterion or rule_context(config['criteria'][0]),
        approved if approved is not None else [query['query_id'] for query in config['queries']],
        source_policies=source_policies, include_configuration_review=include_configuration_review)


def arguments(**overrides):
    return {'purpose': 'Corroborar un cambio de precio.', 'max_items': 2, **overrides}


def call(tool, *, identity='call-1', args=None):
    return {'type': 'tool_call', 'name': tool.name, 'id': identity, 'args': args or arguments()}


class Invoker:
    def __init__(self, response=None):
        self.bound, self.invocations = [], []
        self.response = response

    def bind_tools(self, tools, **kwargs):
        self.bound.append((tools, kwargs))
        return self

    async def ainvoke(self, messages):
        self.invocations.append(messages)
        return self.response


def run(invoker, tools):
    return asyncio.run(research.plan_research_tools(invoker, [HumanMessage(content='Corrobora el anuncio.')], tools))


def test_factory_uses_exact_projection_names_and_hides_provider_identity():
    config = configuration()
    original = copy.deepcopy(config)
    tools = build(config)
    expected = {row['tool_name'] for row in tool_catalog_rows(config)}
    assert {tool.name for tool in tools} == expected and len(tools) == 6
    for tool in tools:
        schema = tool.args_schema.model_json_schema()
        assert set(schema['properties']) == {'purpose', 'max_items', 'target_override'}
        assert schema['additionalProperties'] is False
        assert schema['required'] == ['purpose', 'max_items']
        assert not {'query_id', 'source_id', 'actor_id', 'credentials', 'max_cost_microusd', 'step'} & set(schema['properties'])
    assert config == original


def test_real_tool_returns_prepared_artifact_and_server_stable_step():
    tools = build()
    query = next(query for query in configuration()['queries'] if tools[0].name == 'sonda_query_'+UUID(query['query_id']).hex)
    result = tools[0].invoke(call(tools[0]))
    assert isinstance(result, ToolMessage) and result.tool_call_id == 'call-1'
    assert result.content == research.RESEARCH_PREPARED_MESSAGE
    assert 'todavía no está en cola' in result.content
    artifact = ResearchStep.model_validate(result.artifact)
    assert str(artifact.query_id) == query['query_id']
    assert artifact.step == 'tool-'+core.sha256(core.encoded([query['query_id'], core.canonical_url(query['target'])]))
    retry = tools[0].invoke(call(tools[0], identity='different-provider-call-id', args=arguments(purpose='Otra redacción de la misma comprobación.')))
    assert retry.artifact['step'] == artifact.step
    different = tools[0].invoke(call(tools[0], args=arguments(target_override='https://example.com/another')))
    assert different.artifact['step'] != artifact.step


@pytest.mark.parametrize('args', [
    arguments(max_items=True), arguments(max_items='2'), arguments(max_items=0), arguments(max_items=11),
    arguments(purpose=' '), arguments(purpose=3), arguments(purpose='x'*2001),
    arguments(query_id=str(UUID(int=99))), arguments(source_id=str(UUID(int=99))),
    arguments(actor_id='foreign/actor'), arguments(step='model-controlled-step'),
    arguments(max_cost_microusd=1), arguments(credentials={}),
])
def test_real_tool_rejects_overcaps_coercions_hidden_fields_and_freeform_step(args):
    tool = build()[0]
    with pytest.raises(ValueError):
        tool.invoke(call(tool, args=args))


@pytest.mark.parametrize('target', [
    'https://foreign.example/page', 'http://127.0.0.1/private', 'https://example.com.evil/page',
    'https://example.com:8443/page', 'https://user:password@example.com/page', 'file:///tmp/data',
    'https://localhost/page', 'https://example.com@127.0.0.1/page',
])
def test_target_override_stays_on_reviewed_public_host(target):
    tool = build()[0]
    with pytest.raises(ValueError):
        tool.invoke(call(tool, args=arguments(target_override=target)))


def test_default_query_target_must_also_be_public_and_reviewed():
    config = configuration()
    config['queries'][0]['target'] = 'https://foreign.example/page'
    with pytest.raises(ValueError, match='scope'):
        build(config)


def test_social_query_never_exposes_or_accepts_target_override():
    config = configuration()
    connector = config['connectors'][0]
    connector.update(operation='account_posts', modes=['posts'], research_fields=[])
    config['catalog_sha256'] = core.catalog_fingerprint(config['connectors'])
    config['sources'][0].update(category='social', platform='youtube', allowed_domains=[])
    for query in config['queries']:
        query.update(mode='posts', target='https://www.youtube.com/@oracle')
    tool = build(config)[0]
    assert set(tool.args_schema.model_json_schema()['properties']) == {'purpose', 'max_items'}
    with pytest.raises(ValueError):
        tool.invoke(call(tool, args=arguments(target_override=None)))
    assert tool.invoke(call(tool)).artifact['target_override'] is None


@pytest.mark.parametrize('mutation', [
    lambda config: config['criteria'][0].update(allow_research=False),
    lambda config: config['criteria'][0].update(status='paused'),
    lambda config: [query.update(research_allowed=False) for query in config['queries']],
    lambda config: [query.update(status='paused') for query in config['queries']],
    lambda config: config['connectors'][0].update(max_cost_microusd_per_item=None),
])
def test_only_current_approved_permitted_queries_with_known_caps_become_tools(mutation):
    config = configuration()
    mutation(config)
    config['catalog_sha256'] = core.catalog_fingerprint(config['connectors'])
    assert build(config) == []


def test_approved_query_case_filter_and_criterion_revision_cannot_expand_scope():
    config = configuration()
    assert len(build(config, approved=[config['queries'][0]['query_id'], str(UUID(int=999))])) == 1
    assert build(config, approved=[]) == []
    criterion = rule_context(config['criteria'][0])
    criterion['revision'] = 99
    assert build(config, criterion=criterion) == []
    config['criteria'][0]['query_ids'] = [config['queries'][0]['query_id']]
    assert len(build(config)) == 1


def source_policy(config=None, **changes):
    config = config or configuration()
    source = config['sources'][0]
    return {'source_id': source['source_id'], 'source_revision': source['revision'], 'policy_revision': 1,
        'max_items_per_execution': None, 'max_cost_per_round_microusd': None, 'max_starts_per_round': None, **changes}


def test_source_query_and_global_static_caps_are_minimized_without_reserving():
    config = configuration()
    tools = build(config, source_policies=[source_policy(config, max_items_per_execution=3, max_cost_per_round_microusd=2000)])
    assert all(tool.args_schema.model_json_schema()['properties']['max_items']['maximum'] == 2 for tool in tools)
    config['policy']['research_reserve_microusd'] = 1000
    assert all(tool.args_schema.model_json_schema()['properties']['max_items']['maximum'] == 1 for tool in build(config))
    for change in ({'max_items_per_execution': 0}, {'max_starts_per_round': 0}, {'max_cost_per_round_microusd': 0}):
        assert build(source_policies=[source_policy(**change)]) == []
    config['connectors'][0]['max_cost_microusd_per_item'] = 0
    config['catalog_sha256'] = core.catalog_fingerprint(config['connectors'])
    assert build(config, source_policies=[source_policy(config, max_cost_per_round_microusd=0)])


@pytest.mark.parametrize('policies', [[], [source_policy(source_revision=2)],
    [source_policy(), source_policy()], [source_policy(source_id=str(UUID(int=999)))]])
def test_supplied_source_policy_snapshot_must_be_complete_and_exact(policies):
    with pytest.raises(ValueError):
        build(source_policies=policies)


def test_concurrent_cases_use_isolated_immutable_closures():
    first = configuration()
    second = configuration()
    second['queries'][0]['target'] = 'https://example.com/second-case'
    second['queries'][0]['max_items'] = 2
    a, b = build(first)[0], build(second)[0]
    assert a.name == b.name
    first['queries'][0]['target'] = 'https://foreign.example/mutated'
    second['queries'][0]['target'] = 'https://foreign.example/mutated'
    async def both():
        return await asyncio.gather(a.ainvoke(call(a)), b.ainvoke(call(b)))
    results = asyncio.run(both())
    assert results[0].artifact['step'] != results[1].artifact['step']
    with pytest.raises(ValueError):
        b.invoke(call(b, args=arguments(max_items=3)))
    assert a.invoke(call(a, args=arguments(max_items=3))).artifact['max_items'] == 3


def test_planner_binds_once_executes_actual_tools_and_returns_existing_plan():
    tools = build()
    response = AIMessage(content='', tool_calls=[call(tools[0]), call(tools[1], identity='call-2')])
    invoker = Invoker(response)
    plan = run(invoker, tools)
    assert len(invoker.bound) == len(invoker.invocations) == 1
    assert invoker.bound[0] == (tools, {'parallel_tool_calls': False})
    assert len(plan['requests']) == 2 and plan['propose'] is False
    assert {request['query_id'] for request in plan['requests']} == {str(UUID(int=100)), str(UUID(int=101))}


def test_no_tools_does_not_bind_or_call_model():
    invoker = Invoker()
    assert run(invoker, []) == {'requests': [], 'propose': False}
    assert invoker.bound == invoker.invocations == []


def test_empty_model_tool_calls_do_not_invent_research():
    invoker = Invoker(AIMessage(content='No hace falta ampliar la investigación.'))
    assert run(invoker, build()) == {'requests': [], 'propose': False}
    assert len(invoker.invocations) == 1


@pytest.mark.parametrize('response', [
    {'tool_calls': []}, AIMessageChunk(content='partial'),
    AIMessage(content='', invalid_tool_calls=[{'name': 'invalid', 'args': '{bad', 'id': 'bad', 'error': 'parse error'}]),
])
def test_invalid_incomplete_model_response_does_not_become_a_plan(response):
    with pytest.raises(ValueError):
        run(Invoker(response), build())


def test_unknown_and_duplicate_tool_call_ids_fail_closed():
    tools = build()
    unknown = call(tools[0])
    unknown['name'] = 'unregistered_network_request'
    for calls in ([unknown], [call(tools[0]), call(tools[1])]):
        with pytest.raises(ValueError):
            run(Invoker(AIMessage(content='', tool_calls=calls)), tools)


def test_identical_semantic_retries_deduplicate_but_conflicting_calls_reject():
    tools = build()
    calls = [call(tools[0]), call(tools[0], identity='different-id')]
    assert len(run(Invoker(AIMessage(content='', tool_calls=calls)), tools)['requests']) == 1
    calls[1]['args'] = arguments(max_items=3)
    with pytest.raises(ValueError, match='Conflicting duplicate'):
        run(Invoker(AIMessage(content='', tool_calls=calls)), tools)


def test_bounded_batch_and_strict_tool_arguments_fail_without_model_retry():
    tools = build()
    calls = [call(tools[0], identity='call-'+str(index)) for index in range(21)]
    invoker = Invoker(AIMessage(content='', tool_calls=calls))
    with pytest.raises(ValueError, match='bounded tool call batch'):
        run(invoker, tools)
    assert len(invoker.invocations) == 1
    for bad in (arguments(max_items=True), arguments(max_items=999), arguments(step='freeform')):
        invoker = Invoker(AIMessage(content='', tool_calls=[call(tools[0], args=bad)]))
        with pytest.raises(ValueError):
            run(invoker, tools)
        assert len(invoker.invocations) == 1


def test_unsupported_model_does_not_silently_fallback_or_claim_requests():
    class Unsupported:
        def bind_tools(self, *args, **kwargs):
            raise NotImplementedError('Unsupported tool calling')
    with pytest.raises(NotImplementedError):
        run(Unsupported(), build())


def test_duplicate_case_tool_names_and_oversized_definitions_reject_before_model():
    tools = build()
    invoker = Invoker()
    with pytest.raises(ValueError, match='distinct'):
        run(invoker, [tools[0], tools[0]])
    tools[0].description = 'x'*100000
    with pytest.raises(ValueError, match='schema window'):
        run(invoker, tools)
    assert invoker.bound == invoker.invocations == []


def test_configuration_review_sets_existing_plan_flag_without_becoming_data_source():
    tools = build(include_configuration_review=True)
    review = next(tool for tool in tools if tool.name == research.CONFIGURATION_REVIEW_TOOL)
    assert set(review.args_schema.model_json_schema()['properties']) == {'purpose'}
    assert 'no es una fuente de datos' in review.description
    query = tools[0]
    response = AIMessage(content='', tool_calls=[call(query), call(review, identity='review-1', args={'purpose': 'Revisar el criterio con evidencia nueva.'})])
    plan = run(Invoker(response), tools)
    assert len(plan['requests']) == 1 and plan['propose'] is True
    artifact = review.invoke(call(review, args={'purpose': 'Revisar los filtros.'})).artifact
    assert artifact == {'request_configuration_review': True}


def test_configuration_review_not_exposed_for_paused_or_stale_criterion():
    config = configuration()
    config['criteria'][0]['status'] = 'paused'
    assert build(config, include_configuration_review=True) == []
    config['criteria'][0]['status'] = 'active'
    criterion = rule_context(config['criteria'][0])
    criterion['revision'] = 99
    assert build(config, criterion=criterion, include_configuration_review=True) == []


def test_configuration_review_preserved_when_no_new_research_queries_available():
    tools = build(approved=[], include_configuration_review=True)
    assert len(tools) == 1 and tools[0].name == research.CONFIGURATION_REVIEW_TOOL
    response = AIMessage(content='', tool_calls=[call(tools[0], args={'purpose': 'Revisar las consultas aprobadas.'})])
    assert run(Invoker(response), tools) == {'requests': [], 'propose': True}


def test_configuration_review_duplicates_or_unreviewed_fields_fail_closed():
    tools = build(include_configuration_review=True)
    review = tools[-1]
    calls = [call(review, args={'purpose': 'Revisar filtros.'}), call(review, identity='review-2', args={'purpose': 'Otra revisión.'})]
    with pytest.raises(ValueError, match='at most once'):
        run(Invoker(AIMessage(content='', tool_calls=calls)), tools)
    for args in ({'purpose': ' '}, {'purpose': 'Revisar', 'changes': [{'status': 'active'}]}):
        with pytest.raises(ValueError):
            review.invoke(call(review, args=args))
