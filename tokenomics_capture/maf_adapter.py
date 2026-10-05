"""
tokenomics_capture/maf_adapter.py
OPTIONAL — only wire in if MAF (Microsoft Agent Framework) is confirmed as the orchestration
layer running inside the AgentCore entrypoint (decision item 9, companion doc section 8).

Build bedrock_hook.py + agentcore_runtime.py first (they are framework-agnostic).
Wire this in once decision item 9 is confirmed.  See companion doc section 6.2 for the
CallMeter / RunMeter implementation pattern.
"""


class CallMeter:
    """MAF ChatMiddleware for per-call token capture.
    Stub: raises NotImplementedError on instantiation until decision item 9 is confirmed.
    Pattern to implement: companion document section 6.2 CallMeter sketch."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "maf_adapter.CallMeter: wire this in once decision item 9 is confirmed "
            "(MAF as orchestration layer). See companion document section 6.2."
        )


class RunMeter:
    """MAF AgentMiddleware for run-level token capture.
    Stub: raises NotImplementedError on instantiation until decision item 9 is confirmed."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "maf_adapter.RunMeter: wire this in once decision item 9 is confirmed "
            "(MAF as orchestration layer). See companion document section 6.2."
        )
