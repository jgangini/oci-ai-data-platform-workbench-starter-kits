"""Execute the standalone delivery, not only the modular authoring sources."""
import ast
import asyncio
import importlib.util
import inspect
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip('langgraph', reason='Standalone execution is checked in the pinned runtime CI job')
from langgraph.checkpoint.memory import InMemorySaver

from app.market_installation.agent_package import FILES, agent_package
from app.market_installation.agent_source import assemble_agent
from app.lab_packs import load_lab_pack
from test_market_intelligence_repository import MemorySnapshot, SCOPE
from test_market_intelligence_workflow import ScenarioModel, SyntheticProvider, TOPIC, THREAD


@pytest.fixture
def config():
    return {'credential_name': 'AidpRuntime', 'identity_sha256': '0' * 64, 'region': 'us-chicago-1',
            'model_endpoint': 'https://inference.generativeai.us-chicago-1.oci.oraclecloud.com',
            'model_id': 'fixture-model', 'compartment_id': 'fixture-compartment', 'guardrails_config': {'policies': []},
            'tenant_id': 'fixture-tenant', 'generation_id': SCOPE.generation_id, 'namespace': 'fixture', 'bucket': 'fixture',
            'catalog': 'fixture', 'framework_schema': 'oci_artifacts', 'gold_query_compute_id': 'fixture',
            'budget_microusd': 0, 'run_cap_microusd': 0, 'step_cap_microusd': 0, 'timezone': 'America/Bogota',
            'usage_cost_verified': False, 'research_enabled': False}


@pytest.fixture
def standalone(tmp_path, monkeypatch, config):
    source, assets = agent_package(config)
    assert set(assets) == {'requirements.txt'}
    (tmp_path / 'market_intelligence.py').write_bytes(source)
    (tmp_path / 'requirements.txt').write_bytes(assets['requirements.txt'])
    (tmp_path / 'agent-manifest.json').write_text('{}', encoding='utf-8')
    spec = importlib.util.spec_from_file_location('market_standalone_fixture', tmp_path / 'market_intelligence.py')
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert {path.name for path in tmp_path.iterdir() if path.is_file()} == {
        'market_intelligence.py', 'requirements.txt', 'agent-manifest.json'}
    return module, source


def test_standalone_delivery_is_deterministic_and_contains_no_runtime_loader(standalone, config):
    module, source = standalone
    assert source == agent_package(config)[0]
    tree = ast.parse(source)
    assert not any(isinstance(node, ast.ImportFrom) and (node.level or
        (node.module or '').startswith('ai_market_intelligence')) for node in ast.walk(tree))
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id in {'exec', 'eval', '__import__'} for node in ast.walk(tree))
    assert module.RUNTIME_CONFIG == config and module.RUNTIME_CONFIG['research_enabled'] is False
    entrypoints = [item for item in vars(module).values() if inspect.isclass(item)
                   and item.__module__ == module.__name__ and callable(getattr(item, 'setup', None))
                   and callable(getattr(item, 'invoke', None))]
    assert entrypoints == [module.MarketIntelligenceAgent]
    assert len(module.definition()['tables']) == 14
    first = module.definition()
    first['tables'].clear()
    assert len(module.definition()['tables']) == 14
    model = module.DecisionModel(object())
    assert set(model.prompts) == {'understand', 'plan_research', 'assess_evidence', 'draft_report',
                                  'detect_opportunities', 'handle_confirmation'}
    model.prompts.clear()
    assert module.DecisionModel(object()).prompts


def _flow(module):
    journal = module.Journal(MemorySnapshot(), 1000000)
    model, provider = ScenarioModel(), SyntheticProvider()
    topic, target = SCOPE.key(module.digest(['topic', TOPIC])), SCOPE.key('reference')
    def seed(records, state):
        records.upsert('ai_mi_topics', {'topic_id': topic, 'canonical_scope_json': module.json_text(TOPIC)})
        for name, profile in [('x', 'x_search'), ('web', 'website')]:
            records.upsert('ai_mi_sources', {'source_key': SCOPE.key(name), 'name': name,
                'connector_revision': profile, 'status': 'active',
                'allowed_scope_json': module.json_text({'allow_discovery': name == 'x'})})
        records.upsert('ai_mi_source_targets', {'target_id': target, 'source_key': SCOPE.key('web'),
            'canonical_url_or_id': 'https://example.com/reference', 'description': 'Synthetic reference', 'status': 'active'})
        records.upsert('ai_mi_topic_sources', {'topic_source_id': SCOPE.key('link'), 'topic_id': topic, 'target_id': target,
            'approved_scope_json': module.json_text({'urls': ['https://example.com/reference']}), 'status': 'active'})
    journal.transact(seed)
    flow = module.MarketWorkflow(journal, model, provider, run_cap_microusd=100000,
                                step_cap_microusd=50000, usage_cost_verified=True)
    return flow, provider, flow.build(InMemorySaver())


def test_standalone_real_graph_streams_contrasts_persists_and_reopens_without_recapture(standalone):
    module, _ = standalone
    flow, provider, graph = _flow(module)
    async def exercise():
        events = [event async for event in module.stream_turn(graph,
            {'request_id': 'assembled-1', 'question': 'Novedades de D1 en Colombia'}, THREAD)]
        result = events[-1]['state']['response']
        assert any(event['event'] == 'progress' for event in events[:-1])
        assert result['report_id'] and '[Referencia]' in module.response_markdown(result)
        replay = await module.run_turn(graph,
            {'request_id': 'assembled-1', 'question': 'Novedades de D1 en Colombia'}, THREAD)
        assert replay['response'] == result
    asyncio.run(exercise())
    assert len(provider.starts) == 2
    assert len(flow.records().rows('ai_mi_documents')) == 2
    assert len(flow.records().rows('ai_mi_reports')) == 1
    assert flow.records().rows('ai_mi_research_runs')[0]['termination_reason'] == 'sufficient'


