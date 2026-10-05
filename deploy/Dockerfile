# ARM64 base image — AgentCore Runtime requires linux/arm64 [D-26]
FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# Install dependencies first (layer-cache friendly)
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-cache --extra agentcore

COPY . .

# AgentCore Runtime requires port 8080 [D-26]
EXPOSE 8080

# Managed CloudWatch GenAI Observability [D-28]
ENV AGENT_OBSERVABILITY_ENABLED=true
# Required pairing for AgentCore's OTEL pipeline [D-28]
ENV OTEL_PYTHON_DISTRO=aws_distro
ENV OTEL_PYTHON_CONFIGURATOR=aws_configurator

CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
