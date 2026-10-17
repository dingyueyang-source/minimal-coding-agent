import asyncio
import base64
import json
import uuid
from dataclasses import replace

import pytest
import pytest_asyncio

from app.config import Settings
from app.deadline import Deadline
from app.errors import AdmissionError, AgentError
from app.sandbox import SandboxManager
from app.task_manager import TaskManager
from tests.fakes import FakeLLM, call, done, tools

pytestmark = pytest.mark.docker


@pytest_asyncio.fixture
async def sandbox():
    settings = Settings(project_id="mca-test-" + uuid.uuid4().hex[:10], task_seconds=20)
    manager = SandboxManager(settings)
    await manager.initialize()
    try:
        yield manager
    finally:
        # Test-owned containers only, including failed assertions and created-only containers.
        await manager.recover(set())
        assert not await manager.list_project(Deadline.after(10))
        assert not manager.runner.processes


async def execute(sandbox, model, *, settings=None):
    settings = settings or sandbox.settings
    manager = TaskManager(settings, sandbox, model)
    manager.ready = True  # Fixture initialized real Docker; startup smoke has its own test.
    try:
        accepted = await manager.submit("Create text files")
        result = await manager.wait(accepted["task_id"])
        assert not manager.background_errors
        assert result["phase"] == "complete"
        assert result["cleanup"]["status"] == "done"
        assert not await sandbox.list_project(Deadline.after(10))
        return result
    finally:
        await manager.close()


async def raw_helper(sandbox, ref, operation, payload):
    result = await sandbox.runner.run(["docker", "exec", "-i", ref.name, "python3", "-I", "-B", "/opt/app/tool.py", operation],
        stdin=json.dumps(payload).encode(), timeout=10)
    return result.returncode, json.loads(result.stdout)


async def fixture_code(sandbox, ref, code):
    # Test-only filesystem fixture. No such interface exists in production Agent tools.
    return await sandbox.command(["exec", ref.name, "python3", "-I", "-B", "-c", code], Deadline.after(10))


async def test_startup_smoke(sandbox):
    manager = TaskManager(sandbox.settings, sandbox, FakeLLM())
    await manager.start()
    try:
        assert manager.health()["ready"]
        assert not await sandbox.list_project(Deadline.after(10))
    finally:
        await manager.close()


async def test_success_multiple_overwrite_empty_and_nested(sandbox):
    model = FakeLLM(tools(call("hello.txt", "Hello, world!"), call("深层/空 格.txt", "初版", "2"), call("empty", "", "3")),
                    tools(call("深层/空 格.txt", "最后😀")), done())
    result = await execute(sandbox, model)
    assert result["status"] == result["execution_status"] == "succeeded"
    assert len(result["container_id"]) == 12
    assert [(f["path"], f["content"]) for f in result["files"]] == [
        ("hello.txt", "Hello, world!"), ("深层/空 格.txt", "最后😀"), ("empty", "")]
    assert all(f["read_back"] and not f["truncated"] and not f["decode_replaced"] for f in result["files"])
    assert [f["size_bytes"] for f in result["files"]] == [13, 10, 0]


async def test_readback_is_actual_container_content(sandbox):
    async def change_then_finish(messages, deadline):
        ref = next(iter(sandbox.refs.values()))
        await fixture_code(sandbox, ref, "from pathlib import Path; Path('/workspace/proof').write_text('changed inside container')")
        return done()
    result = await execute(sandbox, FakeLLM(tools(call("proof", "original tool argument")), change_then_finish))
    assert result["files"][0]["content"] == "changed inside container"


