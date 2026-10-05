"""
tokenomics_capture/strands_adapter.py
Optional Strands Agents SDK adapter for the tokenomics capture module.

WIRE IN alongside bedrock_hook.py when AWS Strands Agents (strands-agents package,
harness-sdk monorepo) is the orchestration layer running inside the AgentCore entrypoint.
The two adapters capture DIFFERENT things and are complementary — do not choose one:
  - bedrock_hook.py    → per-call Bedrock token counts (guaranteed, via botocore)
  - strands_adapter.py → run-level context tagging: framework='strands', agent name,
                         user_id from invocation_state, and run-level usage from Strands
                         metrics (the authoritative Strands view of the whole invocation)

API note (harness-sdk vs old strands-agents 1.57.1):
  - Old 1.57.1: AfterModelCallEvent had stop_response.message["metadata"]["usage"] for
    per-call usage. That path is GONE in the harness-sdk monorepo [S checked 2026-10-03].
  - New harness-sdk: AfterModelCallEvent.stop_response is a ModelStopResponse dataclass
    with only {message, stop_reason} — no usage. [S]
  - Per-invocation usage in new SDK: event.result.metrics.latest_agent_invocation.usage [S]
    Do NOT use accumulated_usage — it accumulates across ALL agent(__call__) invocations on
    the same Agent instance (companion doc section 6.1 correction). [S][T]
  - Per-cycle (per-LLM-call) usage: metrics.agent_invocations[-1].cycles[-1].usage [S]
    This fires after the AfterInvocationEvent; do not try to read it in AfterModelCallEvent.
"""
import logging

from .bedrock_hook import RUN_CTX

logger = logging.getLogger(__name__)

try:
    from strands.hooks import HookProvider, HookRegistry
    from strands.hooks.events import AfterInvocationEvent, BeforeInvocationEvent

    _STRANDS_AVAILABLE = True
except ImportError:
    _STRANDS_AVAILABLE = False
    HookProvider = object  # fallback base so the class definition below doesn't fail


class StrandsTokenMeter(HookProvider):
    """Strands HookProvider that enriches tokenomics events with Strands-native context.

    Usage:
        import boto3
        from strands import Agent
        from strands.models.bedrock import BedrockModel
        from tokenomics_capture import create_meter, register_bedrock_token_meter
        from tokenomics_capture.strands_adapter import StrandsTokenMeter

        meter = create_meter()
        bedrock_client = boto3.client("bedrock-runtime")
        register_bedrock_token_meter(bedrock_client, meter)   # per-call capture

        agent = Agent(
            model=BedrockModel(model_id="anthropic.claude-3-haiku-20240307-v1:0"),
            hooks=[StrandsTokenMeter(meter)],                 # run-level context
        )
        agent("Hello", invocation_state={"user_id": "alice"})

    What this adds on top of bedrock_hook.py:
      - framework='strands' on every event for this invocation (via RUN_CTX)
      - agent_name from event.agent.name
      - user_id from invocation_state
      - A run-level UsageEvent with Strands-authoritative per-invocation totals
    """

    def __init__(self, meter):
        if not _STRANDS_AVAILABLE:
            raise ImportError(
                "strands-agents is not installed. "
                "Install it with: pip install strands-agents"
            )
        self._meter = meter

    def register_hooks(self, registry: "HookRegistry") -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_before_invocation)
        registry.add_callback(AfterInvocationEvent, self._on_after_invocation)

    def _on_before_invocation(self, event: "BeforeInvocationEvent") -> None:
        """Update RUN_CTX with Strands-native context so bedrock_hook picks it up."""
        existing = RUN_CTX.get({})
        RUN_CTX.set({
            **existing,
            "framework": "strands",
            "agent_name": getattr(event.agent, "name", None),
            "user_id": event.invocation_state.get("user_id"),
        })

    def _on_after_invocation(self, event: "AfterInvocationEvent") -> None:
        """Emit a run-level event using per-invocation usage from Strands metrics.

        Use latest_agent_invocation.usage, NOT accumulated_usage.
        accumulated_usage never resets across agent.__call__ invocations on the same
        Agent instance (companion doc section 6.1 correction, verified [S][T] against
        strands-agents 1.57.1; same behaviour confirmed in harness-sdk [S]).
        """
        # event.result is None when invoked from structured_output methods
        inv = None
        if event.result is not None:
            inv = event.result.metrics.latest_agent_invocation

        usage: dict = dict(inv.usage) if inv and inv.usage else {}

        ctx = RUN_CTX.get({})
        self._meter.emit(
            level="run",
            usage=usage,
            framework="strands",
            agent_name=getattr(event.agent, "name", None),
            user_id=event.invocation_state.get("user_id"),
            **{k: v for k, v in ctx.items()
               if k not in ("framework", "agent_name", "user_id")},
        )
