import copy
import asyncio
import json
import sys
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.ai_data_governance.response_graph import governance_a2ui_enabled, governance_relationship_graph, governance_visual_response
from app.modules.ai_data_governance.governance_source import governance_agent_source
from tests.support.ai_dg_fixtures import binding


CATALOG = '/a2ui_specification/2.0.0/agent_hub_a2ui_custom_component_catalog.json'
CAPS = {'supportedCatalogIds': [CATALOG]}


def observation():
    # Synthetic observations remain local fixtures, never seeded into AIDP.
    facts = {'anchor': 'cat.gold.sales', 'nodes': [
        {'id':'a', 'qualifiedName':'cat.silver.sales.total', 'type':'COLUMN'},
        {'id':'b', 'qualifiedName':'cat.gold.sales.total', 'type':'COLUMN'},
        {'id':'c', 'displayName':'report_task', 'type':'TASK'}],
        'links':[{'fromNodeId':'a', 'toNodeId':'b'}, {'fromNodeId':'b', 'toNodeId':'c'}],
        'pagination_complete':True, 'omitted_nodes':0, 'unresolved_links':0,
        'output_truncated':False, 'traversal_depth':2, 'execution_evidence':False}
    proof = {'evidence_id':'e1', 'asset_id':'asset', 'version_id':'v1', 'kind':'RUNTIME_LINEAGE',
        'coverage':'complete', 'facts':facts}
    return {'intent':{'intent':'lineage'}, 'assets':[{'asset_id':'asset','version_id':'v1','name':'cat.gold.sales'}],
        'evidence':[proof], 'review':{'evidence_ids':['e1'], 'supported_claims':[{'evidence_ids':['e1']}]},
        'response':{'result_state':'answered','cited_evidence_ids':['e1']}}


def test_direct_edges_and_column_task_labels_preserve_direction_without_inferred_execution():
    state = observation()
    graph = governance_relationship_graph(state)
    nodes = {n['id']:n for n in graph['nodes']}
    assert [(nodes[e['source']]['label'],nodes[e['target']]['label']) for e in graph['edges']] == [
        ('cat.silver.sales.total','cat.gold.sales.total'), ('cat.gold.sales.total','report\\_task')]
    assert all(e['basis'] == 'RUNTIME_LINEAGE' and e['evidence_id'] == 'e1' for e in graph['edges'])
    assert all(n['asset_version_id'] == 'v1' for n in graph['nodes'])
    assert len(graph['edges']) == 2 and graph['execution_proven'] is False
    assert 'otras dependencias' in graph['notice']


@pytest.mark.parametrize('field,value', [('pagination_complete',False), ('omitted_nodes',1),
    ('unresolved_links',1), ('output_truncated',True)])
def test_incomplete_native_coverage_is_visible(field, value):
    state = observation()
    state['evidence'][0]['facts'][field] = value
    assert governance_relationship_graph(state)['partial'] is True


@pytest.mark.parametrize('failure', ['uncited','foreign_version','dangling','duplicate_id','unreviewed'])
def test_unadmitted_or_ambiguous_graph_never_leaks_or_replaces_the_answer(failure):
    state = observation()
    if failure == 'uncited': state['response']['cited_evidence_ids'] = []
    if failure == 'foreign_version': state['evidence'][0]['version_id'] = 'foreign'
    if failure == 'dangling': state['evidence'][0]['facts']['links'][0]['toNodeId'] = 'unknown'
    if failure == 'duplicate_id': state['evidence'][0]['facts']['nodes'].append({'id':'a','displayName':'conflict'})
    if failure == 'unreviewed': state['review']['supported_claims'] = []
    assert governance_visual_response(state, 'Validated answer', {}, 'session') == ('Validated answer', None)


def test_cycles_are_direct_observations_not_infinite_traversals_or_component_cycles():
    state = observation()
    state['evidence'][0]['facts']['links'].append({'fromNodeId':'c','toNodeId':'a'})
    assert len(governance_relationship_graph(state)['edges']) == 3


