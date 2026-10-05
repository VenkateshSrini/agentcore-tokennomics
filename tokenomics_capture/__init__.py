"""
tokenomics_capture/__init__.py
Public API for the tokenomics_capture package.
"""
from .config import TokenomicsConfig, settings
from .meter import Meter, UsageEvent
from .bedrock_hook import register_bedrock_token_meter, RUN_CTX
from .agentcore_runtime import RunMeterMiddleware, session_id_from_context, tokenomics_lifespan
from .pricing import get_price, normalise_usage, compute_token_cost
from .buckets import attribute_buckets
from .strands_adapter import StrandsTokenMeter
from .adk_adapter import ADKTokenMeter


def create_meter(config: TokenomicsConfig | None = None) -> Meter:
    """Factory: creates a Meter backed by the writer selected by PERSISTENCE_BACKEND.
    Defaults to JsonWriter (json) for local/test use; set PERSISTENCE_BACKEND=postgresql
    and NAIE_DB_DSN for production PostgreSQL persistence.
    """
    cfg = config or settings
    writer = _make_writer(cfg)
    return Meter(writer=writer, config=cfg)


def _make_writer(config: TokenomicsConfig):
    if config.persistence_backend == "postgresql":
        from .pg_writer import PgWriter
        if not config.db_dsn:
            raise ValueError(
                "NAIE_DB_DSN must be set when PERSISTENCE_BACKEND=postgresql"
            )
        return PgWriter(config.db_dsn)
    from .json_writer import JsonWriter
    return JsonWriter(config.json_data_dir)


__all__ = [
    "TokenomicsConfig",
    "settings",
    "Meter",
    "UsageEvent",
    "create_meter",
    "register_bedrock_token_meter",
    "RUN_CTX",
    "RunMeterMiddleware",
    "session_id_from_context",
    "tokenomics_lifespan",
    "get_price",
    "normalise_usage",
    "compute_token_cost",
    "attribute_buckets",
    "StrandsTokenMeter",
    "ADKTokenMeter",
]