async def test_path_rejection_uncorrected(sandbox):
    async def verify(messages, deadline):
        ref = next(iter(sandbox.refs.values()))
        await fixture_code(sandbox, ref, "from pathlib import Path; assert not Path('/secret.txt').exists(); assert Path('/workspace/outside.txt').read_text()=='sentinel'")
        return done("Could not create requested file")
    async def seed(messages, deadline):
        ref = next(iter(sandbox.refs.values()))
        await sandbox.helper(ref, "write", {"path": "outside.txt", "content": "sentinel"}, deadline)
        return tools(call("../secret.txt", "bad"), call("../workspace/outside.txt", "bad", "2"))
    model = FakeLLM(seed, verify)
    result = await execute(sandbox, model)
    assert result["status"] == "failed" and result["error"]["code"] == "NO_FILES_CREATED"
    assert result["failed_phase"] == "agent_running" and not result["files"]
    assert all(r["error"]["code"] == "PATH_REJECTED" for r in model.tool_results)
    assert len(result["warnings"]) == 2


async def test_path_rejection_then_correction(sandbox):
    model = FakeLLM(tools(call("a/../bad", "bad")), tools(call("safe/file", "recovered")), done())
    result = await execute(sandbox, model)
    assert result["status"] == "succeeded"
    assert result["warnings"][0]["code"] == "PATH_REJECTED"
    assert any(e["code"] == "PATH_REJECTED" for e in result["events"])


async def test_slow_llm_timeout_no_late_write(sandbox):
    async def slow(messages, deadline):
        await asyncio.sleep(30)
        return tools(call("late", "must never write"))
    model = FakeLLM(slow)
    result = await execute(sandbox, model, settings=replace(sandbox.settings, task_seconds=3))
    assert result["status"] == "timeout" and result["execution_status"] == "timeout"
    assert model.cancelled and not result["files"]
    assert result["error"]["code"] in ("TASK_TIMEOUT", "LLM_CALL_TIMEOUT")


async def test_written_but_not_read_before_timeout(sandbox):
    async def slow_finish(messages, deadline):
        await asyncio.sleep(30)
        return done()
    result = await execute(sandbox, FakeLLM(tools(call("written", "not a result")), slow_finish),
                           settings=replace(sandbox.settings, task_seconds=3))
    assert result["status"] == "timeout" and not result["files"]
    assert any(e["code"] == "FILE_WRITTEN" for e in result["events"])


async def test_readback_timeout_is_not_success(sandbox):
    original = sandbox.helper
    async def slow_read(ref, operation, payload, deadline):
        if operation == "read":
            await asyncio.sleep(30)
        return await original(ref, operation, payload, deadline)
    sandbox.helper = slow_read
    result = await execute(sandbox, FakeLLM(tools(call("a", "b")), done()),
                           settings=replace(sandbox.settings, task_seconds=3))
    assert result["status"] == "timeout" and result["failed_phase"] == "retrieving"
    assert result["error"]["code"] == "RESULT_READ_TIMEOUT" and not result["files"]


@pytest.mark.parametrize("kind", ["link", "middle-link", "directory", "fifo", "hardlink"])
async def test_helper_rejects_unsafe_filesystem(sandbox, kind):
    ref = sandbox.reserve(str(uuid.uuid4()))
    try:
        await sandbox.start(ref, Deadline.after(15))
        setups = {
            "link": ("os.symlink('/workspace/sentinel','/workspace/target')", "target"),
            "middle-link": ("os.mkdir('/workspace/real'); os.symlink('/workspace/real','/workspace/link')", "link/file"),
            "directory": ("os.mkdir('/workspace/target')", "target"),
            "fifo": ("os.mkfifo('/workspace/target')", "target"),
            "hardlink": ("os.link('/workspace/sentinel','/workspace/target')", "target")}
        setup, path = setups[kind]
        await fixture_code(sandbox, ref, "import os; from pathlib import Path; Path('/workspace/sentinel').write_text('safe'); " + setup)
        for operation, payload in (("write", {"path": path, "content": "bad"}), ("read", {"path": path})):
            code, reply = await raw_helper(sandbox, ref, operation, payload)
            assert code != 0 and not reply["ok"]
        await fixture_code(sandbox, ref, "from pathlib import Path; assert Path('/workspace/sentinel').read_text()=='safe'")
    finally:
        await sandbox.cleanup(ref, Deadline.after(15))


