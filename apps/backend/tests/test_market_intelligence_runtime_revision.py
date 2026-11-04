"""Revision provenance and checkpoint safety, with synthetic local research only."""
import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.market_installation.agent_package import agent_package
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import native
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import digest, json_text
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.research import Research, ResearchRevisionMismatch
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.proposals import Proposals
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow import MarketWorkflow
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.streaming import run_turn
from test_market_intelligence_agent_source import config, standalone
from test_market_intelligence_workflow import configured, ScenarioModel, SyntheticProvider, THREAD, TOPIC
from test_market_intelligence_scheduled_discovery import approved_due_search


def binding(mark='a'):
    return {'version': 1, **{key: mark * 64 for key in ('implementation_sha256', 'deployed_file_sha256',
            'runtime_config_sha256', 'prompts_sha256', 'policy_sha256', 'model_sha256')},
            'model_id': 'synthetic-model', 'dependencies': {'langgraph': 'synthetic', 'aidp-utils': None}}


def bound_flow(journal, revision):
    return MarketWorkflow(journal, ScenarioModel(), SyntheticProvider(), runtime_binding=revision,
                          usage_cost_verified=True)


def test_source_hash_separates_injected_configuration_from_semantic_code(tmp_path, config):
    path = tmp_path / 'agent.py'
    path.write_bytes(agent_package(config)[0])
    first = native.runtime_source_hashes(path, config)
    changed = {**config, 'timezone': 'UTC'}
    path.write_bytes(agent_package(changed)[0])
    second = native.runtime_source_hashes(path, changed)
    assert first[0] == second[0] and first[1] != second[1]
    path.write_bytes(path.read_bytes().replace(b'max_tokens\': 6000', b'max_tokens\': 5999'))
    third = native.runtime_source_hashes(path, changed)
    assert third[0] != second[0] and third[1] != second[1]


def test_binding_measures_effective_prompts_policy_model_and_config(standalone, config, monkeypatch):
    module, _ = standalone
    settings = {'model_id': config['model_id'], 'model_kwargs': {'temperature': 0}, 'guardrails_config': {'policies': []}}
    def build(prompts=None, contract=None, model=None):
        return module.build_runtime_binding(module.__file__, config, prompts or module.AGENT_PROMPTS,
                                            contract or module.MCP_CONTRACT, model or settings)
    first = build()
    assert first['deployed_file_sha256'] and first['implementation_sha256']
    assert first['runtime_config_sha256'] == module.digest(config)
    assert first['model_id'] == settings['model_id']
    assert build(prompts={**module.AGENT_PROMPTS, 'understand': 'Different reviewed prompt'})['prompts_sha256'] != first['prompts_sha256']
    assert build(contract={**module.MCP_CONTRACT, 'fixture_revision': 2})['policy_sha256'] != first['policy_sha256']
    assert build(model={**settings, 'model_kwargs': {'temperature': 0.1}})['model_sha256'] != first['model_sha256']
    profiles = copy.deepcopy(module.PROFILES)
    profiles['website']['build'] = 'synthetic-other-build'
    monkeypatch.setattr(module, 'PROFILES', profiles)
    assert build()['policy_sha256'] != first['policy_sha256']


def test_missing_or_mismatched_source_cannot_claim_verified_revision(tmp_path, config):
    with pytest.raises(OSError):
        native.runtime_source_hashes(tmp_path / 'missing.py', config)
    path = tmp_path / 'agent.py'
    path.write_text('print(1)', encoding='utf-8')
    with pytest.raises(RuntimeError, match='standalone source'):
        native.runtime_source_hashes(path, config)
    path.write_bytes(agent_package(config)[0])
    with pytest.raises(RuntimeError, match='runtime configuration'):
        native.runtime_source_hashes(path, {**config, 'timezone': 'UTC'})


def test_missing_distribution_metadata_is_explicit_unknown(standalone, config, monkeypatch):
    from importlib import metadata
    def missing(name):
        raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(metadata, 'version', missing)
    module, _ = standalone
    result = module.build_runtime_binding(module.__file__, config, module.AGENT_PROMPTS,
        module.MCP_CONTRACT, {'model_id': 'synthetic-model'})
    assert result['dependencies'] == {name: None for name in ('aidp-utils', 'langgraph', 'langchain-core', 'mcp')}


def prepare(revision):
    flow, _, _, _ = configured()
    research = Research(flow.journal, runtime_binding=revision)
    question, _ = research.record_question('revision-test', 'Novedades de D1 en Colombia', TOPIC)
    now = datetime.now(timezone.utc)
    window = {'start': (now - timedelta(days=7)).isoformat(), 'end': now.isoformat()}
    run = research.prepare_run(question, window, 'Novedades', 100000)
    return research, flow, question, window, run


def test_initial_binding_is_durable_detached_and_immutable_on_retry():
    revision = binding()
    research, flow, question, window, run = prepare(revision)
    revision['model_id'] = 'changed-external-object'
    initial = copy.deepcopy(flow.records().get('ai_mi_research_runs', run))
    assert json.loads(initial['brief_json'])['runtime_binding'] == binding()
    assert research.prepare_run(question, window, 'Novedades', 100000) == run
    assert flow.records().get('ai_mi_research_runs', run) == initial
    before = flow.journal.read()[0]
    with pytest.raises(ResearchRevisionMismatch):
        Research(flow.journal, runtime_binding=binding('b')).prepare_run(question, window, 'Novedades', 100000)
    assert flow.journal.read()[0] == before


