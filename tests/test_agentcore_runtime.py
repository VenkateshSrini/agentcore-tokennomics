"""
tests/test_agentcore_runtime.py
Acceptance tests for agentcore_runtime.py (section 4 of the build prompt).

Uses Starlette TestClient (synchronous HTTP testing) with a minimal app wrapped in
RunMeterMiddleware.  No live AWS required.

Requires httpx2 (not httpx) — starlette 1.7.0 deprecated httpx with TestClient.

Asserts:
  - RUN_CTX ContextVar is readable from inside a sync request handler.
  - session_id from the AgentCore header is propagated correctly.
  - A run-level UsageEvent is emitted after every request (even if the handler raises).
"""
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient  # requires httpx2

from tokenomics_capture.agentcore_runtime import (
    RunMeterMiddleware,
    session_id_from_context,
)
from tokenomics_capture.bedrock_hook import RUN_CTX
from tokenomics_capture.config import TokenomicsConfig
from tokenomics_capture.json_writer import JsonWriter
from tokenomics_capture.meter import Meter, UsageEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_app(meter, config, routes):
    app = Starlette(routes=routes)
    app.add_middleware(RunMeterMiddleware, meter=meter, config=config)
    return app


@pytest.fixture
def meter_and_config(tmp_path):
    config = TokenomicsConfig(app="test-app", team="test-team", json_data_dir=str(tmp_path))
    writer = JsonWriter(str(tmp_path))
    m = Meter(writer=writer, config=config)
    return m, config


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_run_ctx_set_with_run_id_and_session(meter_and_config):
    """RUN_CTX must contain run_id and session_id inside a sync handler."""
    meter, config = meter_and_config
    captured: dict = {}

    def handler(request):
        ctx = RUN_CTX.get({})
        captured["run_id"] = ctx.get("run_id")
        captured["session_id"] = ctx.get("session_id")
        captured["hosting"] = ctx.get("hosting")
        return JSONResponse({"ok": True})

    app = _make_app(meter, config, [Route("/", handler)])
    client = TestClient(app)
    resp = client.get(
        "/",
        headers={"x-amzn-bedrock-agentcore-runtime-session-id": "sess-abc-123"},
    )

    assert resp.status_code == 200
    assert captured["run_id"] is not None
    assert len(captured["run_id"]) == 36  # UUID string
    assert captured["session_id"] == "sess-abc-123"
    assert captured["hosting"] == "agentcore_runtime"


def test_run_level_event_emitted_after_request(meter_and_config):
    """A run-level UsageEvent must be queued for every request."""
    meter, config = meter_and_config

    def handler(request):
        return JSONResponse({"ok": True})

    app = _make_app(meter, config, [Route("/invocations", handler, methods=["POST"])])
    client = TestClient(app)
    client.post("/invocations", json={"prompt": "hello"})

    assert meter._queue.qsize() == 1
    ev: UsageEvent = meter._queue.get_nowait()
    assert ev.level == "run"
    assert ev.hosting == "agentcore_runtime"
    assert ev.app == "test-app"
    assert ev.team == "test-team"
    assert ev.run_id is not None


def test_run_level_event_emitted_even_on_handler_error(meter_and_config):
    """The run-level event must still be emitted if the handler raises (finally block)."""
    meter, config = meter_and_config

    def bad_handler(request):
        raise RuntimeError("agent crash")

    app = _make_app(meter, config, [Route("/", bad_handler)])
    client = TestClient(app, raise_server_exceptions=False)
    client.get("/")

    assert meter._queue.qsize() == 1
    ev: UsageEvent = meter._queue.get_nowait()
    assert ev.level == "run"


def test_run_id_unique_per_request(meter_and_config):
    """Each request must receive a different run_id."""
    meter, config = meter_and_config
    run_ids: list = []

    def handler(request):
        run_ids.append(RUN_CTX.get({}).get("run_id"))
        return JSONResponse({})

    app = _make_app(meter, config, [Route("/", handler)])
    client = TestClient(app)
    client.get("/")
    client.get("/")

    assert len(run_ids) == 2
    assert run_ids[0] != run_ids[1]


def test_session_id_missing_header(meter_and_config):
    """When the session header is absent, session_id must be None (not raise)."""
    meter, config = meter_and_config
    captured: dict = {}

    def handler(request):
        captured["session_id"] = RUN_CTX.get({}).get("session_id")
        return JSONResponse({})

    app = _make_app(meter, config, [Route("/", handler)])
    TestClient(app).get("/")  # no session header

    assert captured["session_id"] is None


def test_session_id_from_context_returns_none_for_missing_attr():
    """session_id_from_context must return None rather than raise for unknown objects."""

    class FakeContext:
        pass

    assert session_id_from_context(FakeContext()) is None


def test_session_id_from_context_reads_attribute():
    """session_id_from_context must return the session_id attribute when present."""

    class FakeContext:
        session_id = "ctx-session-42"

    assert session_id_from_context(FakeContext()) == "ctx-session-42"