async def test_direct_helper_path_policy_and_no_partial_overwrite(sandbox):
    ref = sandbox.reserve(str(uuid.uuid4()))
    try:
        await sandbox.start(ref, Deadline.after(15))
        await sandbox.helper(ref, "write", {"path": "outside.txt", "content": "sentinel"}, Deadline.after(10))
        for path in ("../secret.txt", "../workspace/outside.txt", "C:/x", "a/../b", "/workspace/outside.txt"):
            code, reply = await raw_helper(sandbox, ref, "write", {"path": path, "content": "bad"})
            assert code != 0 and reply["error"]["code"] == "PATH_REJECTED"
        await fixture_code(sandbox, ref, "from pathlib import Path; assert not Path('/secret.txt').exists(); assert Path('/workspace/outside.txt').read_text()=='sentinel'")
        code, reply = await raw_helper(sandbox, ref, "write", {"path": "outside.txt", "content": "x" * 65537})
        assert code != 0 and reply["error"]["code"] == "FILE_TOO_LARGE"
        read = await sandbox.helper(ref, "read", {"path": "outside.txt"}, Deadline.after(10))
        assert base64.b64decode(read["data_base64"]) == b"sentinel"
    finally:
        await sandbox.cleanup(ref, Deadline.after(15))


@pytest.mark.parametrize("model,expected,refusal", [
    (FakeLLM(done()), "NO_FILES_CREATED", None),
    (FakeLLM(tools({"id": "x", "type": "function", "function": {"name": "bad", "arguments": "{}"}}), done()), "NO_FILES_CREATED", "UNKNOWN_TOOL"),
    (FakeLLM(tools({"id": "x", "type": "function", "function": {"name": "create_file", "arguments": "{"}}), done()), "NO_FILES_CREATED", "INVALID_JSON"),
    (FakeLLM(tools(call("a", 2)), done()), "NO_FILES_CREATED", "INVALID_ARGUMENT"),
    (FakeLLM(tools({"id": "x", "type": "function", "function": {"name": "create_file",
     "arguments": json.dumps({"path": "a", "content": "b", "mode": "755"})}}), done()), "NO_FILES_CREATED", "INVALID_ARGUMENT"),
    (FakeLLM(tools(call("a", "b"))), "LLM_BUDGET_EXCEEDED", None)])
async def test_model_failure_branches(sandbox, model, expected, refusal):
    settings = replace(sandbox.settings, max_llm_calls=1) if expected == "LLM_BUDGET_EXCEEDED" else sandbox.settings
    result = await execute(sandbox, model, settings=settings)
    assert result["status"] == result["execution_status"] == "failed" and result["error"]["code"] == expected
    assert result["failed_phase"] == "agent_running" and not result["files"]
    if refusal:
        # The model received a structured tool error, and the UI-visible summary names it.
        assert model.tool_results[0] == {"ok": False, "error": {"code": refusal, "message": model.tool_results[0]["error"]["message"]}}
        assert [w["code"] for w in result["warnings"]] == [refusal] and refusal in result["error"]["message"]
        assert not any(e["code"] == "FILE_WRITTEN" for e in result["events"])


async def test_batch_budget_rejects_entire_batch(sandbox):
    result = await execute(sandbox, FakeLLM(tools(call("a", "b"), call("c", "d", "2"))),
                           settings=replace(sandbox.settings, max_tool_calls=1))
    assert result["error"]["code"] == "TOOL_BUDGET_EXCEEDED"
    assert not any(e["code"] == "FILE_WRITTEN" for e in result["events"])


