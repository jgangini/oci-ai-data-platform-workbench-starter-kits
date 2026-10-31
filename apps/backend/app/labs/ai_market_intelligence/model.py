"""Structured model calls with bounded data and the six approved prompts."""
import json
from pathlib import Path


class DecisionModel:
    def __init__(self, llm):
        self.llm = llm
        self.prompts = json.loads(Path(__file__).with_name('prompts.json').read_text(encoding='utf-8'))

    async def ask(self, name, schema, payload):
        from langchain_core.messages import HumanMessage, SystemMessage
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(data.encode('utf-8')) > 100000:
            raise ValueError('Decision context exceeds the admitted size; no model called')
        return await self.llm.with_structured_output(schema, method='json_schema').ainvoke([
            SystemMessage(content=self.prompts[name]),
            HumanMessage(content=data)])
