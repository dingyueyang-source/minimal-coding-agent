import asyncio
import sys

import pytest

from app.errors import AgentError
from app.process import ProcessRunner


async def test_process_stdin_and_output():
    runner = ProcessRunner()
    result = await runner.run([sys.executable, "-c", "import sys; print(sys.stdin.read()); print('err',file=sys.stderr)"],
                              stdin=b"hello", timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == b"hello" and result.stderr.strip() == b"err"
    assert not runner.processes


async def test_process_timeout_reaps_child():
    runner = ProcessRunner()
    with pytest.raises(TimeoutError):
        await runner.run([sys.executable, "-c", "import time; time.sleep(20)"], timeout=0.2)
    assert not runner.processes


async def test_process_cancel_reaps_child():
    runner = ProcessRunner()
    job = asyncio.create_task(runner.run([sys.executable, "-c", "import time; time.sleep(20)"], timeout=30))
    async with asyncio.timeout(5):
        while not runner.processes:
            if job.done():
                await job
            await asyncio.sleep(0.01)
    processes = list(runner.processes)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert all(proc.returncode is not None for proc in processes)
    assert not runner.processes


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_process_caps_total_output(stream):
    runner = ProcessRunner()
    with pytest.raises(AgentError, match="output exceeds"):
        await runner.run([sys.executable, "-c", f"import sys; sys.{stream}.buffer.write(b'x'*2000000)"], timeout=5)
    assert not runner.processes
