"""Native entry; implementation and prompts are in documentary_runtime.py."""
import documentary_runtime
from aidputils.agents.toolkit.agent_helper import pre_invoke_setup


class DataGovernanceDocumentationAgent:
    def __init__(self):
        self.runtime = documentary_runtime.DataGovernanceDocumentationAgent()

    def setup(self):
        self.runtime.setup()

    async def invoke(self, user_query, **kwargs):
        pre_invoke_setup(**kwargs)
        return await self.runtime.invoke(user_query, **kwargs)
