"""
tokenomics_capture/pricing.py
Single source of truth for price lookups and token normalisation (section 4.2).
Called from meter.py; never re-implemented inline in adapters.
"""
from typing import Optional

# Default in-memory price catalog (USD per million tokens).
# [U] — verify: current pricing against https://aws.amazon.com/bedrock/pricing/
# These figures come from third-party aggregator pages [W-30][W-31], not the official AWS page.
_DEFAULT_CATALOG: dict[tuple[str, str], dict] = {
    # key: (provider, model_id_prefix)
    ("aws.bedrock", "anthropic.claude-3-haiku"): {
        "input_per_mtok": 0.25,
        "output_per_mtok": 1.25,
        "cache_read_per_mtok": 0.03,
        "cache_write_per_mtok": 0.30,
        "reasoning_billed_separately": False,
    },
    ("aws.bedrock", "anthropic.claude-3-5-sonnet"): {
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_read_per_mtok": 0.30,
        "cache_write_per_mtok": 3.75,
        "reasoning_billed_separately": False,
    },
    ("aws.bedrock", "anthropic.claude-3-opus"): {
        "input_per_mtok": 15.00,
        "output_per_mtok": 75.00,
        "cache_read_per_mtok": 1.50,
        "cache_write_per_mtok": 18.75,
        "reasoning_billed_separately": False,
    },
    ("aws.bedrock", "anthropic.claude-3-sonnet"): {
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_read_per_mtok": 0.30,
        "cache_write_per_mtok": 3.75,
        "reasoning_billed_separately": False,
    },
    ("aws.bedrock", "amazon.nova-micro"): {
        "input_per_mtok": 0.035,
        "output_per_mtok": 0.14,
        "cache_read_per_mtok": 0.0,
        "cache_write_per_mtok": 0.0,
        "reasoning_billed_separately": False,
    },
    ("aws.bedrock", "amazon.nova-lite"): {
        "input_per_mtok": 0.06,
        "output_per_mtok": 0.24,
        "cache_read_per_mtok": 0.0,
        "cache_write_per_mtok": 0.0,
        "reasoning_billed_separately": False,
    },
    ("aws.bedrock", "amazon.nova-pro"): {
        "input_per_mtok": 0.80,
        "output_per_mtok": 3.20,
        "cache_read_per_mtok": 0.0,
        "cache_write_per_mtok": 0.0,
        "reasoning_billed_separately": False,
    },
    ("aws.bedrock", "amazon.titan-text"): {
        "input_per_mtok": 0.30,
        "output_per_mtok": 0.40,
        "cache_read_per_mtok": 0.0,
        "cache_write_per_mtok": 0.0,
        "reasoning_billed_separately": False,
    },
}


def get_price(provider: str, model: str) -> Optional[dict]:
    """Return the price entry for provider+model, matching by longest prefix.
    Returns None if no entry found — callers should set price_missing=True in that case.
    """
    if not model:
        return None
    exact = (provider, model)
    if exact in _DEFAULT_CATALOG:
        return _DEFAULT_CATALOG[exact]
    # Longest-prefix match so 'anthropic.claude-3-haiku-20240307-v1:0' matches
    best_key: tuple | None = None
    best_len = 0
    for (p, m_prefix) in _DEFAULT_CATALOG:
        if p == provider and model.startswith(m_prefix) and len(m_prefix) > best_len:
            best_key = (p, m_prefix)
            best_len = len(m_prefix)
    return _DEFAULT_CATALOG[best_key] if best_key else None


def normalise_usage(raw_usage: dict) -> dict:
    """
    Normalise a raw provider usage object into the schema's token columns (section 4.2).
    Rule: input_tokens stores UNCACHED input only (cache is always separate).

    Uses the Strands total-vs-parts test to detect whether a provider folds cache into input:
    if inputTokens + outputTokens < totalTokens, cache tokens appear folded into inputTokens;
    we subtract cache_read to recover the uncached count.

    # [U] — verify: whether MAF input_token_count and Gemini prompt_token_count fold cache
    # into their input field — test per provider before relying on this in production.
    """
    input_t = int(raw_usage.get("inputTokens", 0) or 0)
    output_t = int(raw_usage.get("outputTokens", 0) or 0)
    total_t = int(raw_usage.get("totalTokens", 0) or 0)
    cache_read = int(raw_usage.get("cacheReadInputTokens", 0) or 0)
    cache_write = int(raw_usage.get("cacheWriteInputTokens", 0) or 0)
    reasoning = int(raw_usage.get("reasoningTokens", 0) or 0)

    if total_t > 0 and (input_t + output_t) < total_t:
        # Cache appears to be folded into inputTokens — separate it out.
        input_t = max(0, input_t - cache_read)

    return {
        "input_tokens": max(0, input_t),
        "output_tokens": max(0, output_t),
        "cache_read_tokens": max(0, cache_read),
        "cache_write_tokens": max(0, cache_write),
        "reasoning_tokens": max(0, reasoning),
    }


def compute_token_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    reasoning_tokens: int,
    price: dict,
) -> float:
    """Calculate token cost in USD from normalised counts and a price entry (section 4.1)."""
    cost = (
        input_tokens * price["input_per_mtok"]
        + cache_read_tokens * price["cache_read_per_mtok"]
        + cache_write_tokens * price["cache_write_per_mtok"]
        + output_tokens * price["output_per_mtok"]
    )
    if price.get("reasoning_billed_separately"):
        cost += reasoning_tokens * price["output_per_mtok"]
    return cost / 1_000_000.0
