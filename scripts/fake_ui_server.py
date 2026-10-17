"""UI test server: real FastAPI + real Docker sandbox, but a SCRIPTED model (not a real LLM).

Used only to exercise the page's failure displays (XSS text, path rejection, timeout) without
spending model tokens. The model is chosen here in code, never via HTTP. The /health "model"
field and the page banner say "FakeLLM", so this cannot be mistaken for a real-LLM demo.

    .venv\\Scripts\\python.exe -m scripts.fake_ui_server      # http://127.0.0.1:8001
Request keywords: "xss", "traversal", "slow", "multi"; anything else writes demo.txt.
"""
import asyncio
import json
from dataclasses import replace

import uvicorn

from app.config import Settings
from app.main import create_app

PORT = 8001


def call(path, content, identity):
    return {"id": identity, "type": "function",
            "function": {"name": "create_file", "arguments": json.dumps({"path": path, "content": content})}}


class KeywordFakeLLM:
    label = "FakeLLM (scripted UI test, not a real model)"

    async def next(self, messages, deadline):
        request = messages[1]["content"].lower()
        turn = sum(1 for m in messages if m["role"] == "assistant")
        if turn:
            return {"role": "assistant", "content": "<b>Done</b> (scripted reply rendered as text)"}
        if "slow" in request:
            await asyncio.sleep(120)
        if "traversal" in request:
            plan = [call("../secret.txt", "should be rejected", "c1")]
        elif "xss" in request:
            plan = [call("xss.html", "<img src=x onerror=alert('xss')><script>alert(1)</script>", "c1")]
        elif "multi" in request:
            plan = [call("a/b/nested.txt", "nested", "c1"), call("empty.txt", "", "c2"), call("中文.txt", "你好", "c3")]
        else:
            plan = [call("demo.txt", "scripted content", "c1")]
        return {"role": "assistant", "content": None, "tool_calls": plan}


def main():
    settings = replace(Settings.from_env(), task_seconds=15)
    app = create_app(settings, KeywordFakeLLM(), origins=(f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"))
    uvicorn.run(app, host="127.0.0.1", port=PORT, workers=1, access_log=False)


if __name__ == "__main__":
    main()
