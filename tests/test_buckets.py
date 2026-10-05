"""
tests/test_buckets.py
Unit tests for the section 2.2 bucket-attribution algorithm in buckets.py.
No I/O, no AWS — pure arithmetic.
"""
from tokenomics_capture.buckets import attribute_buckets

_BUCKET_KEYS = (
    "est_context",
    "est_retrieval",
    "est_tool",
    "est_coordination",
    "est_governance",
    "est_unattributed",
)


def _total(result: dict) -> int:
    return sum(result[k] for k in _BUCKET_KEYS)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_system_message_goes_to_context():
    messages = [{"role": "system", "content": "You are a helpful assistant."}]
    result = attribute_buckets(messages, tool_bucket_map={}, measured_input=100)
    assert result["bucket_method"] == "estimated"
    assert result["est_context"] > 0
    assert result["est_retrieval"] == 0
    assert result["est_tool"] == 0


def test_user_message_goes_to_context():
    messages = [{"role": "user", "content": "Hello world, this is a test message."}]
    result = attribute_buckets(messages, tool_bucket_map={}, measured_input=100)
    assert result["est_context"] > 0


def test_retrieval_bucket_for_tagged_tool():
    messages = [
        {"role": "user", "content": "What is the weather?"},
        {"role": "tool", "content": "Sunny, 72°F", "tool_name": "weather_search"},
    ]
    result = attribute_buckets(
        messages,
        tool_bucket_map={"weather_search": "retrieval"},
        measured_input=100,
    )
    assert result["est_retrieval"] > 0


def test_tool_bucket_default_for_unregistered_tool():
    messages = [{"role": "tool", "content": "42", "tool_name": "calculator"}]
    result = attribute_buckets(messages, tool_bucket_map={}, measured_input=100)
    assert result["est_tool"] > 0
    assert result["est_retrieval"] == 0


def test_governance_bucket_for_tagged_agent():
    messages = [{"role": "assistant", "content": "Approved", "agent_name": "safety_judge"}]
    result = attribute_buckets(
        messages,
        tool_bucket_map={},
        measured_input=100,
        governance_agent_tags={"safety_judge"},
    )
    assert result["est_governance"] > 0


def test_calibration_sums_exactly_to_measured_input():
    """After calibration, bucket totals must equal measured_input exactly (section 2.2)."""
    messages = [
        {"role": "system", "content": "System prompt " * 20},
        {"role": "user", "content": "User message " * 10},
        {"role": "tool", "content": "Tool result " * 5, "tool_name": "search"},
    ]
    for measured_input in (50, 100, 500, 1_000, 7_777):
        result = attribute_buckets(
            messages,
            tool_bucket_map={"search": "retrieval"},
            measured_input=measured_input,
        )
        assert _total(result) == measured_input, (
            f"Expected total={measured_input}, got {_total(result)}"
        )


def test_empty_messages_assigns_all_to_unattributed():
    result = attribute_buckets([], tool_bucket_map={}, measured_input=123)
    assert result["est_unattributed"] == 123
    assert _total(result) == 123


def test_zero_measured_input_no_crash():
    messages = [{"role": "user", "content": "Hello"}]
    result = attribute_buckets(messages, tool_bucket_map={}, measured_input=0)
    assert result["bucket_method"] == "estimated"
    # All counts are 0 or the raw estimates when measured_input is 0
    assert _total(result) >= 0


def test_multi_part_content_flattened():
    """Converse-style multi-part content (list of dicts) must be tokenised correctly."""
    messages = [
        {
            "role": "user",
            "content": [
                {"text": "Hello there, "},
                {"text": "how are you?"},
            ],
        }
    ]
    result = attribute_buckets(messages, tool_bucket_map={}, measured_input=100)
    assert result["est_context"] > 0


def test_coordination_bucket_for_agent_messages():
    messages = [
        {"role": "assistant", "content": "Sub-agent result", "agent_name": "subagent_1"},
    ]
    result = attribute_buckets(
        messages,
        tool_bucket_map={},
        measured_input=100,
        governance_agent_tags=set(),  # subagent_1 is NOT in governance set
    )
    assert result["est_coordination"] > 0


def test_governance_tool_via_tool_bucket_map():
    """A tool marked 'governance' in tool_bucket_map goes to the governance bucket."""
    messages = [
        {"role": "tool", "content": "pass", "tool_name": "content_filter"},
    ]
    result = attribute_buckets(
        messages,
        tool_bucket_map={"content_filter": "governance"},
        measured_input=100,
    )
    assert result["est_governance"] > 0
    assert result["est_tool"] == 0
