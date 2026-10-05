import asyncio

import pytest

from tokenomics_capture.bedrock_hook import RUN_CTX
from tokenomics_capture.config import TokenomicsConfig
from tokenomics_capture.json_writer import JsonWriter
from tokenomics_capture.meter import Meter, UsageEvent


class _FakeUsageMetadata:
    def __init__(self, prompt=100, candidates=50, total=150, cached=10, thoughts=0):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.total_token_count = total
        self.cached_content_token_count = cached
        self.thoughts_token_count = thoughts


class _FakeLlmResponse:
    def __init__(self, usage=None, partial=None):
        self.usage_metadata = usage
        self.partial = partial


class _FakeCallbackContext:
    def __init__(
        self,
        agent_name="test-adk-agent",
        invocation_id="inv-001",
        user_id="user-adk",
        session_id="sess-adk",
    ):
        self.agent_name = agent_name
        self.invocation_id = invocation_id
        self.user_id = user_id
        self.session = type("S", (), {"id": session_id})()


@pytest.fixture
def meter(tmp_path):
    config = TokenomicsConfig(app="test-app", json_data_dir=str(tmp_path))
    writer = JsonWriter(str(tmp_path))
    return Meter(writer=writer, config=config)


def _make_adapter_with_stubs(meter, provider="google.gemini"):
    import tokenomics_capture.adk_adapter as mod

    original_available = mod._ADK_AVAILABLE
    original_base_plugin = mod.BasePlugin

    mod._ADK_AVAILABLE = True
    mod.BasePlugin = object

    from tokenomics_capture.adk_adapter import ADKTokenMeter

    class PatchedMeter(ADKTokenMeter):
        def __init__(self, m, p):
            self._meter = m
            self._provider = p

    adapter = PatchedMeter(meter, provider)

    mod._ADK_AVAILABLE = original_available
    mod.BasePlugin = original_base_plugin
    return adapter


def test_adk_emit_correct_tokens(meter):
    adapter = _make_adapter_with_stubs(meter)
    response = _FakeLlmResponse(usage=_FakeUsageMetadata())
    callback_context = _FakeCallbackContext()

    asyncio.run(
        adapter.after_model_callback(
            callback_context=callback_context,
            llm_response=response,
        )
    )

    assert meter._queue.qsize() == 1
    ev: UsageEvent = meter._queue.get_nowait()
    assert ev.input_tokens == 100
    assert ev.output_tokens == 50
    assert ev.cache_read_tokens == 10
    assert ev.estimated is False


def test_adk_skips_partial_response(meter):
    adapter = _make_adapter_with_stubs(meter)
    response = _FakeLlmResponse(usage=_FakeUsageMetadata(), partial=True)

    asyncio.run(
        adapter.after_model_callback(
            callback_context=_FakeCallbackContext(),
            llm_response=response,
        )
    )

    assert meter._queue.qsize() == 0


def test_adk_skips_none_usage(meter):
    adapter = _make_adapter_with_stubs(meter)
    response = _FakeLlmResponse(usage=None)

    asyncio.run(
        adapter.after_model_callback(
            callback_context=_FakeCallbackContext(),
            llm_response=response,
        )
    )

    assert meter._queue.qsize() == 0


def test_adk_maps_reasoning_tokens(meter):
    adapter = _make_adapter_with_stubs(meter)
    response = _FakeLlmResponse(usage=_FakeUsageMetadata(thoughts=30))

    asyncio.run(
        adapter.after_model_callback(
            callback_context=_FakeCallbackContext(),
            llm_response=response,
        )
    )

    ev: UsageEvent = meter._queue.get_nowait()
    assert ev.reasoning_tokens == 30


def test_adk_framework_tag_is_adk(meter):
    adapter = _make_adapter_with_stubs(meter)
    response = _FakeLlmResponse(usage=_FakeUsageMetadata())

    asyncio.run(
        adapter.after_model_callback(
            callback_context=_FakeCallbackContext(),
            llm_response=response,
        )
    )

    ev: UsageEvent = meter._queue.get_nowait()
    assert ev.framework == "adk"


def test_adk_returns_none(meter):
    adapter = _make_adapter_with_stubs(meter)
    response = _FakeLlmResponse(usage=_FakeUsageMetadata())

    result = asyncio.run(
        adapter.after_model_callback(
            callback_context=_FakeCallbackContext(),
            llm_response=response,
        )
    )

    assert result is None


def test_adk_merges_run_ctx(meter):
    adapter = _make_adapter_with_stubs(meter)
    response = _FakeLlmResponse(usage=_FakeUsageMetadata())
    token = RUN_CTX.set({"run_id": "run-adk-001", "hosting": "agentcore_runtime"})
    try:
        asyncio.run(
            adapter.after_model_callback(
                callback_context=_FakeCallbackContext(),
                llm_response=response,
            )
        )
    finally:
        RUN_CTX.reset(token)

    ev: UsageEvent = meter._queue.get_nowait()
    assert ev.run_id == "run-adk-001"
    assert ev.hosting == "agentcore_runtime"
