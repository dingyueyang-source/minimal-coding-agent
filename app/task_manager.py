"""Atomic admission, execution deadlines, independent cleanup and bounded retention."""
import asyncio
import base64
import uuid

from app.agent import Agent
from app.deadline import Deadline
from app.errors import AdmissionError, AgentError
from app.schemas import TaskRecord, utcnow
from shared.path_policy import validate_request


class TaskManager:
    def __init__(self, settings, sandbox, llm):
        self.settings, self.sandbox = settings, sandbox
        self.agent = Agent(settings, llm, sandbox)
        self.records = {}
        self.jobs = {}
        self.cleanup_jobs = {}
        self.occupied = set()
        self.lock = asyncio.Lock()
        self.ready = False
        self.closing = False
        self.reaper = None
        self.background_errors = []

    async def start(self):
        await self.sandbox.initialize()
        await self.sandbox.recover(set())
        await self.sandbox.smoke()
        self.ready = True
        self.reaper = asyncio.create_task(self._reap(), name="mca-recovery")

    def _prune(self, reserve=False):
        now = asyncio.get_running_loop().time()
        for task_id, record in list(self.records.items()):
            if record.terminal_at is not None and now - record.terminal_at >= self.settings.ttl_seconds:
                del self.records[task_id]
        while len(self.records) > self.settings.max_records - int(reserve):
            candidates = [(r.terminal_at, key) for key, r in self.records.items() if r.terminal_at is not None]
            if not candidates:
                break
            del self.records[min(candidates)[1]]

    async def submit(self, request):
        validate_request(request)  # Invalid requests reserve nothing.
        async with self.lock:
            self._prune(reserve=True)
            if not self.ready or self.closing or self.sandbox.pending:
                raise AdmissionError(503, "NOT_READY", "Sandbox/cleanup system is unavailable")
            if len(self.occupied) >= self.settings.concurrency:
                raise AdmissionError(429, "CAPACITY_EXCEEDED", "All execution/cleanup slots are occupied")
            task_id = str(uuid.uuid4())
            ref = self.sandbox.reserve(task_id)  # Name registered before create.
            record = TaskRecord(task_id, ref.name, Deadline.after(self.settings.task_seconds))
            self.records[task_id] = record
            self.occupied.add(task_id)
            record.event("TASK_ACCEPTED", "Task accepted", self.settings.max_events)
            job = asyncio.create_task(self._execute(request, record, ref), name=f"mca-task-{task_id}")
            self.jobs[task_id] = job
            job.add_done_callback(lambda done, key=task_id: self._collect(key, done))
            return {"task_id": task_id, "status_url": f"/tasks/{task_id}"}

    def _collect(self, task_id, job):
        self.jobs.pop(task_id, None)
        if not job.cancelled():
            error = job.exception()
            if error is not None:
                self.background_errors.append(type(error).__name__)
                self.background_errors[:] = self.background_errors[-16:]
                self.ready = False

    def get(self, task_id):
        # Read-only: no task execution, retry or cleanup is triggered by querying.
        record = self.records.get(task_id)
        if record is None:
            return None
        if record.terminal_at is not None and asyncio.get_running_loop().time() - record.terminal_at >= self.settings.ttl_seconds:
            return None
        return record.snapshot()

    def health(self):
        return {"ready": self.ready and not self.closing and not self.sandbox.pending,
                "status": "ready" if self.ready and not self.closing and not self.sandbox.pending else "degraded",
                "occupied": len(self.occupied)}

    def _phase(self, record, phase):
        record.phase = phase
        record.event("PHASE_CHANGED", phase, self.settings.max_events)

    async def _execute(self, request, record, ref):
        try:
            async with asyncio.timeout_at(record.deadline.at):
                if self.closing or record.cancelled:
                    raise asyncio.CancelledError()
                await self.sandbox.start(ref, record.deadline)
                record.container_id = ref.container_id[:12]
                self._phase(record, "agent_running")
                paths = await self.agent.run(request, record, ref)
                self._phase(record, "retrieving")
                for path in paths:
                    record.deadline.check("RESULT_READ_TIMEOUT")
                    result = await self.sandbox.helper(ref, "read", {"path": path}, record.deadline)
                    actual = base64.b64decode(result["data_base64"], validate=True)
                    try:
                        content = actual.decode("utf-8", errors="strict")
                        replaced = False
                    except UnicodeDecodeError:
                        content = actual.decode("utf-8", errors="replace")
                        replaced = True
                    record.files.append({"path": path, "content": content, "size_bytes": result["size_bytes"],
                                         "bytes_returned": len(actual), "truncated": result["truncated"],
                                         "decode_replaced": replaced, "read_back": True})
                    record.revision += 1
                    if result["truncated"]:
                        raise AgentError("RESULT_TOO_LARGE", "Readback exceeds limit; partial preview retained")
                record.deadline.check("RESULT_READ_TIMEOUT")
                record.execution_status = "succeeded"
        except asyncio.CancelledError:
            self._fail(record, AgentError("TASK_CANCELLED", "Backend stopped task execution"))
        except TimeoutError:
            code = {"sandbox_starting": "SANDBOX_START_TIMEOUT", "retrieving": "RESULT_READ_TIMEOUT"}.get(record.phase, "TASK_TIMEOUT")
            self._fail(record, AgentError(code, "Task execution deadline exceeded", timeout=True))
        except AgentError as exc:
            self._fail(record, exc)
        except Exception:
            self._fail(record, AgentError("EXECUTION_FAILED", "Unexpected execution failure; details withheld"))
        finally:
            if ref.container_id and not record.container_id:
                record.container_id = ref.container_id[:12]
            request = None
            record.execution_finished_at = utcnow()
            self._phase(record, "cleaning")
            record.cleanup = {"status": "running", "error": None}
            cleanup = asyncio.create_task(self._cleanup(record, ref), name=f"mca-cleanup-{record.task_id}")
            self.cleanup_jobs[record.task_id] = cleanup
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Preserve and finish the independently budgeted cleanup job.
                await asyncio.shield(cleanup)
            finally:
                self.cleanup_jobs.pop(record.task_id, None)
            if record.execution_status == "succeeded" and record.cleanup["status"] == "done":
                record.status = "succeeded"
            elif record.execution_status == "timeout":
                record.status = "timeout"
            else:
                record.status = "failed"
                if record.error is None:
                    record.error = {"code": "CLEANUP_FAILED", "message": "Execution completed but cleanup was not confirmed", "phase": "cleaning"}
            record.finished_at = utcnow()
            record.terminal_at = asyncio.get_running_loop().time()
            self._phase(record, "complete")

    def _fail(self, record, error):
        record.cancelled = True
        record.execution_status = "timeout" if error.timeout else "failed"
        record.failed_phase = record.phase
        record.error = {"code": error.code, "message": str(error), "phase": record.phase}
        record.event(error.code, str(error), self.settings.max_events)

    async def _cleanup(self, record, ref):
        try:
            async with asyncio.timeout(self.settings.cleanup_seconds):
                await self.sandbox.cleanup(ref, Deadline.after(self.settings.cleanup_seconds))
            record.cleanup = {"status": "done", "error": None}
            self.occupied.discard(record.task_id)
        except (Exception, asyncio.CancelledError):
            self.sandbox.pending.add(ref.name)
            record.cleanup = {"status": "failed", "error": {"code": "CLEANUP_FAILED", "message": "Sandbox removal could not be confirmed; recovery retained"}}
            self.ready = False
        record.event("CLEANUP_" + record.cleanup["status"].upper(), "Cleanup " + record.cleanup["status"], self.settings.max_events)

    async def recover_once(self):
        active = {r.container_name for r in self.records.values() if r.status == "running"}
        removed = await self.sandbox.recover(active)
        for record in self.records.values():
            if record.container_name in removed and record.cleanup["status"] == "failed":
                record.cleanup = {"status": "done", "error": None}
                record.event("CLEANUP_RECOVERED", "Background recovery confirmed sandbox removal", self.settings.max_events)
        # Slot recovery also works when the task's visible record has expired/been evicted.
        remaining_task_ids = {ref.task_id for name, ref in self.sandbox.refs.items() if name in self.sandbox.pending}
        self.occupied.intersection_update(set(self.jobs) | remaining_task_ids)
        if not self.sandbox.pending and not self.closing:
            await self.sandbox.initialize()  # Verify daemon/image before reopening admission.
            self.ready = True

    async def _reap(self):
        while True:
            await asyncio.sleep(self.settings.scan_seconds)
            try:
                await self.recover_once()
                async with self.lock:
                    self._prune()
            except Exception:
                self.ready = False

    async def wait(self, task_id):
        job = self.jobs.get(task_id)
        if job is not None:
            await asyncio.shield(job)
        return self.get(task_id)

    async def close(self):
        self.closing = True
        self.ready = False
        if self.reaper:
            self.reaper.cancel()
            await asyncio.gather(self.reaper, return_exceptions=True)
        # Let newly accepted jobs enter their try/finally before cancellation.
        await asyncio.sleep(0)
        jobs = list(self.jobs.values())
        for job in jobs:
            # Tasks already cleaning have their own finite budget and should be allowed to finish.
            task_id = next((key for key, value in self.jobs.items() if value is job), None)
            if task_id not in self.cleanup_jobs:
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
