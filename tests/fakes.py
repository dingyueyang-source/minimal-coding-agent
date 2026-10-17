"""Deterministic injected model; never selected through HTTP or user input."""
import asyncio
import copy
import json


def call(path, content, identity="call-1"):
    return {"id": identity, "type": "function", "function": {"name": "create_file",
            "arguments": json.dumps({"path": path, "content": content})}}


def tools(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def done(text="Finished"):
    return {"role": "assistant", "content": text}


class FakeLLM:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0
        self.cancelled = False
        self.tool_results = []

    async def next(self, messages, deadline):
        self.tool_results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        self.calls += 1
        if not self.script:
            raise AssertionError("FakeLLM script exhausted")
        step = self.script.pop(0)
        try:
            if callable(step):
                return await step(messages, deadline)
            return copy.deepcopy(step)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