async def test_container_security_and_no_credentials(sandbox, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "never-copy-this-secret")
    ref = sandbox.reserve(str(uuid.uuid4()))
    try:
        await sandbox.start(ref, Deadline.after(15))
        info = json.loads(await sandbox.command(["inspect", ref.name], Deadline.after(10)))[0]
        host, config = info["HostConfig"], info["Config"]
        assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
        assert host["Memory"] == host["MemorySwap"] == 134217728
        assert host["PidsLimit"] == 64 and host["NanoCpus"] == 500000000
        assert host["CapDrop"] == ["ALL"] and "no-new-privileges" in host["SecurityOpt"]
        assert host["AutoRemove"] and config["User"] == "1000:1000" and config["WorkingDir"] == "/workspace"
        assert not host["Binds"] and not host["PortBindings"] and not host["Privileged"] and not host["Devices"]
        assert all(m["Type"] != "bind" for m in info["Mounts"])
        for flag in ("size=10m", "uid=1000", "gid=1000", "mode=0700", "nosuid", "nodev", "noexec"):
            assert flag in host["Tmpfs"]["/workspace"]
        assert "LLM_API_KEY" not in str(config["Env"]) and "never-copy-this-secret" not in str(config["Env"])
        await fixture_code(sandbox, ref, "import os; assert not os.access('/opt/app/tool.py',os.W_OK); assert not os.access('/opt/app/shared/path_policy.py',os.W_OK)")
    finally:
        await sandbox.cleanup(ref, Deadline.after(15))


async def test_concurrent_isolation_and_fourth_rejected(sandbox):
    gate = asyncio.Event()
    async def first(messages, deadline):
        await gate.wait()
        text = messages[1]["content"]
        return tools(call("same.txt", text))
    class Model:
        async def next(self, messages, deadline):
            return done() if any(m["role"] == "tool" for m in messages) else await first(messages, deadline)
    manager = TaskManager(sandbox.settings, sandbox, Model())
    manager.ready = True
    try:
        accepted = await asyncio.gather(*(manager.submit(text) for text in ("one", "two", "three")))
        with pytest.raises(AdmissionError) as error:
            await manager.submit("four")
        assert error.value.status_code == 429 and len(sandbox.refs) == 3
        gate.set()
        results = await asyncio.gather(*(manager.wait(a["task_id"]) for a in accepted))
        assert all(r["status"] == "succeeded" for r in results)
        assert [r["files"][0]["content"] for r in results] == ["one", "two", "three"]
        assert len({r["container_id"] for r in results}) == 3
    finally:
        await manager.close()


async def test_created_only_and_start_failure_cleanup(sandbox):
    original = sandbox.command
    async def fail_start(args, deadline, **kwargs):
        if args[0] == "start":
            # The controlled failure is known not to have been sent to Docker.
            ref = next(iter(sandbox.refs.values()))
            ref.mutation_outcome_unknown = False
            raise AgentError("SANDBOX_START_FAILED", "Injected start failure")
        return await original(args, deadline, **kwargs)
    sandbox.command = fail_start
    result = await execute(sandbox, FakeLLM())
    assert result["error"]["code"] == "SANDBOX_START_FAILED" and result["failed_phase"] == "sandbox_starting"


async def test_cleanup_failure_degraded_then_recovery(sandbox):
    original = sandbox.cleanup
    async def unavailable(ref, deadline):
        raise AgentError("CLEANUP_FAILED", "Injected daemon failure")
    sandbox.cleanup = unavailable
    manager = TaskManager(sandbox.settings, sandbox, FakeLLM(tools(call("a", "b")), done()))
    manager.ready = True
    try:
        accepted = await manager.submit("create")
        result = await manager.wait(accepted["task_id"])
        assert result["execution_status"] == "succeeded" and result["status"] == "failed"
        assert result["error"]["code"] == "CLEANUP_FAILED" and result["cleanup"]["status"] == "failed"
        assert result["files"][0]["content"] == "b" and manager.occupied
        assert not manager.health()["ready"]
        with pytest.raises(AdmissionError) as error:
            await manager.submit("blocked")
        assert error.value.status_code == 503
        sandbox.cleanup = original
        await manager.recover_once()
        recovered = manager.get(accepted["task_id"])
        assert recovered["status"] == "failed" and recovered["cleanup"]["status"] == "done"
        assert recovered["revision"] > result["revision"]
        assert not manager.occupied and manager.health()["ready"]
    finally:
        sandbox.cleanup = original
        await manager.close()


