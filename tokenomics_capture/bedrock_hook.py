"""
tokenomics_capture/bedrock_hook.py
Section 9.3: Framework-agnostic botocore event hook on bedrock-runtime.

Registers on after-call.bedrock-runtime.* to capture token usage from EVERY Bedrock Runtime
API call, regardless of what code made the call — boto3 directly, MAF's Bedrock client,
LangChain, or any other library that goes through botocore. [A][W-39]

One function, one registration call. No per-framework branching here — that belongs in the
framework-specific adapter (maf_adapter.py etc.). This file's only job is:
  "turn a botocore after-call event into one call to meter.emit()"
"""
import contextvars
import logging
import uuid

logger = logging.getLogger(__name__)

# Set by RunMeterMiddleware (agentcore_runtime.py) at request-boundary time.
# Safe default ({}) so the hook works outside AgentCore too.
RUN_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("run_ctx", default={})


def register_bedrock_token_meter(client, meter) -> None:
    """Register a botocore after-call hook on a boto3 'bedrock-runtime' client.

    Args:
        client: a boto3 client created with boto3.client('bedrock-runtime').
        meter:  a Meter instance whose emit() will be called for each Bedrock call.

    Call this once per client at application startup, before any Bedrock calls are made.
    """

    def _after_call(http_response, parsed, model, params, **kw):
        operation = model.name  # e.g. Converse | ConverseStream | InvokeModel | ...

        usage = parsed.get("usage")  # Present for Converse / ConverseStream [D-16]
        estimated = False

        if usage is None:
            # InvokeModel family: token counts come from HTTP response headers [W-32][W-33]
            h: dict = {}
            if http_response is not None and hasattr(http_response, "headers"):
                h = http_response.headers or {}
            usage = {
                "inputTokens": int(h.get("x-amzn-bedrock-input-token-count", 0) or 0),
                "outputTokens": int(h.get("x-amzn-bedrock-output-token-count", 0) or 0),
                "cacheReadInputTokens": int(
                    h.get("x-amzn-bedrock-cache-read-input-token-count", 0) or 0
                ),
                "cacheWriteInputTokens": int(
                    h.get("x-amzn-bedrock-cache-write-input-token-count", 0) or 0
                ),
            }
            if not any(usage.values()):
                # Provider does not populate headers (AI21/Cohere on Bedrock) [W-33]
                # Mark estimated=True; leave counts at 0 rather than guessing.
                # The character-based estimator from section 2.2 lives in buckets.py and
                # only fires where request content is visible — not here. [KISS/DRY]
                estimated = True

        # [U] — verify: ConverseStream / InvokeModelWithResponseStream — usage is very likely
        # only available in the FINAL event of the stream, not at after-call time.  This needs
        # a real streaming test (checklist item 14) before being trusted for billing accuracy.
        if "Stream" in operation:
            logger.debug(
                "tokenomics: streaming op %s — after-call usage may be incomplete [U checklist-14]",
                operation,
            )

        ctx = RUN_CTX.get({})
        meter.emit(
            level="call",
            usage=usage,
            model=params.get("modelId"),
            operation=operation,
            estimated=estimated,
            event_id=str(uuid.uuid4()),
            **ctx,
        )

    client.meta.events.register("after-call.bedrock-runtime.*", _after_call)
