# Agent Tokenomics: Gap Analysis vs Splunk and Design Addendum (v3)



**Builds on:** `agent-tokenomics-design.md` (28 Sep 2026), `agent-tokenomics-gap-analysis-and-design-v2.md` (29 Sep 2026)

**Date:** 3 October 2026 (rev. 3: Amazon Bedrock AgentCore added as a fourth, and primary-in-production, layer — see section 9)

**Scope:** Python agents on Microsoft Agent Framework (MAF), AWS Strands Agents, Google ADK, **plus Amazon Bedrock AgentCore** (`bedrock-agentcore` SDK) as the actual hosting/runtime layer in production use



**Why this revision exists.** You clarified that the production agents are built on the **`bedrock-agentcore-sdk-python`** framework (https://github.com/aws/bedrock-agentcore-sdk-python) and do **not** use AWS Strands. AgentCore is architecturally different from the other three: it is a **framework-agnostic runtime/hosting layer** (Runtime, Memory, Gateway, Identity, Code Interpreter, Browser, Observability), not an agent-orchestration SDK. It does not call the model itself or decide what to send it — whatever orchestration code you run inside it (MAF, raw `boto3`, or nothing but a prompt template) does that. This changes *where* token capture has to attach, so it gets its own section (9) rather than a row in the section 6 matrix. Sections 1-8 are otherwise unchanged from v2 and remain the reference for whichever orchestration code runs inside the AgentCore entrypoint.



---



## 0. Pinned SDK versions and how claims were checked



Versions are the latest on PyPI on 29 Sep 2026 (PyPI JSON API). All three require Python >= 3.10.



| Framework | Package | Pinned version | Released |

|---|---|---|---|

| Microsoft Agent Framework | `agent-framework-core` (the `agent-framework` meta-package is also 1.19.0) | **1.19.0** | 2026-09-18 |

| Strands Agents | `strands-agents` | **1.57.1** | 2026-09-25 |

| Google ADK | `google-adk` | **2.10.0** | 2026-09-25 |

| Amazon Bedrock AgentCore SDK | `bedrock-agentcore` | **1.24.0** | 2026-09-28 |

| AgentCore Starter Toolkit (CLI: `agentcore configure`/`launch`/`invoke`) | `bedrock-agentcore-starter-toolkit` | latest on PyPI at time of writing | — |



Transitive versions used in tests: `google-genai 2.25.0`, `opentelemetry-sdk 1.42.1`. See `tests/requirements-pinned.txt`. **AgentCore SDK claims are [D]/[W] in this revision — not [S]/[T]: I read AWS documentation and the public GitHub repo pages, but did not `pip install bedrock-agentcore==1.24.0` and execute code against it the way sections 1-8 were verified against the other three SDKs. Treat section 9 code as reviewed design, not tested code, until it is run once in your environment (see section 9.9).**



**Important:** the original document referenced ADK 1.14.1 behaviour. ADK is now 2.x, so ADK conclusions there need re-reading against 2.10.0 (section 6).



**Evidence labels**



| Label | Meaning |

|---|---|

| **[S]** | Read directly in the installed source of the pinned version (file noted). |

| **[T]** | Executed by me against the pinned version, using a **fake model/client** that returns fixed usage numbers. Proves SDK plumbing, not real-provider behaviour. Scripts are in `tests/`. |

| **[D]** | Official documentation page (URL in References). |

| **[W]** | Third-party page, PR or issue. Useful but weaker evidence. |

| **[A]** | My analysis or recommendation. Not from a source. |

| **[U]** | Not verified. Must be checked before relying on it. |



---



## 1. What is missing: gap analysis against the Splunk article



Source: Splunk, *What is Agent Tokenomics?* (Pratik Bhavsar, 1 Jul 2026) [D-1]. I fetched and read the full page.



| # | Splunk point | In the original MD? | Gap | Action in this addendum |

|---|---|---|---|---|

| G1 | **Token buckets**: context, reasoning, retrieval, tool, coordination, governance | No. Only input/output/cache | Cannot answer "which tokens bought progress vs overhead" | Section 2 |

| G2 | Context is re-sent on every step, so cost compounds per call | Mentioned as [A]/[U] | No way to *see* it | Per-call rows plus a context-growth panel (sections 3, 6) |

| G3 | **Cost per accepted task** = model cost + tool/runtime cost + human review cost | No. Only cost per run | Missing two of three cost terms | Section 4 |

| G4 | **Token yield** = successful sessions per million tokens; needs evals to define "successful" | No | No outcome signal in schema | Section 4 (outcome fields) |

| G5 | Stack-level view: GPU utilisation, model performance, application task completion, agent behaviour (attributed to Cisco's Jeetu Patel) | Agent layer only | No infra/quota layer | Section 3 (quota panel) |

| G6 | Governance/eval tokens are "often invisible in the bill" | No | Judge/guardrail agents not tagged | Section 2 (governance tagging) |

| G7 | Spread between users (LeanOps: 20x between developers on one team, as quoted by Splunk) | User is a schema field only | No per-user analysis or how to obtain user identity | Section 5 |

| G8 | Start with input/output per call, add bucket split later | Consistent | None | Adopted as the phasing |



**Notes on the Splunk article**

- The key-takeaways list says "five buckets" and names five, but the body defines **six** (it adds Tool Tokens). I use six.

- The article's statistics (62% re-sent context attributed to Stanford Digital Economy Lab, the 20x spread, the $87k to $24k case) reach it through secondary blogs. Treat them as motivating claims, not benchmarks. **[W]**

- Its worked example says answers needing human review cost "about $3.02". The arithmetic is: 15 min at $30/h = $7.50 per review, so a reviewed answer costs about **$7.52**, and the **blended** cost across all answers (40% reviewed) is 0.02 + 0.4 x 7.50 = **$3.02**. The article's wording conflates the two. **[A]**



**Corrections to the original MD found while verifying the frameworks**



| Original statement | Finding |

|---|---|

| "Budgets and limits: Not found" for Strands, ADK, MAF | Per-run **limits do exist** in all three (call/turn/token caps, not dollar budgets). See section 6.4. **[S]** |

| Strands adapter reads `result.metrics.accumulated_usage` as run-level usage | **Wrong for a reused agent.** `accumulated_usage` accumulates across all calls on the instance (300 tokens after two calls of 100 and 200). Use `latest_agent_invocation.usage` for one run. **[S][T]** |

| Open question: does `accumulated_usage` reset per call? | **Answered: no, it does not reset.** **[T]** |

| Strands: hook API "unverified" | Confirmed: `AfterModelCallEvent` / `AfterInvocationEvent`. Per-call usage is on `event.stop_response.message["metadata"]["usage"]`. **[S][T]** |

| ADK: "Register on the Runner (parameter name to be confirmed)" | In 2.10.0, `Runner(plugins=...)` is **deprecated**. Register via `App(name, root_agent, plugins=[...])` and `Runner(app=app, ...)`. **[S]** |

| ADK plugin sketch emits on every `after_model_callback` | In SSE streaming the callback also fires for **partial chunks**. Emitting on each would double-count if chunks carry usage. Skip `llm_response.partial`. **[S][T]** |

| ADK: cache/thought token fields "not confirmed" | Confirmed on `usage_metadata`: `cached_content_token_count`, `thoughts_token_count`, `tool_use_prompt_token_count`. **[S]** (google-genai 2.25.0) |

| MAF: middleware class names "unverified" | Confirmed: `AgentMiddleware`, `ChatMiddleware`, `FunctionMiddleware`; contexts `AgentContext`, `ChatContext`. **[S][T]** |

| MAF `UsageDetails` keys | Also has `reasoning_output_token_count` (not in original). **[S]** |

| *(v3)* Assumption that AWS Strands is the likely runtime pairing for a Bedrock-based deployment | **Corrected.** Confirmed with you: the production agents run on **Amazon Bedrock AgentCore** (`bedrock-agentcore-sdk-python`) and do not use Strands. AgentCore is framework-agnostic, so this does not block anything in sections 1-8 — whatever orchestration code (MAF, raw `boto3`, or none) runs inside the AgentCore entrypoint is captured the same way it would be if it ran standalone. Section 9 adds the AgentCore-specific layer underneath. **[A]** |



---



## 2. Requirement 1: Token buckets and how to retrieve them



### 2.1 The core finding



**No provider response or framework field I inspected reports the Splunk buckets.** What is reported is only:



| Measured (reported by provider, passed on by framework) | Strands | ADK | MAF |

|---|---|---|---|

| input | `inputTokens` | `prompt_token_count` | `input_token_count` |

| output | `outputTokens` | `candidates_token_count` | `output_token_count` |

| total | `totalTokens` | `total_token_count` | `total_token_count` |

| cache read | `cacheReadInputTokens` | `cached_content_token_count` | `cache_read_input_token_count` |

| cache write | `cacheWriteInputTokens` | none seen | `cache_creation_input_token_count` |

| reasoning | **none in `Usage`** | `thoughts_token_count` | `reasoning_output_token_count` |

| tool-use prompt | none | `tool_use_prompt_token_count` | none |



Sources: Strands `types/event_loop.py`; google-genai `GenerateContentResponseUsageMetadata`; MAF `_types.py:418-439`. **[S]** Whether each provider actually populates these fields is **[U]** per provider.



So the six Splunk buckets split into two tiers. **[A]**



| Tier | Buckets | How obtained |

|---|---|---|

| **Measured** | reasoning (where reported), plus cache read/write, input, output | Straight from the usage fields above |

| **Attributed (derived)** | context, retrieval, tool, coordination, governance | Not reported by anyone. Derived by classifying the request's content and calibrating to the measured input total |



### 2.2 Proposed attribution method [A]



For each LLM call, capture the request just before it is sent, split it into segments, estimate tokens per segment, then scale to the measured input.



| Segment in the request | Bucket |

|---|---|

| System prompt, safety policy, conversation history | **context** |

| Tool/function schemas sent with the request | **context** (flag "schema overhead" separately, as Splunk does) |

| Tool results from tools you tag as retrieval (RAG, search, vector lookup) | **retrieval** |

| Other tool results (APIs, DB rows, function output) | **tool** |

| Messages originating from another agent, orchestrator role prompts, shared state | **coordination** |

| Whole calls made by agents tagged as judge/guardrail/evaluator/human-review trigger | **governance** (bucket by agent tag, not by content) |

| Output tokens | reasoning (if reported) vs answer |



Calibration: `bucket_i = est_i / sum(est) x measured_input`. Store the residual as `unattributed`. Mark every derived value `bucket_method = 'estimated'`. Tokenizer choice per provider is **[U]**; a character-based heuristic is acceptable for an MVP if the estimated flag and calibration are kept.



### 2.3 Where to capture the request, per framework



| Framework | Hook | Request fields available | Evidence |

|---|---|---|---|

| Strands | `BeforeModelCallEvent` | `event.agent` (agent and its messages), `invocation_state`, and **`projected_input_tokens`** (a built-in estimate of the upcoming call's input) | **[S]** `hooks/events.py` |

| ADK | `before_model_callback(callback_context, llm_request)` | `llm_request.contents`, `llm_request.config`, `llm_request.tools_dict` | **[S]** `models/llm_request.py`; [W-17] uses `tools_dict` |

| MAF | `ChatMiddleware.process(context, call_next)` | `context.messages`, `context.options` (tools are an option), `context.session` | **[S]** `_middleware.py:555-600` |



Bucket classification of tool results into retrieval vs tool needs a **tool-name-to-bucket config** owned by each app team. **[A]**



### 2.4 Phasing (matches Splunk's "start with what you can pull")



1. **MVP:** measured tier only (input, output, cache, reasoning), per call.

2. **Phase 2:** context vs tool-result vs retrieval split using the hooks above.

3. **Phase 3:** coordination (multi-agent) and governance tagging. Multi-agent attribution is **not tested** by me.



---



## 3. Requirement 2: Storing (NAIE DB) and presenting (dashboard plus infra quota)



### 3.1 Storage: PostgreSQL, hybrid relational + JSONB



"NAIE DB" is treated as a plain PostgreSQL database (confirmed by you). Design rule, as requested:



> **A real column exists only if we filter, group, join or SUM on it. Everything else goes in a JSONB `details` column.**



This keeps the schema stable while frameworks change (the original doc shows how quickly their APIs move) and lets new fields be added without migrations. **[A]**



**Why the token counts are columns and not JSONB.** Dashboards `SUM()` them on every load. Plain `bigint` columns are the simplest and fastest option for that. They are the only "non-searched" columns kept, and they are kept for aggregation. **[A]**



**Write path (assumption, please confirm).** Agents (or an OTLP collector) insert one row per LLM call. Retention and PII policy are still open (section 8).



### 3.2 Schema (DDL)



Validated: I ran this DDL on PostgreSQL 16.2 (via the `pgserver` pip package) with sample rows. It created without error, the views returned the expected costs (hand-checked), the run-level fallback and missing-price flag behaved as intended, and the per-user query used its index. **[T]** Not tested: large volumes, concurrency, partitioning. Test files: `sql/schema.sql` (the DDL below, verbatim) and `sql/test_schema.py`.



**Grain and double counting.** Store one row per LLM call (`level='call'`) as the source of truth. Use `level='run'` only for paths with no per-call hook. The view `v_usage_effective` uses call rows when a run has any, otherwise the run row, so the two are never summed together. (The original doc's "never sum both" rule, now enforced in SQL.)



**Token normalisation (from section 4.2).** `input_tokens` is stored as *uncached input only*. Cache read and write are separate columns. The adapter does the normalisation at write time and keeps the provider's raw usage object in `details.raw_usage`. Which providers fold cache into input is **[U]** and must be tested per provider in Phase 0.
```sql

-- Agent Tokenomics: PostgreSQL schema

-- Rule: real columns only for what we filter, group, join or SUM on. Everything else lives in JSONB.



CREATE TABLE usage_event (

    event_id        uuid        NOT NULL DEFAULT gen_random_uuid(),

    ts              timestamptz NOT NULL,

    run_id          text        NOT NULL,

    session_id      text,

    team            text,

    app             text        NOT NULL,

    user_id         text,                         -- hashed/pseudonymised at write time

    agent_name      text        NOT NULL,

    framework       text        NOT NULL CHECK (framework IN ('strands','adk','maf')),

    provider        text        NOT NULL,

    model           text        NOT NULL,

    level           text        NOT NULL CHECK (level IN ('call','run')),

    status          text        NOT NULL DEFAULT 'ok',

    estimated       boolean     NOT NULL DEFAULT false,

    -- normalised token counts (see design doc 4.2): input_tokens EXCLUDES cache

    input_tokens        bigint  NOT NULL DEFAULT 0,

    cache_read_tokens   bigint  NOT NULL DEFAULT 0,

    cache_write_tokens  bigint  NOT NULL DEFAULT 0,

    output_tokens       bigint  NOT NULL DEFAULT 0,

    reasoning_tokens    bigint  NOT NULL DEFAULT 0,

    details         jsonb       NOT NULL DEFAULT '{}'::jsonb,

    PRIMARY KEY (event_id)

);

CREATE INDEX usage_event_ts_brin       ON usage_event USING brin (ts);

CREATE INDEX usage_event_team_app_ts   ON usage_event (team, app, ts);

CREATE INDEX usage_event_user_ts       ON usage_event (user_id, ts) WHERE user_id IS NOT NULL;

CREATE INDEX usage_event_agent_ts      ON usage_event (agent_name, ts);

CREATE INDEX usage_event_run           ON usage_event (run_id);

CREATE INDEX usage_event_session       ON usage_event (session_id) WHERE session_id IS NOT NULL;

CREATE INDEX usage_event_model_ts      ON usage_event (provider, model, ts);



CREATE TABLE price_catalog (

    provider        text        NOT NULL,

    model           text        NOT NULL,

    effective_from  timestamptz NOT NULL,

    input_per_mtok          numeric(14,6) NOT NULL,

    cache_read_per_mtok     numeric(14,6) NOT NULL DEFAULT 0,

    cache_write_per_mtok    numeric(14,6) NOT NULL DEFAULT 0,

    output_per_mtok         numeric(14,6) NOT NULL,

    reasoning_billed_separately boolean NOT NULL DEFAULT false,

    currency        text        NOT NULL DEFAULT 'USD',

    details         jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- source_url, notes, tiers

    PRIMARY KEY (provider, model, effective_from)

);



CREATE TABLE infra_cost (

    id              bigserial   PRIMARY KEY,

    app             text        NOT NULL,

    period_start    timestamptz NOT NULL,

    period_end      timestamptz NOT NULL,

    cost_type       text        NOT NULL,          -- e.g. compute, vector_db, gateway

    amount          numeric(14,4) NOT NULL,

    currency        text        NOT NULL DEFAULT 'USD',

    details         jsonb       NOT NULL DEFAULT '{}'::jsonb   -- allocation_key, source, tags

);

CREATE INDEX infra_cost_app_period ON infra_cost (app, period_start);



CREATE TABLE run_outcome (

    run_id          text        PRIMARY KEY,

    app             text        NOT NULL,

    accepted        boolean,

    recorded_at     timestamptz NOT NULL DEFAULT now(),

    details         jsonb       NOT NULL DEFAULT '{}'::jsonb   -- eval_score, human_review_minutes, review_cost, eval_tokens

);

CREATE INDEX run_outcome_app_time ON run_outcome (app, recorded_at);



CREATE TABLE quota_snapshot (

    ts              timestamptz NOT NULL,

    provider        text        NOT NULL,

    region          text,

    target          text        NOT NULL,          -- model or deployment

    metric          text        NOT NULL,          -- e.g. tpm, rpm, throttles

    used            numeric,

    limit_value     numeric,

    details         jsonb       NOT NULL DEFAULT '{}'::jsonb   -- raw metric names, dimensions

);

CREATE INDEX quota_snapshot_lookup ON quota_snapshot (provider, target, metric, ts);



CREATE TABLE budget (

    id              bigserial   PRIMARY KEY,

    scope_type      text        NOT NULL CHECK (scope_type IN ('user','agent','team','app')),

    scope_id        text        NOT NULL,

    period          text        NOT NULL CHECK (period IN ('day','week','month')),

    action          text        NOT NULL DEFAULT 'alert' CHECK (action IN ('alert','block')),

    details         jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- token_limit, cost_limit, notify

    UNIQUE (scope_type, scope_id, period)

);



-- One row per (run) with no double counting: use call rows if the run has any, else the run row.

CREATE VIEW v_usage_effective AS

SELECT u.*

FROM usage_event u

WHERE u.level = 'call'

   OR NOT EXISTS (SELECT 1 FROM usage_event c WHERE c.run_id = u.run_id AND c.level = 'call');



-- Cost per event; price chosen as latest effective_from <= event ts.

CREATE VIEW v_usage_cost AS

SELECT u.*,

       p.currency,

       ( u.input_tokens       * p.input_per_mtok

       + u.cache_read_tokens  * p.cache_read_per_mtok

       + u.cache_write_tokens * p.cache_write_per_mtok

       + (u.output_tokens + CASE WHEN p.reasoning_billed_separately THEN u.reasoning_tokens ELSE 0 END)

                              * p.output_per_mtok

       ) / 1000000.0 AS token_cost,

       (p.provider IS NULL) AS price_missing

FROM v_usage_effective u

LEFT JOIN LATERAL (

    SELECT * FROM price_catalog pc

    WHERE pc.provider = u.provider AND pc.model = u.model AND pc.effective_from <= u.ts

    ORDER BY pc.effective_from DESC LIMIT 1

) p ON true;



CREATE VIEW v_run_cost AS

SELECT run_id, min(ts) AS started_at, max(app) AS app, max(team) AS team, max(user_id) AS user_id,

       max(agent_name) AS agent_name, count(*) AS llm_calls,

       sum(input_tokens + cache_read_tokens + cache_write_tokens + output_tokens) AS total_tokens,

       sum(token_cost) AS token_cost, bool_or(estimated) AS any_estimated, bool_or(price_missing) AS any_price_missing

FROM v_usage_cost GROUP BY run_id;

```



**What lives in `usage_event.details` (JSONB) by convention** [A]



| Key | Content |

|---|---|

| `call_seq` | Order of the call within the run (for the context-growth chart) |

| `raw_usage` | Provider/framework usage object exactly as received |

| `agent_id`, `parent_agent_id` | Sub-agent lineage |

| `sdk_version` | e.g. `strands-agents 1.57.1` |

| `est_context`, `est_retrieval`, `est_tool`, `est_coordination`, `est_unattributed`, `bucket_method` | Bucket estimates from section 2 |

| `governance_flag` | True for judge/guardrail/eval agents |

| `latency_ms`, `stop_reason`, `error` | Health |

| `trace_id`, `span_id`, `invocation_id` | Correlation with OTel |

| `tags` | Free-form attributes |



**Promote-on-demand.** If a JSONB key starts being filtered often, add a generated column or an expression index, for example `CREATE INDEX ON usage_event ((details->>'stop_reason'))`, or a `GIN (details jsonb_path_ops)` index for containment queries. No data migration is needed. **[A]**



**Example queries** [T] (all five were executed against the schema with sample rows, and returned results that match hand calculation; the sample data is tiny, so this proves syntax and logic, not performance)



```sql

-- Spend by team/app/model per day

SELECT date_trunc('day', ts) AS day, team, app, model, sum(token_cost) AS cost

FROM v_usage_cost GROUP BY 1,2,3,4 ORDER BY 1 DESC;



-- Per-user spread (p50 / p95 run cost)

SELECT user_id,

       percentile_cont(0.5)  WITHIN GROUP (ORDER BY token_cost) AS p50,

       percentile_cont(0.95) WITHIN GROUP (ORDER BY token_cost) AS p95

FROM v_run_cost GROUP BY user_id;



-- Context growth: input tokens by call sequence inside runs

SELECT (details->>'call_seq')::int AS call_seq, avg(input_tokens + cache_read_tokens) AS avg_input

FROM usage_event WHERE level='call' AND details ? 'call_seq' GROUP BY 1 ORDER BY 1;



-- Data quality: estimated share and unpriced usage

SELECT avg((estimated)::int) AS est_share, avg((price_missing)::int) AS unpriced_share FROM v_usage_cost;



-- Cost per accepted task (Splunk formula; review cost read from JSONB)

SELECT sum(r.token_cost + coalesce((o.details->>'review_cost')::numeric,0))

       / nullif(count(*) FILTER (WHERE o.accepted), 0) AS cost_per_accepted_task

FROM v_run_cost r JOIN run_outcome o USING (run_id);

```



The last query is a simplification: it divides the cost of every run that has an outcome row by the number of accepted ones (runs without an outcome row are excluded by the inner join), which is what "real price of one good result" means, but infra cost is not yet added. Adding it needs an allocation rule (section 4.1) that is still undecided.



**Known limits of this design [A]**

- A single table has no partitioning. Consider range-partitioning `usage_event` by month once volume grows (the primary key would then need to include `ts`). Not tested.

- `v_usage_cost` recomputes cost at query time with a lateral price lookup. Fine for dashboards at moderate volume; materialise if it gets slow. Not benchmarked.

- `user_id` should be hashed or pseudonymised by the writer. The database does not enforce it.



### 3.3 Dashboard (metrics)



| Panel | Content | Needs |

|---|---|---|

| Spend and tokens | By team, app, agent, model, day | `usage_event` + `price_catalog` |

| Per-user spread | p50/p95 cost per user; top users | `user_id` (section 5) |

| Bucket view | Stacked measured tier; estimated composition with an "estimated share" badge | Section 2 |

| Context growth | Input tokens by `call_seq` within a run (makes Splunk's compounding visible) | Call-level rows |

| Cost per accepted task, token yield | Splunk formulas | `run_outcome` |

| Budget burn and alerts | Consumption vs `budget` | `budget` |

| Data quality | % events estimated, % runs with no usage | `estimated` |



### 3.4 Underlying infra with quota (provider capacity panel)



Show two things side by side: **our app's token rate** and the **provider's quota utilisation**. The provider figure may include other consumers of the same account or deployment, so both are needed. **[A]**



| Provider | Usage source | Quota source | Notes |

|---|---|---|---|

| **AWS Bedrock** | CloudWatch runtime metrics `InputTokenCount`, `OutputTokenCount`, `CacheReadInputTokens`, `CacheWriteInputTokens` **[D-16][D-17]** | Service Quotas API for TPM/RPM/TPD **[W-18]** | Quota is consumed at request start as input + cache-write + `max_tokens`, then adjusted using a per-model burndown rate for output **[D-16]**. CloudWatch has **no direct TPM-usage metric**, so it must be calculated **[W-19]**. A sample CDK dashboard exists **[W-18]** |

| **Azure OpenAI / Foundry** | Azure Monitor token/request metrics; response headers `x-ratelimit-remaining-tokens/requests` **[W-22]**; API Management `llm-token-limit` and emit-token-metric policies **[D-20]** | Quota is per subscription, per region, per model in TPM, allocated across deployments **[D-21]** | RPM is about 6 per 1,000 TPM **[W-23]**. TPM enforcement uses an *estimate* (prompt + `max_tokens`), which differs from billed tokens **[W-23]** |

| **Vertex AI (Gemini)** | Cloud Monitoring metrics such as `online_prediction_tokens_per_minute_per_base_model` **[W-24]** | Quota metrics `generate_content_input_tokens_per_minute_per_base_model` and `.../quota/.../usage,limit,exceeded` **[D-25][W-24]** | Quotas apply per project. Google's docs pages have been renamed and tables were reorganised recently, so **re-verify current metric names** **[U]** |



The actual provider(s) in use were not stated, so all three are covered. **Throttling (429) counts** should be a first-class metric next to utilisation. **[A]**



---



## 4. Requirement 3: Cost economics



### 4.1 Formula [A]



Splunk's definition [D-1]: `cost per accepted task = model cost + tool/runtime cost + human review cost`. Your framing splits that into a **derived infra cost** and an **actual token cost**. Combined:



```

run_cost      = token_cost + infra_cost + tool_cost + review_cost   (last two optional)



token_cost    = SUM over calls of

                  input_uncached x p_in  + cache_read x p_cache_read

                + cache_write x p_cache_write + output x p_out

                (reasoning tokens priced per provider rule, see 4.2)



infra_cost    = derived allocation (hosting of the agent app), by either:

                (a) time-based : run_duration_s x (instance $/s / concurrency)

                (b) share-based: period infra cost x (run weight / SUM of weights),

                                 weight = duration or tokens

```



`token_cost` is *actual* (price x measured tokens). `infra_cost` is *derived* (no meter exists; it is an allocation). Source of the infra inputs (cloud billing exports, tags, shared components such as vector DB and gateway) is a **FinOps input I have not researched [U]**. Price values are not sourced here: they come from provider pricing pages into `price_catalog`.



### 4.2 Normalisation risk that changes the maths



Providers differ on whether cached tokens are **inside** `input` or reported **separately**. Strands' own code documents this: it checks whether `inputTokens + outputTokens == totalTokens` to decide if cache is already inside `inputTokens`, otherwise adds the cache counters on top. **[S]** `telemetry/metrics.py` (`_total_prompt_tokens`).



Pricing without normalising would double-charge or under-charge cache tokens. Rule: compute uncached input per event using that same total-vs-parts test and store it in `input_tokens` (the schema in section 3.2 defines `input_tokens` as uncached-only); keep the raw usage in `details.raw_usage`. Whether MAF `input_token_count` and Gemini `prompt_token_count` include cached tokens is **[U]**: test per provider in Phase 0. Whether reasoning tokens are billed as output also varies **[U]**.



### 4.3 Outcome-based metrics (Splunk) [D-1]



- **Token yield** = accepted sessions per million tokens, per workflow, using its own quality bar from your evals. Needs `run_outcome.accepted`.

- **Cost per accepted task** = total cost of a period / accepted tasks, plus review cost. Include **eval cost** (`eval_tokens`), which Splunk says must not be left out.



---



## 5. Requirement 4: User-level and agent-level tracking



| Dimension | Strands 1.57.1 | ADK 2.10.0 | MAF 1.19.0 |

|---|---|---|---|

| **Agent** | `agent.name`, `agent.agent_id` **[S]**; on spans as `gen_ai.agent.name/id` **[S]** | `callback_context.agent_name` **[S][T]** | `context.agent.name` in `AgentContext` **[T]** |

| **Session** | `agent.session_id` **[S]** | `callback_context.session.id` **[S][T]** | `context.session.session_id` **[T]** |

| **User** | **No native field.** Two options: `trace_attributes={"user.id": ...}` on the Agent **[S]** (usage in tutorials **[W-28][W-29]**), or per-call `agent(prompt, invocation_state={"user_id": ...})`, which arrived in hook events **[T]** | **Native:** `callback_context.user_id` (from `runner.run_async(user_id=...)`) **[S][T]** | **No native field.** `AgentSession` holds only `session_id`, `service_session_id`, `state` **[S]**. Carry the user in `session.state` or a `contextvars.ContextVar` set in `AgentMiddleware` |

| **Sub-agent / parent** | Multi-agent results carry accumulated usage per node **[W-3]**; per-call attribution **[U]** | Callback reports the agent making the call; nested sub-agents **[U]** (single agent tested) | Workflow-level attribution **[U]** |

| **Invocation/run id** | Not native; generate in `BeforeInvocationEvent` | `callback_context.invocation_id` **[S][T]** | Not native; generate in `AgentMiddleware` |



**Traps found by testing**

- Strands: metrics live on the **agent instance**. If you pool or reuse instances across users, per-run usage must come from `latest_agent_invocation.usage` (not `accumulated_usage`), and identity must come from `invocation_state`, not instance-level `trace_attributes`. **[T]**

- MAF: `context.metadata` set in `AgentMiddleware` was **not visible** in the inner `ChatMiddleware` (it printed `None`). Use a `ContextVar` or session state to pass user id down. **[T]**

- Strands `invocation_state` worked through `agent(...)`. Agent code also builds some events with an empty `invocation_state` on a path I did not test (`stream_async`, structured output). **[U]**

- Strands `AfterModelCallEvent` is **not fired for `structured_output` calls** **[S]**, so those calls are invisible to a per-call hook.

- Privacy: user ids in telemetry are personal data in many regimes. Hash or pseudonymise unless policy says otherwise. **[A]**



---



## 6. How to retrieve usage per framework (pinned versions)



### 6.1 Summary matrix



| Capability | Strands 1.57.1 | ADK 2.10.0 | MAF 1.19.0 |

|---|---|---|---|

| Run total | `result.metrics.latest_agent_invocation.usage` **[S][T]** | No aggregate object found; sum per-call events by `invocation_id` **[S]** | `response.usage_details` **[S][T]** |

| Per LLM call | `AfterModelCallEvent` -> `stop_response.message["metadata"]["usage"]` **[S][T]** | `BasePlugin.after_model_callback` -> `llm_response.usage_metadata` **[S][T]** | `ChatMiddleware` -> `context.result.usage_details` **[T]** |

| Cumulative on instance | `metrics.accumulated_usage` (never resets) **[T]** | n/a | n/a |

| Cache tokens | read and write **[S]** | read (`cached_content_token_count`) **[S]** | read and write **[S]** |

| Reasoning tokens | none in `Usage` **[S]** | `thoughts_token_count` **[S]** | `reasoning_output_token_count` **[S]** |

| OTel spans | `gen_ai.usage.*` incl. cache attrs on spans **[S]** | `call_llm` span via `trace_call_llm` **[S]**; attribute names not inspected | `invoke_agent`, `chat`, `execute_tool` spans **[D-9]** |

| OTel metrics | `strands.event_loop.input.tokens`, `.output.tokens`, `.cache_read.input.tokens`, `.cache_write.input.tokens` **[S]** | not inspected | `gen_ai.client.token.usage`, `gen_ai.client.operation.duration` **[S][D-9]** |

| Ready-made sink | none | `BigQueryAgentAnalyticsPlugin(project_id, dataset_id, table_id)` logs LLM responses with `usage_metadata` **[S]** | none |

| Dollar cost in SDK | none found **[S]** | none found | none found in core **[S]** |



**Double-counting warning (all frameworks that emit spans).** MAF `invoke_agent` spans repeat the summed tokens of their `chat` spans, so sum tokens on `chat` spans only **[W-11]**. The original doc reports the same pattern for Strands (its ref 5, not re-verified here).



### 6.2 Tested adapters



Both snippets below are drawn from `tests/`, which ran successfully on the pinned versions with fake models. **[T]**



**Strands 1.57.1**



```python

from strands import Agent

from strands.hooks import HookProvider, HookRegistry, AfterModelCallEvent, AfterInvocationEvent



class Meter(HookProvider):

    def register_hooks(self, registry: HookRegistry, **kw):

        registry.add_callback(AfterModelCallEvent, self.on_call)

        registry.add_callback(AfterInvocationEvent, self.on_run)



    def on_call(self, e: AfterModelCallEvent):

        md = (e.stop_response.message.get("metadata") if e.stop_response else None) or {}

        usage = md.get("usage")            # inputTokens, outputTokens, totalTokens, cache*

        emit(level="call", usage=usage, user=e.invocation_state.get("user_id"),

             agent=e.agent.name, session=e.agent.session_id)



    def on_run(self, e: AfterInvocationEvent):

        inv = e.agent.event_loop_metrics.latest_agent_invocation

        emit(level="run", usage=dict(inv.usage), user=e.invocation_state.get("user_id"))



agent = Agent(model=..., hooks=[Meter()])

agent("prompt", invocation_state={"user_id": "alice"})

```



Do **not** read `event_loop_metrics` inside `AfterModelCallEvent`: `update_usage()` runs *after* the hook fires **[S]** `event_loop/event_loop.py`.



**ADK 2.10.0**



```python

from google.adk.apps import App

from google.adk.plugins.base_plugin import BasePlugin



class TokenMeter(BasePlugin):

    def __init__(self): super().__init__(name="token_meter")

    async def after_model_callback(self, *, callback_context, llm_response):

        if llm_response.partial:            # SSE streaming: callback fires per chunk

            return None

        m = llm_response.usage_metadata

        if m:

            emit(level="call", user=callback_context.user_id,

                 session=callback_context.session.id, agent=callback_context.agent_name,

                 run=callback_context.invocation_id,

                 input=m.prompt_token_count, output=m.candidates_token_count,

                 cache_read=m.cached_content_token_count, reasoning=m.thoughts_token_count)

        return None                          # observe only



app = App(name="my_app", root_agent=agent, plugins=[TokenMeter()])

runner = Runner(app=app, session_service=...)   # not Runner(plugins=...), deprecated

```



Caveat: my fake put usage on both the partial and final chunk. Whether real Gemini streaming puts usage on partial chunks is **[U]**. Skipping `partial` is the safe rule, but if a provider only reports usage on a partial chunk it would be missed, so test with the real provider.



**MAF 1.19.0**



```python

from agent_framework import Agent, AgentMiddleware, ChatMiddleware, AgentContext, ChatContext



class CallMeter(ChatMiddleware):

    async def process(self, context: ChatContext, call_next):

        await call_next()

        r = context.result                    # ChatResponse (non-streaming)

        emit(level="call", usage=r.usage_details, model=r.model,

             session=context.session.session_id if context.session else None,

             user=USER_CTX.get(None))         # ContextVar; metadata does not propagate



class RunMeter(AgentMiddleware):

    async def process(self, context: AgentContext, call_next):

        USER_CTX.set(user_from_request())

        await call_next()

        emit(level="run", usage=context.result.usage_details, agent=context.agent.name)



agent = Agent(client=client, name="a1", middleware=[RunMeter(), CallMeter()])

```



For streaming, `context.result` is a `ResponseStream`, not a `ChatResponse`; the `stream_result_hooks` on `ChatContext` are the likely capture point **[S]** but **untested [U]**.



### 6.3 What the tests do and do not prove



| Proven **[T]** | Not proven |

|---|---|

| Hook/plugin/middleware APIs exist with these names and signatures on the pinned versions | Real provider usage population (Bedrock, Azure OpenAI, Gemini, others) |

| Strands accumulation semantics; per-invocation usage path | Streaming behaviour for Strands and MAF |

| ADK partial-chunk callbacks fire in SSE | Multi-agent and sub-agent attribution in all three |

| MAF `ChatMiddleware` sees usage and session (client had to mix in `ChatMiddlewareLayer`; shipped clients not inspected) | Cache/reasoning token semantics per provider |



### 6.4 Built-in limits (correction to the original "not found")



These are **call/turn/token caps per run**, not dollar budgets. Dollar budgets remain ours to build on the usage store. **[A]**



| Framework | Mechanism | Detail |

|---|---|---|

| Strands | `agent(prompt, limits=Limits(turns=..., output_tokens=..., total_tokens=...))` | Per invocation only. Stop reasons `limit_turns`, `limit_total_tokens`, `limit_output_tokens`. **Soft caps** checked at turn boundaries, so one large response can overshoot. **[S]** `types/agent.py` |

| ADK | `RunConfig(max_llm_calls=N)` or env `ADK_MAX_LLM_CALLS` | Caps LLM calls per run; exceeding it stops execution with an exception per the code comment. **[S]** `agents/run_config.py`, `flows/llm_flows/core/_model_call.py` |

| MAF | `function_invocation_configuration`: `max_iterations` (LLM round trips), `max_function_calls`, `max_duration_seconds` | No token cap found. `TokenBudgetComposedStrategy` in `_compaction.py` budgets **context size**, not spend. **[S]** `_tools.py`, `_compaction.py` |



Strands also exposes `projected_input_tokens` on `BeforeModelCallEvent` and supports `event.cancel`, so a **pre-call budget check** against the user's remaining budget is feasible there. **[S]** For ADK, `before_model_callback` may return a response to skip the call **[D-14]**; for MAF, `ChatMiddleware` can short-circuit. Enforcing dollar budgets this way is **[A]** and untested.



---



## 7. Updated Phase 0 checklist



| # | Item | Status |

|---|---|---|

| 1 | Strands `accumulated_usage` reset and per-invocation path | **Resolved** [T] |

| 2 | Strands per-call hook API | **Resolved** [S][T] |

| 3 | ADK cached/thought token fields | **Resolved** [S] |

| 4 | ADK plugin registration | **Resolved** (`App(plugins=)`) [S]. Whether the LiteLLM streaming issue (original ref 6) is fixed in 2.10.0: **not checked** |

| 5 | ADK BigQuery plugin contents | **Partly resolved** [S]: records usage prompt/completion/total plus cached/thoughts/tool-use. Suitability as our store is a decision, not a fact |

| 6 | MAF `usage_details` populated per provider, incl. streaming | **Open** (only a fake client was tested) |

| 7 | MAF middleware names | **Resolved** [S][T]. Status of original ref 12 (context-provider stage) not checked |

| 8 | Streaming and non-primary provider per framework | **Open** |

| 9 | Cache/reasoning semantics per provider (feeds 4.2) | **Open** |

| 10 | *New:* Real-provider run of the three tests in `tests/` with actual credentials | **Open** |

| 11 | *New:* Multi-agent attribution (Strands graph/swarm, ADK sub-agents, MAF workflows) | **Open** |

| 12 | *New:* Streaming capture in MAF (`stream_result_hooks`) | **Open** |

| 13 | *New (v3):* Confirm the `after-call.bedrock-runtime.*` botocore hook (section 9.3) fires identically for a plain `boto3.client("bedrock-runtime")`, a client built by MAF's Bedrock chat client, and any other library — run once with a real model | **Open** |

| 14 | *New (v3):* Confirm where usage lands for `ConverseStream`/`InvokeModelWithResponseStream` — likely only on the final chunk/event of the stream, not on the `after-call` event itself (same class of problem as MAF's `stream_result_hooks`, section 6.2, and ADK's partial chunks, section 2.4) | **Open** |

| 15 | *New (v3):* Confirm the run-scoped `ContextVar` set in the Starlette middleware (section 9.4) is visible inside both a `def` (sync) and `async def` `@app.entrypoint` handler | **Open** |

| 16 | *New (v3):* AgentCore Memory/Gateway botocore operation names and response usage fields for governance/retrieval tagging (section 9.6) — designed, not inspected against the live SDK | **Open** |

| 17 | *New (v3):* Verify AgentCore's 12 billing line items and unit prices (section 9.5) against the official AWS Bedrock AgentCore pricing page — the figures here come from third-party aggregator pages **[W]**, not the AWS pricing page itself | **Open** |



## 8. Decisions and inputs needed



1. PostgreSQL: confirm write path (direct insert vs collector), retention, and PII rules for `user_id` (section 3.1).

2. Which model providers are in use, to scope the quota panel (section 3.4).

3. Owner and source for infra cost inputs (section 4.1).

4. Who owns the tool-name-to-bucket map per app (section 2.3).

5. Source of the "accepted" outcome signal (evals or user feedback) for token yield.

6. Whether per-run caps (section 6.4) should be mandatory defaults in Phase 2.

7. *(v3)* Is the AgentCore Runtime deployed in `PUBLIC` or `VPC` network mode? `VPC` is required if the agent writes directly to a private PostgreSQL/RDS instance for the NAIE DB (section 9.8). **[D-30]**

8. *(v3)* Will AgentCore **Memory** and/or **Gateway** be used, beyond **Runtime**? That decides whether section 9.6 (governance/retrieval tagging for those services) is in scope now or deferred.

9. *(v3)* Confirm which orchestration code actually runs inside the AgentCore `@app.entrypoint` — MAF, raw `boto3` calls, or something else — so the right middleware from section 6.2 is wired in alongside the new botocore hook in section 9.3.



---



## 9. Requirement 5 (v3): Amazon Bedrock AgentCore — the layer actually in production



### 9.1 What AgentCore is, and why it needs a different capture strategy



AgentCore is **not a fourth entry for the section 6 matrix**. Strands, ADK and MAF are agent-*orchestration* SDKs: they decide what goes into a model call and expose hooks/middleware around that decision. AgentCore is a **runtime and hosting layer** — `BedrockAgentCoreApp` wraps whatever Python callable you point it at (`@app.entrypoint`) in an HTTP service (`/invocations`, `/ping`) and packages it into a managed, session-isolated microVM. It is explicitly documented as working with "any framework and model," including Strands, LangGraph, CrewAI, Google ADK, or plain custom code **[W-37]**. Separately, it bundles its own primitives — **Memory**, **Gateway** (tool exposure via MCP), **Identity**, **Code Interpreter**, **Browser**, **Observability**, **Policy**, **Evaluations** **[W-38]**.



Practical consequence for token capture: AgentCore itself never sees the model request. The request leaves your process through whatever makes the actual call — a `boto3` `bedrock-runtime` client, called either directly or indirectly by MAF/Strands/LangChain/anything else. **So the most generic, framework-agnostic capture point is not a framework hook at all — it is a `botocore` event hook on the `bedrock-runtime` client**, one level below every framework in this document. This is the key design change in this revision and is the basis of the prompt file (`agentcore-tokenomics-capture-build-prompt.md`) delivered alongside this document.



### 9.2 Where usage is reported when the model is called through AgentCore



Calling Bedrock from inside an AgentCore entrypoint uses the same Bedrock Runtime API surface as calling it from anywhere else — AgentCore does not proxy or rewrite the model response. So the same two shapes from section 2.1/4.2 apply, confirmed again here because AgentCore samples usually use the `Converse` API **[D-16]**:



| API | Where usage lives | Coverage |

|---|---|---|

| `Converse` / `ConverseStream` | `response["usage"]`: `inputTokens`, `outputTokens`, `totalTokens`, and (model-dependent) `cacheReadInputTokens`, `cacheWriteInputTokens` **[D-16]** | All Bedrock models behind the unified Converse API |

| `InvokeModel` / `InvokeModelWithResponseStream` | **Not in the response body.** HTTP response headers: `x-amzn-bedrock-input-token-count`, `x-amzn-bedrock-output-token-count`, `x-amzn-bedrock-cache-read-input-token-count`, `x-amzn-bedrock-cache-write-input-token-count` **[W-32][W-33]** | Anthropic and Amazon models populate these headers; **AI21 and Cohere models on Bedrock do not expose token counts in headers at all** for raw `InvokeModel` calls **[W-33]**. Falls back to character-based estimation (section 2.2) for those, flagged `estimated = true` |



For `ConverseStream`/`InvokeModelWithResponseStream`, usage is very likely only available in the **final** event of the stream, not at the point the HTTP call returns — this mirrors the MAF `stream_result_hooks` gap (section 6.2) and the ADK partial-chunk gap (section 2.4). **[U]**, checklist item 14.



### 9.3 Primary capture point: a `botocore` event hook on `bedrock-runtime` (framework-agnostic)



`botocore` (the library every AWS SDK call in Python goes through, including calls made *by* MAF's or LangChain's Bedrock integrations) exposes an event system. Registering on the wildcard `after-call.bedrock-runtime.*` event fires once per Bedrock Runtime API call — `Converse`, `ConverseStream`, `InvokeModel`, `InvokeModelWithResponseStream` — **regardless of what code made the call**. This is the one hook that is genuinely identical whether the agent uses MAF, raw `boto3`, a future framework, or a mix of all three in the same process. **[A]**, design pattern grounded in the documented `botocore` events mechanism **[W-39]** and the token-location facts in 9.2.



```python

import contextvars, uuid



RUN_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("run_ctx", default={})



def register_bedrock_token_meter(client, emit):

    """Register once per boto3 'bedrock-runtime' client. Framework-agnostic: this client

    may belong to MAF's Bedrock chat client, a Strands BedrockModel, LangChain, or plain code.

    emit(level, usage, **tags) queues a usage_event row (see section 9.7 for the writer). [A]

    """

    def _after_call(http_response, parsed, model, params, **kw):

        operation = model.name  # Converse | ConverseStream | InvokeModel | InvokeModelWithResponseStream

        usage = parsed.get("usage")                      # Converse/ConverseStream body [D-16]

        estimated = False

        if usage is None:                                  # InvokeModel family: headers only [W-32][W-33]

            h = (http_response.headers if http_response is not None else {}) or {}

            usage = {

                "inputTokens":  int(h.get("x-amzn-bedrock-input-token-count", 0) or 0),

                "outputTokens": int(h.get("x-amzn-bedrock-output-token-count", 0) or 0),

                "cacheReadInputTokens":  int(h.get("x-amzn-bedrock-cache-read-input-token-count", 0) or 0),

                "cacheWriteInputTokens": int(h.get("x-amzn-bedrock-cache-write-input-token-count", 0) or 0),

            }

            if not any(usage.values()):

                estimated = True   # provider does not expose headers (AI21/Cohere) -- fall back to section 2.2

        ctx = RUN_CTX.get({})

        emit(level="call", usage=usage, model=params.get("modelId"), operation=operation,

             estimated=estimated, event_id=str(uuid.uuid4()), **ctx)



    client.meta.events.register("after-call.bedrock-runtime.*", _after_call)

```



**Why this, instead of adding a fifth row to the section 6 matrix:** the section 6 hooks (Strands `AfterModelCallEvent`, ADK `after_model_callback`, MAF `ChatMiddleware`) only fire if the *orchestration SDK itself* calls the model — they say nothing if a different framework (or none) is used. The `botocore` hook fires on the **transport**, so it is the only mechanism in this document that does not need to know, or care, which orchestration SDK — if any — is in use inside the AgentCore entrypoint. It composes with the section 6 hooks rather than replacing them: keep the MAF `ChatMiddleware` (section 6.2) if MAF is the orchestration layer, because only it can see the *request* contents needed for the bucket attribution in section 2.2 (context vs retrieval vs tool vs coordination); the `botocore` hook gives you the *measured* tier (section 2.1) with total certainty regardless of orchestration layer, as a safety net and cross-check.



### 9.4 Run-level capture: AgentCore Runtime lifecycle



`BedrockAgentCoreApp` extends Starlette and accepts a standard `middleware=` list **[W-34]**, so run-level timing and identity can be captured the same way the MAF `AgentMiddleware` captures a run in section 6.2 — just one layer further out, at the HTTP request boundary that AgentCore owns:



```python

from starlette.middleware import Middleware

from starlette.middleware.base import BaseHTTPMiddleware

import time, uuid



class RunMeterMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request, call_next):

        run_id = str(uuid.uuid4())

        session_id = request.headers.get("X-Amzn-Bedrock-AgentCore-Runtime-Session-Id")  # [D-27]

        token = RUN_CTX.set({"run_id": run_id, "session_id": session_id, "hosting": "agentcore_runtime"})

        t0 = time.monotonic()

        try:

            response = await call_next(request)

            return response

        finally:

            emit(level="run", usage=None, run_id=run_id, session_id=session_id,

                 latency_ms=int((time.monotonic() - t0) * 1000))

            RUN_CTX.reset(token)



app = BedrockAgentCoreApp(middleware=[Middleware(RunMeterMiddleware)])

```



Inside the entrypoint itself, the simpler native path is `context.session_id` on the `RequestContext` object AgentCore passes in **[D-27]** — use that when you only need identity, and the middleware above when you also need true request-boundary latency and a guaranteed `finally` for the run-level row even if the handler raises.



**Open question [U] (checklist item 15):** whether the `ContextVar` set in the middleware is visible from inside a **synchronous** `def handler(payload, context)` entrypoint, which Starlette/Uvicorn may run on a worker thread rather than the request's `asyncio.Task`. Confirm with both a sync and an async entrypoint before relying on this for user/session tagging in call-level rows. If it does not propagate reliably, fall back to passing `run_id`/`session_id` explicitly into whatever client factory builds the `bedrock-runtime` client per request.



### 9.5 Costs that are AgentCore's, not the model's (new `infra_cost` rows)



AgentCore bills Runtime/Browser/Code Interpreter compute, Gateway invocations, Memory events/records/retrievals, and Identity requests, **separately from model token cost**, on a consumption basis **[W-30][W-35]**. These are exactly the `infra_cost` rows the schema already has a table for (section 3.2) — they are not token counts and should never be added to `usage_event`.



| AgentCore component | Meter | Rate (third-party reported, **verify against the official AWS pricing page** [U]) |

|---|---|---|

| Runtime / Browser / Code Interpreter | vCPU-hour, active only | ~$0.0895 **[W-30][W-31]** |

| Runtime / Browser / Code Interpreter | GB-hour, peak memory, active only | ~$0.00945 **[W-30][W-31]** |

| Gateway | API invocation | ~$0.005 per 1,000 **[W-31]** |

| Gateway | Search API invocation | ~$0.025 per 1,000 **[W-31]** |

| Gateway | Tool indexing | ~$0.02 per 100 tools/month **[W-31]** |

| Memory | Short-term event | ~$0.25 per 1,000 **[W-31]** |

| Memory | Long-term record stored/month (built-in extraction) | ~$0.75 per 1,000 **[W-31]** |

| Memory | Long-term retrieval | ~$0.50 per 1,000 **[W-31]** |

| Identity | Token/API-key request (free via Runtime/Gateway) | ~$0.010 per 1,000 **[W-31]** |



Suggested `infra_cost.cost_type` values (free text, no DDL change needed): `agentcore_runtime_compute`, `agentcore_gateway_invocation`, `agentcore_gateway_search`, `agentcore_memory_short_term`, `agentcore_memory_long_term_storage`, `agentcore_memory_retrieval`, `agentcore_identity`. **[A]** These feed `run_cost` the same way the generic `infra_cost` allocation in section 4.1 already does — CPU/GB-hour is naturally time-based allocation (4.1a), Gateway/Memory counts are naturally share-based (4.1b).



### 9.6 Tagging AgentCore Memory/Gateway calls into the Splunk buckets (section 2)



If Memory or Gateway are in use, their calls are also `boto3` operations under the hood (service names `bedrock-agentcore` / `bedrock-agentcore-control`), so the same `botocore` event-hook pattern from 9.3 applies to tag them into buckets per the section 2.2 method: a `RetrieveMemoryRecords`-style call tags as **retrieval**; an `InvokeTool`-style Gateway call tags as **tool** by default, or **governance** if the tool is on the judge/guardrail allow-list from section 2.4. The exact operation names were not inspected against the live SDK in this revision — **[U]**, checklist item 16 — so treat this as a design pattern to validate, not a tested mapping.



### 9.7 Schema deltas (additive, no breaking changes to v2)



Two additive columns distinguish *what orchestration code* built a call (`framework`, already in the schema) from *where it physically ran* (new `hosting` column) — a MAF agent hosted on AgentCore should show `framework='maf', hosting='agentcore_runtime'`, while a raw-`boto3` call with no orchestration SDK shows `framework='raw', hosting='agentcore_runtime'`:



```sql

ALTER TABLE usage_event

    ADD COLUMN hosting text NOT NULL DEFAULT 'local'

        CHECK (hosting IN ('agentcore_runtime','ecs','eks','lambda','local'));



ALTER TABLE usage_event

    DROP CONSTRAINT usage_event_framework_check;

ALTER TABLE usage_event

    ADD CONSTRAINT usage_event_framework_check

        CHECK (framework IN ('strands','adk','maf','raw'));



CREATE INDEX usage_event_hosting_ts ON usage_event (hosting, ts);

```



`'raw'` covers calls made with no orchestration SDK at all — a case that becomes much more common once AgentCore is in the picture, since AgentCore does not require one. **[A]**, not yet run against PostgreSQL (checklist item, add to section 7 if adopted).



### 9.8 Observability: AgentCore's own dashboard is complementary, not a replacement



Setting `AGENT_OBSERVABILITY_ENABLED=true` plus the standard OTEL GenAI environment variables turns on a managed CloudWatch **GenAI Observability** dashboard with session counts, latency, and **token usage per call already broken out**, with no code to maintain **[D-28][W-35]**. This is useful for live debugging and as a cross-check against the PostgreSQL pipeline, but it does **not** give you: the Splunk bucket split (section 2), cost-per-accepted-task or token yield (section 4), the per-user spread view (section 5), or a durable FinOps store you can join against outcomes and budgets. Recommendation: keep both — CloudWatch for "is this agent healthy right now," PostgreSQL via sections 2-5 and 9.3-9.7 for "what did this cost and was it worth it." **[A]**



### 9.9 What is proven here, and what is not



| Proven **[D]/[W]** (documented, not executed by me) | Open **[U]** (design only, run once before relying on it) |

|---|---|

| `Converse`/`ConverseStream` usage location; `InvokeModel` header names and the AI21/Cohere gap | Whether the `after-call.bedrock-runtime.*` hook fires identically across MAF/raw/other clients in *your* code |

| `BedrockAgentCoreApp` is a Starlette subclass accepting `middleware=`; `RequestContext.session_id`; ARM64 + port 8080 requirement | Streaming (`ConverseStream`/`InvokeModelWithResponseStream`) usage capture timing |

| AgentCore's 12 billable components exist and bill independently of token cost | Exact AgentCore pricing figures (third-party sourced); Memory/Gateway operation names for bucket tagging |

| Sync-vs-async `ContextVar` propagation through Starlette's request lifecycle |



Full build-ready code, a Dockerfile, IAM policy, and step-by-step deployment commands for this design are in the companion prompt file, `agentcore-tokenomics-capture-build-prompt.md`, so that generating and testing the actual module is a tracked, reviewable step rather than inline code in this analysis document.



---



## References



D/W/S/T items D-1 through D-25 and W-3 through W-29 accessed 29 September 2026 (v2). Items D-26 through D-30 and W-30 through W-39 accessed 3 October 2026 (v3, AgentCore). Items marked PR/issue describe code at a point in time.



**Official documentation [D]**

- D-1. Splunk, *What is Agent Tokenomics?*: https://www.splunk.com/en_us/blog/artificial-intelligence/what-is-agent-tokenomics.html

- D-2. Strands, *Metrics*: https://strandsagents.com/docs/user-guide/observability-evaluation/metrics/

- D-3. Strands, *Traces*: https://strandsagents.com/latest/user-guide/observability-evaluation/traces

- D-9. Microsoft Learn, *Agent Framework observability*: https://learn.microsoft.com/en-us/agent-framework/agents/observability

- D-14. ADK docs, *Callbacks*: https://google.github.io/adk-docs/callbacks/

- D-15. ADK docs, *Plugins*: https://github.com/google/adk-docs/blob/main/docs/plugins/index.md

- D-16. AWS, *How tokens are counted in Amazon Bedrock*: https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html

- D-17. AWS, *Bedrock runtime metrics*: https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-runtime-metrics.html

- D-20. Microsoft Learn, *AI gateway in Azure API Management*: https://learn.microsoft.com/en-ca/azure/api-management/genai-gateway-capabilities

- D-21. Microsoft Learn, *Manage Azure OpenAI quota*: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota

- D-25. Google Cloud, *Generative AI rate limits*: https://cloud.google.com/vertex-ai/generative-ai/docs/quotas

- D-26. AWS, *Get started without the starter toolkit* (AgentCore Runtime, ARM64, port 8080): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/getting-started-custom.html

- D-27. AWS, *AgentCore Python SDK reference* (`RequestContext`, `ping`, `async_task`): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-python-sdk-reference.html

- D-28. AWS, *AgentCore Observability Quickstart*: https://aws.github.io/bedrock-agentcore-starter-toolkit/user-guide/observability/quickstart.html

- D-29. AWS, *Runtime troubleshooting* (session ID access, ping handler pattern): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-troubleshooting.html

- D-30. AWS CLI reference, `create-agent-runtime` (`networkMode`, VPC config): https://docs.aws.amazon.com/cli/v1/reference/bedrock-agentcore-control/create-agent-runtime.html



**Third-party, PRs and issues [W]**

- W-30. CloudBurn, *Amazon Bedrock AgentCore Pricing: 12 Components Breakdown*: https://cloudburn.io/blog/amazon-bedrock-agentcore-pricing

- W-31. Akal Cloud, *Amazon Bedrock AgentCore pricing (2026)*: https://akalcloud.ai/blog/bedrock-agentcore-pricing/

- W-32. `dd-trace` (Datadog), `bedrockruntime.js` instrumentation (Bedrock Runtime header names): https://app.unpkg.com/dd-trace@5.81.0/files/packages/dd-trace/src/llmobs/plugins/bedrockruntime.js

- W-33. Zilliz AI FAQ, *token usage metrics from Amazon Bedrock* (per-provider header-exposure gap): https://zilliz.com/ai-faq/is-it-possible-to-get-token-usage-metrics-or-other-usage-details-from-ama…

- W-34. AWS, AgentCore Starter Toolkit, *Runtime Overview* (Starlette `middleware=`, `context.request`): https://aws.github.io/bedrock-agentcore-starter-toolkit/user-guide/runtime/overview.html

- W-35. PyPI, `splunk-otel-instrumentation-bedrock-agentcore` (trace hierarchy, `gen_ai.*` attributes, `DISABLE_ADOT_OBSERVABILITY`): https://pypi.org/project/splunk-otel-instrumentation-bedrock-agentcore/

- W-36. AWS, Modern Data Architecture Accelerator, *Bedrock AgentCore Runtime L3 Construct* (execution-role IAM permissions list, VPC requirement, `enableTransactionSearch`): https://aws.github.io/modern-data-architecture-accelerator/packages/constructs/L3/ai/bedrock-agentc…

- W-37. GitHub, `aws/bedrock-agentcore-sdk-python` README ("any framework and model", Strands/LangGraph/CrewAI/Autogen examples): https://github.com/aws/bedrock-agentcore-sdk-python

- W-38. dev.to, *AWS AgentCore Blueprints* (AgentCore GA date, 13 services, framework-agnostic claim): https://dev.to/tarekcheikh/aws-agentcore-blueprints-a-free-36-chapter-field-manual-27n4

- W-39. `botocore` events documentation, referenced via AWS's own guidance on using event handlers with AgentCore Runtime headers: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-header-allowlist.html

- W-3. strands-agents/sdk-python PR #961 (multi-agent node usage): https://github.com/strands-agents/sdk-python/pull/961

- W-11. SigNoz, *Microsoft Agent Framework observability*: https://signoz.io/docs/microsoft-agent-framework-observability/

- W-17. Betiku, *Production-grade observability in Google ADK* (Medium, Jan 2026): https://medium.com/@betikuoluwatobi7/beyond-print-building-production-grade-observability-in-google…

- W-18. aws-samples, *Quota dashboard for Amazon Bedrock*: https://github.com/aws-samples/sample-quota-dashboard-for-amazon-bedrock

- W-19. AWS re:Post, *TPM/RPM quota monitoring dashboard for Bedrock*: https://repost.aws/articles/ARfUsSkaWeSLiWZbv0OVSG1Q/tpm-rpm-quota-monitoring-dashboard-for-amazon-…

- W-22. Siebler, *Azure OpenAI x-ratelimit headers*: https://clemenssiebler.com/posts/understanding-azure-openai-x-ratelimit-remaining-tokens-x-ratelimi…

- W-23. Microsoft Community Hub, *Optimizing Azure OpenAI: limits and quotas*: https://techcommunity.microsoft.com/blog/fasttrackforazureblog/optimizing-azure-openai-a-guide-to-l…

- W-24. Dynatrace, *GCP Vertex AI metrics*: https://docs.dynatrace.com/docs/setup-and-configuration/google-cloud-platform/gcp-integrations/gcp-…

- W-28. TraceAI Strands integration (PyPI): https://pypi.org/project/traceai-strands/

- W-29. DEV Community, *Strands Agents with Langfuse*: https://dev.to/aws/building-strands-agents-with-a-few-lines-of-code-observability-and-with-langfuse…



**Source inspected [S]** (installed from PyPI at the pinned versions): `strands/types/event_loop.py`, `strands/types/agent.py`, `strands/telemetry/{metrics,metrics_constants,tracer}.py`, `strands/hooks/events.py`, `strands/event_loop/event_loop.py`, `strands/agent/{agent,agent_result}.py`; `google/adk/plugins/{base_plugin,bigquery_agent_analytics_plugin}.py`, `google/adk/runners.py`, `google/adk/apps/app.py`, `google/adk/agents/{run_config,readonly_context}.py`, `google/adk/flows/llm_flows/core/_model_call.py`, `google/adk/models/{llm_request,llm_response}.py`, `google/genai/types` (2.25.0); `agent_framework/{_types,_middleware,_sessions,_tools,_compaction,observability}.py`.



**Test scripts [T]:** `sql/schema.sql`, `sql/test_schema.py` (PostgreSQL 16.2), `tests/test_strands_1_57_1.py`, `tests/test_adk_2_10_0.py`, `tests/test_maf_1_19_0.py`, `tests/requirements-pinned.txt`.