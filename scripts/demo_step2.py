"""Run the checkpoint-2 chain with an explicit FakeLLM and real Linux containers."""
import asyncio
import json
from dataclasses import replace

from app.config import Settings
from app.sandbox import SandboxManager
from app.task_manager import TaskManager
from tests.fakes import FakeLLM, call, done, tools


async def run_case(name, model, *, timeout=60):
    settings = replace(Settings.from_env(), task_seconds=timeout)
    sandbox = SandboxManager(settings)
    manager = TaskManager(settings, sandbox, model)
    try:
        await manager.start()
        accepted = await manager.submit("Checkpoint 2 scripted demonstration: " + name)
        result = await manager.wait(accepted["task_id"])
        print(json.dumps({"case": name, "model": "FakeLLM (not a real LLM)", **result}, ensure_ascii=True))
    finally:
        await manager.close()


async def slow(messages, deadline):
    await asyncio.sleep(30)
    return tools(call("late.txt", "must never be written"))


async def main():
    await run_case("success", FakeLLM(tools(call("nested/demo.txt", "Read back from Docker")), done()))
    await run_case("path-rejected", FakeLLM(tools(call("../secret.txt", "blocked")), done()))
    await run_case("timeout", FakeLLM(slow), timeout=3)


if __name__ == "__main__":
    asyncio.run(main())