def test_limits_are_connected_and_explicitly_partial():
    state = observation()
    facts = state['evidence'][0]['facts']
    facts['nodes'] = [{'id':str(n),'displayName':str(n)} for n in range(16)]
    facts['links'] = [{'fromNodeId':str(n),'toNodeId':str(n+1)} for n in range(15)]
    graph = governance_relationship_graph(state)
    assert len(graph['nodes']) == 8 and len(graph['edges']) == 7 and graph['partial']
    assert {e[k] for e in graph['edges'] for k in ('source','target')} == {n['id'] for n in graph['nodes']}


def test_observations_do_not_merge_on_display_names_or_native_ids():
    state = observation()
    other = copy.deepcopy(state['evidence'][0])
    other['evidence_id'] = 'e2'
    state['evidence'].append(other)
    state['review']['evidence_ids'].append('e2')
    state['review']['supported_claims'].append({'evidence_ids':['e2']})
    state['response']['cited_evidence_ids'].append('e2')
    graph = governance_relationship_graph(state)
    assert len(graph['nodes']) == 6 and len({n['id'] for n in graph['nodes']}) == 6


@pytest.mark.parametrize('kwargs,expected', [({},False), ({'metadata':{'a2uiClientCapabilities':{}}},False),
    ({'metadata':{'a2uiClientCapabilities':CAPS}},True),
    ({'session_variables':{'a2uiClientCapabilities':{'value':json.dumps(CAPS)}}},True),
    ({'session_variables':{'sessionvariables.a2uiClientCapabilities':CAPS}},True),
    ({'metadata':{'a2uiClientCapabilities':{**CAPS,'enabled':False}}},False),
    ({'metadata':{'a2uiClientCapabilities':{**CAPS,'version':'0.8'}}},False),
    ({'metadata':{'a2uiClientCapabilities':{**CAPS,'version':[]}}},False),
    ({'metadata':{'a2uiClientCapabilities':{'supportedCatalogIds':['foreign']}}},False),
    ({'metadata':{'a2uiClientCapabilities':{**CAPS,'inlineCatalogs':[{}]}}},False),
    ({'session_variables':{'a2uiClientCapabilities':'not-json'}},False)])
def test_only_explicit_known_catalog_receives_a2ui(kwargs, expected):
    assert governance_a2ui_enabled(kwargs) is expected


def test_rich_transport_retains_text_sources_and_literal_labels_and_fresh_surface_ids():
    state = observation()
    state['evidence'][0]['facts']['nodes'][0]['qualifiedName'] = '<img src=x> [click](https://evil) ${binding}\n`column`'
    kwargs = {'metadata':{'a2uiClientCapabilities':CAPS}}
    content, graph = governance_visual_response(state, 'Answer [1]', kwargs, 'session')
    again, _ = governance_visual_response(state, 'Answer [1]', kwargs, 'session')
    assert content.startswith('A2UI\n') and content != again
    parts = [p['root'] for p in json.loads(content[5:])]
    assert parts[0]['kind'] == 'text' and 'Answer [1]' in parts[0]['text']
    assert 'Relaciones observadas:' not in parts[0]['text']  # Rich cards do not duplicate the textual diagram.
    operations = [p['data'] for p in parts if p['kind'] == 'data']
    assert all(p.get('metadata',{}).get('mimeType') == 'application/json+a2ui' for p in parts[1:])
    components = operations[1]['updateComponents']['components']
    assert any('fuente [1]' in c.get('text','') for c in components)
    assert all('<img' not in c.get('text','') for c in components)
    assert components[0]['id'] == 'root'
    assert {c['component'] for c in components} == {'Column','Row','Card','Text'}
    ids = {c['id'] for c in components}
    assert len(ids) == len(components)
    for c in components:
        assert set(c.get('children',[])).issubset(ids)
        if 'child' in c: assert c['child'] in ids
        assert not any(k in c for k in ('action','url','functionCall','path'))
    assert graph['nodes'][0]['label'].startswith('&lt;img')


@pytest.mark.parametrize('intent,result', [('meaning','answered'),('lineage','not_evaluable'),('impact','clarification')])
def test_unrelated_or_insufficient_answers_get_no_graph(intent, result):
    state = observation()
    state['intent']['intent'], state['response']['result_state'] = intent, result
    assert governance_visual_response(state, 'Answer', {}, 'session') == ('Answer',None)


