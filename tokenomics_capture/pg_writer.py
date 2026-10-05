"""
tokenomics_capture/pg_writer.py
Batched async INSERT into usage_event / run_outcome using SQLAlchemy + asyncpg.
Also defines the SQLAlchemy table metadata (shared with json_writer for the row shape).
"""
import datetime
import logging
from typing import Sequence

import sqlalchemy as sa
from sqlalchemy import JSON
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .meter import UsageEvent

logger = logging.getLogger(__name__)

# JSONB on PostgreSQL, JSON on SQLite (used in tests).
try:
    from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB
    _JSONB = _PG_JSONB().with_variant(JSON(), "sqlite")
except ImportError:  # pragma: no cover
    _JSONB = JSON()

metadata = sa.MetaData()

usage_event_table = sa.Table(
    "usage_event",
    metadata,
    sa.Column("event_id", sa.String(36), primary_key=True),
    sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text),
    sa.Column("team", sa.Text),
    sa.Column("app", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text),
    # user_id should be hashed/pseudonymised by the caller — not enforced here (section 5)
    sa.Column("agent_name", sa.Text, nullable=False, default="unknown"),
    sa.Column("framework", sa.Text, nullable=False, default="raw"),
    sa.Column("hosting", sa.Text, nullable=False, default="local"),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False, default="unknown"),
    sa.Column("level", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, default="ok"),
    sa.Column("estimated", sa.Boolean, nullable=False, default=False),
    sa.Column("input_tokens", sa.BigInteger, nullable=False, default=0),
    sa.Column("cache_read_tokens", sa.BigInteger, nullable=False, default=0),
    sa.Column("cache_write_tokens", sa.BigInteger, nullable=False, default=0),
    sa.Column("output_tokens", sa.BigInteger, nullable=False, default=0),
    sa.Column("reasoning_tokens", sa.BigInteger, nullable=False, default=0),
    sa.Column("details", _JSONB, nullable=False, default={}),
)

run_outcome_table = sa.Table(
    "run_outcome",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("app", sa.Text, nullable=False),
    sa.Column("accepted", sa.Boolean),
    sa.Column("recorded_at", sa.DateTime(timezone=True)),
    sa.Column("details", _JSONB, nullable=False, default={}),
)


def event_to_row(ev: UsageEvent) -> dict:
    """Convert a UsageEvent to a dict suitable for INSERT into usage_event_table.
    Exported so json_writer can reuse the same shape without reimplementing it (DRY).
    """
    return {
        "event_id": ev.event_id,
        "ts": datetime.datetime.fromtimestamp(ev.ts, tz=datetime.timezone.utc),
        "run_id": ev.run_id or "unset",
        "session_id": ev.session_id,
        "team": ev.team,
        "app": ev.app or "unset",
        "user_id": ev.user_id,
        "agent_name": ev.agent_name or "unknown",
        "framework": ev.framework or "raw",
        "hosting": ev.hosting or "local",
        "provider": ev.provider or "aws.bedrock",
        "model": ev.model or "unknown",
        "level": ev.level,
        "status": "ok",
        "estimated": ev.estimated,
        "input_tokens": ev.input_tokens,
        "cache_read_tokens": ev.cache_read_tokens,
        "cache_write_tokens": ev.cache_write_tokens,
        "output_tokens": ev.output_tokens,
        "reasoning_tokens": ev.reasoning_tokens,
        "details": ev.details or {},
    }


class PgWriter:
    """Writes usage_event batches to PostgreSQL (or any SQLAlchemy-supported database)
    using async SQLAlchemy. For PostgreSQL, asyncpg is the driver.
    For tests, pass a sqlite+aiosqlite:// DSN."""

    def __init__(self, dsn: str):
        async_dsn = dsn
        # Auto-upgrade bare postgresql:// to the asyncpg driver dialect
        if dsn.startswith("postgresql://"):
            async_dsn = "postgresql+asyncpg://" + dsn[len("postgresql://"):]
        elif dsn.startswith("postgres://"):
            async_dsn = "postgresql+asyncpg://" + dsn[len("postgres://"):]
        # sqlite+aiosqlite:// and other fully-specified DSNs are passed through unchanged

        is_sqlite = async_dsn.startswith("sqlite")
        engine_kwargs: dict = {}
        if not is_sqlite:
            engine_kwargs["pool_pre_ping"] = True

        self._engine: AsyncEngine = create_async_engine(async_dsn, **engine_kwargs)

    async def initialise(self) -> None:
        """Create tables if they do not exist. Useful for dev/test; in production
        run sql/schema.sql against your database directly."""
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def write_batch(self, batch: Sequence[UsageEvent]) -> None:
        if not batch:
            return
        rows = [event_to_row(ev) for ev in batch]
        async with self._engine.begin() as conn:
            await conn.execute(usage_event_table.insert(), rows)

    async def close(self) -> None:
        await self._engine.dispose()
