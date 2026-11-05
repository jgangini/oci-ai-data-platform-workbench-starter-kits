"""Native entry; maintained implementation is in governance_runtime.py."""
import asyncio
import governance_runtime
from aidputils.agents.toolkit.agent_helper import pre_invoke_setup
from langchain_core.messages import AIMessageChunk


class DataGovernanceAgent:
    def __init__(self):
        self.runtime = governance_runtime.DataGovernanceAgent()

    def setup(self):
        self.runtime.setup()

    async def invoke(self, user_query, **kwargs):
        pre_invoke_setup(**kwargs)
        stream = kwargs.get("stream", False)
        if type(stream) is not bool:
            raise ValueError("The native stream flag must be boolean")
        if stream:
            return self._stream_messages(user_query, dict(kwargs))
        return await self.runtime.invoke(user_query, **kwargs)

    async def _stream_messages(self, user_query, kwargs):
        task = asyncio.create_task(self.runtime.invoke(user_query, **kwargs))
        try:
            yield self._keepalive()
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=15)
                if not done:
                    yield self._keepalive()
            result = await task
            for message in result["messages"]:
                yield message
        finally:
            await self._finish_stream(task)

    async def _finish_stream(self, task):
        if not task.done():
            task.cancel()
        joined = asyncio.gather(task, return_exceptions=True)
        while not joined.done():
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError:
                continue

    def _keepalive(self):
        return AIMessageChunk(content="", additional_kwargs={"governance_stream_event":"keepalive"})
