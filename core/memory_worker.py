"""
core/memory_worker.py — Long-lived background queue for auto-learning.

Decouples auto-memory extraction from the request lifecycle so the task
is never GC'd, all failures are visible at WARNING level, and the
skill-dispatch path also gets captured.

The MemoryWorker is created once in AIAgent.initialize() and lives for
the lifetime of the process.  The agent calls worker.submit() after every
final answer (including skill dispatches) — the worker does the extraction
and Weaviate writes asynchronously without holding any request context.
"""

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class MemoryWorker:
    """Processes auto-memory extraction jobs from a bounded async queue."""

    def __init__(self, agent, maxsize: int = 200):
        self._agent = agent
        self._q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._task: Optional[asyncio.Task] = None
        self.stats = {
            "queued": 0,
            "stored": 0,
            "dropped": 0,
            "errors": 0,
            "skipped": 0,
        }

    def start(self) -> None:
        """Start the background consumer task (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="memory-worker")
            logger.info("MemoryWorker started (queue maxsize=%d)", self._q.maxsize)

    async def stop(self) -> None:
        """Gracefully drain the queue and stop the consumer."""
        if self._task and not self._task.done():
            await self._q.put(None)  # sentinel
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        logger.info("MemoryWorker stopped — final stats: %s", self.stats)

    def submit(
        self, user_input: str, ai_response: str, session_id: str = ""
    ) -> None:
        """Enqueue an exchange for auto-memory extraction (non-blocking)."""
        # Skip trivial exchanges — same guard as the original hook
        if len(user_input.strip()) < 20 or len(ai_response.strip()) < 20:
            self.stats["skipped"] += 1
            logger.info(
                "[MemoryWorker] Skipping memory extraction: exchange too short "
                "(user_len=%d ai_len=%d) — stats.skipped=%d",
                len(user_input.strip()), len(ai_response.strip()), self.stats['skipped'],
            )
            return
        logger.info("[MemoryWorker] submit queued session=%s user=%d chars ai=%d chars",
                    session_id or "?", len(user_input.strip()), len(ai_response.strip()))
        try:
            self._q.put_nowait((user_input, ai_response, session_id))
            self.stats["queued"] += 1
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            logger.warning(
                "MemoryWorker queue full (%d) — dropping exchange",
                self._q.maxsize,
            )

    async def _run(self) -> None:
        """Consumer loop — runs until a None sentinel is received."""
        while True:
            item = await self._q.get()
            if item is None:
                self._q.task_done()
                break
            user_input, ai_response, session_id = item
            try:
                n = await self._agent._auto_memory_extract(
                    user_input, ai_response, session_id=session_id
                )
                self.stats["stored"] += n or 0
                if n:
                    logger.info(
                        "MemoryWorker stored %d fact(s) [session=%s]", n, session_id or "?"
                    )
            except Exception as exc:
                self.stats["errors"] += 1
                logger.warning("MemoryWorker extraction failed: %s", exc)
            finally:
                self._q.task_done()