def test_standalone_provider_observation_does_not_shadow_persistence_validation(standalone):
    module, _ = standalone
    class Tool:
        async def ainvoke(self, arguments):
            return {'structuredContent': {'items': [{'url': 'https://example.com/reference', 'text': 'Evidence'}]}}
    provider = module.ApifyProvider({name: Tool() for name in ('get-dataset-items', 'get-actor-run', 'call-actor')})
    observations, cost = asyncio.run(provider.observations(
        {'runId': 'fixture-run', 'datasetId': 'fixture-data', 'status': 'SUCCEEDED'},
        {'actor': 'apify/website-content-crawler', 'input': {'maxCrawlPages': 1}}))
    assert observations[0]['text'] == 'Evidence'
    assert module._provider_observation is not module._observation


def test_standalone_native_setup_reads_embedded_config_and_contract_not_sibling_files(standalone, monkeypatch):
    module, _ = standalone
    identity = {'tenancy': 'fixture-tenancy', 'user': 'fixture-user', 'fingerprint': 'fixture-fingerprint',
                'private_key': 'fixture-not-a-key', 'token': 'fixture-not-a-token'}
    module.RUNTIME_CONFIG['identity_sha256'] = hashlib.sha256(json.dumps(
        [identity[key] for key in ('tenancy', 'user', 'fingerprint')], separators=(',', ':')).encode()).hexdigest()
    monkeypatch.setitem(sys.modules, 'aidputils', SimpleNamespace(secrets=SimpleNamespace(
        get=lambda name, key: identity[key])))
    cleanup = []
    monkeypatch.setitem(sys.modules, 'aidputils.agents.toolkit.agent_helper', SimpleNamespace(
        GenAIChatInvoker=lambda **kwargs: SimpleNamespace(options=kwargs),
        GenerativeAiInferenceV2Client=lambda **kwargs: object(),
        pre_invoke_setup=lambda **kwargs: THREAD,
        pre_tool_setup=lambda **kwargs: 'native-context',
        post_tool_setup=lambda token, **kwargs: cleanup.append(token)))
    calls = []
    def tools(**kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(name=name) for name in ('call-actor', 'get-actor-run', 'get-dataset-items')]
    monkeypatch.setitem(sys.modules, 'aidputils.agents.toolkit.tool_helper', SimpleNamespace(
        build_structured_tools_from_allowed_mcp_tools=tools))
    class McpClient:
        async def get_tools(self, **kwargs):
            return tools(endpoint=module.MCP_CONTRACT['endpoint'], auth={'authType': 'BEARER_TOKEN', 'token': identity['token']})
    monkeypatch.setitem(sys.modules, 'aidputils.agents.tools.mcp.mcp_service', SimpleNamespace(
        get_mcp_client=lambda **kwargs: McpClient()))
    import oci
    monkeypatch.setattr(oci.signer, 'Signer', lambda *args, **kwargs: object())
    monkeypatch.setattr(oci.object_storage, 'ObjectStorageClient', lambda *args, **kwargs: object())
    monkeypatch.setattr(module, 'native_memory', lambda *args: InMemorySaver())
    runtime = module.MarketIntelligenceAgent()
    runtime.setup()
    assert runtime.config == module.RUNTIME_CONFIG
    assert calls[0]['endpoint'] == module.MCP_CONTRACT['endpoint']
    assert calls[0]['auth'] == {'authType': 'BEARER_TOKEN', 'token': identity['token']}
    assert runtime.model.prompts == module.AGENT_PROMPTS
    async def invoke():
        final = await runtime.invoke('hola', subject_id='fixture-owner', trace_id='fixture-trace')
        assert final['messages'][0].additional_kwargs['ai_mi_response']['mode'] == 'configured_paused'
        stream = await runtime.invoke('hola', subject_id='fixture-owner', trace_id='fixture-stream', stream=True)
        messages = [message async for message in stream]
        assert messages[0].additional_kwargs['ai_mi_event'] == 'progress'
        assert messages[-1].additional_kwargs['ai_mi_response']['mcp_verified'] is True
    asyncio.run(invoke())
    assert cleanup == ['native-context', 'native-context']


def test_assembly_fails_on_new_unresolved_relative_dependency(config):
    pack = load_lab_pack('ai_market_intelligence', require_available=False)
    files = {asset.name.rsplit('/', 1)[-1]: asset.read_bytes() for asset in pack.runtime_files}
    inputs = {name: files[name] for name in FILES}
    inputs['native.py'] += b'\nfrom .unknown import hidden\n'
    with pytest.raises(ValueError, match='Unknown standalone import'):
        assemble_agent(inputs, files['market_intelligence.py'], config)


def test_assembly_does_not_silently_discard_changed_entry_behavior(config):
    pack = load_lab_pack('ai_market_intelligence', require_available=False)
    files = {asset.name.rsplit('/', 1)[-1]: asset.read_bytes() for asset in pack.runtime_files}
    changed = files['market_intelligence.py'].replace(b'self.runtime.setup()', b'self.runtime.setup(); raise RuntimeError()')
    with pytest.raises(ValueError, match='wrapper changed'):
        assemble_agent({name: files[name] for name in FILES}, changed, config)
