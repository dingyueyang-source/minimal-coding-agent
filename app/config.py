"""Server-owned settings; no request can override these values."""
import math
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    project_id: str = "minimal-coding-agent"
    image: str = "minimal-coding-agent-sandbox:step2"
    task_seconds: float = 60
    cleanup_seconds: float = 15
    docker_seconds: float = 10
    llm_seconds: float = 25
    concurrency: int = 3
    max_llm_calls: int = 8
    max_tool_calls: int = 16
    max_files: int = 8
    max_records: int = 100
    ttl_seconds: float = 1800
    scan_seconds: float = 5
    max_events: int = 128

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", self.project_id):
            raise ValueError("Invalid project ID")
        if not self.image or self.image.endswith(":latest"):
            raise ValueError("A fixed local sandbox image is required")
        for name in ("task_seconds", "cleanup_seconds", "docker_seconds", "llm_seconds",
                     "ttl_seconds", "scan_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for name in ("concurrency", "max_llm_calls", "max_tool_calls", "max_files", "max_records", "max_events"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_records < self.concurrency or self.max_events < 2:
            raise ValueError("Record/event capacity is too small")

    @classmethod
    def from_env(cls):
        return cls(project_id=os.getenv("MCA_PROJECT_ID", "minimal-coding-agent"),
                   image=os.getenv("MCA_IMAGE", "minimal-coding-agent-sandbox:step2"))


ENV_KEYS = ("MCA_PROJECT_ID", "MCA_IMAGE", "MCA_HOST", "MCA_PORT", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
            "LLM_MAX_OUTPUT_TOKENS", "LLM_TOKEN_PARAM", "LLM_PARALLEL_TOOL_CALLS")


def load_dotenv(path=".env"):
    """Minimal KEY=VALUE loader for known keys only; real environment variables take precedence."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in ENV_KEYS and key not in os.environ:
            os.environ[key] = value


def require_llm_environment():
    """Production startup refuses to run without real model configuration."""
    from app.llm import LLMConfig
    values = {name: os.environ.get(name, "") for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")}
    if not all(values.values()) or any(v.startswith("replace-") for v in values.values()):
        raise ValueError("LLM_API_KEY, LLM_BASE_URL and LLM_MODEL are required")
    url = urlsplit(values["LLM_BASE_URL"])
    local = url.hostname in ("localhost", "127.0.0.1", "::1")
    if not url.hostname or url.username or url.password or url.fragment or url.query or (url.scheme != "https" and not (local and url.scheme == "http")):
        raise ValueError("LLM_BASE_URL requires HTTPS (loopback HTTP is allowed for tests)")
    parallel = os.environ.get("LLM_PARALLEL_TOOL_CALLS", "false").lower()
    if parallel not in ("false", "omit"):
        raise ValueError("LLM_PARALLEL_TOOL_CALLS must be false or omit")
    try:
        tokens = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "4096"))
    except ValueError:
        raise ValueError("LLM_MAX_OUTPUT_TOKENS must be an integer") from None
    return LLMConfig(api_key=values["LLM_API_KEY"], base_url=values["LLM_BASE_URL"], model=values["LLM_MODEL"],
                     max_output_tokens=tokens, token_param=os.environ.get("LLM_TOKEN_PARAM", "max_tokens"),
                     parallel_tool_calls=parallel == "false")


def server_bind():
    host = os.environ.get("MCA_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("This unauthenticated demo only listens on loopback")
    port = int(os.environ.get("MCA_PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid MCA_PORT")
    return host, port
