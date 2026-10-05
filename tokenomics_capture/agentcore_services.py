"""
tokenomics_capture/agentcore_services.py
Section 9.6: botocore hooks for AgentCore Memory and Gateway services.
OPTIONAL — only register these if Memory / Gateway are confirmed in use (decision item 8).

All functions here are marked [U] because the exact operation names and response shapes were
NOT inspected against the live AgentCore SDK in the companion document (checklist item 16).
Treat this as a design pattern to validate, not tested code.
"""
import logging

logger = logging.getLogger(__name__)


def register_memory_token_meter(client, meter) -> None:
    """Register a botocore after-call hook on a 'bedrock-agentcore' Memory client.
    Tags Memory retrieval calls as bucket='retrieval' per section 2.2/9.6.

    Args:
        client: boto3 client created with boto3.client('bedrock-agentcore').
        meter:  Meter instance whose emit() will be called.

    # [U] — verify: operation names for AgentCore Memory (e.g. 'RetrieveMemoryRecords')
    # against the live SDK (checklist item 16). The service name 'bedrock-agentcore' is also
    # unverified — check the boto3 service endpoint name.
    """
    def _after_call(http_response, parsed, model, params, **kw):
        operation = model.name
        # [U] — verify: usage/token fields in Memory response, if any
        meter.emit(
            level="call",
            operation=operation,
            details={"bucket": "retrieval", "service": "agentcore_memory"},
        )

    client.meta.events.register("after-call.bedrock-agentcore.*", _after_call)


def register_gateway_token_meter(client, meter, tool_bucket_map: dict) -> None:
    """Register a botocore after-call hook on a 'bedrock-agentcore-control' Gateway client.
    Tags Gateway tool calls as bucket='tool' or 'governance' per the tool_bucket_map.

    Args:
        client:          boto3 client created with boto3.client('bedrock-agentcore-control').
        meter:           Meter instance whose emit() will be called.
        tool_bucket_map: app-supplied mapping of tool_name -> bucket label.

    # [U] — verify: service name 'bedrock-agentcore-control', operation names (e.g.
    # 'InvokeTool'), and the parameter key for tool name (checklist item 16).
    """
    def _after_call(http_response, parsed, model, params, **kw):
        operation = model.name
        # [U] — verify: correct parameter key for tool name in Gateway requests
        tool_name: str = params.get("toolName") or params.get("name") or operation
        bucket = tool_bucket_map.get(tool_name, "tool")
        meter.emit(
            level="call",
            operation=operation,
            details={"bucket": bucket, "service": "agentcore_gateway", "tool_name": tool_name},
        )

    client.meta.events.register("after-call.bedrock-agentcore-control.*", _after_call)
