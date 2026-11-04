"""A valid consultation must not wait for Spark monitoring materialization."""
import asyncio
import json
from unittest.mock import Mock

import pytest

from app.ai_data_governance import native_governance
from app.ai_data_governance.inventory import identity
from test_ai_dg_native_activity import activity_harness


@pytest.mark.parametrize('result_state', ['answered', 'partial', 'not_evaluable'])
def test_record_only_finish_preserves_outcome_without_any_spark_command(monkeypatch, result_state):
    activity, journal, runner, _ = activity_harness(monkeypatch)
    runner.side_effect = AssertionError('Spark monitoring must not run before delivery')

    async def run():
        await activity.begin('subject', 'session', 'question')
        return await activity.finish({'response': {'result_state': result_state}}, materialize=False)

    recorded = asyncio.run(run())
    key = identity('consultation-activity-commit', activity.intent['operation_id'])
    batch = journal.read('outcomes', key)
    assert batch['operation_id'] == recorded['operation_id']
    assert batch['run']['component_version'] == activity.binding['component_revision']
    assert json.loads(batch['run']['manifest_json'])['result_state'] == result_state
    assert recorded == {'schema_version': 1, 'kind': 'CONSULTATION_ACTIVITY_RECORDED',
        'operation_id': batch['operation_id'], 'input_digest': identity('activity-batch', batch),
        'recorded': True, 'materialized': False}
    runner.assert_not_called()
    assert not any(kind in {'claims', 'materialized'} for kind, _ in journal.values)


def test_normal_delivery_selects_only_durable_recording(monkeypatch):
    activity, journal, runner, _ = activity_harness(monkeypatch)
    runner.side_effect = AssertionError('Do not invoke monitoring SQL')

    async def run():
        await activity.begin('subject', 'session', 'question')
        return await native_governance.finish_consultation_activity(activity,
            {'response': {'result_state': 'answered'}})

    result = asyncio.run(run())
    assert result['activity_record']['recorded'] is True
    assert result['activity_record']['materialized'] is False
    assert 'activity_receipt' not in result
    runner.assert_not_called()


def test_recording_failure_is_an_incident_without_a_second_write_or_spark(monkeypatch):
    activity, journal, runner, _ = activity_harness(monkeypatch)

    async def run():
        await activity.begin('subject', 'session', 'question')
        original = journal.create
        def create(kind, key, body):
            if kind == 'outcomes':
                raise TimeoutError('Record acknowledgment unavailable')
            return original(kind, key, body)
        journal.create = Mock(side_effect=create)
        result = await native_governance.finish_consultation_activity(activity,
            {'response': {'result_state': 'answered'}})
        assert result == {'activity_pending': True}
        assert sum(c.args[0] == 'outcomes' for c in journal.create.call_args_list) == 1

    asyncio.run(run())
    runner.assert_not_called()


@pytest.mark.parametrize('value', [0, 1, None, 'false'])
def test_materialization_switch_requires_an_explicit_boolean(monkeypatch, value):
    activity, journal, runner, _ = activity_harness(monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(activity.finish({}, materialize=value))
    assert not journal.create_calls
    runner.assert_not_called()