@pytest.mark.parametrize('revision', [None, binding()])
def test_changed_or_unknown_checkpoint_revision_blocks_before_model_and_provider(revision):
    base, _, _, _ = configured()
    first = bound_flow(base.journal, revision)
    checkpointer = InMemorySaver()
    graph = first.build(checkpointer)
    graph.interrupt_after_nodes = ['prepare_run']
    request = {'request_id': 'revision-checkpoint', 'question': 'Novedades de D1 en Colombia'}
    async def exercise():
        await graph.ainvoke(request, THREAD)
        state = await graph.aget_state(THREAD)
        assert state.next == ('plan_research',) and state.values['run_id']
        before = base.journal.read()[0]
        second = bound_flow(base.journal, binding('b'))
        with pytest.raises(ResearchRevisionMismatch):
            await run_turn(second.build(checkpointer), request, THREAD)
        assert not second.model.calls and not second.provider.starts
        assert base.journal.read()[0] == before
    asyncio.run(exercise())


def test_same_revision_resumes_and_report_retains_binding():
    base, _, _, _ = configured()
    first = bound_flow(base.journal, binding())
    checkpointer = InMemorySaver()
    graph = first.build(checkpointer)
    graph.interrupt_after_nodes = ['prepare_run']
    request = {'request_id': 'same-revision', 'question': 'Novedades de D1 en Colombia'}
    async def exercise():
        await graph.ainvoke(request, THREAD)
        second = bound_flow(base.journal, binding())
        result = await run_turn(second.build(checkpointer), request, THREAD)
        report = second.records().get('ai_mi_reports', result['response']['report_id'])
        assert json.loads(report['coverage_json'])['runtime_binding'] == binding()
        assert len(second.provider.starts) == 2
    asyncio.run(exercise())


def test_closed_legacy_report_remains_readable_after_revision_change():
    first, _, provider, graph = configured()
    request = {'request_id': 'legacy-report', 'question': 'Novedades de D1 en Colombia'}
    async def exercise():
        original = await run_turn(graph, request, THREAD)
        before = first.journal.read()[0]
        second = bound_flow(first.journal, binding())
        replay = await run_turn(second.build(graph.checkpointer), request, THREAD)
        assert replay['response'] == original['response']
        assert not second.model.calls and not second.provider.starts
        assert first.journal.read()[0] == before
    asyncio.run(exercise())


def test_scheduled_revision_is_recorded_before_work_and_legacy_pending_not_relabelled():
    flow, _, _, _, _, schedule, legacy_run = approved_due_search()
    proposals = Proposals(flow.journal, runtime_binding=binding())
    before = flow.journal.read()[0]
    with pytest.raises(ResearchRevisionMismatch):
        proposals.claim_due(schedule, resume_pending=True)
    assert flow.journal.read()[0] == before
    def close_fixture(records, state):
        run = records.get('ai_mi_research_runs', legacy_run)
        records.upsert('ai_mi_research_runs', {**run, 'state': 'closed'})
    flow.journal.transact(close_fixture)
    run = proposals.claim_due(schedule, datetime.now(timezone.utc) + timedelta(days=8))
    assert run != legacy_run
    assert json.loads(flow.records().get('ai_mi_research_runs', run)['brief_json'])['runtime_binding'] == binding()
    assert 'runtime_binding' not in json.loads(flow.records().get('ai_mi_research_runs', legacy_run)['brief_json'])

@pytest.mark.parametrize('revision', [None, binding()])
def test_post_publication_checkpoint_cannot_mix_revisions_or_create_proposals(revision):
    base, _, _, _ = configured()
    first = bound_flow(base.journal, revision)
    checkpointer = InMemorySaver()
    graph = first.build(checkpointer)
    graph.interrupt_after_nodes = ['publish_result']
    request = {'request_id': 'revision-after-publish', 'question': 'Novedades de D1 en Colombia'}
    async def exercise():
        await graph.ainvoke(request, THREAD)
        state = await graph.aget_state(THREAD)
        assert state.next == ('detect_opportunities',)
        assert first.records().get('ai_mi_research_runs', state.values['run_id'])['state'] == 'closed'
        before = base.journal.read()[0]
        second = bound_flow(base.journal, binding('b'))
        with pytest.raises(ResearchRevisionMismatch):
            await run_turn(second.build(checkpointer), request, THREAD)
        assert not second.model.calls and not second.provider.starts
        assert base.journal.read()[0] == before
    asyncio.run(exercise())


def test_closed_report_delivery_only_can_recover_under_a_new_revision():
    base, _, _, _ = configured()
    first = bound_flow(base.journal, binding())
    checkpointer = InMemorySaver()
    graph = first.build(checkpointer)
    graph.interrupt_after_nodes = ['detect_opportunities']
    request = {'request_id': 'revision-delivery-only', 'question': 'Novedades de D1 en Colombia'}
    async def exercise():
        await graph.ainvoke(request, THREAD)
        state = await graph.aget_state(THREAD)
        assert state.next == ('deliver_and_read',)
        before = base.journal.read()[0]
        second = bound_flow(base.journal, binding('b'))
        result = await run_turn(second.build(checkpointer), request, THREAD)
        assert result['response'] == state.values['response']
        assert not second.model.calls and not second.provider.starts
        assert base.journal.read()[0] == before
    asyncio.run(exercise())

@pytest.mark.parametrize('statement', [
    'ALSO = RUNTIME_CONFIG = {}',
    'RUNTIME_CONFIG = {}\nRUNTIME_CONFIG = {}',
    'RUNTIME_CONFIG: dict = {}',
])
def test_only_the_single_plain_configuration_assignment_can_be_neutralized(tmp_path, statement):
    path = tmp_path / 'ambiguous.py'
    path.write_text(statement, encoding='utf-8')
    with pytest.raises(RuntimeError, match='standalone source'):
        native.runtime_source_hashes(path, {})
