-- Agent Tokenomics: PostgreSQL schema (v3)
-- Base schema from companion document section 3.2, with v3 deltas from section 9.7 applied.
-- Rule: real columns only for what we filter, group, join, or SUM on.
-- Everything else lives in the JSONB details column.

-- ---------------------------------------------------------------------------
-- Main event table: one row per LLM call (level='call') or per run (level='run')
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_event (
    event_id            uuid        NOT NULL DEFAULT gen_random_uuid(),
    ts                  timestamptz NOT NULL,
    run_id              text        NOT NULL,
    session_id          text,
    team                text,
    app                 text        NOT NULL,
    user_id             text,           -- hashed/pseudonymised at write time (section 5)
    agent_name          text        NOT NULL DEFAULT 'unknown',
    -- framework: which orchestration SDK built the call (section 9.7)
    framework           text        NOT NULL DEFAULT 'raw'
                            CHECK (framework IN ('strands', 'adk', 'maf', 'raw')),
    -- hosting: where the agent container is physically running (section 9.7)
    hosting             text        NOT NULL DEFAULT 'local'
                            CHECK (hosting IN ('agentcore_runtime', 'ecs', 'eks', 'lambda', 'local')),
    provider            text        NOT NULL,
    model               text        NOT NULL DEFAULT 'unknown',
    level               text        NOT NULL CHECK (level IN ('call', 'run')),
    status              text        NOT NULL DEFAULT 'ok',
    estimated           boolean     NOT NULL DEFAULT false,
    -- Normalised token counts (section 4.2): input_tokens EXCLUDES cache
    input_tokens        bigint      NOT NULL DEFAULT 0,
    cache_read_tokens   bigint      NOT NULL DEFAULT 0,
    cache_write_tokens  bigint      NOT NULL DEFAULT 0,
    output_tokens       bigint      NOT NULL DEFAULT 0,
    reasoning_tokens    bigint      NOT NULL DEFAULT 0,
    details             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (event_id)
);

CREATE INDEX IF NOT EXISTS usage_event_ts_brin       ON usage_event USING brin (ts);
CREATE INDEX IF NOT EXISTS usage_event_team_app_ts   ON usage_event (team, app, ts);
CREATE INDEX IF NOT EXISTS usage_event_user_ts       ON usage_event (user_id, ts) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS usage_event_agent_ts      ON usage_event (agent_name, ts);
CREATE INDEX IF NOT EXISTS usage_event_run           ON usage_event (run_id);
CREATE INDEX IF NOT EXISTS usage_event_session       ON usage_event (session_id) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS usage_event_model_ts      ON usage_event (provider, model, ts);
CREATE INDEX IF NOT EXISTS usage_event_hosting_ts    ON usage_event (hosting, ts);

-- ---------------------------------------------------------------------------
-- Price catalog: token prices per provider/model, versioned by effective_from date
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_catalog (
    provider                text        NOT NULL,
    model                   text        NOT NULL,
    effective_from          timestamptz NOT NULL,
    input_per_mtok          numeric(14,6) NOT NULL,
    cache_read_per_mtok     numeric(14,6) NOT NULL DEFAULT 0,
    cache_write_per_mtok    numeric(14,6) NOT NULL DEFAULT 0,
    output_per_mtok         numeric(14,6) NOT NULL,
    reasoning_billed_separately boolean NOT NULL DEFAULT false,
    currency                text        NOT NULL DEFAULT 'USD',
    details                 jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (provider, model, effective_from)
);

-- ---------------------------------------------------------------------------
-- Infrastructure cost: amortised hosting/infra cost per app/period (section 4.1)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS infra_cost (
    id              bigserial   PRIMARY KEY,
    app             text        NOT NULL,
    period_start    timestamptz NOT NULL,
    period_end      timestamptz NOT NULL,
    cost_type       text        NOT NULL,    -- 'compute' | 'vector_db' | 'gateway' | ...
    amount          numeric(14,4) NOT NULL,
    currency        text        NOT NULL DEFAULT 'USD',
    details         jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS infra_cost_app_period ON infra_cost (app, period_start);

-- ---------------------------------------------------------------------------
-- Run outcome: accepted/rejected signal for token yield metric (section 4.3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_outcome (
    run_id          text        PRIMARY KEY,
    app             text        NOT NULL,
    accepted        boolean,
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    -- details: eval_score, human_review_minutes, review_cost, eval_tokens
    details         jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS run_outcome_app_time ON run_outcome (app, recorded_at);

-- ---------------------------------------------------------------------------
-- Quota snapshot: provider-side rate limit utilisation (section 3.4)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quota_snapshot (
    ts              timestamptz NOT NULL,
    provider        text        NOT NULL,
    region          text,
    target          text        NOT NULL,    -- model ID or deployment name
    metric          text        NOT NULL,    -- 'tpm' | 'rpm' | 'throttles'
    used            numeric,
    limit_value     numeric,
    details         jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS quota_snapshot_lookup ON quota_snapshot (provider, target, metric, ts);

-- ---------------------------------------------------------------------------
-- Budget: per-scope spend/token caps (section 3.3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS budget (
    id              bigserial   PRIMARY KEY,
    scope_type      text        NOT NULL CHECK (scope_type IN ('user', 'agent', 'team', 'app')),
    scope_id        text        NOT NULL,
    period          text        NOT NULL CHECK (period IN ('day', 'week', 'month')),
    action          text        NOT NULL DEFAULT 'alert' CHECK (action IN ('alert', 'block')),
    details         jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- token_limit, cost_limit, notify
    UNIQUE (scope_type, scope_id, period)
);

-- ---------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------

-- One row per run with no double-counting: prefer call rows when available (section 3.2)
CREATE OR REPLACE VIEW v_usage_effective AS
SELECT u.*
FROM usage_event u
WHERE u.level = 'call'
   OR NOT EXISTS (
       SELECT 1 FROM usage_event c WHERE c.run_id = u.run_id AND c.level = 'call'
   );

-- Cost per event; price chosen as latest effective_from <= event ts
CREATE OR REPLACE VIEW v_usage_cost AS
SELECT u.*,
       p.currency,
       (
           u.input_tokens       * p.input_per_mtok
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

-- Run-level cost rollup
CREATE OR REPLACE VIEW v_run_cost AS
SELECT  run_id,
        min(ts)             AS started_at,
        max(app)            AS app,
        max(team)           AS team,
        max(user_id)        AS user_id,
        max(agent_name)     AS agent_name,
        count(*)            AS llm_calls,
        sum(input_tokens + cache_read_tokens + cache_write_tokens + output_tokens) AS total_tokens,
        sum(token_cost)     AS token_cost,
        bool_or(estimated)  AS any_estimated,
        bool_or(price_missing) AS any_price_missing
FROM v_usage_cost
GROUP BY run_id;