def test_no_native_edges_means_no_invented_topology():
    state = observation()
    state['evidence'][0]['facts']['links'] = []
    assert governance_relationship_graph(state) is None


def test_absent_anchor_is_partial_and_impact_does_not_reverse_upstream_edges():
    state = observation()
    state['intent']['intent'] = 'impact'
    state['evidence'][0]['facts']['anchor'] = 'unobserved.anchor'
    graph = governance_relationship_graph(state)
    assert graph['partial'] is True
    nodes = {n['id']:n for n in graph['nodes']}
    assert nodes[graph['edges'][0]['source']]['label'] == 'cat.silver.sales.total'
    assert graph['edges'][0]['label'] == 'relación observada'


def test_native_catalog_uri_is_readable_without_losing_native_node_identity():
    state = observation()
    for node in state['evidence'][0]['facts']['nodes']:
        if 'qualifiedName' in node:
            node['qualifiedName'] = 'aidp://catalogs@platform/o/' + node['qualifiedName']
    graph = governance_relationship_graph(state)
    assert graph['partial'] is False
    assert graph['nodes'][0]['label'] == 'cat.silver.sales.total'


def test_supported_claim_citations_need_not_be_repeated_in_review_root_list():
    state = observation()
    state['review']['evidence_ids'] = []
    assert governance_relationship_graph(state) is not None


def test_static_task_file_asset_flow_is_explicitly_not_execution():
    state = observation()
    state['intent']['intent'] = 'usage'
    state['evidence'][0].update(kind='STATIC_CODE', coverage='partial', facts={'target':'cat.gold.sales',
        'references':[{'source_path':'/Workspace/model.py','line':39,'task_key':'create_model'}],
        'execution_evidence':False, 'analysis_complete':False})
    content, graph = governance_visual_response(state, 'Answer', {}, 'session')
    assert len(graph['edges']) == 2 and graph['partial'] and not graph['execution_proven']
    assert 'referencia estática' in content and 'línea 39' in content
    assert {n['kind'] for n in graph['nodes']} == {'Activo','Código','Tarea'}


@pytest.mark.parametrize('basis,count', [('STATIC_CODE',1), ('RUNTIME_LINEAGE',2),
    ('RUNTIME_LINEAGE',0), ('STATIC_CODE',None)])
def test_composer_receives_relationship_observations_and_runtime_presentation_ownership(basis, count):
    from app.modules.ai_data_governance.governance_graph import GovernanceNodes
    state = observation()
    state.update(question='Explica las relaciones observadas', diagnosis={}, treatment={})
    state['review'] = {'evidence_ids':['e1'], 'supported_claims':[
        {'text':'Relación observada en el ejemplo local.', 'evidence_ids':['e1'], 'kind':'observed'}],
        'conflicts':[], 'gaps':[], 'coverage':'sufficient', 'next_action':'answer'}
    if basis == 'STATIC_CODE':
        state['evidence'][0].update(kind=basis, facts={} if count is None else {
            'target':'cat.gold.sales',
            'references':[{'source_path':'/Workspace/model.py','line':39,'task_key':'create_model'}],
            'analysis_complete':False, 'execution_evidence':False})
    elif count == 0:
        state['evidence'][0]['facts']['links'] = []
    model = AsyncMock(return_value=json.dumps({'answer_markdown':'Relaciones observadas [1].',
        'cited_evidence_ids':['e1'], 'limitations':[], 'case_ref':None, 'result_state':'answered'}))
    nodes = GovernanceNodes(authorize=AsyncMock(), resolve_assets=AsyncMock(), read=AsyncMock(),
        call_model=model, capabilities=(), reauthorize_evidence=AsyncMock())
    result = asyncio.run(nodes.respond(state, {}))
    phase, payload = model.call_args.args[:2]
    assert phase == 'G6'
    assert payload['presentation']['relationship_view_owner'] == 'runtime'
    assert payload['presentation']['model_controls_graph_inclusion'] is False
    assert payload['references'][0]['relationship_observations'] == {'basis':basis, 'count':count}
    assert result['response']['cited_evidence_ids'] == ['e1']
    _, graph = governance_visual_response({**state, **result}, 'Answer [1]', {}, 'local')
    assert bool(graph) is (count is not None and count > 0)


