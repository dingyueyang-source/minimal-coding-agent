"""Bounded, cancellation-aware subprocess execution; never invokes a shell."""
import asyncio
from dataclasses import dataclass

from app.errors import AgentError


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProcessRunner:
    def __init__(self):
        self.processes = set()

    async def run(self, argv, *, stdin=b"", timeout=10):
        proc = None
        io_tasks = []
        # Shield creation, retain the task and await its actual result even when cancelled.
        creation = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE))
        try:
            async with asyncio.timeout(timeout):
                proc = await asyncio.shield(creation)
                self.processes.add(proc)

                async def consume(stream, maximum):
                    data = bytearray()
                    while block := await stream.read(16384):
                        if len(data) + len(block) > maximum:
                            raise AgentError("CLI_OUTPUT_TOO_LARGE", "CLI output exceeds limit")
                        data.extend(block)
                    return bytes(data)

                async def feed():
                    try:
                        proc.stdin.write(stdin)
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        proc.stdin.close()

                io_tasks = [asyncio.create_task(consume(proc.stdout, 1024 * 1024)),
                            asyncio.create_task(consume(proc.stderr, 64 * 1024)),
                            asyncio.create_task(feed()), asyncio.create_task(proc.wait())]
                out, err, _, code = await asyncio.gather(*io_tasks)
                return ProcessResult(code, out, err)
        finally:
            if proc is None:
                # Creation itself can be cancelled after the OS spawned a child.
                try:
                    proc = await asyncio.shield(creation)
                except Exception:
                    pass
            if proc is not None:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                # Close stdin and drain both pipes concurrently before waiting.
                if proc.stdin is not None:
                    proc.stdin.close()
                for task in io_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*io_tasks, return_exceptions=True)
                await asyncio.gather(proc.stdout.read(), proc.stderr.read(), proc.wait(), return_exceptions=True)
                self.processes.discard(proc)
