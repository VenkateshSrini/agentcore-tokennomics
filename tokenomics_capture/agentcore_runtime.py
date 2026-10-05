"""
tokenomics_capture/agentcore_runtime.py
Section 9.4: Pure ASGI middleware for run-level capture and RequestContext helpers.

Provides:
  - RunMeterMiddleware  : Pure ASGI middleware (scope/receive/send) — generates run_id,
                          reads the AgentCore session header, times the request, sets RUN_CTX
                          so bedrock_hook.py can tag every LLM call, and emits a run-level
                          event in a finally block (written even on errors).
  - session_id_from_context : helper for entrypoints that have a RequestContext but not a Request.
  - tokenomics_lifespan     : asynccontextmanager that starts run_flush_loop and drains on shutdown.

Why Pure ASGI instead of BaseHTTPMiddleware:
  BaseHTTPMiddleware wraps every response in an additional task group to buffer the response
  body. This can swallow exceptions and interfere with streaming responses. Pure ASGI middleware
  is the Starlette-recommended approach for production middleware. [D Starlette docs]
"""
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from starlette.types import ASGIApp, Receive, Scope, Send

from .bedrock_hook import RUN_CTX

logger = logging.getLogger(__name__)

# AgentCore injects the session ID via this HTTP header [D-27]
_SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"


class RunMeterMiddleware:
    """Pure ASGI middleware that:
      1. Generates a unique run_id for each HTTP request.
      2. Reads session_id from the AgentCore session header [D-27].
      3. Pushes both into RUN_CTX so bedrock_hook.py can tag every LLM call.
      4. Emits a run-level usage_event in a finally block (written even on errors).

    Wire it in via app.add_middleware(RunMeterMiddleware, meter=meter, config=config).
    """

    def __init__(self, app: ASGIApp, meter, config) -> None:
        self._app = app
        self._meter = meter
        self._config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Pass through non-HTTP scopes (lifespan, websocket) untouched.
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        run_id = str(uuid.uuid4())
        # Headers in ASGI scope are raw bytes; find ours case-insensitively.
        session_id = _header_value(scope, _SESSION_HEADER)
        start = time.time()

        ctx_token = RUN_CTX.set({
            "run_id": run_id,
            "session_id": session_id,
            "app": self._config.app,
            "team": self._config.team,
            "framework": "raw",
            "hosting": "agentcore_runtime",
        })
        # [U] — verify: that this ContextVar is visible from both sync `def entrypoint(payload,
        # context)` and `async def entrypoint(...)` handlers (checklist item 15). Starlette runs
        # sync handlers in a thread pool via anyio.to_thread; ContextVar values are propagated by
        # Python's contextvars mechanism across asyncio, but this must be confirmed with a real
        # AgentCore entrypoint before relying on it.
        try:
            await self._app(scope, receive, send)
        finally:
            elapsed_ms = (time.time() - start) * 1000
            self._meter.emit(
                level="run",
                run_id=run_id,
                session_id=session_id,
                app=self._config.app,
                team=self._config.team,
                framework="raw",
                hosting="agentcore_runtime",
                details={"latency_ms": elapsed_ms},
            )
            RUN_CTX.reset(ctx_token)


def _header_value(scope: Scope, name: str) -> str | None:
    """Return the first matching header value from an ASGI scope, or None.
    Header name comparison is case-insensitive per HTTP/1.1 spec.
    ASGI delivers headers as a list of (name_bytes, value_bytes) pairs.
    """
    name_bytes = name.lower().encode()
    for header_name, header_value in scope.get("headers", []):
        if header_name.lower() == name_bytes:
            return header_value.decode()
    return None


def session_id_from_context(context: Any) -> str | None:
    """Extract session_id from a BedrockAgentCoreApp RequestContext.
    For entrypoints that have the native RequestContext but not the ASGI scope,
    use this instead of reading the header directly.

    # [U] — verify: the attribute name `session_id` on RequestContext against the SDK [D-27].
    """
    return getattr(context, "session_id", None)


@asynccontextmanager
async def tokenomics_lifespan(meter) -> AsyncIterator[None]:
    """asynccontextmanager that starts the flush loop and drains on shutdown.

    Usage in your BedrockAgentCoreApp:

        from contextlib import asynccontextmanager
        from tokenomics_capture.agentcore_runtime import tokenomics_lifespan

        @asynccontextmanager
        async def lifespan(app):
            await meter.initialise()
            async with tokenomics_lifespan(meter):
                yield

        app = BedrockAgentCoreApp("my_agent", lifespan=lifespan)
    """
    task = asyncio.create_task(meter.run_flush_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if hasattr(meter, "_writer") and hasattr(meter._writer, "close"):
            await meter._writer.close()
