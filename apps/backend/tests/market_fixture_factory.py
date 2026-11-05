"""Plugin fixture runner for the portable graph; never a real-model evaluation."""
import json

from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.state import MarketRunStore
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
from tests.modules.ai_market_intelligence.test_market_intelligence_graph import MarketFixtureCollector, MarketSnapshot


class MarketRecordedFixtureModel:
    def __init__(self,model):
        self.model=model

    async def ask(self,name,schema,payload):
        text=await self.model.complete(name,json.dumps({'schema':schema,'payload':payload},ensure_ascii=False))
        return json.loads(text)


def build_graph(runtime):
    from aidp_agent_flow.model import FixtureModel
    if runtime['external_effects_allowed'] is not False or type(runtime['model']) is not FixtureModel:
        raise ValueError('This factory requires the plugin fixture model and forbids external effects')
    store=MarketRunStore(MarketSnapshot(),'fixture-only-current')
    workflow=MarketWorkflow(store,MarketRecordedFixtureModel(runtime['model']),MarketFixtureCollector(),enabled_sources=('web',))
    return workflow.build(runtime['checkpointer'])
