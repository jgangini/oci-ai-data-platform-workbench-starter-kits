import asyncio
from copy import deepcopy
import json
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from app.modules.ai_data_governance.code_context import WorkspaceCodeReader
from app.modules.ai_data_governance.governance_source import governance_agent_source
from app.modules.ai_data_governance.response_graph import governance_visual_response
from tests.modules.ai_data_governance.test_ai_dg_code_usage import JOB, PATH, usage_harness, read_usage
from tests.support.ai_dg_fixtures import binding
from tests.modules.ai_data_governance.test_ai_dg_response_graph import observation


def test_legacy_stored_reference_gains_fresh_job_name_without_rewriting_history():
    hooks, assets, _, _, job, caller, _, candidate, _ = usage_harness()
    job['name'] = 'wf_create_orders'
    before = deepcopy(candidate)
    proofs = read_usage(hooks, assets)
    assert proofs[0]['facts']['references'][0]['job_name'] == job['name']
    assert candidate == before
    assert not any(call.args[1].endswith('/jobs') for call in caller.call_args_list)
    asyncio.run(hooks.reauthorize_evidence({}, assets=assets, evidence=proofs))


def test_discovery_and_its_admission_preserve_the_observed_workflow_name():
    hooks, assets, _, _, job, caller, query, _, request = usage_harness()
    job['name'] = 'wf_create_orders'
    query.return_value = []
    def current(method, path, **kwargs):
        if path.endswith('/jobs'):
            return {'items': [{'key': JOB}]}, {}
        return request(method, path, **kwargs)
    caller.side_effect = current
    refs = WorkspaceCodeReader(caller, hooks.binding['workspace_key'], check_active=caller.check_identity).find('catalog.schema.orders', 'ABC13')
    assert refs[0]['job_name'] == job['name']
    proofs = read_usage(hooks, assets)
    assert proofs[0]['facts']['references'][0]['job_name'] == job['name']
    assert proofs[0]['facts']['execution_evidence'] is False
    assert PATH in json.dumps(proofs) and 'never-return-this' not in json.dumps(proofs)


@pytest.mark.parametrize('replacement', ['renamed_workflow', None])
def test_changed_name_invalidates_a_proof_even_with_unchanged_source_and_task(replacement):
    hooks, assets, _, _, job, _, _, _, _ = usage_harness()
    job['name'] = 'wf_create_orders'
    proofs = read_usage(hooks, assets)
    before = deepcopy(proofs)
    job['name'] = replacement
    with pytest.raises(PermissionError, match='no longer authorized or current'):
        asyncio.run(hooks.reauthorize_evidence({}, assets=assets, evidence=proofs))
    with pytest.raises(PermissionError, match='exact source observation changed'):
        asyncio.run(hooks.reobserve_evidence({}, assets=assets, evidence=proofs))
    assert proofs == before


@pytest.mark.parametrize('name', [None, '', ' ', 42, {}, 'bad\nname', 'bad\u202ename', 'x' * 257])
def test_missing_or_invalid_native_label_does_not_hide_a_valid_static_reference(name):
    hooks, assets, _, _, job, _, _, _, _ = usage_harness()
    job['name'] = name
    proofs = read_usage(hooks, assets)
    assert len(proofs) == 1 and 'job_name' not in proofs[0]['facts']['references'][0]
    asyncio.run(hooks.reauthorize_evidence({}, assets=assets, evidence=proofs))


def test_forged_stored_workflow_name_is_not_admitted():
    hooks, assets, _, _, job, _, _, candidate, _ = usage_harness()
    job['name'] = 'real_workflow'
    facts = json.loads(candidate['facts_json'])
    facts['usage_context']['references'][0]['job_name'] = 'unrelated_workflow'
    candidate['facts_json'] = json.dumps(facts)
    assert read_usage(hooks, assets) == []


def test_verified_workflow_name_is_literal_in_the_existing_graph_label():
    state = observation()
    state['intent']['intent'] = 'usage'
    state['evidence'][0].update(kind='STATIC_CODE', coverage='partial', facts={
        'target': 'cat.gold.sales', 'references': [{'source_path':'/Workspace/model.py',
            'line':39, 'task_key':'create_model', 'job_name':'wf_[orders]<script>'}],
        'execution_evidence':False, 'analysis_complete':False})
    content, graph = governance_visual_response(state, 'Answer', {}, 'session')
    task = next(node for node in graph['nodes'] if node['kind'] == 'Tarea')
    assert task['label'] == r'wf\_\[orders\]&lt;script&gt; · create\_model'
    assert '<script>' not in content and 'referencia estática' in content
    assert len(graph['edges']) == 2 and not graph['execution_proven']


def test_generated_runtime_keeps_the_same_job_label_contract():
    module = ModuleType('workflow_names_runtime')
    exec(compile(governance_agent_source(binding()), 'governance_runtime.py', 'exec'), module.__dict__)
    assert module.workspace_job_name({'name':'wf_orders'}) == 'wf_orders'
    assert module.workspace_job_name({'name':'bad\nname'}) is None


def test_recorded_graph_passes_the_verified_name_to_review_and_delivered_answer():
    from langgraph.checkpoint.memory import InMemorySaver
    from app.modules.ai_data_governance.governance_graph import build_governance_graph
    from app.modules.ai_data_governance.native_governance import consultation_model_sources
    hooks, assets, row, _, job, _, query, candidate, _ = usage_harness()
    job['name'] = 'wf_create_orders'
    query.side_effect = lambda sql, **kwargs: [candidate] if 'e.evidence_kind' in sql else [row]
    phases = []
    async def model(phase, payload, *args):
        phases.append(phase)
        if phase == 'G1':
            return json.dumps({'intent':'usage', 'asset_mentions':['ABC13']})
        if phase == 'G3':
            projected, _ = consultation_model_sources(phase, payload)
            proof = next(p for p in projected['evidence'] if p['kind'] == 'STATIC_CODE')
            assert proof['facts']['references'][0]['job_name'] == job['name']
            ids = [p['evidence_id'] for p in payload['evidence'] if p['kind'] == 'STATIC_CODE']
            return json.dumps({'evidence_ids':ids, 'supported_claims':[
                {'text':job['name']+' contiene la referencia estática de read_orders.', 'evidence_ids':ids, 'kind':'observed'}],
                'conflicts':[], 'gaps':['No acredita ejecución.'], 'coverage':'partial', 'next_action':'answer'})
        assert phase == 'G6' and job['name'] in payload['review']['supported_claims'][0]['text']
        return json.dumps({'answer_markdown':job['name']+' referencia el campo; no demuestra ejecución.',
            'cited_evidence_ids':payload['review']['evidence_ids'], 'limitations':['Análisis estático parcial.'],
            'case_ref':None, 'result_state':'partial'})
    graph = build_governance_graph(authorize=hooks.authorize, resolve_assets=AsyncMock(return_value=assets),
        read=hooks.read, call_model=model, capabilities=hooks.capabilities,
        reauthorize_evidence=hooks.reauthorize_evidence, checkpointer=InMemorySaver())
    result = asyncio.run(graph.ainvoke({'question':'¿Qué workflow usa ABC13?'},
        config={'configurable':{'thread_id':'workflow-name-recorded-control'}}))
    assert phases == ['G1','G3','G6']
    assert job['name'] in result['response']['answer_markdown']
    assert 'no demuestra ejecución' in result['response']['answer_markdown']
