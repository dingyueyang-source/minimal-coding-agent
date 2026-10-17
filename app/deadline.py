import asyncio
from dataclasses import dataclass

from app.errors import AgentError


@dataclass(frozen=True)
class Deadline:
    at: float

    @classmethod
    def after(cls, seconds):
        return cls(asyncio.get_running_loop().time() + seconds)

    def remaining(self):
        return max(0.0, self.at - asyncio.get_running_loop().time())

    def check(self, code="TASK_TIMEOUT"):
        if self.remaining() <= 0:
            raise AgentError(code, "Execution deadline exceeded", timeout=True)

    def limit(self, seconds):
        self.check()
        return min(seconds, self.remaining())