async def test_unknown_create_outcome_retained_until_observed(sandbox):
    ref = sandbox.reserve(str(uuid.uuid4()))
    ref.mutation_outcome_unknown = True
    sandbox.pending.add(ref.name)
    with pytest.raises(AgentError, match="unconfirmed"):
        await sandbox.cleanup(ref, Deadline.after(10))
    assert ref.name in sandbox.pending
    # Simulate a daemon create completing after the cancelled client exited.
    await sandbox.command(sandbox.create_args(ref), Deadline.after(15))
    removed = await sandbox.recover(set())
    assert ref.name in removed and ref.name not in sandbox.pending


async def test_slow_container_exec_killed_by_cleanup(sandbox):
    original = sandbox.helper
    async def slow_helper(ref, operation, payload, deadline):
        if operation == "write":
            await sandbox.runner.run(["docker", "exec", ref.name, "python3", "-I", "-B", "-c",
                "import time; time.sleep(30)"], timeout=deadline.limit(10))
        return await original(ref, operation, payload, deadline)
    sandbox.helper = slow_helper
    result = await execute(sandbox, FakeLLM(tools(call("slow", "data"))),
                           settings=replace(sandbox.settings, task_seconds=3))
    assert result["status"] == "timeout" and not result["files"]
    assert not sandbox.runner.processes

@pytest.mark.parametrize("started", [False, True])
async def test_shutdown_cancels_execution_and_finishes_cleanup(sandbox, started):
    entered = asyncio.Event()
    async def slow(messages, deadline):
        entered.set()
        await asyncio.sleep(30)
        return tools(call("late", "never"))
    manager = TaskManager(sandbox.settings, sandbox, FakeLLM(slow))
    manager.ready = True
    accepted = await manager.submit("cancel on shutdown")
    if started:
        async with asyncio.timeout(15):
            await entered.wait()
    await manager.close()
    result = manager.get(accepted["task_id"])
    assert result["status"] == "failed" and result["cleanup"]["status"] == "done"
    assert result["error"]["code"] == "TASK_CANCELLED"
    assert not manager.occupied and not sandbox.runner.processes
    assert not await sandbox.list_project(Deadline.after(10))


@pytest.mark.parametrize("data,replaced", [(b"\xff", True), ("\ufffd".encode(), False)])
async def test_readback_decode_replacement_is_accurate(sandbox, data, replaced):
    async def mutate(messages, deadline):
        ref = next(iter(sandbox.refs.values()))
        await fixture_code(sandbox, ref, "from pathlib import Path; Path('/workspace/a').write_bytes(" + repr(data) + ")")
        return done()
    result = await execute(sandbox, FakeLLM(tools(call("a", "original")), mutate))
    assert result["status"] == "succeeded"
    assert result["files"][0]["decode_replaced"] is replaced
    assert result["files"][0]["content"] == "\ufffd"


async def test_oversized_readback_is_partial_failure(sandbox):
    async def mutate(messages, deadline):
        ref = next(iter(sandbox.refs.values()))
        await fixture_code(sandbox, ref, "from pathlib import Path; Path('/workspace/a').write_bytes(b'x'*65537)")
        return done()
    result = await execute(sandbox, FakeLLM(tools(call("a", "original")), mutate))
    assert result["status"] == "failed" and result["error"]["code"] == "RESULT_TOO_LARGE"
    file = result["files"][0]
    assert file["size_bytes"] == 65537 and file["bytes_returned"] == 65536 and file["truncated"]
