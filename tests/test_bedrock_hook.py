"""
tests/test_bedrock_hook.py
Acceptance test for bedrock_hook.py (section 3 of the build prompt).

Uses a minimal fake botocore client with a stub pub/sub event system — no live AWS required.

Asserts:
  - Converse response with usage → correct token counts, estimated=False
  - InvokeModel response with headers → correct token counts, estimated=False
  - InvokeModel response with NO headers → counts=0, estimated=True
"""
import pytest

from tokenomics_capture.bedrock_hook import register_bedrock_token_meter
from tokenomics_capture.config import TokenomicsConfig
from tokenomics_capture.json_writer import JsonWriter
from tokenomics_capture.meter import Meter, UsageEvent


# ---------------------------------------------------------------------------
# Fake botocore infrastructure
# ---------------------------------------------------------------------------
class _FakeModel:
    def __init__(self, name: str):
        self.name = name


class _FakeEventSystem:
    """Minimal pub/sub stub that matches botocore's wildcard event naming convention."""

    def __init__(self):
        self._handlers: list[tuple[str, object]] = []

    def register(self, pattern: str, handler) -> None:
        self._handlers.append((pattern, handler))

    def fire(self, event_name: str, **kwargs) -> None:
        for pattern, handler in self._handlers:
            if pattern.endswith(".*"):
                prefix = pattern[:-2]  # strip ".*"
                if event_name.startswith(prefix):
                    handler(**kwargs)
            elif pattern == event_name:
                handler(**kwargs)


class _FakeMeta:
    def __init__(self):
        self.events = _FakeEventSystem()


class _FakeClient:
    def __init__(self):
        self.meta = _FakeMeta()


class _FakeHttpResponse:
    def __init__(self, headers: dict):
        self.headers = headers


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def meter(tmp_path):
    config = TokenomicsConfig(app="test-app", json_data_dir=str(tmp_path))
    writer = JsonWriter(str(tmp_path))
    return Meter(writer=writer, config=config)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_converse_emits_correct_token_counts(meter):
    client = _FakeClient()
    register_bedrock_token_meter(client, meter)

    client.meta.events.fire(
        "after-call.bedrock-runtime.Converse",
        http_response=None,
        parsed={
            "usage": {
                "inputTokens": 100,
                "outputTokens": 50,
                "totalTokens": 150,
            }
        },
        model=_FakeModel("Converse"),
        params={"modelId": "anthropic.claude-3-haiku-20240307-v1:0"},
    )

    assert meter._queue.qsize() == 1
    ev: UsageEvent = meter._queue.get_nowait()
    assert isinstance(ev, UsageEvent)
    assert ev.level == "call"
    assert ev.input_tokens == 100
    assert ev.output_tokens == 50
    assert ev.cache_read_tokens == 0
    assert ev.estimated is False
    assert ev.model == "anthropic.claude-3-haiku-20240307-v1:0"
    assert ev.provider == "aws.bedrock"


def test_invoke_model_uses_headers(meter):
    client = _FakeClient()
    register_bedrock_token_meter(client, meter)

    client.meta.events.fire(
        "after-call.bedrock-runtime.InvokeModel",
        http_response=_FakeHttpResponse({
            "x-amzn-bedrock-input-token-count": "200",
            "x-amzn-bedrock-output-token-count": "80",
            "x-amzn-bedrock-cache-read-input-token-count": "10",
            "x-amzn-bedrock-cache-write-input-token-count": "0",
        }),
        parsed={},
        model=_FakeModel("InvokeModel"),
        params={"modelId": "amazon.nova-lite-v1:0"},
    )

    assert meter._queue.qsize() == 1
    ev: UsageEvent = meter._queue.get_nowait()
    assert ev.input_tokens == 200
    assert ev.output_tokens == 80
    assert ev.cache_read_tokens == 10
    assert ev.estimated is False


def test_invoke_model_no_headers_sets_estimated(meter):
    """Providers that don't expose headers (e.g. AI21, Cohere) result in estimated=True."""
    client = _FakeClient()
    register_bedrock_token_meter(client, meter)

    client.meta.events.fire(
        "after-call.bedrock-runtime.InvokeModel",
        http_response=None,  # No response object at all
        parsed={},
        model=_FakeModel("InvokeModel"),
        params={"modelId": "ai21.jamba-instruct-v1:0"},
    )

    assert meter._queue.qsize() == 1
    ev: UsageEvent = meter._queue.get_nowait()
    assert ev.estimated is True
    assert ev.input_tokens == 0
    assert ev.output_tokens == 0


def test_converse_with_cache_tokens(meter):
    client = _FakeClient()
    register_bedrock_token_meter(client, meter)

    client.meta.events.fire(
        "after-call.bedrock-runtime.Converse",
        http_response=None,
        parsed={
            "usage": {
                "inputTokens": 80,
                "outputTokens": 40,
                "totalTokens": 120,
                "cacheReadInputTokens": 20,
                "cacheWriteInputTokens": 5,
            }
        },
        model=_FakeModel("Converse"),
        params={"modelId": "anthropic.claude-3-5-sonnet-20241022-v2:0"},
    )

    ev: UsageEvent = meter._queue.get_nowait()
    assert ev.cache_read_tokens == 20
    assert ev.cache_write_tokens == 5
    assert ev.estimated is False


def test_multiple_clients_each_emit_independently(meter):
    client_a = _FakeClient()
    client_b = _FakeClient()
    register_bedrock_token_meter(client_a, meter)
    register_bedrock_token_meter(client_b, meter)

    for client in (client_a, client_b):
        client.meta.events.fire(
            "after-call.bedrock-runtime.Converse",
            http_response=None,
            parsed={"usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}},
            model=_FakeModel("Converse"),
            params={"modelId": "amazon.nova-micro-v1:0"},
        )

    assert meter._queue.qsize() == 2


def test_emit_does_not_raise_on_malformed_usage(meter):
    """Telemetry must never raise into the caller's path (section 0 ground rule 3)."""
    client = _FakeClient()
    register_bedrock_token_meter(client, meter)

    # Parsed with no 'usage' and no HTTP response — should not raise
    client.meta.events.fire(
        "after-call.bedrock-runtime.Converse",
        http_response=None,
        parsed={},          # missing 'usage' key
        model=_FakeModel("Converse"),
        params={},
    )
    # Either queued an event or not — what matters is no exception
