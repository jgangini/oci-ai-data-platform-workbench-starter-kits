"""Recorded fixture completions exercise the same portable workflow without OCI."""
import asyncio
from collections import deque
import json
import sys
from types import ModuleType

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from market_fixture_factory import build_graph


def fixture_runtime(monkeypatch,responses):
    class FixtureModel:
        def __init__(self):
            self.responses=deque(responses)
            self.calls=0
        async def complete(self,system,question):
            self.calls+=1
            assert system.startswith('market_') and json.loads(question)['schema']
            return self.responses.popleft()
    module=ModuleType('aidp_agent_flow.model')
    module.FixtureModel=FixtureModel
    monkeypatch.setitem(sys.modules,'aidp_agent_flow.model',module)
    return {'model':FixtureModel(),'external_effects_allowed':False,'checkpointer':InMemorySaver()}


@pytest.mark.parametrize('intent,expected',[
    ('research','needs_clarification'),('reuse_report','needs_clarification'),('schedule_proposal','partial'),
])
def test_fixture_factory_consumes_recorded_completion_and_supplied_checkpoint(monkeypatch,intent,expected):
    brief={'intent':intent,'entity':'Fixture','market':'Colombia','objective':'Inspect the fixture',
        'start':None,'end':None,'sources':[],'clarification':'Specify the source' if intent=='research' else None,
        'assumptions':[]}
    responses=[json.dumps(brief)]
    if intent=='schedule_proposal':
        responses.append(json.dumps({'entity':'Fixture','objective':'Inspect the fixture','sources':[],
            'frequency':None,'timezone':None,'destination':None}))
    runtime=fixture_runtime(monkeypatch,responses)
    graph=build_graph(runtime)
    assert graph.checkpointer is runtime['checkpointer']
    result=asyncio.run(graph.ainvoke({'request_id':'one','question':'Fixture'},
        config={'configurable':{'thread_id':'one'}}))
    assert result['response']['status']==expected and runtime['model'].calls==(2 if intent=='schedule_proposal' else 1)
    assert not runtime['model'].responses
    if intent=='schedule_proposal':
        assert result['response']['proposal']['effects_created']==0


def test_fixture_factory_refuses_real_models_and_external_effect_permission(monkeypatch):
    runtime=fixture_runtime(monkeypatch,[])
    runtime['external_effects_allowed']=True
    with pytest.raises(ValueError,match='forbids'):
        build_graph(runtime)
    runtime.update(external_effects_allowed=False,model=object())
    with pytest.raises(ValueError,match='fixture model'):
        build_graph(runtime)
