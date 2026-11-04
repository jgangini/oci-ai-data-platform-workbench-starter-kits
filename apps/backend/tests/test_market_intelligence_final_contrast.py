"""Final contrast is distinct from independently accepted evidence batches."""
import asyncio
import copy
import json

import pytest

pytest.importorskip('langgraph')
from test_market_intelligence_workflow import configured, turn, THREAD
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.contracts import ReportDraft, ResearchConclusion


@pytest.mark.parametrize('status,gaps', [('answered', ['Material gap']), ('partial', []), ('unresolved', [])])
def test_conclusion_requires_consistent_material_gaps(status, gaps):
    with pytest.raises(ValueError):
        ResearchConclusion.model_validate({'status': status, 'reason': 'Decision', 'material_gaps': gaps})


@pytest.mark.parametrize('scenario', ['contradiction', 'homonym', 'republication'])
def test_final_report_contrasts_items_across_batch_boundaries_before_closing(scenario):
    flow, model, provider, graph = configured()
    original_ask, original_observations = model.ask, provider.observations
    batches, global_views = [], []

    async def observations(receipt, call):
        rows, cost = await original_observations(receipt, call)
        return [{**rows[0], 'url': f'https://example.com/reference/{i}',
                 'text': f'{scenario} boundary item {i}'} for i in range(9)], cost

    async def ask(name, schema, payload):
        if name == 'assess_evidence':
            assert payload['corpus_evidence_count'] == 9
            batches.append([row['text'] for row in payload['evidence']])
            return {'assessments': [{'evidence_id': row['evidence_id'], 'stance': 'context',
                    'reason': 'Batch alone does not resolve the pair', 'independence': 'unknown'}
                    for row in payload['evidence']], 'continue_research': False, 'reason': 'Batch complete', 'gap': ''}
        result = await original_ask(name, schema, payload)
        if name == 'draft_report':
            global_views.append([row['text'] for row in payload['evidence']])
            assert len(global_views[-1]) == 9
            result['decision'] = {'status': 'partial', 'reason': f'Cross-batch {scenario} remains unresolved.',
                                  'material_gaps': [f'Verify the {scenario} in items 0 and 8.']}
        return result

    model.ask, provider.observations = ask, observations
    result = turn(graph, 'global-' + scenario)
    assert [len(batch) for batch in batches] == [8, 1]
    assert len(global_views) == 1 and len(provider.starts) == 1
    run = flow.records().get('ai_mi_research_runs', result['run_id'])
    report = flow.records().get('ai_mi_reports', result['response']['report_id'])
    assert run['termination_reason'] == 'unresolved'
    assert json.loads(report['coverage_json'])['conclusion'] == {
        'version': 1, 'status': 'partial', 'reason': f'Cross-batch {scenario} remains unresolved.',
        'material_gaps': [f'Verify the {scenario} in items 0 and 8.']}


@pytest.mark.parametrize('reason', ['budget', 'time', 'step_limit', 'uncertain', 'no_data', 'unresolved'])
def test_model_cannot_override_deterministic_termination(reason):
    flow, _, _, _ = configured()
    draft = {'decision': {'status': 'answered', 'reason': 'Bounded answer', 'material_gaps': []}}
    assert flow.reviewed_close_reason(reason, draft, {'omitted': 0}) == reason


def test_bounded_answer_may_close_with_ordinary_limitations_but_not_omitted_evidence():
    flow, _, _, _ = configured()
    draft = {'decision': {'status': 'answered', 'reason': 'Answers the selected page question', 'material_gaps': []},
             'limitations': ['Not a survey of the entire market.']}
    assert flow.reviewed_close_reason('sufficient', draft, {'omitted': 0}) == 'sufficient'
    assert flow.reviewed_close_reason('sufficient', draft, {'omitted': 1}) == 'unresolved'


