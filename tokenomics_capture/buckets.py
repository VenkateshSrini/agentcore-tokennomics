"""
tokenomics_capture/buckets.py
Section 2.2 bucket-attribution algorithm — implemented once, called from any adapter
that has access to request content (currently maf_adapter.py).

NOT called from bedrock_hook.py (which only sees the response, not the request body).
"""
from typing import Any

# Character-based token heuristic for MVP (section 2.2).
# [U] — verify: replace with tiktoken or the model's tokenizer if billing accuracy matters.
_CHARS_PER_TOKEN = 4

_BUCKET_KEYS = (
    "est_context",
    "est_retrieval",
    "est_tool",
    "est_coordination",
    "est_governance",
    "est_unattributed",
)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _classify_tool_result(tool_name: str, tool_bucket_map: dict[str, str]) -> str:
    """Return the bucket for a tool result; default is 'tool' if not in the app map."""
    return tool_bucket_map.get(tool_name, "tool")


def attribute_buckets(
    messages: list[dict],
    tool_bucket_map: dict[str, str],
    measured_input: int,
    governance_agent_tags: set[str] | None = None,
) -> dict[str, Any]:
    """
    Attribute estimated token counts to the six Splunk buckets (section 2.2).

    Args:
        messages: list of message dicts with at least {"role": ..., "content": ...}.
                  Optional keys: "tool_name", "name", "agent_name".
        tool_bucket_map: app-supplied mapping of tool_name -> bucket label.
        measured_input: measured inputTokens from the provider response, used to calibrate.
        governance_agent_tags: set of agent names classified as judge/guardrail/evaluator.

    Returns a dict with est_* bucket keys, bucket_method='estimated'.
    All values are proportionally calibrated to measured_input so they sum to it exactly.
    Mark every derived value with bucket_method='estimated' per section 2.4. [A]
    """
    governance_agent_tags = governance_agent_tags or set()

    buckets: dict[str, int] = {k: 0 for k in _BUCKET_KEYS}

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        tool_name: str | None = msg.get("tool_name") or msg.get("name")
        agent_name: str | None = msg.get("agent_name")

        # Flatten multi-part content (Converse API format uses list-of-dicts)
        if isinstance(content, list):
            text = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        else:
            text = str(content) if content else ""

        est = _estimate_tokens(text)

        if agent_name and agent_name in governance_agent_tags:
            buckets["est_governance"] += est
        elif role == "system":
            # System prompt, safety policy, conversation history → context
            buckets["est_context"] += est
        elif role == "tool" or (role == "user" and tool_name):
            bucket = _classify_tool_result(tool_name or "", tool_bucket_map)
            if bucket == "retrieval":
                buckets["est_retrieval"] += est
            elif bucket == "governance":
                buckets["est_governance"] += est
            else:
                buckets["est_tool"] += est
        elif agent_name:
            # Messages from another agent → coordination bucket
            buckets["est_coordination"] += est
        else:
            # Human turns, conversation history → context
            buckets["est_context"] += est

    total_est = sum(buckets.values())

    if total_est > 0 and measured_input > 0:
        # Calibrate: scale each estimate so the sum equals measured_input (section 2.2)
        scale = measured_input / total_est
        buckets = {k: round(v * scale) for k, v in buckets.items()}
        # Assign rounding residual to unattributed so the total is exact
        residual = measured_input - sum(buckets.values())
        buckets["est_unattributed"] += residual
    elif total_est == 0:
        buckets["est_unattributed"] = measured_input

    return {**buckets, "bucket_method": "estimated"}
