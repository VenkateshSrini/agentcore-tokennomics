"""
app.py — AWS Bedrock AgentCore entry point for tokenomics_capture.

Wires three things at startup:
  1. bedrock_hook   — botocore event hook on bedrock-runtime; captures every LLM call
  2. RunMeterMiddleware — Starlette middleware; tags each request with run_id / session_id
  3. tokenomics_lifespan — starts the async flush loop; drains gracefully on shutdown

Replace the handler body with your agent logic.
This file is intentionally AWS-only; no framework fallback.

Local development
-----------------
  cp .env.example .env          # fill in AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
                                 # AWS_REGION, BEDROCK_MODEL_ID at minimum
  uv run python app.py          # starts on http://127.0.0.1:8080

  curl http://127.0.0.1:8080/ping
  curl -s -X POST http://127.0.0.1:8080/invocations \\
       -H "Content-Type: application/json" \\
       -d '{"prompt": "What is 2 + 2?"}'
"""
# load_dotenv() must run BEFORE tokenomics_capture is imported so that
# TokenomicsConfig.from_env() (called at import time) sees the .env values.
# In production, AgentCore injects env vars before process start, so
# load_dotenv() is a silent no-op there (it never overrides existing vars).
from dotenv import load_dotenv

load_dotenv()  # reads .env if present; ignored silently if absent

import logging
from contextlib import asynccontextmanager

import boto3
from bedrock_agentcore import BedrockAgentCoreApp, RequestContext  # type: ignore[import]

from tokenomics_capture import create_meter, register_bedrock_token_meter
from tokenomics_capture.agentcore_runtime import (
    RunMeterMiddleware,
    session_id_from_context,
    tokenomics_lifespan,
)
from tokenomics_capture.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared infrastructure (module-level so it survives between warm invocations)
# ---------------------------------------------------------------------------
meter = create_meter()
bedrock_client = boto3.client("bedrock-runtime", region_name=settings.bedrock_region)
register_bedrock_token_meter(bedrock_client, meter)


@asynccontextmanager
async def lifespan(app):
    await meter.initialise()
    async with tokenomics_lifespan(meter):
        yield


# ---------------------------------------------------------------------------
# AgentCore application
# ---------------------------------------------------------------------------
app = BedrockAgentCoreApp("agent-tokenomics", lifespan=lifespan)
app.add_middleware(RunMeterMiddleware, meter=meter, config=settings)


@app.entrypoint
def handler(payload: dict, context: RequestContext) -> dict:
    """Replace this body with your agent logic.

    session_id is available from the AgentCore RequestContext.
    Token usage is captured automatically by bedrock_hook for every
    bedrock_client.converse() / bedrock_client.invoke_model() call.
    """
    session = session_id_from_context(context)
    logger.info("run started session=%s payload_keys=%s", session, list(payload.keys()))

    response = bedrock_client.converse(
        modelId=payload.get("model_id", settings.bedrock_model_id),
        messages=[{"role": "user", "content": [{"text": payload.get("prompt", "Hello")}]}],
    )
    answer = response["output"]["message"]["content"][0]["text"]
    return {"response": answer, "session_id": session}


@app.ping
def ping():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Local entry point  —  python app.py  or  uv run python app.py
# BedrockAgentCoreApp.run() auto-detects host:
#   127.0.0.1  when running directly on a laptop (no /.dockerenv)
#   0.0.0.0    when running inside Docker (/.dockerenv present)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(port=8080)
