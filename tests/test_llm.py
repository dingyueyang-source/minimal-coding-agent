"""Real HTTP adapter behaviour against an in-process mock provider (no network, no key)."""
import asyncio
import json

import httpx
import pytest

from app.agent import CREATE_FILE_TOOL
from app.deadline import Deadline
from app.errors import AgentError
from app.llm import RESPONSE_LIMIT, ChatCompletionsClient, LLMConfig

CONFIG = LLMConfig(api_key="sk-test-secret", base_url="https://provider.test/v1", model="test-model")


def completion(message, finish="stop"):
    return {"id": "x", "choices": [{"index": 0, "message": message, "finish_reason": finish}]}


def client(handler, config=CONFIG):
    return ChatCompletionsClient(config, [CREATE_FILE_TOOL], transport=httpx.MockTransport(handler))


async def test_request_shape_and_tool_call_parse():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion({"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "create_file",
             "arguments": '{"path":"hello.txt","content":"Hello, world!"}'}}]}, "tool_calls"))

    llm = client(handler)
    message = await llm.next([{"role": "user", "content": "hi"}], Deadline.after(5))
    await llm.aclose()
    assert seen["url"] == "https://provider.test/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test-secret"
    body = seen["body"]
    assert body["tool_choice"] == "auto" and body["parallel_tool_calls"] is False
    assert body["model"] == "test-model" and body["max_tokens"] == 4096
    assert body["tools"][0]["function"]["name"] == "create_file"
    assert message["tool_calls"][0]["id"] == "c1" and message["content"] is None


async def test_optional_provider_parameters():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=completion({"role": "assistant", "content": "done"}))

    config = LLMConfig(api_key="k", base_url="https://p.test/v1", model="m", token_param="max_completion_tokens",
                       max_output_tokens=100, parallel_tool_calls=False)
    llm = client(handler, config)
    assert (await llm.next([], Deadline.after(5)))["content"] == "done"
    await llm.aclose()
    assert "parallel_tool_calls" not in seen and seen["max_completion_tokens"] == 100 and "max_tokens" not in seen


@pytest.mark.parametrize("status,code", [(401, "LLM_AUTH_FAILED"), (403, "LLM_AUTH_FAILED"), (429, "LLM_RATE_LIMITED"),
                                         (500, "LLM_UNAVAILABLE"), (503, "LLM_UNAVAILABLE"),
                                         (400, "LLM_REQUEST_REJECTED"), (302, "LLM_REQUEST_REJECTED")])
async def test_http_status_mapping_hides_provider_body(status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="provider echoed sk-test-secret and the prompt",
                              headers={"location": "https://elsewhere.test/"})

    llm = client(handler)
    with pytest.raises(AgentError) as info:
        await llm.next([], Deadline.after(5))
    await llm.aclose()
    assert info.value.code == code and "sk-test" not in str(info.value) and "prompt" not in str(info.value)
    assert len(calls) == 1  # no retries, no redirect following


async def test_connection_error_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused")

    llm = client(handler)
    with pytest.raises(AgentError) as info:
        await llm.next([], Deadline.after(5))
    await llm.aclose()
    assert info.value.code == "LLM_UNAVAILABLE"


async def test_response_body_limit():
    def handler(request):
        return httpx.Response(200, content=b"{" + b" " * RESPONSE_LIMIT + b"}")

    llm = client(handler)
    with pytest.raises(AgentError) as info:
        await llm.next([], Deadline.after(5))
    await llm.aclose()
    assert info.value.code == "LLM_RESPONSE_TOO_LARGE"


async def test_slow_provider_is_cancelled_by_deadline():
    async def handler(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json=completion({"role": "assistant", "content": "late"}))

    llm = client(handler)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(AgentError) as info:
        await llm.next([], Deadline.after(0.3))
    await llm.aclose()
    assert info.value.code == "LLM_CALL_TIMEOUT" and info.value.timeout
    assert loop.time() - started < 2


@pytest.mark.parametrize("raw,code", [
    (b"not json", "LLM_PROTOCOL_ERROR"),
    (json.dumps({"choices": []}).encode(), "LLM_PROTOCOL_ERROR"),
    (json.dumps(completion({"role": "assistant", "content": "cut"}, "length")).encode(), "LLM_OUTPUT_TRUNCATED"),
    (json.dumps(completion({"role": "assistant", "content": None, "refusal": "no"})).encode(), "LLM_REFUSED"),
    (json.dumps(completion({"role": "assistant", "content": ""}, "content_filter")).encode(), "LLM_REFUSED"),
    (json.dumps(completion({"role": "user", "content": "x"})).encode(), "LLM_PROTOCOL_ERROR"),
    (json.dumps(completion({"role": "assistant", "content": "x"}, "weird")).encode(), "LLM_PROTOCOL_ERROR"),
])
def test_parse_failures(raw, code):
    with pytest.raises(AgentError) as info:
        ChatCompletionsClient.parse(raw)
    assert info.value.code == code


def test_config_repr_redacts_key():
    assert "sk-test-secret" not in repr(CONFIG)
    with pytest.raises(ValueError):
        LLMConfig(api_key="k", base_url="https://p", model="m", token_param="tokens")
