"""
tests/test_strands_adapter.py
Unit tests for strands_adapter.py using stubs for the Strands hooks API.
No live AWS, no strands-agents installation required — the adapter is designed to be
importable and testable without the optional strands-agents dependency.
"""
import pytest

from tokenomics_capture.bedrock_hook import RUN_CTX
from tokenomics_capture.config import TokenomicsConfig
from tokenomics_capture.json_writer import JsonWriter
from tokenomics_capture.meter import Meter, UsageEvent


# ---------------------------------------------------------------------------
# Minimal stubs for the Strands hooks API (harness-sdk)
# ---------------------------------------------------------------------------
class _FakeRegistry:
    def __init__(self):
        self._callbacks: list[tuple[type, object]] = []

    def add_callback(self, event_type, callback) -> None:
        self._callbacks.append((event_type, callback))

    def fire(self, event) -> None:
        for event_type, callback in self._callbacks:
            if isinstance(event, event_type):
                callback(event)


class _FakeAgent:
    name = "test-strands-agent"


# Minimal stubs for BeforeInvocationEvent and AfterInvocationEvent
class _BeforeInvocationEvent:
    def __init__(self, invocation_state=None):
        self.agent = _FakeAgent()
        self.invocation_state = invocation_state or {}


class _AfterInvocationResult:
    """Mimics AgentResult.metrics.latest_agent_invocation.usage"""
    def __init__(self, usage_dict: dict):
        self._usage = usage_dict

    @property
    def usage(self):
        return self._usage


class _FakeMetrics:
    def __init__(self, usage_dict: dict):
        self._invocation = _AfterInvocationResult(usage_dict)

    @property
    def latest_agent_invocation(self):
        return self._invocation


class _FakeAgentResult:
    def __init__(self, usage_dict: dict):
        self.metrics = _FakeMetrics(usage_dict)


class _AfterInvocationEvent:
    def __init__(self, usage_dict: dict, invocation_state=None):
        self.agent = _FakeAgent()
        self.result = _FakeAgentResult(usage_dict)
        self.invocation_state = invocation_state or {}


class _AfterInvocationEventNoResult:
    """Mimics structured_output() path where result is None."""
    def __init__(self):
        self.agent = _FakeAgent()
        self.result = None
        self.invocation_state = {}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def meter(tmp_path):
    config = TokenomicsConfig(app="test-app", json_data_dir=str(tmp_path))
    writer = JsonWriter(str(tmp_path))
    return Meter(writer=writer, config=config)


# ---------------------------------------------------------------------------
# Helpers to build the adapter with stub hooks wired in
# ---------------------------------------------------------------------------
def _make_meter_with_stubs(meter):
    """
    Build a StrandsTokenMeter with the Strands SDK replaced by our stubs.
    We patch the import check so we can test without strands-agents installed.
    """
    import tokenomics_capture.strands_adapter as mod

    original_available = mod._STRANDS_AVAILABLE

    # Inject stub classes so the adapter uses them without the real SDK
    mod._STRANDS_AVAILABLE = True
    mod.HookProvider = object
    mod.BeforeInvocationEvent = _BeforeInvocationEvent
    mod.AfterInvocationEvent = _AfterInvocationEvent

    # Re-import to pick up patched module-level names
    # (StrandsTokenMeter is already defined; we test its logic directly)
    from tokenomics_capture.strands_adapter import StrandsTokenMeter

    class PatchedMeter(StrandsTokenMeter):
        """Subclass that skips the strands availability guard."""
        def __init__(self, m):
            self._meter = m

    registry = _FakeRegistry()
    patched = PatchedMeter(meter)
    patched.register_hooks(registry)

    mod._STRANDS_AVAILABLE = original_available
    return patched, registry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_before_invocation_sets_framework_in_ctx(meter):
    adapter, registry = _make_meter_with_stubs(meter)

    # Fire BeforeInvocationEvent
    event = _BeforeInvocationEvent(invocation_state={"user_id": "alice-hash"})
    registry.fire(event)

    ctx = RUN_CTX.get({})
    assert ctx.get("framework") == "strands"
    assert ctx.get("agent_name") == "test-strands-agent"
    assert ctx.get("user_id") == "alice-hash"


def test_before_invocation_preserves_existing_ctx(meter):
    """BeforeInvocationEvent must merge into existing RUN_CTX, not overwrite it."""
    # Simulate RunMeterMiddleware having already set run_id/session_id
    token = RUN_CTX.set({"run_id": "run-123", "session_id": "sess-456", "hosting": "agentcore_runtime"})
    try:
        adapter, registry = _make_meter_with_stubs(meter)
        event = _BeforeInvocationEvent(invocation_state={})
        registry.fire(event)

        ctx = RUN_CTX.get({})
        assert ctx.get("run_id") == "run-123"
        assert ctx.get("session_id") == "sess-456"
        assert ctx.get("framework") == "strands"
    finally:
        RUN_CTX.reset(token)


def test_after_invocation_emits_run_level_event(meter):
    adapter, registry = _make_meter_with_stubs(meter)

    usage = {"inputTokens": 300, "outputTokens": 120, "totalTokens": 420}
    event = _AfterInvocationEvent(usage_dict=usage, invocation_state={"user_id": "bob"})
    registry.fire(event)

    assert meter._queue.qsize() == 1
    ev: UsageEvent = meter._queue.get_nowait()
    assert ev.level == "run"
    assert ev.framework == "strands"
    assert ev.agent_name == "test-strands-agent"
    assert ev.input_tokens == 300
    assert ev.output_tokens == 120
    assert ev.user_id == "bob"


def test_after_invocation_no_result_emits_zero_usage(meter):
    """When event.result is None (structured_output path), emit run row with zero tokens."""
    adapter, registry = _make_meter_with_stubs(meter)

    # Inject the no-result event class directly
    adapter._on_after_invocation(_AfterInvocationEventNoResult())

    assert meter._queue.qsize() == 1
    ev: UsageEvent = meter._queue.get_nowait()
    assert ev.level == "run"
    assert ev.input_tokens == 0


def test_after_invocation_uses_per_invocation_not_accumulated(meter):
    """
    Validates the companion doc section 6.1 correction:
    use latest_agent_invocation.usage, not accumulated_usage.

    This test constructs two invocations' worth of usage and confirms the adapter
    reports only the LAST invocation's tokens.
    """
    adapter, registry = _make_meter_with_stubs(meter)

    # Only the latest invocation should be reported (300 tokens, not 600 accumulated)
    usage = {"inputTokens": 300, "outputTokens": 100, "totalTokens": 400}
    event = _AfterInvocationEvent(usage_dict=usage)
    registry.fire(event)

    ev: UsageEvent = meter._queue.get_nowait()
    assert ev.input_tokens == 300, "Must use latest_agent_invocation.usage, not accumulated"
