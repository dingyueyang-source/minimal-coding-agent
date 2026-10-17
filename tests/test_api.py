"""HTTP contract checks that need neither Docker nor a model key."""
import asyncio
import re
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.config import Settings
from app.errors import AgentError
from app.main import create_app
from tests.fakes import FakeLLM

STATIC = Path(__file__).resolve().parent.parent / "static"


class UnavailableSandbox:
    """Docker deliberately unavailable: startup checks fail, so admission stays closed."""
    pending = set()
    refs = {}

    async def initialize(self):
        raise AgentError("DOCKER_FAILED", "daemon unavailable")


@pytest_asyncio.fixture
async def api():
    app = create_app(Settings(), FakeLLM(), UnavailableSandbox(), origins=("http://127.0.0.1:8000",),
                     start_retry_seconds=60)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as http:
            yield http


async def test_page_and_security_headers(api):
    response = await api.get("/")
    assert response.status_code == 200 and "<button id=\"run\"" in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert (await api.get("/static/app.js")).status_code == 200


async def test_untrusted_host_rejected(api):
    assert (await api.get("/health", headers={"host": "evil.example"})).status_code == 400


async def test_health_reports_startup_failure_without_secrets(api):
    for _ in range(50):
        body = (await api.get("/health")).json()
        if body["startup_checks"] != "running":
            break
        await asyncio.sleep(0.02)
    assert body["ready"] is False and body["startup_error"] == "DOCKER_FAILED"
    assert not any("key" in k.lower() for k in body)


@pytest.mark.parametrize("kwargs,status", [
    ({"content": b'{"request":"x"}', "headers": {"content-type": "text/plain"}}, 415),
    ({"content": b"{bad", "headers": {"content-type": "application/json"}}, 422),
    ({"json": {"request": "x", "image": "alpine"}}, 422),
    ({"json": {"request": "   "}}, 422),
    ({"json": {"request": 5}}, 422),
    ({"json": {"request": "a" * 8193}}, 422),
    ({"json": {"request": "a" * 70000}}, 413),
    ({"json": {"request": "ok"}, "headers": {"origin": "https://evil.example"}}, 403),
    ({"json": {"request": "ok"}}, 503),   # valid, but startup checks failed: no container created
    ({"json": {"request": "ok"}, "headers": {"origin": "http://127.0.0.1:8000"}}, 503),
])
async def test_submit_validation(api, kwargs, status):
    response = await api.post("/tasks", **kwargs)
    assert response.status_code == status
    assert response.json()["error"]["code"]


async def test_streamed_oversized_body_without_content_length(api):
    async def chunks():
        for _ in range(20):
            yield b"x" * 8192

    response = await api.post("/tasks", content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 413


@pytest.mark.parametrize("task_id", ["00000000-0000-0000-0000-000000000000", "not-a-uuid", "..%2f..%2fetc"])
async def test_unknown_task_404(api, task_id):
    response = await api.get(f"/tasks/{task_id}")
    assert response.status_code == 404 and response.json()["error"]["code"] in ("TASK_NOT_FOUND", "NOT_FOUND")


def test_frontend_never_uses_html_injection_sinks():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(", source)
    assert "textContent" in source
