"""Full HTTP flow (POST → poll → read-back result) through FastAPI, real Docker, injected FakeLLM."""
import asyncio
import uuid

import httpx
import pytest
import pytest_asyncio

from app.config import Settings
from app.deadline import Deadline
from app.main import create_app
from app.sandbox import SandboxManager
from tests.fakes import FakeLLM, call, done, tools

pytestmark = pytest.mark.docker


class Harness:
    def __init__(self, http, app, sandbox, model):
        self.http, self.app, self.sandbox, self.model = http, app, sandbox, model

    async def run(self, text="Create a file"):
        accepted = await self.http.post("/tasks", json={"request": text})
        assert accepted.status_code == 202, accepted.text
        url = accepted.json()["status_url"]
        seen_phases = []
        for _ in range(400):
            snapshot = (await self.http.get(url)).json()
            if not seen_phases or seen_phases[-1] != snapshot["phase"]:
                seen_phases.append(snapshot["phase"])
            if snapshot["status"] != "running":
                return snapshot, seen_phases
            await asyncio.sleep(0.1)
        raise AssertionError("task did not finish")


async def start(model, **overrides):
    settings = Settings(project_id="mca-test-" + uuid.uuid4().hex[:10], **overrides)
    sandbox = SandboxManager(settings)
    app = create_app(settings, model, sandbox, origins=("http://127.0.0.1:8000",))
    return settings, sandbox, app


@pytest_asyncio.fixture
async def harness(request):
    model, overrides = request.param
    settings, sandbox, app = await start(model, **overrides)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as http:
                for _ in range(600):
                    health = (await http.get("/health")).json()
                    if health["ready"] or health["startup_checks"] == "failed":
                        break
                    await asyncio.sleep(0.1)
                assert health["ready"], health
                yield Harness(http, app, sandbox, model)
    finally:
        await sandbox.recover(set())
        assert not await sandbox.list_project(Deadline.after(10))


async def slow(messages, deadline):
    await asyncio.sleep(30)
    return tools(call("late.txt", "never"))


@pytest.mark.parametrize("harness", [(FakeLLM(tools(call("hello.txt", "Hello, world!")), done("Created")), {})],
                         indirect=True)
async def test_http_success_flow(harness):
    result, phases = await harness.run("Create hello.txt in the workspace with the content Hello, world!")
    assert result["status"] == "succeeded" and result["cleanup"]["status"] == "done"
    assert result["files"] == [{"path": "hello.txt", "content": "Hello, world!", "size_bytes": 13, "bytes_returned": 13,
                                "truncated": False, "decode_replaced": False, "read_back": True}]
    assert phases[-1] == "complete" and len(result["container_id"]) == 12
    event_phases = [e["message"] for e in result["events"] if e["code"] == "PHASE_CHANGED"]
    assert event_phases == ["agent_running", "retrieving", "cleaning", "complete"]
    assert not await harness.sandbox.list_project(Deadline.after(10))


@pytest.mark.parametrize("harness", [(FakeLLM(tools(call("../secret.txt", "x")), done("I could not")), {})], indirect=True)
async def test_http_path_traversal_reported(harness):
    result, _ = await harness.run()
    assert result["status"] == "failed" and result["error"]["code"] == "NO_FILES_CREATED"
    assert "PATH_REJECTED" in result["error"]["message"] and result["failed_phase"] == "agent_running"
    assert harness.model.tool_results[0]["error"]["code"] == "PATH_REJECTED"
    assert result["cleanup"]["status"] == "done" and result["files"] == []


@pytest.mark.parametrize("harness", [(FakeLLM(slow), {"task_seconds": 3})], indirect=True)
async def test_http_timeout_reported(harness):
    result, _ = await harness.run()
    assert result["status"] == "timeout" and result["execution_status"] == "timeout"
    assert result["error"]["code"] in ("TASK_TIMEOUT", "LLM_CALL_TIMEOUT")
    assert result["failed_phase"] == "agent_running" and result["cleanup"]["status"] == "done"
    assert harness.model.cancelled
    assert not await harness.sandbox.list_project(Deadline.after(10))


@pytest.mark.parametrize("harness", [(FakeLLM(tools(call("x.html", "<img src=x onerror=alert(1)>")), done("<b>hi</b>")), {})],
                         indirect=True)
async def test_http_html_content_returned_verbatim(harness):
    result, _ = await harness.run()
    assert result["files"][0]["content"] == "<img src=x onerror=alert(1)>"
    assert result["agent_message"] == "<b>hi</b>"


async def gate(messages, deadline):
    await asyncio.sleep(3)
    return done("held")


@pytest.mark.parametrize("harness", [(FakeLLM(gate, gate, gate), {})], indirect=True)
async def test_http_fourth_concurrent_task_gets_429(harness):
    responses = await asyncio.gather(*[harness.http.post("/tasks", json={"request": f"task {i}"}) for i in range(4)])
    codes = sorted(r.status_code for r in responses)
    assert codes == [202, 202, 202, 429]
    for response in responses:
        if response.status_code == 202:
            while (await harness.http.get(response.json()["status_url"])).json()["status"] == "running":
                await asyncio.sleep(0.1)
