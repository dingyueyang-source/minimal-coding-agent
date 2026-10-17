"""Linux Docker CLI adapter. All commands and security settings are server owned."""
import asyncio
import base64
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.deadline import Deadline
from app.errors import AgentError
from app.process import ProcessRunner
from shared.path_policy import FILE_LIMIT, INPUT_LIMIT, validate_path, validate_write


@dataclass
class SandboxRef:
    name: str
    task_id: str
    container_id: str | None = None
    mutation_outcome_unknown: bool = False


class SandboxManager:
    def __init__(self, settings, runner=None):
        self.settings = settings
        self.runner = runner or ProcessRunner()
        self.instance_id = str(uuid.uuid4())
        self.image_id = None
        self.refs = {}  # Retained independently of task TTL/capacity.
        self.pending = set()

    async def command(self, args, deadline, *, stdin=b"", code="DOCKER_FAILED"):
        result = await self.runner.run(["docker", *args], stdin=stdin,
                                       timeout=deadline.limit(self.settings.docker_seconds))
        deadline.check()
        if result.returncode:
            # Never expose raw daemon output, tool content or environment data.
            raise AgentError(code, "Docker operation failed; check local daemon and sandbox state")
        return result.stdout

    def reserve(self, task_id):
        ref = SandboxRef(f"mca-{self.settings.project_id}-{task_id}", task_id)
        self.refs[ref.name] = ref
        return ref

    async def initialize(self):
        deadline = Deadline.after(30)
        info = json.loads(await self.command(["info", "--format", "{{json .}}"], deadline))
        if info.get("OSType") != "linux":
            raise AgentError("UNSUPPORTED_ENGINE", "A Linux Docker engine is required")
        raw = await self.command(["image", "inspect", self.settings.image, "--format", "{{.Id}}"], deadline,
                                 code="IMAGE_NOT_FOUND")
        image_id = raw.decode().strip()
        if not image_id.startswith("sha256:") or len(image_id) != 71:
            raise AgentError("IMAGE_NOT_FOUND", "Invalid local image ID")
        self.image_id = image_id

    def create_args(self, ref):
        ttl = self.settings.task_seconds + self.settings.cleanup_seconds + 10
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        return ["create", "--pull", "never", "--name", ref.name,
                "--label", f"mca-project={self.settings.project_id}",
                "--label", f"mca-instance={self.instance_id}", "--label", f"agent-task={ref.task_id}",
                "--label", f"mca-expires={expires.isoformat()}",
                "--network", "none", "--read-only", "--tmpfs",
                "/workspace:rw,size=10m,uid=1000,gid=1000,mode=0700,nosuid,nodev,noexec",
                "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "64", "--memory", "128m", "--memory-swap", "128m",
                "--cpus", "0.5", "--restart", "no", "--rm", "--workdir", "/workspace",
                self.image_id, "sleep", str(math.ceil(ttl))]

    async def start(self, ref, deadline):
        if not self.image_id:
            raise AgentError("NOT_READY", "Initialize the sandbox manager first")
        try:
            deadline.check()
            ref.mutation_outcome_unknown = True
            output = await self.command(self.create_args(ref), deadline, code="SANDBOX_START_FAILED")
            ref.container_id = output.decode().strip()
            if len(ref.container_id) != 64 or any(c not in "0123456789abcdef" for c in ref.container_id):
                raise AgentError("SANDBOX_START_FAILED", "Invalid container identity")
            ref.mutation_outcome_unknown = False
            deadline.check()
            ref.mutation_outcome_unknown = True
            await self.command(["start", ref.name], deadline, code="SANDBOX_START_FAILED")
            ref.mutation_outcome_unknown = False
            deadline.check()
        except TimeoutError:
            raise AgentError("SANDBOX_START_TIMEOUT", "Sandbox startup deadline exceeded", timeout=True) from None

    async def helper(self, ref, operation, payload, deadline):
        if operation not in ("write", "read"):
            raise ValueError("Fixed helper operation required")
        if operation == "write":
            validate_write(payload)
        else:
            validate_path(payload["path"])
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        if len(data) > INPUT_LIMIT:
            raise AgentError("HELPER_INPUT_TOO_LARGE", "Helper input exceeds limit")
        code = "TOOL_EXECUTION_FAILED" if operation == "write" else "RESULT_READ_FAILED"
        try:
            result = await self.runner.run(["docker", "exec", "-i", ref.name, "python3", "-I", "-B",
                                            "/opt/app/tool.py", operation], stdin=data,
                                           timeout=deadline.limit(self.settings.docker_seconds))
        except TimeoutError:
            raise AgentError("TOOL_TIMEOUT" if operation == "write" else "RESULT_READ_TIMEOUT",
                             "Sandbox operation deadline exceeded", timeout=True) from None
        deadline.check("TOOL_TIMEOUT" if operation == "write" else "RESULT_READ_TIMEOUT")
        try:
            reply = json.loads(result.stdout)
            if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
                raise ValueError()
            if not reply["ok"]:
                # Shared backend policy already checked input; FS/protocol failure is infrastructure.
                raise AgentError(code, "Container helper refused the filesystem operation")
            if result.returncode != 0 or reply.get("path") != payload["path"]:
                raise ValueError()
            if operation == "write":
                if type(reply.get("bytes_written")) is not int or reply["bytes_written"] != len(payload["content"].encode("utf-8")):
                    raise ValueError()
            else:
                if (type(reply.get("size_bytes")) is not int or reply["size_bytes"] < 0
                        or type(reply.get("bytes_returned")) is not int or type(reply.get("truncated")) is not bool):
                    raise ValueError()
                actual = base64.b64decode(reply["data_base64"], validate=True)
                if len(actual) > FILE_LIMIT or len(actual) != reply["bytes_returned"]:
                    raise ValueError()
                if reply["truncated"] != (reply["size_bytes"] > FILE_LIMIT):
                    raise ValueError()
                if len(actual) != min(FILE_LIMIT, reply["size_bytes"]):
                    raise ValueError()
            return reply
        except (ValueError, TypeError, KeyError):
            raise AgentError(code, "Invalid container helper protocol") from None

    async def list_project(self, deadline):
        output = await self.command(["ps", "-a", "--filter", f"label=mca-project={self.settings.project_id}",
                                     "--format", "{{json .}}"], deadline)
        try:
            return [json.loads(line) for line in output.decode().splitlines() if line.strip()]
        except ValueError:
            raise AgentError("DOCKER_FAILED", "Invalid daemon listing") from None

    async def cleanup(self, ref, deadline):
        # No exec lock: rm can terminate a helper still running inside the container.
        result = await self.runner.run(["docker", "rm", "--force", ref.name],
                                       timeout=deadline.limit(self.settings.docker_seconds))
        removed_identity = result.stdout.decode("utf-8", errors="replace").strip()
        if result.returncode == 0 and removed_identity in (ref.name, ref.container_id):
            ref.mutation_outcome_unknown = False
        items = await self.list_project(deadline)  # A daemon error never counts as NotFound.
        if any(item.get("Names") == ref.name for item in items):
            raise AgentError("CLEANUP_FAILED", "Sandbox still exists")
        if ref.mutation_outcome_unknown:
            raise AgentError("CLEANUP_FAILED", "Docker mutation outcome is unconfirmed; recovery retained")
        self.pending.discard(ref.name)
        self.refs.pop(ref.name, None)

    async def recover(self, active_names):
        deadline = Deadline.after(self.settings.cleanup_seconds)
        items = await self.list_project(deadline)
        removed = set()
        for item in items:
            name = item["Names"]
            if name in active_names:
                continue
            ref = self.refs.get(name) or SandboxRef(name, "recovery")
            self.refs[name] = ref
            self.pending.add(name)
            await self.cleanup(ref, deadline)
            removed.add(name)
        for name in list(self.pending):
            if name in active_names or name in removed:
                continue
            ref = self.refs[name]
            await self.cleanup(ref, deadline)
            removed.add(name)
        return removed

    async def smoke(self):
        ref = self.reserve(str(uuid.uuid4()))
        try:
            deadline = Deadline.after(30)
            await self.start(ref, deadline)
            uid = await self.command(["exec", ref.name, "id", "-u"], deadline)
            if uid.strip() != b"1000":
                raise AgentError("SMOKE_FAILED", "Unexpected sandbox uid")
            inspected = json.loads(await self.command(["inspect", ref.name], deadline))[0]
            host = inspected["HostConfig"]
            if host["Memory"] != 128 * 1024 * 1024 or host["MemorySwap"] != host["Memory"]:
                raise AgentError("SMOKE_FAILED", "Memory/swap limits unavailable")
            cgroup = await self.command(["exec", ref.name, "python3", "-I", "-B", "-c",
                "from pathlib import Path; p=Path('/sys/fs/cgroup'); "
                "v2=(p/'memory.max').exists(); "
                "m=int((p/'memory.max' if v2 else p/'memory/memory.limit_in_bytes').read_text()); "
                "s=int((p/'memory.swap.max' if v2 else p/'memory/memory.memsw.limit_in_bytes').read_text()); "
                "print(m, s if v2 else s-m)"], deadline)
            if cgroup.split() != [b"134217728", b"0"]:
                raise AgentError("SMOKE_FAILED", "Effective cgroup memory/swap limits unavailable")
            await self.helper(ref, "write", {"path": "smoke/nested.txt", "content": "smoke"}, deadline)
            value = await self.helper(ref, "read", {"path": "smoke/nested.txt"}, deadline)
            if base64.b64decode(value["data_base64"]) != b"smoke":
                raise AgentError("SMOKE_FAILED", "Helper smoke readback mismatch")
        finally:
            await self.cleanup(ref, Deadline.after(self.settings.cleanup_seconds))
