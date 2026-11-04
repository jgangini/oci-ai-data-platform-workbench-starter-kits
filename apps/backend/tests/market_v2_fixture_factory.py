"""Plugin fixture runner for the portable graph; never a real-model evaluation."""
import json

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_state import V2RunStore
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_workflow import V2Workflow
from test_market_intelligence_v2 import V2FixtureCollector, V2Snapshot


class V2RecordedFixtureModel:
    def __init__(self,model):
        self.model=model

    async def ask(self,name,schema,payload):
        text=await self.model.complete(name,json.dumps({'schema':schema,'payload':payload},ensure_ascii=False))
        return json.loads(text)


def build_graph(runtime):
    from aidp_agent_flow.model import FixtureModel
    if runtime['external_effects_allowed'] is not False or type(runtime['model']) is not FixtureModel:
        raise ValueError('This factory requires the plugin fixture model and forbids external effects')
    store=V2RunStore(V2Snapshot(),'fixture-only-v2')
    workflow=V2Workflow(store,V2RecordedFixtureModel(runtime['model']),V2FixtureCollector(),enabled_sources=('web',))
    return workflow.build(runtime['checkpointer'])
