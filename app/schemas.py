"""Mutable records are owned by one event loop; snapshots are deep copies."""
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskRecord:
    task_id: str
    container_name: str
    deadline: object
    status: str = "running"
    phase: str = "sandbox_starting"
    execution_status: str = "pending"
    failed_phase: str | None = None
    container_id: str | None = None
    cleanup: dict = field(default_factory=lambda: {"status": "pending", "error": None})
    files: list = field(default_factory=list)
    error: dict | None = None
    warnings: list = field(default_factory=list)
    events: list = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    execution_finished_at: str | None = None
    finished_at: str | None = None
    agent_message: str = ""
    revision: int = 0
    cancelled: bool = False
    terminal_at: float | None = None

    def event(self, code, message, maximum=128):
        entry = {"at": utcnow(), "phase": self.phase, "code": code, "message": message}
        if len(self.events) < maximum:
            self.events.append(entry)
        elif self.events[-1]["code"] != "EVENTS_OMITTED":
            self.events[-1] = {**entry, "code": "EVENTS_OMITTED", "message": "Further events omitted"}
        self.revision += 1

    def snapshot(self):
        fields = ("task_id", "container_id", "status", "phase", "execution_status", "failed_phase",
                  "cleanup", "files", "error", "warnings", "events", "created_at",
                  "execution_finished_at", "finished_at", "agent_message", "revision")
        return copy.deepcopy({name: getattr(self, name) for name in fields})
