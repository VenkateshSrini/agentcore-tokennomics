"""
tokenomics_capture/json_writer.py
JSON Lines writer for local development and testing.
Writes the same row shape as PgWriter to .jsonl files in a configured directory.
One file per table; safe for concurrent async use via a per-file asyncio Lock.
"""
import asyncio
import datetime
import json
import logging
from pathlib import Path
from typing import Sequence

from .meter import UsageEvent
from .pg_writer import event_to_row  # DRY: reuse the single row-shape function

logger = logging.getLogger(__name__)


def _serialise(value) -> object:
    """Convert types that are not JSON-serialisable by default."""
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    return value


def _row_to_json(row: dict) -> str:
    return json.dumps({k: _serialise(v) for k, v in row.items()})


class JsonWriter:
    """Writes usage_event batches to JSON Lines files for local/test use.
    Each call to write_batch appends rows to <data_dir>/usage_event.jsonl.
    Reads back: `jq '.' data/usage_event.jsonl` or `pandas.read_json(..., lines=True)`.
    """

    def __init__(self, data_dir: str):
        self._data_dir = Path(data_dir)
        # Per-file locks prevent interleaved writes in concurrent async code
        self._lock = asyncio.Lock()

    async def initialise(self) -> None:
        """Create the data directory if it does not exist."""
        await asyncio.to_thread(self._data_dir.mkdir, **{"parents": True, "exist_ok": True})

    async def write_batch(self, batch: Sequence[UsageEvent]) -> None:
        if not batch:
            return
        rows = [event_to_row(ev) for ev in batch]
        target = self._data_dir / "usage_event.jsonl"
        async with self._lock:
            await asyncio.to_thread(self._append_jsonl, target, rows)

    def _append_jsonl(self, path: Path, rows: list[dict]) -> None:
        with path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(_row_to_json(row) + "\n")

    async def close(self) -> None:
        pass  # Nothing to close for a file-backed writer
