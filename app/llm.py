"""Injectable asynchronous model boundary and the real Chat Completions adapter."""
import asyncio
import json
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.deadline import Deadline
from app.errors import AgentError

RESPONSE_LIMIT = 1024 * 1024


class LLMClient(Protocol):
    async def next(self, messages: list[dict], deadline: Deadline) -> dict:
        """Return a Chat Completions assistant message (including tool_calls)."""
        ...


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    max_output_tokens: int = 4096
    token_param: str = "max_tokens"           # or max_completion_tokens (provider specific)
    parallel_tool_calls: bool = True          # send parallel_tool_calls=false where supported

    def __post_init__(self):
        if self.token_param not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("LLM_TOKEN_PARAM must be max_tokens or max_completion_tokens")
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 65536:
            raise ValueError("LLM_MAX_OUTPUT_TOKENS must be within 1..65536")

    def __repr__(self):
        return f"LLMConfig(base_url={self.base_url!r}, model={self.model!r}, api_key=<redacted>)"


class ChatCompletionsClient:
    """Async httpx adapter. No retries, no redirects, bounded response body.

    The Agent wraps every call in asyncio.timeout(min(25s, remaining)); httpx coroutines are
    cancellable, so that outer limit actually stops the local request. httpx phase timeouts are
    additional network limits, not a replacement for the hard deadline.
    """

    def __init__(self, config: LLMConfig, tools: list[dict], *, transport=None):
        self.config = config
        self.tools = tools
        self.http = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(25, connect=10),
            follow_redirects=False,
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            trust_env=False)

    @property
    def label(self):
        return self.config.model

    async def aclose(self):
        await self.http.aclose()

    def _body(self, messages):
        body = {"model": self.config.model, "messages": messages, "tools": self.tools,
                "tool_choice": "auto", self.config.token_param: self.config.max_output_tokens}
        if self.config.parallel_tool_calls:
            body["parallel_tool_calls"] = False
        return body

    async def next(self, messages, deadline):
        timeout = deadline.limit(25)
        try:
            async with asyncio.timeout(timeout):
                async with self.http.stream("POST", "chat/completions", json=self._body(messages)) as response:
                    status = response.status_code
                    if status in (401, 403):
                        raise AgentError("LLM_AUTH_FAILED", f"Model provider rejected credentials (HTTP {status})")
                    if status == 429:
                        raise AgentError("LLM_RATE_LIMITED", "Model provider rate limit or quota exceeded (HTTP 429)")
                    if status >= 500:
                        raise AgentError("LLM_UNAVAILABLE", f"Model provider unavailable (HTTP {status})")
                    if 300 <= status < 400:
                        raise AgentError("LLM_REQUEST_REJECTED", f"Model endpoint redirected (HTTP {status}); redirects are disabled")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > RESPONSE_LIMIT:
                            raise AgentError("LLM_RESPONSE_TOO_LARGE", "Model response exceeds 1 MiB")
                    if status != 200:
                        # Provider bodies may echo prompts; only the status code is surfaced.
                        raise AgentError("LLM_REQUEST_REJECTED", f"Model provider rejected the request (HTTP {status})")
        except (TimeoutError, httpx.TimeoutException):
            raise AgentError("LLM_CALL_TIMEOUT", "Model call deadline exceeded", timeout=True) from None
        except httpx.HTTPError:
            raise AgentError("LLM_UNAVAILABLE", "Could not reach the model provider") from None
        return self.parse(bytes(body))

    @staticmethod
    def parse(raw):
        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            message = choice["message"]
            finish = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError):
            raise AgentError("LLM_PROTOCOL_ERROR", "Malformed model response") from None
        if not isinstance(message, dict):
            raise AgentError("LLM_PROTOCOL_ERROR", "Malformed model message")
        if message.get("refusal"):
            raise AgentError("LLM_REFUSED", "Model refused the request")
        if finish == "length":
            raise AgentError("LLM_OUTPUT_TRUNCATED", "Model output hit the token limit")
        if finish == "content_filter":
            raise AgentError("LLM_REFUSED", "Model output was blocked by the provider content filter")
        if finish not in ("stop", "tool_calls", None):
            raise AgentError("LLM_PROTOCOL_ERROR", "Unexpected model finish reason")
        # Keep only the fields the Agent loop and the provider history need.
        result = {"role": "assistant", "content": message.get("content")}
        if message.get("tool_calls"):
            result["tool_calls"] = message["tool_calls"]
        if isinstance(message.get("reasoning_content"), str):
            result["reasoning_content"] = message["reasoning_content"]
        if message.get("role") != "assistant":
            raise AgentError("LLM_PROTOCOL_ERROR", "Expected an assistant message")
        return result
