"""
tests/test_pg_writer.py
Tests for PgWriter and JsonWriter using an in-process SQLite database (aiosqlite).
No live PostgreSQL required to run — aiosqlite provides a compatible async engine.

The same SQLAlchemy metadata is used for both backends, so table-creation and row-shape
correctness are tested here against a real (SQLite) database engine.
"""
import datetime

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tokenomics_capture.config import TokenomicsConfig
from tokenomics_capture.json_writer import JsonWriter
from tokenomics_capture.meter import UsageEvent
from tokenomics_capture.pg_writer import PgWriter, metadata, usage_event_table


def _make_event(**overrides) -> UsageEvent:
    defaults = dict(
        event_id="evt-test-001",
        ts=datetime.datetime(2026, 10, 3, 5, 0, 0, tzinfo=datetime.timezone.utc).timestamp(),
        level="call",
        framework="raw",
        hosting="agentcore_runtime",
        run_id="run-test-001",
        session_id="sess-001",
        app="test-app",
        team="test-team",
        model="anthropic.claude-3-haiku-20240307-v1:0",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=10,
        cache_write_tokens=0,
        reasoning_tokens=0,
        estimated=False,
    )
    defaults.update(overrides)
    return UsageEvent(**defaults)


# ---------------------------------------------------------------------------
# PgWriter tests (SQLite backend via aiosqlite)
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def pg_writer(tmp_path):
    dsn = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    writer = PgWriter(dsn)
    await writer.initialise()
    yield writer
    await writer.close()


@pytest.mark.asyncio
async def test_pg_writer_inserts_single_event(pg_writer):
    ev = _make_event()
    await pg_writer.write_batch([ev])

    async with pg_writer._engine.connect() as conn:
        result = await conn.execute(
            sa.select(usage_event_table).where(usage_event_table.c.event_id == "evt-test-001")
        )
        rows = result.fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row.event_id == "evt-test-001"
    assert row.input_tokens == 100
    assert row.output_tokens == 50
    assert row.cache_read_tokens == 10
    assert row.level == "call"
    assert row.framework == "raw"
    assert row.hosting == "agentcore_runtime"
    assert row.app == "test-app"
    assert row.estimated is False


@pytest.mark.asyncio
async def test_pg_writer_inserts_batch(pg_writer):
    events = [
        _make_event(event_id=f"evt-{i}", run_id="run-batch", input_tokens=i * 10)
        for i in range(5)
    ]
    await pg_writer.write_batch(events)

    async with pg_writer._engine.connect() as conn:
        result = await conn.execute(
            sa.select(sa.func.count()).select_from(usage_event_table)
        )
        count = result.scalar()

    assert count == 5


@pytest.mark.asyncio
async def test_pg_writer_empty_batch_no_error(pg_writer):
    """write_batch([]) must succeed silently."""
    await pg_writer.write_batch([])


@pytest.mark.asyncio
async def test_pg_writer_run_level_event(pg_writer):
    ev = _make_event(event_id="evt-run-001", level="run", run_id="run-002")
    await pg_writer.write_batch([ev])

    async with pg_writer._engine.connect() as conn:
        result = await conn.execute(
            sa.select(usage_event_table.c.level).where(
                usage_event_table.c.event_id == "evt-run-001"
            )
        )
        row = result.fetchone()

    assert row.level == "run"


@pytest.mark.asyncio
async def test_pg_writer_estimated_flag_persisted(pg_writer):
    ev = _make_event(event_id="evt-est-001", estimated=True, input_tokens=0, output_tokens=0)
    await pg_writer.write_batch([ev])

    async with pg_writer._engine.connect() as conn:
        result = await conn.execute(
            sa.select(usage_event_table.c.estimated).where(
                usage_event_table.c.event_id == "evt-est-001"
            )
        )
        row = result.fetchone()

    assert row.estimated is True


# ---------------------------------------------------------------------------
# JsonWriter tests
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def json_writer(tmp_path):
    writer = JsonWriter(str(tmp_path))
    await writer.initialise()
    return writer


@pytest.mark.asyncio
async def test_json_writer_creates_jsonl_file(json_writer, tmp_path):
    ev = _make_event(event_id="evt-json-001")
    await json_writer.write_batch([ev])

    jsonl_file = tmp_path / "usage_event.jsonl"
    assert jsonl_file.exists()

    import json
    lines = jsonl_file.read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event_id"] == "evt-json-001"
    assert row["input_tokens"] == 100


@pytest.mark.asyncio
async def test_json_writer_appends_multiple_batches(json_writer, tmp_path):
    for i in range(3):
        await json_writer.write_batch([_make_event(event_id=f"evt-json-{i}")])

    import json
    lines = (tmp_path / "usage_event.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3
    ids = {json.loads(ln)["event_id"] for ln in lines}
    assert ids == {"evt-json-0", "evt-json-1", "evt-json-2"}


@pytest.mark.asyncio
async def test_json_writer_empty_batch_no_file(json_writer, tmp_path):
    """write_batch([]) must not create the file (nothing to write)."""
    await json_writer.write_batch([])
    assert not (tmp_path / "usage_event.jsonl").exists()


@pytest.mark.asyncio
async def test_json_writer_serialises_datetime(json_writer, tmp_path):
    ev = _make_event(event_id="evt-dt-001")
    await json_writer.write_batch([ev])

    import json
    row = json.loads((tmp_path / "usage_event.jsonl").read_text())
    # ts field is a datetime serialised as ISO string
    assert isinstance(row["ts"], str)
    assert "T" in row["ts"]  # ISO 8601 datetime string
