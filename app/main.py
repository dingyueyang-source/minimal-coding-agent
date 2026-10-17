"""Same-origin FastAPI app: page, static assets and JSON task API.

Production: `python -m app` (or `uvicorn app.main:create_app --factory --workers 1`).
Tests inject a model client and sandbox through create_app(); no HTTP parameter selects them.
"""
import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.agent import CREATE_FILE_TOOL
from app.config import Settings, load_dotenv, require_llm_environment, server_bind
from app.errors import AdmissionError, AgentError
from app.sandbox import SandboxManager
from app.task_manager import TaskManager
from shared.path_policy import PolicyError

BODY_LIMIT = 64 * 1024
STATIC = Path(__file__).resolve().parent.parent / "static"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
log = logging.getLogger("mca")


def error(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def create_app(settings=None, llm=None, sandbox=None, *, origins=None, start_retry_seconds=5.0):
    if llm is None:
        # Production wiring: refuse to start without real model configuration.
        load_dotenv()
        from app.llm import ChatCompletionsClient
        llm = ChatCompletionsClient(require_llm_environment(), [CREATE_FILE_TOOL])
    settings = settings or Settings.from_env()
    sandbox = sandbox or SandboxManager(settings)
    manager = TaskManager(settings, sandbox, llm)
    state = {"startup_error": None, "started": False}
    if origins is None:
        _, port = server_bind()
        origins = (f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}")
    allowed_origins = set(origins)

    async def startup_loop():
        # Startup checks (daemon, image, residue sweep, smoke sandbox) gate admission; retry while
        # Docker Desktop is still booting instead of accepting tasks.
        while True:
            try:
                await manager.start()
                state["startup_error"] = None
                state["started"] = True
                log.info("startup checks passed")
                return
            except AgentError as exc:
                state["startup_error"] = exc.code
            except Exception as exc:  # noqa: BLE001 - details withheld from clients
                state["startup_error"] = "STARTUP_FAILED"
                log.warning("startup check failed: %s", type(exc).__name__)
            await asyncio.sleep(start_retry_seconds)

    @asynccontextmanager
    async def lifespan(app):
        starter = asyncio.create_task(startup_loop(), name="mca-startup")
        app.state.starter = starter
        try:
            yield
        finally:
            starter.cancel()
            await asyncio.gather(starter, return_exceptions=True)
            await manager.close()
            if hasattr(llm, "aclose"):
                await llm.aclose()

    app = FastAPI(title="Minimal Coding Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        return error(exc.status_code, codes.get(exc.status_code, "HTTP_ERROR"), str(exc.detail))

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/health")
    async def health():
        info = manager.health()
        info["startup_checks"] = "passed" if state["started"] else ("failed" if state["startup_error"] else "running")
        info["startup_error"] = state["startup_error"]
        info["capacity"] = settings.concurrency
        info["model"] = getattr(llm, "label", type(llm).__name__)
        return JSONResponse(info, headers={"Cache-Control": "no-store"})

    @app.post("/tasks")
    async def submit(request: Request):
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return error(403, "ORIGIN_REJECTED", "Cross-origin submissions are not accepted")
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return error(415, "UNSUPPORTED_MEDIA_TYPE", "POST /tasks accepts application/json only")
        body = bytearray()
        async for chunk in request.stream():  # Count actual bytes; Content-Length is not trusted.
            body.extend(chunk)
            if len(body) > BODY_LIMIT:
                return error(413, "BODY_TOO_LARGE", "Request body exceeds 64 KiB")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return error(422, "INVALID_JSON", "Body must be UTF-8 JSON")
        if not isinstance(payload, dict) or set(payload) != {"request"}:
            return error(422, "INVALID_REQUEST", 'Body must be {"request": "..."}')
        try:
            accepted = await manager.submit(payload["request"])
        except PolicyError as exc:
            return error(422, exc.code, str(exc))
        except AdmissionError as exc:
            return error(exc.status_code, exc.code, str(exc))
        return JSONResponse(accepted, status_code=202, headers={"Cache-Control": "no-store"})

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str):
        snapshot = manager.get(task_id) if UUID.fullmatch(task_id) else None
        if snapshot is None:
            return error(404, "TASK_NOT_FOUND", "Task does not exist, has expired, or was lost on restart")
        return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})

    return app
