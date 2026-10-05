"""
tokenomics_capture/meter.py
The ONE place a usage_event row gets built (DRY boundary).
Every adapter calls Meter.emit() — none of them talk to the writer directly.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from .pricing import normalise_usage

logger = logging.getLogger(__name__)

# All tags that map directly to UsageEvent fields (not packed into details).
_EVENT_FIELDS = frozenset({
    "event_id", "ts", "framework", "hosting", "run_id", "session_id",
    "app", "team", "user_id", "agent_name", "provider", "model", "estimated",
})


@dataclass
class UsageEvent:
    event_id: str
    ts: float
    level: str                  # 'call' | 'run'
    framework: str              # 'maf' | 'raw' | 'strands' | 'adk'  (section 9.7)
    hosting: str                # 'agentcore_runtime' | 'ecs' | 'eks' | 'lambda' | 'local'
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
    agentcore_services) calls emit() and nothing else — no adapter talks to the writer directly.
    This is the DRY boundary: one queue, one batching flush, one row shape."""

    def __init__(self, writer, config, maxsize: int = 10_000):
        self._queue: asyncio.Queue[UsageEvent] = asyncio.Queue(maxsize=maxsize)
        self._writer = writer
        self._config = config

    async def initialise(self) -> None:
        """Initialise the backing writer (create tables / directories as needed)."""
        if hasattr(self._writer, "initialise"):
            await self._writer.initialise()

    def emit(self, *, level: str, usage: dict | None = None, **tags) -> None:
        """Non-blocking. Normalises `usage` into a UsageEvent and enqueues it.
        Never raises into the caller's request path: catches all exceptions and returns."""
        try:
            ev = self._build_event(level, usage, tags)
            self._queue.put_nowait(ev)
        except asyncio.QueueFull:
            logger.debug("tokenomics queue full; dropping event")
        except Exception as exc:
            logger.debug("tokenomics emit error (suppressed): %s", exc)

    def _build_event(self, level: str, usage: dict | None, tags: dict) -> UsageEvent:
        """Build a UsageEvent from a level, raw usage dict, and free-form tags.
        Tags that match UsageEvent fields are set directly; everything else goes into details."""
        normalised = normalise_usage(usage or {})
        extra_details = {k: v for k, v in tags.items() if k not in _EVENT_FIELDS}
        return UsageEvent(
            event_id=tags.get("event_id") or str(uuid.uuid4()),
            ts=tags.get("ts") or time.time(),
            level=level,
            framework=tags.get("framework", "raw"),
            hosting=tags.get("hosting", "local"),
            run_id=tags.get("run_id"),
            session_id=tags.get("session_id"),
            app=tags.get("app") or self._config.app,
            team=tags.get("team") or self._config.team,
            user_id=tags.get("user_id"),
            agent_name=tags.get("agent_name"),
            provider=tags.get("provider", "aws.bedrock"),
            model=tags.get("model"),
            estimated=bool(tags.get("estimated", False)),
            details={**extra_details, "raw_usage": usage},
            **normalised,
        )

    async def run_flush_loop(self) -> None:
        """Background task: drains the queue in batches and calls writer.write_batch().
        Start this once, from the AgentCore app's lifespan (see agentcore_runtime.py)."""
        batch: list[UsageEvent] = []
        while True:
            try:
                deadline = time.monotonic() + self._config.flush_interval_s
                while len(batch) < self._config.batch_size:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        ev = await asyncio.wait_for(self._queue.get(), timeout=max(remaining, 0.01))
                        batch.append(ev)
                    except asyncio.TimeoutError:
                        break
                if batch:
                    await self._flush(batch)
                    batch = []
                else:
                    # Nothing arrived; sleep briefly before next iteration
                    await asyncio.sleep(self._config.flush_interval_s)
            except asyncio.CancelledError:
                # Drain remaining events on shutdown
                while not self._queue.empty():
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                if batch:
                    await self._flush(batch)
                raise

    async def _flush(self, batch: list[UsageEvent]) -> None:
        try:
            await self._writer.write_batch(batch)
        except Exception as exc:
            logger.error("tokenomics write error (suppressed): %s", exc)
