# Build Prompt: Generic Agent Tokenomics Capture Module for Amazon Bedrock AgentCore



**Use this file as the prompt/spec for a coding agent (Claude Code, Claude in a terminal, or a human engineer).**

Paste it in as-is, or hand it the whole repo plus this file and say "implement this."



**Companion document:** `agent-tokenomics-gap-analysis-and-design-v3.md`, section 9. Read that section

first — it contains the evidence, citations, and the [S]/[D]/[W]/[A]/[U] labels for every design decision

referenced below. This prompt file turns that design into a buildable task list; it does not re-derive it.



---



## 0. Ground rules for whoever builds this



1. **Do not invent AWS or framework APIs.** Every API used below is cited in section 9 of the companion

   document with a [D] (docs) or [W] (third-party, weaker) label. Where this prompt says `[U]` (unverified),

   write the code so it is easy to confirm or fix after one real run — do not silently assume the unverified

   behaviour is correct.

2. **Framework-agnostic first.** The agent being metered may run MAF, raw `boto3`, both, or something else

   inside the AgentCore entrypoint. The module must work if the only thing present is a `bedrock-runtime`

   client — MAF integration is an optional *addition*, not a prerequisite.

3. **Never block the request path on telemetry.** A slow or failed write to PostgreSQL must never fail or

   visibly delay the agent's response. Buffer and flush asynchronously; log and swallow write errors.

4. **DRY — Don't Repeat Yourself.** There is exactly **one** function that turns a raw usage object into a

   `usage_event` row and queues it (`emit()` in `meter.py`). The `botocore` hook, the Starlette middleware,

   the MAF middleware, and the Memory/Gateway taggers are all thin adapters that extract fields from their

   respective event shapes and call that one function — they must not each implement their own row-building,

   their own PostgreSQL write, or their own cost-bucket logic. The bucket-attribution algorithm (section 2.2

   of the companion doc) is implemented **once**, in `buckets.py`, and called from wherever a request body is

   visible (currently only the MAF middleware) — do not re-derive it inline anywhere else. Pricing lookups,

   token normalisation (section 4.2), and the estimated-vs-measured flag are each implemented once and

   imported, not copy-pasted between the `botocore` hook and any framework-specific adapter.

5. **KISS — Keep It Simple.** Build exactly what sections 9.3-9.7 below ask for, in the order given, and stop.

   Concretely: no new abstraction layer, plugin system, or config DSL beyond the tool-name-to-bucket mapping

   that section 2.2 already calls for; no speculative support for providers, frameworks, or AgentCore

   components not named in this prompt; no premature batching/queueing sophistication beyond "a bounded

   in-memory queue with a background flush task" (section 1). If a requirement below can be met with a

   15-line function, write the 15-line function, not a class hierarchy. Prefer one obvious code path over a

   configurable one when both satisfy the acceptance criteria. If something feels like it needs a second

   abstraction to be "clean," that is a signal to re-read the requirement and cut, not to build the

   abstraction.

6. **Reuse the existing PostgreSQL schema** from the companion document (section 3.2) plus the additive

   deltas in section 9.7. Do not design a new schema.

7. **Label your own code the same way the design doc does.** Comments on anything you couldn't verify

   against a real AWS account/model should say `# [U] — verify: <what to check>`, following checklist items

   13-17 in the companion document's section 7.



---



## 1. Module layout to produce



```

tokenomics_capture/

  __init__.py

  meter.py            # the ONE emit() function + bounded async queue + batching flush to PostgreSQL

  buckets.py           # section 2.2 bucket-attribution algorithm — ported once, called by any adapter

                        # that can see request content (currently: maf_adapter.py)

  bedrock_hook.py      # section 9.3 — generic botocore hook on bedrock-runtime (framework-agnostic)

  agentcore_runtime.py # section 9.4 — Starlette middleware + RequestContext helpers, run-level rows

  maf_adapter.py        # section 6.2 pattern, OPTIONAL — only wired in if MAF is the orchestration layer

  agentcore_services.py # section 9.6, OPTIONAL — Memory/Gateway botocore hooks for governance/retrieval tags

  pricing.py            # price_catalog lookups + token normalisation (section 4.2) — single source of truth

  pg_writer.py          # batched INSERT into usage_event / run_outcome, using the schema as-is

  config.py             # tool-name-to-bucket map, app/team identifiers, DSN, batch size/interval

tests/

  test_bedrock_hook.py        # fake bedrock-runtime client with a stubbed botocore event system

  test_agentcore_runtime.py   # Starlette TestClient against a minimal BedrockAgentCoreApp

  test_buckets.py              # section 2.2 calibration arithmetic

  test_pg_writer.py            # against a local/ephemeral PostgreSQL (pgserver or testcontainers)

deploy/

  Dockerfile            # ARM64, see section 5 below

  requirements.txt

  execution-role-policy.json

  execution-role-trust-policy.json

  agentcore.yaml         # starter-toolkit config, OR the equivalent create_agent_runtime() call

  README.md              # generated deployment runbook — see section 6 below for required contents

```



