"""
Optional Google ADK plugin adapter for the tokenomics capture module.

Use this ONLY when Google ADK is calling a non-Bedrock model provider such as Gemini.
If ADK connects to Bedrock via LiteLLM or another Bedrock-backed path, bedrock_hook.py
already captures the underlying Bedrock runtime call; wiring both would double-count.

Registration pattern:
    runner = Runner(plugins=[ADKTokenMeter(meter)], ...)

Unknowns [U] to validate against real ADK runs:
  - ADK/Gemini does not appear to expose cache write tokens, so cache_write_tokens=0 here.
  - thoughts_token_count may not exist on every model/version.
  - Streaming partial-vs-final usage behaviour should be verified to confirm that skipping
    partial fragments avoids duplicates without missing the final usage-bearing callback.
"""
import logging

from .bedrock_hook import RUN_CTX

logger = logging.getLogger(__name__)

try:
    from google.adk.plugins.base_plugin import BasePlugin

    _ADK_AVAILABLE = True
except ImportError:
    _ADK_AVAILABLE = False
    BasePlugin = object


class ADKTokenMeter(BasePlugin):
    """Google ADK BasePlugin that observes model usage callbacks and emits token events."""

    def __init__(self, meter, provider: str = "google.gemini"):
        if not _ADK_AVAILABLE:
            raise ImportError(
                "google-adk is not installed. Install it with the google.adk package."
            )
        self._meter = meter
        self._provider = provider

    async def after_model_callback(self, *, callback_context, llm_response):
        try:
            if getattr(llm_response, "partial", None) is True:
                return None

            usage_metadata = getattr(llm_response, "usage_metadata", None)
            if usage_metadata is None:
                return None

            usage = {
                "inputTokens": getattr(usage_metadata, "prompt_token_count", 0) or 0,
                "outputTokens": getattr(usage_metadata, "candidates_token_count", 0) or 0,
                "totalTokens": getattr(usage_metadata, "total_token_count", 0) or 0,
                "cacheReadInputTokens": (
                    getattr(usage_metadata, "cached_content_token_count", 0) or 0
                ),
                "cacheWriteInputTokens": 0,
                "reasoningTokens": getattr(usage_metadata, "thoughts_token_count", 0) or 0,
            }

            # [U] — verify: callback_context.agent_name vs callback_context.agent.name — test against real ADK run
            agent_name = getattr(callback_context, "agent_name", None)
            invocation_id = getattr(callback_context, "invocation_id", None)
            user_id = getattr(callback_context, "user_id", None)
            session_id = getattr(getattr(callback_context, "session", None), "id", None)

            ctx = dict(RUN_CTX.get({}) or {})
            run_id = ctx.pop("run_id", None)
            framework = ctx.pop("framework", None)
            provider = ctx.pop("provider", None)
            existing_agent_name = ctx.pop("agent_name", None)
            existing_user_id = ctx.pop("user_id", None)
            existing_session_id = ctx.pop("session_id", None)

            self._meter.emit(
                level="call",
                usage=usage,
                framework=framework or "adk",
                provider=provider or self._provider,
                agent_name=agent_name or existing_agent_name,
                user_id=user_id or existing_user_id,
                session_id=session_id or existing_session_id,
                run_id=run_id,
                invocation_id=invocation_id,
                **ctx,
            )
        except Exception as exc:
            logger.debug("adk token meter suppressed error: %s", exc)
        return None
