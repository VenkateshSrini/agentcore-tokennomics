# Agent Tokenomics Capture — Deployment Runbook

This README covers building, deploying, and validating the `tokenomics_capture` module on
Amazon Bedrock AgentCore Runtime.

---

## Prerequisites

- AWS CLI v2 configured with credentials for your target account/region
- Docker (buildx with `linux/arm64` support)
- Python 3.11+, `uv` or `pip`
- A PostgreSQL database reachable from the AgentCore VPC (NAIE DB)
- An ECR repository for the container image

---

## 1. Build the ARM64 container image

AgentCore Runtime requires `linux/arm64` [D-26]:

```bash
docker buildx build --platform linux/arm64 \
  -t agent-tokenomics:arm64 \
  -f deploy/Dockerfile \
  --load .
```

---

## 2. Local smoke test (before pushing to ECR)

```bash
docker run --platform linux/arm64 -p 8080:8080 \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN \
  -e AWS_REGION \
  -e PERSISTENCE_BACKEND=json \
  -e JSON_DATA_DIR=/tmp/tokenomics \
  -e TOKENOMICS_APP=smoke-test \
  agent-tokenomics:arm64

# In a second terminal:
curl http://localhost:8080/ping
# Expected: {"status": "ok"}
```

---

## 3. Create the IAM execution role

```bash
# Create the role with the trust policy
aws iam create-role \
  --role-name AgentCoreTokenomicsExecutionRole \
  --assume-role-policy-document file://deploy/execution-role-trust-policy.json

# Attach the permissions policy
aws iam put-role-policy \
  --role-name AgentCoreTokenomicsExecutionRole \
  --policy-name AgentCoreTokenomicsPolicy \
  --policy-document file://deploy/execution-role-policy.json
```

> **Scope model ARNs** in `execution-role-policy.json` to only the model IDs your agent
> actually uses — do not leave them as wildcards in production.

---

## 4. Push the image to ECR

```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=us-east-1  # change to your region

aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin \
  $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

docker tag agent-tokenomics:arm64 \
  $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/agent-tokenomics:latest

docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/agent-tokenomics:latest
```

---

## 5. Apply the database schema

Run once against your PostgreSQL (NAIE DB) before deploying the runtime:

```bash
psql "$NAIE_DB_DSN" -f sql/schema.sql
```

---

## 6. Configure and deploy the AgentCore Runtime

### Option A: AgentCore Starter Toolkit CLI

```bash
# Install the toolkit
pip install bedrock-agentcore-starter-toolkit

# Edit deploy/agentcore.yaml — fill in VPC, subnets, security groups, ECR image, role ARN

# Configure then launch
agentcore configure --config deploy/agentcore.yaml
agentcore launch
```

### Option B: Raw AWS CLI

```bash
aws bedrock-agentcore-control create-agent-runtime \
  --agent-runtime-name agent-tokenomics \
  --agent-runtime-artifact containerConfiguration="{imageUri=$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/agent-tokenomics:latest}" \
  --execution-role-arn arn:aws:iam::$AWS_ACCOUNT_ID:role/AgentCoreTokenomicsExecutionRole \
  --network-configuration "networkMode=VPC,vpcConfig={subnetIds=[subnet-REPLACE],securityGroupIds=[sg-REPLACE]}" \
  --environment-variables "PERSISTENCE_BACKEND=postgresql,NAIE_DB_DSN=<dsn>,TOKENOMICS_APP=my-app,AGENT_OBSERVABILITY_ENABLED=true"
```

> **`networkMode: VPC` is required** if the agent writes to a private PostgreSQL instance.
> `PUBLIC` mode has no route to a VPC-internal RDS/PostgreSQL endpoint [D-30].

---

## 7. Remote smoke test

```bash
# After agentcore launch completes:
agentcore invoke '{"prompt": "Hello, what is 2+2?"}'

# Expected: a JSON response from your agent handler in app.py
```

---

## 8. Verify the first real run (checklist items 13-17)

After the first production invocation, check the following before relying on the data for billing:

| # | What to check | Where |
|---|---|---|
| 13 | `after-call.bedrock-runtime.*` botocore hook fires for your client (MAF/raw/other) | CloudWatch Logs — look for rows in usage_event with `estimated=false` |
| 14 | Streaming usage capture: `ConverseStream` / `InvokeModelWithResponseStream` — does usage appear? | Query `SELECT estimated, sum(output_tokens) FROM usage_event WHERE model LIKE '%' GROUP BY estimated` — streaming calls may show `estimated=true` |
| 15 | `RUN_CTX` ContextVar visible from sync `def entrypoint(payload, context)` | Add a log line inside your entrypoint: `print(RUN_CTX.get({}))` and check the run_id appears |
| 16 | AgentCore Memory/Gateway operation names (if using those services) | Check `details.service` column in usage_event for 'agentcore_memory' / 'agentcore_gateway' rows |
| 17 | Verify token prices in `pricing.py` against the [AWS Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/) | Query `SELECT provider, model, sum(input_tokens), sum(output_tokens) FROM usage_event GROUP BY 1,2` and cross-check with your AWS bill |

---

## Environment variables reference

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `PERSISTENCE_BACKEND` | No | `json` | `postgresql` or `json` — selects the writer backend |
| `NAIE_DB_DSN` | When postgresql | — | PostgreSQL connection string (`postgresql://user:pass@host/db`) |
| `JSON_DATA_DIR` | No | `./data` | Directory for JSON Lines output files (json backend only) |
| `TOKENOMICS_APP` | Yes | `unset` | App name written to every usage_event row |
| `TOKENOMICS_TEAM` | No | — | Team name written to every usage_event row |
| `TOKENOMICS_BATCH_SIZE` | No | `100` | Events per write batch |
| `TOKENOMICS_FLUSH_INTERVAL_S` | No | `2.0` | Flush interval in seconds |
| `AGENT_OBSERVABILITY_ENABLED` | No | — | Set `true` to enable managed CloudWatch GenAI dashboard [D-28] |
| `OTEL_PYTHON_DISTRO` | No | — | Set `aws_distro` when using AgentCore's OTEL pipeline [D-28] |
| `OTEL_PYTHON_CONFIGURATOR` | No | — | Set `aws_configurator` when using AgentCore's OTEL pipeline [D-28] |
| `AWS_REGION` | Yes | — | AWS region for the Bedrock client |

---

## Running tests locally

```bash
# Install test dependencies
pip install -e ".[test]"

# Run all tests (no live AWS, no PostgreSQL required)
pytest tests/ -v

# Run a specific test file
pytest tests/test_bedrock_hook.py -v
pytest tests/test_buckets.py -v
```

The test suite uses SQLite (via `aiosqlite`) and Starlette `TestClient` as stand-ins —
no live AWS account or PostgreSQL is required to run tests.
