from ai_market_intelligence.native import AgentRuntime


class MarketIntelligenceAgent:
    def __init__(self):
        self.runtime = AgentRuntime()

    def setup(self):
        self.runtime.setup()

    async def invoke(self, user_query, **kwargs):
        return await self.runtime.invoke(user_query, **kwargs)