@pytest.mark.parametrize('count,material_conflict', [(1, False), (9, False), (9, True)])
def test_final_contrast_resolves_only_provisional_assessment_gaps(count, material_conflict):
    flow, model, provider, graph = configured()
    original_ask, original_observations = model.ask, provider.observations
    batches = []

    async def observations(receipt, call):
        rows, cost = await original_observations(receipt, call)
        return [{**rows[0], 'url': f'https://example.com/document/{index}',
                 'text': f'Documentary statement {index}'} for index in range(count)], cost

    async def ask(name, schema, payload):
        if name == 'assess_evidence':
            batches.append(len(payload['evidence']))
            return {'assessments': [{'evidence_id': row['evidence_id'], 'stance': 'supports',
                    'reason': 'Documentary evidence', 'independence': 'unknown'} for row in payload['evidence']],
                    'continue_research': False, 'reason': 'Batch evaluated',
                    'gap': 'Only a limitation, or a document may occur in another batch.'}
        result = await original_ask(name, schema, payload)
        if name == 'draft_report':
            assert len(payload['evidence']) == count
            result['decision'] = {'status': 'unresolved' if material_conflict else 'answered',
                'reason': 'Global contrast', 'material_gaps': ['Unresolved same-period conflict'] if material_conflict else []}
        return result

    model.ask, provider.observations = ask, observations
    state = turn(graph, f'provisional-{count}-{material_conflict}')
    assert batches == ([8, 1] if count == 9 else [1])
    assert len(provider.starts) == 1
    run = flow.records().get('ai_mi_research_runs', state['run_id'])
    report = flow.records().get('ai_mi_reports', state['response']['report_id'])
    assert run['state'] == 'closed'
    assert run['termination_reason'] == ('unresolved' if material_conflict else 'sufficient')
    assert json.loads(report['coverage_json'])['conclusion']['status'] == ('unresolved' if material_conflict else 'answered')
    assert flow.bind_request({**state, 'request_id': 'next-turn'})['provisional_assessment_close'] is False


@pytest.mark.parametrize('reason', ['budget', 'time', 'step_limit', 'uncertain', 'no_data'])
def test_provisional_flag_cannot_elevate_external_or_mechanical_closures(reason):
    flow, _, _, _ = configured()
    draft = {'decision': {'status': 'answered', 'reason': 'Documentary answer', 'material_gaps': []}}
    assert flow.reviewed_close_reason(reason, draft, {'omitted': 0}, provisional=True) == reason


def test_provisional_closure_requires_complete_context_and_explicit_current_marker():
    flow, _, _, graph = configured()
    draft = {'decision': {'status': 'answered', 'reason': 'Documentary answer', 'material_gaps': []}}
    assert flow.reviewed_close_reason('unresolved', draft, {'omitted': 0}) == 'unresolved'
    assert flow.reviewed_close_reason('unresolved', draft, {'omitted': 1}, provisional=True) == 'unresolved'
    for coverage in ({}, {'omitted': None}, {'omitted': False}):
        assert flow.reviewed_close_reason('unresolved', draft, coverage, provisional=True) == 'unresolved'
    state = turn(graph, 'closure-origin')
    assessment = {'continue_research': False, 'gap': 'Potential gap'}
    assert flow.decide_next({**state, 'assessment': assessment, 'connector_issue': 'Invalid provider output'})['provisional_assessment_close'] is False
    assert flow.decide_next({**state, 'assessment': {**assessment, 'continue_research': True}})['provisional_assessment_close'] is False
    for limit in ('time', 'step_limit'):
        flow.close_limit = lambda run_id, reason=limit: reason
        decision = flow.decide_next({**state, 'assessment': assessment})
        assert decision['reason'] == limit and decision['provisional_assessment_close'] is False
    flow.close_limit = lambda run_id: None
    flow.run_evidence = lambda state: []
    assert flow.decide_next({**state, 'assessment': assessment})['provisional_assessment_close'] is False


def test_unpublished_legacy_draft_is_rebuilt_from_journal_without_recapture():
    flow, model, provider, graph = configured()
    state = asyncio.run(graph.ainvoke({'request_id': 'legacy-final-decision', 'question': 'D1'}, THREAD,
                                     interrupt_before=['publish_result']))
    old = copy.deepcopy(state['draft'])
    old.pop('decision')
    graph.update_state(THREAD, {'draft': old}, as_node='project_chart')
    starts = len(provider.starts)
    result = asyncio.run(graph.ainvoke(None, THREAD))
    assert len(provider.starts) == starts and len(flow.records().rows('ai_mi_reports')) == 1
    ReportDraft.model_validate(result['draft'])
    report = flow.records().get('ai_mi_reports', result['response']['report_id'])
    assert json.loads(report['coverage_json'])['conclusion']['status'] == 'answered'


def test_published_report_replay_keeps_original_coverage_without_new_model_or_tool_call():
    flow, model, provider, graph = configured()
    state = turn(graph, 'immutable-published-decision')
    report = copy.deepcopy(flow.records().get('ai_mi_reports', state['response']['report_id']))
    calls, starts = len(model.calls), len(provider.starts)
    result = asyncio.run(flow.publish_result({**state, 'draft': {}}))
    assert result['response']['report_id'] == report['report_id']
    assert flow.records().get('ai_mi_reports', report['report_id']) == report
    assert (len(model.calls), len(provider.starts)) == (calls, starts)
