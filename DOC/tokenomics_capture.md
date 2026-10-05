# Agent Tokenomics Capture — Complete Reference

**Module:** `tokenomics_capture`  
**Version:** 0.1.0  
**Python:** ≥ 3.11  
**Hosting target:** Amazon Bedrock AgentCore Runtime (ARM64, port 8080)  
**Persistence:** PostgreSQL (production) or JSON Lines files (development / testing)  
**Companion design doc:** `agent-tokenomics-gap-analysis-and-design-v3.md`

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Module Reference](#3-module-reference)
4. [Database Schema](#4-database-schema)
5. [Framework Integration](#5-framework-integration)
   - 5.1 [AWS Bedrock AgentCore (primary)](#51-aws-bedrock-agentcore-primary)
   - 5.2 [AWS Strands Agents](#52-aws-strands-agents)
   - 5.3 [Google ADK](#53-google-adk)
   - 5.4 [Microsoft Agent Framework (MAF)](#54-microsoft-agent-framework-maf)
6. [Persistence Backends](#6-persistence-backends)
7. [Configuration Reference](#7-configuration-reference)
8. [Deployment Guide](#8-deployment-guide)
9. [Testing Guide](#9-testing-guide)
10. [Production Validation Checklist](#10-production-validation-checklist)
11. [Example Queries](#11-example-queries)

---

## 1. Overview

`tokenomics_capture` is a lightweight, framework-agnostic module that attaches to any Python agent
running on **Amazon Bedrock AgentCore** and records every LLM token consumption event to a
durable store — PostgreSQL in production, JSON Lines on disk for development.

### What it captures

| Category | Metric | Source |
|---|---|---|
| **Per LLM call** | input, output, cache-read, cache-write, reasoning tokens | Bedrock API response / framework hooks |
| **Per agent run** | total tokens, latency, session/run ID | AgentCore session header / middleware |
| **Cost** | Estimated USD cost per call and per run | `price_catalog` table |
| **Bucket attribution** | context, retrieval, tool, coordination, governance (estimated) | `buckets.py` attribution |
| **Data quality** | `estimated=True` flag when token counts are missing | Automatic |

### What it does NOT replace

- **AWS CloudWatch GenAI dashboard** (`AGENT_OBSERVABILITY_ENABLED=true`): fine for real-time
  health monitoring. Cannot do cost-per-accepted-task, per-user spread, or outcome-based analysis.
- **AWS billing console**: source of truth for dollars charged.

`tokenomics_capture` is the FinOps layer: durable, queryable, outcome-linked.

---

## 2. Architecture

```
                          ┌─────────────────────────────────────┐
                          │   Amazon Bedrock AgentCore Runtime   │
                          │   (linux/arm64, port 8080, VPC mode) │
                          │                                       │
    HTTP /invocations ───►│  RunMeterMiddleware (Starlette)      │
                          │   ├─ generates run_id (UUID)          │
                          │   ├─ reads session_id from header     │
                          │   └─ sets RUN_CTX (ContextVar)        │
                          │                                       │
                          │  @app.entrypoint (your agent code)   │
                          │   │                                   │
                          │   └─► boto3 bedrock-runtime client    │
                          │         │                             │
                          │   ◄─────┘  after-call botocore hook  │
                          │   bedrock_hook._after_call()          │
                          │   ├─ reads usage from response        │
                          │   ├─ reads run_id/session from RUN_CTX│
                          │   └─ Meter.emit(level="call", ...)    │
                          │                                       │
    HTTP response ◄───────│  RunMeterMiddleware.finally block     │
                          │   └─ Meter.emit(level="run", ...)     │
                          └─────────────────────────────────────┘
                                         │
                                         │ asyncio.Queue (bounded, 10k)
                                         ▼
                               ┌─────────────────────┐
                               │  Meter.run_flush_loop │
                               │  (background task)    │
                               │  batched write every  │
                               │  2 s or 100 events    │
                               └─────────────────────┘
                                         │
                          ┌──────────────┴──────────────┐
                          │                              │
                   PERSISTENCE_BACKEND=postgresql  PERSISTENCE_BACKEND=json
                          │                              │
                    PgWriter                        JsonWriter
                (SQLAlchemy + asyncpg)          (JSON Lines files)
                    PostgreSQL                    ./data/usage_event.jsonl
```

### DRY contract

**One function builds a `UsageEvent` row**: `Meter._build_event()`.  
**One function writes rows**: `writer.write_batch()`.  
Every adapter (botocore hook, middleware, Strands hook, ADK plugin) calls only `Meter.emit()`.  
No adapter talks to the writer directly.

```
bedrock_hook.py ──┐
agentcore_runtime ──┤── Meter.emit() ──► _build_event() ──► Queue ──► writer.write_batch()
strands_adapter ──┤
adk_adapter ──────┤
maf_adapter ──────┘
```

---

## 3. Module Reference

### `tokenomics_capture/config.py`

Loads all settings from environment variables at import time.

```python
from tokenomics_capture.config import settings, TokenomicsConfig

# Override programmatically (e.g. in tests):
custom = TokenomicsConfig(
    app="my-agent",
    team="platform",
    persistence_backend="json",
    json_data_dir="/tmp/tokens",
    tool_bucket_map={"web_search": "retrieval", "safety_check": "governance"},
)
```

| Attribute | Env var | Default | Notes |
|---|---|---|---|
| `app` | `TOKENOMICS_APP` | `"unset"` | Written to every row |
| `team` | `TOKENOMICS_TEAM` | `None` | Optional |
| `batch_size` | `TOKENOMICS_BATCH_SIZE` | `100` | Events per write |
| `flush_interval_s` | `TOKENOMICS_FLUSH_INTERVAL_S` | `2.0` | Seconds between flushes |
| `db_dsn` | `NAIE_DB_DSN` | `None` | Required for postgresql backend |
| `persistence_backend` | `PERSISTENCE_BACKEND` | `"json"` | `"postgresql"` or `"json"` |
| `json_data_dir` | `JSON_DATA_DIR` | `"./data"` | JSON backend output dir |
| `tool_bucket_map` | — | `{}` | Set in code; tool_name → bucket label |

---

### `tokenomics_capture/pricing.py`

**Single source of truth** for price lookups and token normalisation.

```python
from tokenomics_capture.pricing import get_price, normalise_usage, compute_token_cost

# Look up price for a model
price = get_price("aws.bedrock", "anthropic.claude-3-haiku-20240307-v1:0")
# → {"input_per_mtok": 0.25, "output_per_mtok": 1.25, ...}

# Normalise a raw usage dict (converts provider-specific keys, applies total-vs-parts test)
normalised = normalise_usage({"inputTokens": 80, "outputTokens": 40, "totalTokens": 120,
                              "cacheReadInputTokens": 20})
# → {"input_tokens": 60, "output_tokens": 40, "cache_read_tokens": 20, ...}

# Compute cost
cost_usd = compute_token_cost(60, 40, 20, 0, 0, price)
```

**Token normalisation rule (section 4.2):** `input_tokens` is stored as *uncached input only*.
Strands' `_total_prompt_tokens` logic: if `inputTokens + outputTokens == totalTokens`, cache is
already excluded; otherwise subtract `cacheReadInputTokens` from `inputTokens`.

> **[U]** Whether MAF `input_token_count` and Gemini `prompt_token_count` fold cache into input
> varies by provider — test per provider before trusting costs.

---

### `tokenomics_capture/buckets.py`

The Splunk six-bucket attribution algorithm (section 2.2 of the design doc). Called **only** from
adapters that see the request body (currently `maf_adapter.py` once wired in).

```python
from tokenomics_capture.buckets import attribute_buckets

result = attribute_buckets(
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the weather in Seattle?"},
        {"role": "tool", "content": "Sunny, 68°F", "tool_name": "weather_api"},
    ],
    tool_bucket_map={"weather_api": "retrieval"},
    measured_input=500,   # from the provider response, used to calibrate
)
# → {"est_context": 370, "est_retrieval": 130, "est_tool": 0, ..., "bucket_method": "estimated"}
```

All bucket values are proportionally calibrated to `measured_input` so they sum exactly to it.
Mark `bucket_method = "estimated"` always — these are derived, not measured.

**Bucket definitions:**

| Segment in the request | Bucket |
|---|---|
| System prompt, safety policy, conversation history | `context` |
| Tool/function schemas | `context` (schema overhead) |
| Tool results from retrieval tools | `retrieval` |
| Other tool results | `tool` |
| Messages from another agent, orchestrator role | `coordination` |
| Calls tagged as judge/guardrail/evaluator | `governance` |

---

### `tokenomics_capture/meter.py`

The **one DRY entry point** for all token events. No adapter builds rows or writes to storage directly.

```python
from tokenomics_capture.meter import Meter, UsageEvent

meter = Meter(writer=writer, config=config)

# Non-blocking; catches all exceptions; never raises into the request path
meter.emit(
    level="call",               # "call" | "run"
    usage={"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
    model="anthropic.claude-3-haiku-20240307-v1:0",
    run_id="uuid-here",
    session_id="session-id",
    framework="strands",
    hosting="agentcore_runtime",
    estimated=False,
    agent_name="my-agent",
    user_id="hashed-user-id",
)

# Must be started in lifespan; drains on CancelledError (shutdown)
asyncio.create_task(meter.run_flush_loop())

# Initialise backing storage (creates tables/dirs)
await meter.initialise()
```

**`UsageEvent` dataclass fields:**

| Field | Type | Description |
|---|---|---|
| `event_id` | `str` | UUID (auto-generated) |
| `ts` | `float` | Unix timestamp |
| `level` | `str` | `"call"` or `"run"` |
| `framework` | `str` | `"raw"`, `"strands"`, `"adk"`, `"maf"` |
| `hosting` | `str` | `"agentcore_runtime"`, `"local"`, etc. |
| `run_id` | `str?` | Set by `RunMeterMiddleware` |
| `session_id` | `str?` | From AgentCore session header |
| `app` | `str` | From `TOKENOMICS_APP` |
| `team` | `str?` | From `TOKENOMICS_TEAM` |
| `user_id` | `str?` | Hash before storing |
| `agent_name` | `str?` | From framework hook / adapter |
| `provider` | `str` | `"aws.bedrock"`, `"google.gemini"`, etc. |
| `model` | `str?` | Model ID |
| `input_tokens` | `int` | Uncached input tokens |
| `cache_read_tokens` | `int` | Cache-read tokens |
| `cache_write_tokens` | `int` | Cache-write tokens |
| `output_tokens` | `int` | Output tokens |
| `reasoning_tokens` | `int` | Reasoning/thinking tokens |
| `estimated` | `bool` | True when provider omits token counts |
| `details` | `dict` | JSONB overflow: raw_usage, operation, buckets, etc. |

---

### `tokenomics_capture/pg_writer.py`

SQLAlchemy async writer. Works with PostgreSQL (asyncpg driver) and SQLite (aiosqlite, for tests).

```python
from tokenomics_capture.pg_writer import PgWriter

writer = PgWriter("postgresql://user:pass@host:5432/naiedb")
await writer.initialise()   # CREATE TABLE IF NOT EXISTS (dev/test convenience)
await writer.write_batch(events)
await writer.close()
```

Also exports:
- `metadata` — SQLAlchemy `MetaData` (all table definitions)
- `usage_event_table`, `run_outcome_table` — `Table` objects for querying in tests
- `event_to_row(ev: UsageEvent) -> dict` — **shared with `JsonWriter`** (DRY)

> **Driver note:** Pass a plain `postgresql://` DSN; the writer auto-upgrades it to
> `postgresql+asyncpg://`. Pass `sqlite+aiosqlite://` directly for tests.

---

### `tokenomics_capture/json_writer.py`

JSON Lines writer for local development and testing. Same `event_to_row()` shape as `PgWriter`.

```python
from tokenomics_capture.json_writer import JsonWriter

writer = JsonWriter("/tmp/tokenomics")
await writer.initialise()   # mkdir -p
await writer.write_batch(events)
```

Output: `<json_data_dir>/usage_event.jsonl` — one JSON object per line.

**Reading back:**
```bash
# Pretty-print all events
jq '.' data/usage_event.jsonl

# Filter by model
jq 'select(.model | startswith("anthropic"))' data/usage_event.jsonl

# Aggregate input tokens
jq -s '[.[].input_tokens] | add' data/usage_event.jsonl
```

```python
import pandas as pd
df = pd.read_json("data/usage_event.jsonl", lines=True)
print(df.groupby("model")["input_tokens"].sum())
```

---

### `tokenomics_capture/bedrock_hook.py`

**Framework-agnostic**, per-call capture via botocore. Fires on every `bedrock-runtime` API call
regardless of what code made the call.

```python
import boto3
from tokenomics_capture.bedrock_hook import register_bedrock_token_meter, RUN_CTX

client = boto3.client("bedrock-runtime", region_name="us-east-1")
register_bedrock_token_meter(client, meter)  # call once at startup
```

**Coverage:**

| API | Token source | Notes |
|---|---|---|
| `Converse` / `ConverseStream` | `response["usage"]` | `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheWriteInputTokens` |
| `InvokeModel` | HTTP headers | `x-amzn-bedrock-input-token-count` etc. |
| `InvokeModel` (AI21/Cohere) | None available | `estimated=True`, counts=0 |
| `ConverseStream` / `InvokeModelWithResponseStream` | After-call, may be incomplete | `[U]` — see checklist item 14 |

**`RUN_CTX`** is a `ContextVar[dict]` set by `RunMeterMiddleware`. The hook reads it to tag every
LLM call with `run_id`, `session_id`, `hosting`, `framework`. Works safely without AgentCore too
(defaults to `{}`).

---

### `tokenomics_capture/agentcore_runtime.py`

Starlette middleware and helpers for AgentCore run-level capture.

```python
from tokenomics_capture.agentcore_runtime import (
    RunMeterMiddleware,
    session_id_from_context,
    tokenomics_lifespan,
)

# In your lifespan
@asynccontextmanager
async def lifespan(app):
    await meter.initialise()
    async with tokenomics_lifespan(meter):  # starts flush loop
        yield

# Add middleware
app.add_middleware(RunMeterMiddleware, meter=meter, config=settings)
```

**Per-request actions of `RunMeterMiddleware`:**
1. Generates `run_id = str(uuid.uuid4())`
2. Reads `session_id` from `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header
3. Pushes both into `RUN_CTX` (ContextVar), so `bedrock_hook` picks them up
4. After response (in `finally`): emits a `level="run"` event with latency

**`session_id_from_context(context)`:** reads `context.session_id` from a native `RequestContext`
for entrypoints that don't have a Starlette `Request` object.  
> `[U]` verify attribute name against SDK [D-27].

---

### `tokenomics_capture/strands_adapter.py`

Optional `HookProvider` for **AWS Strands Agents** (harness-sdk). Complements `bedrock_hook.py` —
do not choose one or the other; they capture different things:

| | `bedrock_hook.py` | `strands_adapter.py` |
|---|---|---|
| What | Per-call Bedrock token counts | Run-level Strands metrics + context tags |
| When | After each Bedrock API call | After each `agent.__call__()` invocation |
| Needs Strands installed? | No | Yes |

```python
from strands import Agent
from strands.models.bedrock import BedrockModel
from tokenomics_capture import create_meter, register_bedrock_token_meter
from tokenomics_capture.strands_adapter import StrandsTokenMeter

meter = create_meter()
bedrock_client = boto3.client("bedrock-runtime")
register_bedrock_token_meter(bedrock_client, meter)   # per-call via botocore

agent = Agent(
    model=BedrockModel(model_id="anthropic.claude-3-haiku-20240307-v1:0"),
    hooks=[StrandsTokenMeter(meter)],                 # run-level via Strands hooks
)
result = agent("What is 2 + 2?", invocation_state={"user_id": "hashed-alice"})
```

**API used (harness-sdk, verified [S] 2026-10-03):**

| Hook | Action |
|---|---|
| `BeforeInvocationEvent` | Update `RUN_CTX` with `framework="strands"`, `agent_name`, `user_id` |
| `AfterInvocationEvent` | Emit `level="run"` with `event.result.metrics.latest_agent_invocation.usage` |

> **Critical:** use `latest_agent_invocation.usage`, **not** `accumulated_usage`.
> `accumulated_usage` never resets across `agent.__call__()` invocations on the same `Agent`
> instance — reading it for a run gives the sum of all previous runs. [S][T]

> **API change from strands-agents 1.57.1:** `AfterModelCallEvent.stop_response` in the
> harness-sdk is a `ModelStopResponse` dataclass with only `{message, stop_reason}`.
> The `stop_response.message["metadata"]["usage"]` path from the old SDK **no longer exists** [S].
> Per-call token counts come from `bedrock_hook.py` (botocore) instead.

---

### `tokenomics_capture/adk_adapter.py`

Optional `BasePlugin` for **Google ADK** (google-adk package). Captures from
`llm_response.usage_metadata` after each model call.

```python
from google.adk.runners import Runner
from tokenomics_capture.adk_adapter import ADKTokenMeter

runner = Runner(
    app=app,
    session_service=session_service,
    plugins=[ADKTokenMeter(meter)],
)
```

> **When to use vs `bedrock_hook.py`:**  
> If ADK calls **Gemini** directly: use `adk_adapter.py` only — botocore hook won't fire.  
> If ADK calls **Bedrock via LiteLLM**: use `bedrock_hook.py` only — both would double-count.  
> If ADK calls **Bedrock natively**: check which path fires and use only one.

**Usage field mapping (Gemini → Bedrock convention):**

| Gemini field | Stored as |
|---|---|
| `prompt_token_count` | `input_tokens` |
| `candidates_token_count` | `output_tokens` |
| `total_token_count` | (used for normalisation) |
| `cached_content_token_count` | `cache_read_tokens` |
| `thoughts_token_count` | `reasoning_tokens` |
| cache write | `0` — not reported by Gemini `[U]` |

**Streaming:** skips responses where `llm_response.partial is True` to avoid double-counting
intermediate chunks. [S confirmed in LlmResponse source]

---

### `tokenomics_capture/maf_adapter.py`

**Stub.** Raises `NotImplementedError` on instantiation. Wire in once decision item 9
(confirm MAF as orchestration layer) is resolved — see section 5.4 below for the
full implementation pattern.

---

### `tokenomics_capture/agentcore_services.py`

**Stub with [U] markers.** botocore hooks for AgentCore Memory and Gateway services.
Enable only if Memory/Gateway are confirmed in use (decision item 8). All operation names
are unverified against the live SDK — see checklist item 16.

---

## 4. Database Schema

The full DDL is in [`sql/schema.sql`](../sql/schema.sql). Run it once against your PostgreSQL:

```bash
psql "$NAIE_DB_DSN" -f sql/schema.sql
```

### Core tables

#### `usage_event` — one row per LLM call or per run

| Column | Type | Notes |
|---|---|---|
| `event_id` | uuid PK | Auto-generated |
| `ts` | timestamptz | When the call happened |
| `run_id` | text | Groups calls into runs |
| `session_id` | text | AgentCore session |
| `team`, `app` | text | From env vars |
| `user_id` | text | Hash before storing |
| `agent_name` | text | From framework hook |
| `framework` | text | `raw\|strands\|adk\|maf` |
| `hosting` | text | `agentcore_runtime\|ecs\|lambda\|local` |
| `provider`, `model` | text | `aws.bedrock`, model ID |
| `level` | text | `call` or `run` |
| `estimated` | boolean | True if provider omitted counts |
| `input_tokens` | bigint | Uncached input only |
| `cache_read_tokens` | bigint | |
| `cache_write_tokens` | bigint | |
| `output_tokens` | bigint | |
| `reasoning_tokens` | bigint | |
| `details` | jsonb | raw_usage, operation, bucket estimates, latency_ms, etc. |

#### `price_catalog` — token prices, versioned by `effective_from`

Add rows when pricing changes; views pick the latest price ≤ event timestamp automatically.

```sql
INSERT INTO price_catalog (provider, model, effective_from,
    input_per_mtok, cache_read_per_mtok, cache_write_per_mtok, output_per_mtok)
VALUES ('aws.bedrock', 'anthropic.claude-3-haiku-20240307-v1:0', now(),
        0.25, 0.03, 0.30, 1.25);
```

#### `run_outcome` — accepted/rejected signal for token yield (section 4.3)

```sql
INSERT INTO run_outcome (run_id, app, accepted, details)
VALUES ('run-uuid', 'my-app', true,
        '{"eval_score": 0.92, "review_cost": 0.0}');
```

#### `quota_snapshot`, `infra_cost`, `budget` — see `sql/schema.sql`

### Views

| View | Purpose |
|---|---|
| `v_usage_effective` | Deduplicates call vs run rows (prefers call rows) |
| `v_usage_cost` | Joins with price_catalog via LATERAL; computes `token_cost` |
| `v_run_cost` | Rolls up to run level with total tokens and cost |

### JSONB `details` keys (by convention)

| Key | Content |
|---|---|
| `raw_usage` | Provider usage object as received |
| `operation` | Bedrock API name (Converse, InvokeModel, …) |
| `latency_ms` | Request duration (set by RunMeterMiddleware) |
| `est_context`, `est_retrieval`, `est_tool`, `est_coordination`, `est_governance` | Bucket estimates from `buckets.py` |
| `bucket_method` | Always `"estimated"` for attributed buckets |
| `call_seq` | Call order within the run (for context-growth chart) |
| `trace_id`, `span_id`, `invocation_id` | OTel correlation |

---

## 5. Framework Integration

### 5.1 AWS Bedrock AgentCore (primary)

This is the primary deployment target. The minimal wiring uses only `bedrock_hook.py` +
`agentcore_runtime.py`:

```python
# app.py — complete minimal AgentCore entry point
import asyncio
from contextlib import asynccontextmanager
import boto3
from bedrock_agentcore import BedrockAgentCoreApp, RequestContext

from tokenomics_capture import create_meter, register_bedrock_token_meter
from tokenomics_capture.agentcore_runtime import (
    RunMeterMiddleware, session_id_from_context, tokenomics_lifespan
)
from tokenomics_capture.config import settings

meter = create_meter()
bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
register_bedrock_token_meter(bedrock_client, meter)


@asynccontextmanager
async def lifespan(app):
    await meter.initialise()
    async with tokenomics_lifespan(meter):
        yield


app = BedrockAgentCoreApp("my-agent", lifespan=lifespan)
app.add_middleware(RunMeterMiddleware, meter=meter, config=settings)


@app.entrypoint
def handler(payload: dict, context: RequestContext) -> dict:
    session = session_id_from_context(context)

    response = bedrock_client.converse(
        modelId=payload.get("model_id", "anthropic.claude-3-haiku-20240307-v1:0"),
        messages=[{"role": "user", "content": [{"text": payload["prompt"]}]}],
    )
    return {"answer": response["output"]["message"]["content"][0]["text"]}


@app.ping
def ping():
    return {"status": "ok"}
```

**What gets recorded automatically:**
- `level="call"` event for every `bedrock_client.converse(...)` call
- `level="run"` event for each HTTP `/invocations` request with latency
- Both tagged with `run_id`, `session_id`, `hosting="agentcore_runtime"`

---

### 5.2 AWS Strands Agents

Add `StrandsTokenMeter` when Strands is the orchestration layer inside the AgentCore entrypoint.

```python
# Strands inside AgentCore
import boto3
from strands import Agent
from strands.models.bedrock import BedrockModel

from tokenomics_capture import create_meter, register_bedrock_token_meter
from tokenomics_capture.strands_adapter import StrandsTokenMeter
from tokenomics_capture.agentcore_runtime import RunMeterMiddleware, tokenomics_lifespan
from tokenomics_capture.config import settings

from bedrock_agentcore import BedrockAgentCoreApp, RequestContext
from contextlib import asynccontextmanager

meter = create_meter()
bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
register_bedrock_token_meter(bedrock_client, meter)   # per-call token counts


@asynccontextmanager
async def lifespan(app):
    await meter.initialise()
    async with tokenomics_lifespan(meter):
        yield


app = BedrockAgentCoreApp("strands-agent", lifespan=lifespan)
app.add_middleware(RunMeterMiddleware, meter=meter, config=settings)

# Strands agent — StrandsTokenMeter adds framework='strands', agent_name, user_id
strands_agent = Agent(
    model=BedrockModel(model_id="anthropic.claude-3-haiku-20240307-v1:0"),
    hooks=[StrandsTokenMeter(meter)],
)


@app.entrypoint
def handler(payload: dict, context: RequestContext) -> dict:
    user_id = payload.get("user_id")  # hash/pseudonymise before passing
    result = strands_agent(
        payload["prompt"],
        invocation_state={"user_id": user_id},
    )
    return {"answer": str(result)}
```

**Events produced per request:**
1. `level="call"` per LLM call (from `bedrock_hook.py`) — has token counts, `framework="raw"` initially
2. `level="run"` from `RunMeterMiddleware` — latency
3. `level="run"` from `StrandsTokenMeter.AfterInvocationEvent` — Strands-authoritative totals,
   `framework="strands"`, `agent_name`, `user_id`

> **Note:** The call-level events will show `framework="strands"` after `BeforeInvocationEvent`
> updates `RUN_CTX`. The run-level event from RunMeterMiddleware will show `framework="raw"`
> because it fires before Strands gets to set the framework tag. This is by design — the two
> run-level rows are deduplicated by `v_usage_effective` (run rows only used when no call rows
> exist for that run_id).

---

### 5.3 Google ADK

Add `ADKTokenMeter` when Google ADK is the orchestration layer. For Gemini models, this is the
primary capture mechanism — `bedrock_hook.py` will not fire.

```python
# ADK inside AgentCore
import asyncio
from contextlib import asynccontextmanager

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from tokenomics_capture import create_meter
from tokenomics_capture.adk_adapter import ADKTokenMeter
from tokenomics_capture.agentcore_runtime import RunMeterMiddleware, tokenomics_lifespan
from tokenomics_capture.config import settings

from bedrock_agentcore import BedrockAgentCoreApp, RequestContext
from contextlib import asynccontextmanager

meter = create_meter()


@asynccontextmanager
async def lifespan(app):
    await meter.initialise()
    async with tokenomics_lifespan(meter):
        yield


app = BedrockAgentCoreApp("adk-agent", lifespan=lifespan)
app.add_middleware(RunMeterMiddleware, meter=meter, config=settings)

# ADK setup — ADKTokenMeter is registered as a plugin on the Runner
root_agent = LlmAgent(
    name="my_adk_agent",
    model="gemini-2.0-flash",
    instruction="You are a helpful assistant.",
)
session_service = InMemorySessionService()
runner = Runner(
    app=root_agent,                        # or App(name=..., root_agent=...)
    session_service=session_service,
    plugins=[ADKTokenMeter(meter)],        # [S] plugin registration via Runner
)


@app.entrypoint
async def handler(payload: dict, context: RequestContext) -> dict:
    session = await session_service.create_session(
        app_name="adk-agent",
        user_id=payload.get("user_id", "anonymous"),
    )
    events = runner.run_async(
        user_id=payload.get("user_id", "anonymous"),
        session_id=session.id,
        new_message={"role": "user", "parts": [{"text": payload["prompt"]}]},
    )
    final = None
    async for event in events:
        if event.is_final_response():
            final = event
    return {"answer": final.content.parts[0].text if final else ""}
```

**Events produced per LLM call:**
- `level="call"`, `framework="adk"`, `provider="google.gemini"`, full token counts

> **Streaming note:** `ADKTokenMeter` skips events where `llm_response.partial is True`.
> If the real Gemini model only returns usage on a partial chunk and not on the final event,
> those calls would be missed. Test with the real provider. `[U checklist item 14 equivalent]`

> **[U]** If ADK connects to Bedrock via LiteLLM, do NOT add `ADKTokenMeter` — use
> `bedrock_hook.py` instead. Using both would double-count.

---

### 5.4 Microsoft Agent Framework (MAF)

`maf_adapter.py` is currently a **stub** (raises `NotImplementedError`). Wire it in once you
confirm MAF is the orchestration layer (decision item 9).

**Implementation pattern (companion doc section 6.2, confirmed [S][T] against MAF 1.19.0):**

```python
# tokenomics_capture/maf_adapter.py — full implementation (replace the stub)
import contextvars
from agent_framework import AgentMiddleware, ChatMiddleware, AgentContext, ChatContext

from .bedrock_hook import RUN_CTX

USER_CTX: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_ctx", default=None)


class CallMeter(ChatMiddleware):
    """Per-call meter: reads usage from ChatResponse after each LLM call."""

    def __init__(self, meter):
        self._meter = meter

    async def process(self, context: ChatContext, call_next):
        await call_next()
        r = context.result              # ChatResponse (non-streaming)
        ud = r.usage_details if r else None
        if ud is None:
            return
        # MAF field names → Bedrock convention
        usage = {
            "inputTokens":  getattr(ud, "input_token_count", 0) or 0,
            "outputTokens": getattr(ud, "output_token_count", 0) or 0,
            "totalTokens":  getattr(ud, "total_token_count", 0) or 0,
            "cacheReadInputTokens":  getattr(ud, "cache_read_input_token_count", 0) or 0,
            "cacheWriteInputTokens": getattr(ud, "cache_creation_input_token_count", 0) or 0,
            "reasoningTokens":       getattr(ud, "reasoning_output_token_count", 0) or 0,
        }
        ctx = RUN_CTX.get({})
        self._meter.emit(
            level="call",
            usage=usage,
            model=getattr(r, "model", None),
            framework="maf",
            agent_name=getattr(context, "agent", {}).name if hasattr(context, "agent") else None,
            session_id=context.session.session_id if context.session else None,
            user_id=USER_CTX.get(),
            **{k: v for k, v in ctx.items() if k not in ("framework", "agent_name", "user_id", "session_id", "model")},
        )


class RunMeter(AgentMiddleware):
    """Run-level meter: captures user identity and run totals."""

    def __init__(self, meter, get_user_id_fn=None):
        self._meter = meter
        self._get_user_id = get_user_id_fn or (lambda: None)

    async def process(self, context: AgentContext, call_next):
        USER_CTX.set(self._get_user_id())
        await call_next()
        r = context.result
        ud = r.usage_details if r else None
        if ud is None:
            return
        usage = {
            "inputTokens":  getattr(ud, "input_token_count", 0) or 0,
            "outputTokens": getattr(ud, "output_token_count", 0) or 0,
            "totalTokens":  getattr(ud, "total_token_count", 0) or 0,
        }
        self._meter.emit(
            level="run",
            usage=usage,
            framework="maf",
            agent_name=context.agent.name,
            user_id=USER_CTX.get(),
        )
```

**Wiring into AgentCore entry point:**

```python
# MAF inside AgentCore
from agent_framework import Agent
from azure.ai.inference import ChatCompletionsClient  # or boto3 Bedrock client
from tokenomics_capture.maf_adapter import CallMeter, RunMeter
from tokenomics_capture import create_meter, register_bedrock_token_meter

meter = create_meter()

# If MAF uses Bedrock under the hood, also register the botocore hook:
bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
register_bedrock_token_meter(bedrock_client, meter)

maf_agent = Agent(
    client=bedrock_client,
    name="maf-agent",
    middleware=[
        RunMeter(meter, get_user_id_fn=lambda: None),  # outermost first
        CallMeter(meter),
    ],
)


@app.entrypoint
def handler(payload: dict, context: RequestContext) -> dict:
    result = asyncio.run(maf_agent.run(payload["prompt"]))
    return {"answer": str(result)}
```

> **MAF-specific traps [T]:**  
> - `context.metadata` set in `AgentMiddleware` is **NOT visible** in the inner `ChatMiddleware`.
>   Use `USER_CTX` (ContextVar) or session state to pass user_id down.  
> - For streaming, `context.result` is a `ResponseStream`, not `ChatResponse`; use
>   `stream_result_hooks` on `ChatContext` (untested, `[U]`).  
> - Avoid summing `invoke_agent` spans and `chat` spans — they overlap (double-count warning).

---

## 6. Persistence Backends

### Switching backends

Set `PERSISTENCE_BACKEND` environment variable before the process starts:

```bash
# PostgreSQL (production)
export PERSISTENCE_BACKEND=postgresql
export NAIE_DB_DSN="postgresql://user:pass@host:5432/naiedb"

# JSON Lines (development / testing)
export PERSISTENCE_BACKEND=json
export JSON_DATA_DIR=./data
```

Or in code:
```python
from tokenomics_capture import create_meter
from tokenomics_capture.config import TokenomicsConfig

config = TokenomicsConfig(persistence_backend="json", json_data_dir="/tmp/tokens")
meter = create_meter(config)
```

### PostgreSQL backend (`pg_writer.py`)

- Uses **SQLAlchemy 2.x async** with `asyncpg` driver
- One `AsyncEngine` shared across all writes
- `pool_pre_ping=True` for automatic connection recovery
- Batch inserts with `executemany` semantics
- Schema: see `sql/schema.sql`

**Initialisation options:**
1. **Dev/test:** call `await writer.initialise()` (runs `CREATE TABLE IF NOT EXISTS`)
2. **Production:** run `psql "$NAIE_DB_DSN" -f sql/schema.sql` before deploying

### JSON Lines backend (`json_writer.py`)

- Appends to `<json_data_dir>/usage_event.jsonl` — one JSON object per line
- Thread-safe via `asyncio.Lock` per file
- Timestamps serialised as ISO 8601 strings
- No SQL required — useful for local runs and automated test assertions

**Schema equivalence:** `json_writer.py` calls the same `event_to_row()` function as
`pg_writer.py`, so the JSON field names match the SQL column names exactly.

---

## 7. Configuration Reference

### Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `PERSISTENCE_BACKEND` | No | `json` | `postgresql` or `json` |
| `NAIE_DB_DSN` | When postgresql | — | Full PostgreSQL DSN |
| `JSON_DATA_DIR` | No | `./data` | JSON Lines output directory |
| `TOKENOMICS_APP` | Yes | `unset` | App name on every row |
| `TOKENOMICS_TEAM` | No | — | Team name on every row |
| `TOKENOMICS_BATCH_SIZE` | No | `100` | Events per write batch |
| `TOKENOMICS_FLUSH_INTERVAL_S` | No | `2.0` | Queue drain interval (seconds) |
| `AWS_REGION` | Yes | — | Region for boto3 Bedrock client |
| `AGENT_OBSERVABILITY_ENABLED` | No | — | `true` → CloudWatch GenAI dashboard |
| `OTEL_PYTHON_DISTRO` | No | — | `aws_distro` for AgentCore OTEL pipeline |
| `OTEL_PYTHON_CONFIGURATOR` | No | — | `aws_configurator` for AgentCore OTEL |
| `NAIE_DB_DSN` | When postgresql | — | PostgreSQL DSN |

### `tool_bucket_map` — bucket attribution config

Owned by the app team. Tells `buckets.py` how to classify tool results:

```python
settings.tool_bucket_map = {
    "web_search":      "retrieval",   # RAG, vector DB, search
    "vector_lookup":   "retrieval",
    "safety_check":    "governance",  # judge/guardrail/eval
    "human_review":    "governance",
    "send_email":      "tool",        # default for everything else
}
```

---

## 8. Deployment Guide

### Step 1 — Apply database schema

```bash
psql "$NAIE_DB_DSN" -f sql/schema.sql
```

Seed the price catalog (update prices from [AWS Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)
— default values in `pricing.py` are unverified `[U]`):

```sql
INSERT INTO price_catalog (provider, model, effective_from,
    input_per_mtok, cache_read_per_mtok, cache_write_per_mtok, output_per_mtok)
VALUES
    ('aws.bedrock', 'anthropic.claude-3-haiku-20240307-v1:0',
     '2026-01-01', 0.25, 0.03, 0.30, 1.25),
    ('aws.bedrock', 'anthropic.claude-3-5-sonnet-20241022-v2:0',
     '2026-01-01', 3.00, 0.30, 3.75, 15.00),
    ('aws.bedrock', 'amazon.nova-lite-v1:0',
     '2026-01-01', 0.06, 0.00, 0.00, 0.24);
```

### Step 2 — Create IAM execution role

```bash
aws iam create-role \
  --role-name AgentCoreTokenomicsExecutionRole \
  --assume-role-policy-document file://deploy/execution-role-trust-policy.json

aws iam put-role-policy \
  --role-name AgentCoreTokenomicsExecutionRole \
  --policy-name AgentCoreTokenomicsPolicy \
  --policy-document file://deploy/execution-role-policy.json
```

### Step 3 — Build and push container image

```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=us-east-1

# Build for ARM64 (required by AgentCore)
docker buildx build --platform linux/arm64 \
  -t $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/agent-tokenomics:latest \
  -f deploy/Dockerfile --push .
```

### Step 4 — Deploy to AgentCore

```bash
# Using starter toolkit
pip install bedrock-agentcore-starter-toolkit
# Edit deploy/agentcore.yaml — fill VPC IDs, subnets, image URI
agentcore configure --config deploy/agentcore.yaml
agentcore launch
```

Or with raw CLI:
```bash
aws bedrock-agentcore-control create-agent-runtime \
  --agent-runtime-name agent-tokenomics \
  --agent-runtime-artifact "containerConfiguration={imageUri=...}" \
  --execution-role-arn arn:aws:iam::$AWS_ACCOUNT_ID:role/AgentCoreTokenomicsExecutionRole \
  --network-configuration "networkMode=VPC,vpcConfig={subnetIds=[...],securityGroupIds=[...]}" \
  --environment-variables "PERSISTENCE_BACKEND=postgresql,NAIE_DB_DSN=...,TOKENOMICS_APP=my-app"
```

> **`networkMode=VPC` is required** if writing to a private RDS/PostgreSQL instance.
> `PUBLIC` mode has no route to a VPC-private database [D-30].

### Step 5 — Smoke test

```bash
# Local
curl http://localhost:8080/ping

# Remote (after agentcore launch)
agentcore invoke '{"prompt": "Hello, what is 2+2?"}'
```

---

## 9. Testing Guide

### Running the test suite

```bash
# Install all dependencies including test extras
pip install -e ".[test]"

# Run all 38+ tests (no live AWS, no PostgreSQL required)
pytest tests/ -v

# Run a specific module
pytest tests/test_bedrock_hook.py -v
pytest tests/test_strands_adapter.py -v
pytest tests/test_adk_adapter.py -v
```

### Test strategy

| Test file | What it tests | Backend |
|---|---|---|
| `test_bedrock_hook.py` | botocore hook, all API variants | Fake event system |
| `test_agentcore_runtime.py` | Middleware, ContextVar, session header | Starlette TestClient |
| `test_buckets.py` | Bucket attribution arithmetic | Pure Python |
| `test_pg_writer.py` | PgWriter (SQLite), JsonWriter | aiosqlite in-memory |
| `test_strands_adapter.py` | Strands hook stubs | Stub classes |
| `test_adk_adapter.py` | ADK plugin stubs | Stub classes |

### Acceptance criteria (from build prompt)

- [ ] `tokenomics_capture/` imports with no required dep on MAF, Strands, or ADK
- [ ] A bare `boto3 bedrock-runtime` client produces correct `usage_event` rows for Converse
- [ ] Every adapter calls `Meter.emit()` — no direct writer/INSERT calls outside `pg_writer.py`
- [ ] No `estimated=False` events with zero token counts (unless streaming, which is `[U]`)
- [ ] `attribute_buckets()` totals always equal `measured_input` after calibration

---

## 10. Production Validation Checklist

After the first real AgentCore deployment, verify these items (from companion doc section 7):

| # | What to verify | How |
|---|---|---|
| 13 | `after-call.bedrock-runtime.*` fires for your specific client (MAF's client, Strands' client, raw boto3) | Query `usage_event WHERE estimated=false AND level='call'` — should have rows |
| 14 | Streaming usage (`ConverseStream`, `InvokeModelWithResponseStream`) is captured | Check `usage_event WHERE details->>'operation' LIKE '%Stream%'` — are tokens non-zero? |
| 15 | `RUN_CTX` ContextVar is readable from inside sync `def entrypoint(payload, context)` | Log `RUN_CTX.get({})` inside handler — should contain `run_id` |
| 16 | Memory/Gateway operation names (if using those services) | Check `details->>'service'` column for 'agentcore_memory' / 'agentcore_gateway' rows |
| 17 | Token prices match current AWS pricing page | Cross-check `SELECT sum(token_cost) FROM v_usage_cost` with your AWS bill |

---

## 11. Example Queries

```sql
-- Spend by team/app/model per day
SELECT date_trunc('day', ts) AS day, team, app, model,
       sum(token_cost) AS cost_usd
FROM v_usage_cost
GROUP BY 1, 2, 3, 4
ORDER BY 1 DESC;

-- Per-user cost spread (p50 / p95)
SELECT user_id,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY token_cost) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY token_cost) AS p95
FROM v_run_cost
GROUP BY user_id;

-- Context growth: input tokens by call sequence (shows compounding re-send cost)
SELECT (details->>'call_seq')::int AS call_seq,
       avg(input_tokens + cache_read_tokens) AS avg_input
FROM usage_event
WHERE level = 'call' AND details ? 'call_seq'
GROUP BY 1 ORDER BY 1;

-- Cost per accepted task (Splunk formula)
SELECT sum(r.token_cost + coalesce((o.details->>'review_cost')::numeric, 0))
       / nullif(count(*) FILTER (WHERE o.accepted), 0) AS cost_per_accepted_task
FROM v_run_cost r
JOIN run_outcome o USING (run_id);

-- Data quality: what fraction of events are estimated?
SELECT avg(estimated::int)     AS estimated_fraction,
       avg(price_missing::int) AS unpriced_fraction
FROM v_usage_cost;

-- Token yield (Splunk): accepted sessions per million tokens
SELECT count(*) FILTER (WHERE o.accepted) * 1e6
       / nullif(sum(r.total_tokens), 0) AS token_yield
FROM v_run_cost r
JOIN run_outcome o USING (run_id);

-- Framework breakdown
SELECT framework, hosting, count(*) AS calls,
       sum(input_tokens + output_tokens) AS total_tokens
FROM usage_event
WHERE level = 'call'
GROUP BY 1, 2;
```

---

*Last updated: 2026-10-03. Design source: `agent-tokenomics-gap-analysis-and-design-v3.md`.*
