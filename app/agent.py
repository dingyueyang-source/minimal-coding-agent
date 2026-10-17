"""One execution loop for all injected clients, including scripted tests."""
import asyncio
import json

from app.errors import AgentError
from shared.path_policy import PolicyError, validate_write

SYSTEM = ("You create UTF-8 text files using create_file(path, content). Use only relative POSIX paths; "
          "no dot or parent components, links or absolute paths. Up to 8 files, 64 KiB each. "
          "Scripts are saved as text and never executed. When the user states the content, pass it character for "
          "character: do not add a trailing newline, quotes or any other characters they did not ask for. "
          "Finish with ordinary text after tool results.")
CREATE_FILE_TOOL = {"type": "function", "function": {
    "name": "create_file", "description": "Create or overwrite a UTF-8 text file in the workspace",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"},
        "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}}}


class Agent:
    def __init__(self, settings, llm, sandbox):
        self.settings, self.llm, self.sandbox = settings, llm, sandbox

    async def run(self, request, record, ref):
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": request}]
        paths = []
        calls = 0
        try:
            for _ in range(self.settings.max_llm_calls):
                record.deadline.check()
                if record.cancelled:
                    raise asyncio.CancelledError()
                try:
                    async with asyncio.timeout(record.deadline.limit(self.settings.llm_seconds)):
                        message = await self.llm.next(messages, record.deadline)
                except TimeoutError:
                    raise AgentError("LLM_CALL_TIMEOUT", "Model call deadline exceeded", timeout=True) from None
                record.deadline.check()
                if record.cancelled:
                    raise asyncio.CancelledError()
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    raise AgentError("LLM_PROTOCOL_ERROR", "Expected an assistant message")
                tools = message.get("tool_calls") or []
                if not isinstance(tools, list):
                    raise AgentError("LLM_PROTOCOL_ERROR", "Invalid tool call collection")
                content = message.get("content")
                if content is not None and not isinstance(content, str):
                    raise AgentError("LLM_PROTOCOL_ERROR", "Invalid assistant content")
                if not tools:
                    if content is None:
                        raise AgentError("LLM_PROTOCOL_ERROR", "Expected a normal final response")
                    record.agent_message = content[:4096]
                    if not paths:
                        message = "Agent finished without creating any files"
                        if record.warnings:
                            message += "; tool requests were refused: " + ", ".join(dict.fromkeys(w["code"] for w in record.warnings))
                        raise AgentError("NO_FILES_CREATED", message)
                    return paths
                if calls + len(tools) > self.settings.max_tool_calls:
                    raise AgentError("TOOL_BUDGET_EXCEEDED", "Tool call budget exceeded")
                ids = [t.get("id") if isinstance(t, dict) else None for t in tools]
                if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
                    raise AgentError("LLM_PROTOCOL_ERROR", "Missing or duplicate tool call identity")
                messages.append(message)
                calls += len(tools)
                for tool in tools:
                    record.deadline.check()
                    if record.cancelled:
                        raise asyncio.CancelledError()
                    try:
                        function = tool.get("function", {})
                        if tool.get("type") != "function" or not isinstance(function, dict) or function.get("name") != "create_file":
                            raise PolicyError("UNKNOWN_TOOL", "Only create_file is supported")
                        arguments = function.get("arguments")
                        if not isinstance(arguments, str):
                            raise PolicyError("INVALID_JSON", "Tool arguments must be JSON text")
                        try:
                            payload = json.loads(arguments)
                        except ValueError:
                            raise PolicyError("INVALID_JSON", "Malformed JSON tool arguments") from None
                        path, _ = validate_write(payload)
                        if path not in paths and len(paths) >= self.settings.max_files:
                            raise PolicyError("FILE_COUNT_EXCEEDED", "File count limit exceeded")
                        result = await self.sandbox.helper(ref, "write", payload, record.deadline)
                        if path not in paths:
                            paths.append(path)
                        record.event("FILE_WRITTEN", "File write confirmed by sandbox helper", self.settings.max_events)
                    except PolicyError as exc:
                        result = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
                        if len(record.warnings) < self.settings.max_events:
                            record.warnings.append(result["error"])
                        record.event(exc.code, str(exc), self.settings.max_events)
                    messages.append({"role": "tool", "tool_call_id": tool["id"],
                                     "content": json.dumps(result, ensure_ascii=True)})
            raise AgentError("LLM_BUDGET_EXCEEDED", "Model request budget exhausted")
        finally:
            messages.clear()