If MAF is confirmed as the orchestration layer (decision item 9 in the companion doc's section 8),

wire `maf_adapter.py`'s `ChatMiddleware`/`AgentMiddleware` in alongside `bedrock_hook.py` — do not choose

one over the other; they capture different things (request content/buckets vs. guaranteed measured totals).

If MAF is not confirmed, build `bedrock_hook.py` and `agentcore_runtime.py` first and leave `maf_adapter.py`

as a stub with a clear `NotImplementedError("wire this in once decision item 9 is confirmed")`.



---



## 2. `meter.py` — the one place a `usage_event` row gets built



```python

import asyncio, time, uuid

from dataclasses import dataclass, field



@dataclass

class UsageEvent:

    event_id: str

    ts: float

    level: str                 # 'call' | 'run'

    framework: str              # 'maf' | 'raw' | 'strands' | 'adk'  (section 9.7)

    hosting: str                # 'agentcore_runtime' | 'local' | ... (section 9.7)

    run_id: str | None = None

    session_id: str | None = None

    app: str = "unset"

    team: str | None = None

    user_id: str | None = None   # hash/pseudonymise before this point — see companion doc section 5

    agent_name: str | None = None

    provider: str = "aws.bedrock"

    model: str | None = None

    input_tokens: int = 0

    cache_read_tokens: int = 0

    cache_write_tokens: int = 0

    output_tokens: int = 0

    reasoning_tokens: int = 0

    estimated: bool = False

    details: dict = field(default_factory=dict)



class Meter:

    """Single entry point. Every adapter (bedrock_hook, agentcore_runtime, maf_adapter,

    agentcore_services) calls emit() and nothing else — no adapter talks to PostgreSQL directly.

    This is the DRY boundary: one queue, one batching flush, one row shape."""



    def __init__(self, pg_writer, batch_size=100, flush_interval_s=2.0, maxsize=10_000):

        self._queue: asyncio.Queue[UsageEvent] = asyncio.Queue(maxsize=maxsize)

        self._pg_writer = pg_writer

        self._batch_size = batch_size

        self._flush_interval_s = flush_interval_s



    def emit(self, *, level, usage=None, **tags) -> None:

        """Non-blocking. Normalises `usage` (whatever shape the caller has — Converse body,

        InvokeModel headers, a MAF UsageDetails object) into UsageEvent fields and queues it.

        Never raises into the caller's request path: catches and logs, then returns."""

        try:

            ev = self._build_event(level, usage, tags)

            self._queue.put_nowait(ev)

        except asyncio.QueueFull:

            # KISS: drop-oldest-and-log is enough. Do not build a priority queue for this.

            pass

        except Exception:

            pass  # telemetry must never break the agent — log at debug, not error



    def _build_event(self, level, usage, tags) -> UsageEvent:

        # Token normalisation (section 4.2) belongs in pricing.py, imported here, not reimplemented.

        ...



    async def run_flush_loop(self):

        """Background task: drains the queue in batches and calls pg_writer.write_batch().

        Start this once, from the AgentCore app's lifespan (see agentcore_runtime.py)."""

        ...

```



Everything downstream (the four adapters) must call `meter.emit(...)` — never construct a `UsageEvent`

or touch `pg_writer` directly. That is the DRY contract for this module; enforce it in code review, not

just in this prompt.



---



## 3. `bedrock_hook.py` — section 9.3, the generic capture point



Implement exactly the pattern from the companion document section 9.3:



- Register on `after-call.bedrock-runtime.*` via `client.meta.events.register(...)`.

- Pull usage from `parsed["usage"]` for `Converse`/`ConverseStream` **[D-16]**.

- Fall back to the four `x-amzn-bedrock-*-token-count` HTTP headers for `InvokeModel`/

  `InvokeModelWithResponseStream` **[W-32][W-33]**; if none of the four headers are present, set

  `estimated=True` and leave token counts at 0 rather than guessing — do not implement the

  character-based estimator from section 2.2 inside this file (that belongs in `buckets.py`, and only

  fires where request content is visible, i.e. from `maf_adapter.py`, not here).

- Pull `run_id`/`session_id`/`app`/`team` from the `ContextVar` set by `agentcore_runtime.py`

  (`RUN_CTX.get({})`), with safe defaults if it is empty (e.g., the hook used outside AgentCore).

- One function, one registration call, no per-framework branching inside this file — if you find

  yourself adding an `if framework == "maf":` branch here, that logic belongs in `maf_adapter.py` instead

  (KISS/DRY: this file's only job is "turn a botocore after-call event into a call to `meter.emit`").

- `[U]`: mark the `ConverseStream`/`InvokeModelWithResponseStream` path with a comment that usage may only

  be available on the final stream event, not at `after-call` time, and that this needs a real streaming

  test (checklist item 14) before being trusted for billed accuracy on streaming calls.



**Acceptance test:** a fake `bedrock-runtime` client (stub `meta.events` with a minimal pub/sub) that

returns a fixed `Converse` response with `usage` and a fixed `InvokeModel` response with the four headers

and nothing in the body; assert `meter.emit` is called once per call with the right token counts and

`estimated=False`, and that an `InvokeModel` response with none of the four headers present results in

`estimated=True`.



---



## 4. `agentcore_runtime.py` — section 9.4, run-level capture



- `RunMeterMiddleware(BaseHTTPMiddleware)` exactly as sketched in section 9.4 of the companion doc:

  generate `run_id`, read `session_id` from the `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header

  **[D-27]**, set the `ContextVar`, time the request, call `meter.emit(level="run", ...)` in a `finally`

  block so a run row is written even if the handler raises.

- Wire the `Meter.run_flush_loop()` background task into `BedrockAgentCoreApp`'s `lifespan=` so it starts

  on container startup and drains on shutdown — reuse the `contextlib.asynccontextmanager` pattern AWS

  documents for resource management **[W-34]**; do not build a second mechanism for starting background

  tasks when `@app.async_task` or `lifespan` already cover it.

- Export a small helper, `session_id_from_context(context: RequestContext) -> str | None`, for code paths

  that only have the native `RequestContext` and not the Starlette `Request` (entrypoints that don't need

  true request-boundary timing can use this instead of the middleware) **[D-27]**.

- `[U]`: add an explicit test (see `tests/test_agentcore_runtime.py`) that proves the `ContextVar` set in

  the middleware is readable from inside both a sync `def entrypoint(payload, context)` and an

  `async def entrypoint(...)` — this is checklist item 15 in the companion document, and the generated

  code must not silently assume one case works because the other does.



---



## 5. `maf_adapter.py` (only if MAF is confirmed in use) and `agentcore_services.py` (only if Memory/Gateway in use)



- `maf_adapter.py`: port the `CallMeter`/`RunMeter` `ChatMiddleware`/`AgentMiddleware` pattern from the

  companion document's **section 6.2** verbatim in structure (do not redesign it) — its only new

  responsibility here is calling the shared `buckets.py` attribution function on `context.messages`

  before the call, then calling `meter.emit(level="call", ...)` after, instead of whatever ad hoc `emit()`

  the original section 6.2 sketch used.

- `agentcore_services.py`: register `botocore` hooks on `bedrock-agentcore`/`bedrock-agentcore-control`

  clients the same way `bedrock_hook.py` does for `bedrock-runtime`, tagging Memory retrieval calls as

  `bucket='retrieval'` and Gateway tool calls as `bucket='tool'` or `bucket='governance'` per the

  tool-name-to-bucket map in `config.py` (section 2.2/2.4 of the companion doc). Mark every function here

  `[U]` per checklist item 16 — the exact operation names were not confirmed against the live SDK in the

  companion document and must be checked once Memory/Gateway are actually turned on (decision item 8).



---



## 6. Deployment artifacts the build must also produce



Generate these as real files, not just talk about them in a chat reply — the companion document's

section 9.8/9.9 and the AWS sources below are the source of truth for every value:



1. **`deploy/Dockerfile`** — ARM64 base image, port 8080, matching the documented AgentCore Runtime

   requirement **[D-26]**:

   ```dockerfile

   FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python3.11-bookworm-slim

   WORKDIR /app

   COPY pyproject.toml uv.lock ./

   RUN uv sync --frozen --no-cache

   COPY . .

   EXPOSE 8080

   CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]

   ```

   Build/test commands to include in the README:

   ```bash

   docker buildx build --platform linux/arm64 -t agent-tokenomics:arm64 --load .

   docker run --platform linux/arm64 -p 8080:8080 \

     -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN -e AWS_REGION \

     -e NAIE_DB_DSN agent-tokenomics:arm64

   curl http://localhost:8080/ping

   ```



2. **`deploy/execution-role-policy.json`** / **`execution-role-trust-policy.json`** — the minimum set

   AWS documents for an AgentCore Runtime execution role **[W-36]**: ECR image pull

   (`GetAuthorizationToken`, `BatchGetImage`, `GetDownloadUrlForLayer`), CloudWatch Logs

   (`CreateLogGroup`, `CreateLogStream`, `PutLogEvents`), X-Ray (`PutTraceSegments`,

   `PutTelemetryRecords`), CloudWatch Metrics (`PutMetricData` scoped to the `bedrock-agentcore`

   namespace), AgentCore workload identity (`bedrock-agentcore:GetWorkloadAccessToken`,

   `GetWorkloadAccessTokenForJWT`, `GetWorkloadAccessTokenForUserId`), and

   `bedrock:InvokeModel`/`InvokeModelWithResponseStream`/`Converse`/`ConverseStream` scoped to the model

   ARNs actually in use. Add Memory/Gateway actions only if decision item 8 says they're in scope — KISS:

   do not pre-grant permissions for AgentCore services that are not being used yet.



3. **`deploy/agentcore.yaml`** (or the equivalent `boto3` `create_agent_runtime()` call) — must set

   `networkMode: VPC` with the security groups/subnets that can reach the NAIE PostgreSQL instance, per

   decision item 7 in the companion document — **do not default to `PUBLIC` mode** if the module writes

   directly to a private PostgreSQL instance, since `PUBLIC` mode has no route to it **[D-30]**.



4. **Environment variables** the README must document and the Dockerfile/`agentcore.yaml` must pass

   through:

   | Variable | Purpose |

   |---|---|

   | `NAIE_DB_DSN` | PostgreSQL connection string for `pg_writer.py` |

   | `AGENT_OBSERVABILITY_ENABLED=true` | Turns on the managed CloudWatch GenAI dashboard alongside this module **[D-28]** |

   | `OTEL_PYTHON_DISTRO=aws_distro`, `OTEL_PYTHON_CONFIGURATOR=aws_configurator` | Required pairing for AgentCore's own OTEL pipeline if used **[D-28]** |

   | `TOKENOMICS_APP`, `TOKENOMICS_TEAM` | Static tags written onto every `usage_event` row |

   | `TOKENOMICS_BATCH_SIZE`, `TOKENOMICS_FLUSH_INTERVAL_S` | `meter.py` queue tuning (sane defaults: 100 / 2.0 — do not expose more knobs than this) |



5. **`deploy/README.md`** must include, at minimum: the `agentcore configure` / `agentcore launch`

   commands (or the raw `create-agent-runtime` CLI call) **[D-30]**, a `curl http://localhost:8080/ping`

   local smoke test, an `agentcore invoke '{"prompt": "..."}'` remote smoke test, and a short "what to

   check after the first real run" section that lists checklist items 13-17 from the companion document

   so the first production run doubles as the validation step this design still needs.



---



## 7. Definition of done



- `tokenomics_capture/` imports and runs with **no required dependency on MAF, Strands, or ADK** — a

  bare `boto3` `bedrock-runtime` client wrapped by `bedrock_hook.py` alone produces correct `usage_event`

  rows for `Converse` calls.

- Every adapter calls `Meter.emit()`; grep the package for direct `pg_writer`/`INSERT` usage outside

  `meter.py` and `pg_writer.py` — there should be none (DRY check).

- No file in the package exceeds what sections 3-6 above actually ask for — if `git diff` against this

  prompt's file list shows a new module, config option, or abstraction not named above, cut it or justify

  it in a one-line comment at the top of the file (KISS check).

- `tests/` cover the acceptance criteria in sections 3 and 4 above and pass against a fake/local stand-in

  (no live AWS account required to merge; a live run is still required before trusting the `[U]`-labelled

  behaviour per the companion document's checklist).

- `deploy/` contains a buildable Dockerfile, a deployable `agentcore.yaml`/CLI invocation, and a README a

  new engineer could follow without re-reading this prompt.