"""
tokenomics_capture/config.py
Settings loaded once from environment variables.
Only TOKENOMICS_BATCH_SIZE and TOKENOMICS_FLUSH_INTERVAL_S are exposed as tuning knobs
(KISS: do not add more without a real requirement).
"""
import os
from dataclasses import dataclass, field


@dataclass
class TokenomicsConfig:
    app: str = "unset"
    team: str | None = None
    batch_size: int = 100
    flush_interval_s: float = 2.0
    # PostgreSQL connection string for PgWriter (required when persistence_backend='postgresql')
    db_dsn: str | None = None
    # 'postgresql' | 'json'  — switches writer backend; defaults to 'json' for local/test use
    persistence_backend: str = "json"
    # Directory for JsonWriter output files (used when persistence_backend='json')
    json_data_dir: str = "./data"
    # App-owned mapping: tool_name -> bucket ('retrieval' | 'tool' | 'governance')
    # Populated by the app team per section 2.2/2.4 of the companion document.
    tool_bucket_map: dict = field(default_factory=dict)
    # Bedrock model ID used by the AgentCore entrypoint handler in app.py.
    # Overriding this env var selects a different model without redeploying.
    bedrock_model_id: str = "anthropic.claude-3-haiku-20240307-v1:0"
    # AWS region for the boto3 bedrock-runtime client.
    # boto3 also reads AWS_REGION / AWS_DEFAULT_REGION automatically, but keeping
    # it here gives one explicit source of truth visible via TokenomicsConfig.
    bedrock_region: str = "us-east-1"

    @classmethod
    def from_env(cls) -> "TokenomicsConfig":
        return cls(
            app=os.getenv("TOKENOMICS_APP", "unset"),
            team=os.getenv("TOKENOMICS_TEAM"),
            batch_size=int(os.getenv("TOKENOMICS_BATCH_SIZE", "100")),
            flush_interval_s=float(os.getenv("TOKENOMICS_FLUSH_INTERVAL_S", "2.0")),
            db_dsn=os.getenv("NAIE_DB_DSN"),
            persistence_backend=os.getenv("PERSISTENCE_BACKEND", "json"),
            json_data_dir=os.getenv("JSON_DATA_DIR", "./data"),
            bedrock_model_id=os.getenv(
                "BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"
            ),
            bedrock_region=os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or "us-east-1",
        )


# Module-level singleton loaded from environment at import time.
settings = TokenomicsConfig.from_env()
