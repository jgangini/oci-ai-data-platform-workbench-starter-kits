"""Offline candidate conformance; no broker, model or provider calls."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]/'contracts/sonda-activity/candidate-v1'
spec = importlib.util.spec_from_file_location('activity_candidate_test', ROOT/'reference.py')
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)


def events():
    return json.loads((ROOT/'fixtures/events.json').read_bytes())


def raw(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


@pytest.mark.parametrize('name', ['runs', 'runs-autonomous', 'events', 'events-at-tail',
    'events-uncertain', 'events-degraded', 'events-progress-default', 'events-finish-before-start',
    'runs-provider','runs-publication'])
def test_valid_synthetic_fixtures(name):
    assert candidate.parse((ROOT/f'fixtures/{name}.json').read_bytes())


@pytest.mark.parametrize('name', ['reject-cross-run', 'reject-cross-attempt', 'reject-private-field',
    'reject-native-terminal', 'reject-provider-claim', 'reject-native-degradation',
    'reject-provider-agent-identity','reject-execution-component'])
def test_invalid_fixtures(name):
    with pytest.raises(ValueError):
        candidate.parse((ROOT/f'fixtures/{name}.json').read_bytes())


@pytest.mark.parametrize('field,value', [('producer_sequence',True), ('producer_sequence',0),
    ('producer_sequence',10001), ('type','debug'), ('emitted_at','2026-10-30T00:00:00'),
    ('authority','browser'), ('ingestion_sequence',0)])
def test_invalid_event_fields(field,value):
    batch = events()
    batch['events'][0][field] = value
    with pytest.raises(ValueError):
        candidate.parse(raw(batch))


def test_completed_requires_result_but_failure_can_follow_partial_text():
    batch = events()
    batch['events'][-1]['final_ref'] = None
    with pytest.raises(ValueError):
        candidate.parse(raw(batch))
    batch['events'][-1]['outcome'] = 'failed'
    assert candidate.parse(raw(batch)).events[-1].outcome == 'failed'


def test_duplicate_json_keys_and_oversized_batch_are_rejected():
    for value in (b'{"scope":{},"scope":{}}', b' '*1048577):
        with pytest.raises(ValueError):
            candidate.parse(value)


def test_utf8_event_limit_and_batch_count():
    batch = events()
    batch['events'][2]['public_text_delta'] = '\U0001f600'*4096
    with pytest.raises(ValueError, match='byte limit'):
        candidate.parse(raw(batch))
    batch = events()
    batch['events'] = [copy.deepcopy(batch['events'][0]) for _ in range(101)]
    with pytest.raises(ValueError):
        candidate.parse(raw(batch))


@pytest.mark.parametrize('cursor', [None, '', 123])
def test_empty_tail_still_requires_opaque_resume_cursor(cursor):
    batch = events()
    batch.update(events=[], next_cursor=None, resume_cursor=cursor)
    with pytest.raises(ValueError):
        candidate.parse(raw(batch))


def test_tail_retains_checkpoint_without_pagination_or_completion():
    batch = events()
    batch.update(events=[], next_cursor=None)
    result = candidate.parse(raw(batch))
    assert result.resume_cursor == batch['resume_cursor']
    assert result.next_cursor is None and result.events == []


@pytest.mark.parametrize('field,value', [('work_id',None), ('investigation_id',None),
    ('investigation_id','not-a-hash'), ('work_id','b'*64)])
def test_autonomous_case_hash_and_work_uuid_are_separate_required_fields(field,value):
    batch = json.loads((ROOT/'fixtures/runs-autonomous.json').read_bytes())
    batch['items'][0][field] = value
    with pytest.raises(ValueError):
        candidate.parse(raw(batch))


def test_synthetic_replay_preserves_run_attempt_and_checkpoint():
    scenario = json.loads((ROOT/'fixtures/replay-scenario.json').read_bytes())
    assert scenario['synthetic'] is True
    reads = [candidate.parse(raw(value)) for value in scenario['reads']]
    assert {str(value.run_id) for value in reads} == {scenario['expected_run_id']}
    assert {str(value.attempt_id) for value in reads} == {scenario['expected_attempt_id']}
    assert len({value.resume_cursor for value in reads}) == 1
    assert reads[0].events == reads[2].events


def test_empty_tail_requires_attempt_identity_too():
    batch = events()
    batch['events'] = []
    del batch['attempt_id']
    with pytest.raises(ValueError):
        candidate.parse(raw(batch))


def test_neutral_uncertainty_is_not_a_terminal_or_business_effect():
    batch=candidate.parse((ROOT/'fixtures/events-uncertain.json').read_bytes())
    assert len(batch.events)==1 and batch.events[0].status=='uncertain'
    assert batch.events[0].authority=='activity_broker' and batch.events[0].effect is None
    ordinary=candidate.parse((ROOT/'fixtures/events-progress-default.json').read_bytes())
    assert ordinary.events[0].status=='progress'


@pytest.mark.parametrize('domain,authority', [('query_read','native_agent'),
    ('research_preparation','native_agent'),('provider_capture','provider_broker'),('publication','gold_writer')])
def test_explicit_tool_domain_has_one_allowed_producer_role(domain,authority):
    batch=events()
    batch['events'][0].update(domain=domain,authority=authority)
    assert candidate.parse(raw(batch)).events[0].domain==domain
    batch['events'][0]['authority']='activity_broker'
    with pytest.raises(ValueError):
        candidate.parse(raw(batch))


def test_degraded_stream_keeps_partial_without_claiming_final_success():
    batch=candidate.parse((ROOT/'fixtures/events-degraded.json').read_bytes())
    assert [row.type for row in batch.events]==['text_delta','stage']
    assert batch.events[1].status=='degraded'


def test_provider_run_links_another_execution_without_inventing_native_agent():
    batch=candidate.parse((ROOT/'fixtures/runs-provider.json').read_bytes())
    run=batch.items[0]
    assert run.agent_id is None and run.agent_name is None
    assert run.execution_kind=='provider_execution' and run.component=='huginn_provider_broker'
    assert run.causal_parent.run_id != run.run_id and run.causal_parent.attempt_id != run.attempt_id


@pytest.mark.parametrize('fault',['native_agent_missing','self_parent','duplicate_core_message'])
def test_run_identity_and_causal_shape_are_checked_without_implying_authorization(fault):
    batch=json.loads((ROOT/'fixtures/runs-provider.json').read_bytes())
    run=batch['items'][0]
    if fault=='native_agent_missing':
        run.update(execution_kind='native_invocation',component='native_agent')
    elif fault=='self_parent':
        run['causal_parent']['run_id']=run['run_id']
    else:
        run['core_message_ids']*=2
    with pytest.raises(ValueError):
        candidate.parse(raw(batch))


def test_projected_provider_event_cannot_enter_an_authorized_native_run():
    run=json.loads((ROOT/'fixtures/runs.json').read_bytes())['items'][0]
    event=events()['events'][0]
    assert candidate.validate_run_event(run,event).domain=='query_read'
    event.update(domain='provider_capture',authority='provider_broker')
    # Role/domain shape is valid; the descriptor's execution kind still rejects it.
    assert candidate.ToolStart.model_validate(event)
    with pytest.raises(ValueError,match='role does not match'):
        candidate.validate_run_event(run,event)


def test_neutral_lifecycle_stays_separate_from_tool_and_model_effects():
    run=json.loads((ROOT/'fixtures/runs.json').read_bytes())['items'][0]
    stage=json.loads((ROOT/'fixtures/events-uncertain.json').read_bytes())['events'][0]
    assert candidate.validate_run_event(run,stage).status=='uncertain'
    event=events()['events'][2]
    event['authority']='activity_broker'
    with pytest.raises(ValueError,match='cannot claim'):
        candidate.validate_run_event(run,event)


def test_provider_run_can_confirm_only_its_own_domain_and_does_not_stream_raw_provider_text():
    run=json.loads((ROOT/'fixtures/runs-provider.json').read_bytes())['items'][0]
    event=events()['events'][0]
    event.update(run_id=run['run_id'],attempt_id=run['attempt_id'],domain='provider_capture',authority='provider_broker')
    assert candidate.validate_run_event(run,event).domain=='provider_capture'
    text=events()['events'][2]
    text.update(run_id=run['run_id'],attempt_id=run['attempt_id'],authority='provider_broker')
    with pytest.raises(ValueError,match='only to a native'):
        candidate.validate_run_event(run,text)