@pytest.mark.parametrize('intent', ['inventory', 'meaning', 'quality', 'case_followup'])
def test_non_relationship_response_contract_has_no_presentation_decision(intent):
    from app.modules.ai_data_governance.governance_graph import governance_response_input
    state = observation()
    state.update(question='Explica la evidencia disponible', intent={'intent':intent})
    state['evidence'][0].update(kind='CATALOG', facts={'description':'Descripción [IA]',
        'description_origin':'AI', 'observed_at':'2026-11-03T12:00:00Z'})
    payload = governance_response_input(state)
    assert set(payload) == {'question','review','assets','references','diagnosis','treatment','action_receipts'}
    assert payload['references'] == [{'evidence_id':'e1', 'kind':'CATALOG',
        'asset_name':'cat.gold.sales', 'description_origin':'AI', 'description_marked_ia':True,
        'metadata_observed_at':'2026-11-03T12:00:00Z'}]
    assert payload['action_receipts'] == []


def test_assembled_runtime_includes_same_graph_and_transport_behavior():
    from app.modules.ai_data_governance.governance_graph import governance_response_input
    module = ModuleType('graph_runtime_test')
    exec(compile(governance_agent_source(binding()), 'governance_runtime.py', 'exec'), module.__dict__)
    assert module.governance_relationship_graph(observation()) == governance_relationship_graph(observation())
    state = observation()
    state['question'] = 'Explica el linaje'
    assert module.governance_response_input(state) == governance_response_input(state)
    content, graph = module.governance_visual_response(observation(), 'Answer', {}, 'session')
    assert '→' in content and graph['execution_proven'] is False


@pytest.mark.parametrize('rich', [False,True])
def test_normal_native_invocation_delivers_graph_in_content_not_only_extra_fields(monkeypatch, rich):
    from app.modules.ai_data_governance import native_governance
    state = observation()
    state['response'].update(answer_markdown='Relaciones comprobadas [1].',limitations=[])
    helper = SimpleNamespace(pre_invoke_setup=lambda **kw:{}, flow_setup=lambda **kw:nullcontext())
    monkeypatch.setitem(sys.modules,'aidputils.agents.toolkit.agent_helper',helper)
    monkeypatch.setitem(sys.modules,'aidputils.agents.auth.util',SimpleNamespace(auth_utils=SimpleNamespace(get_auth_context=Mock())))
    monkeypatch.setattr(native_governance,'consultation_config',lambda *args:{})
    caller = SimpleNamespace(session=SimpleNamespace(close=Mock()))
    monkeypatch.setattr(native_governance,'native_central_transport',lambda _:caller)
    hooks = SimpleNamespace(authorize=AsyncMock(),resolve_assets=AsyncMock(),read=AsyncMock(),_read_admitted=AsyncMock(),
        capabilities=(),reauthorize_evidence=AsyncMock(), _validate_admitted_evidence=AsyncMock(), _reobserve_admitted_evidence=AsyncMock())
    monkeypatch.setattr(native_governance,'ConsultationHooks',Mock(return_value=hooks))
    monkeypatch.setattr(native_governance,'build_governance_graph',Mock(return_value=SimpleNamespace(ainvoke=AsyncMock(return_value=state))))
    agent = native_governance.DataGovernanceAgent()
    agent.binding,agent.memory = {'component_revision':'revision'},object()
    kwargs = {'metadata':{'a2uiClientCapabilities':CAPS}} if rich else {}
    message = asyncio.run(agent.invoke('Muestra el linaje',**kwargs))['messages'][0]
    assert message.content.startswith('A2UI\n') is rich
    assert message.additional_kwargs['relationships']['execution_proven'] is False
    if rich:
        text = json.loads(message.content[5:])[0]['root']['text']
    else:
        text = message.content
    assert 'Relaciones comprobadas [1]' in text and '[1] Linaje:' in text
    assert ('→' in text) is not rich
    caller.session.close.assert_called_once()
