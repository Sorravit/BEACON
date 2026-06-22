"""
tests/test_memory_worker.py

Unit tests for core.memory_worker.MemoryWorker.
"""
import asyncio
import pytest
from core.memory_worker import MemoryWorker

# Responses must be >= 20 chars (skip guard: len < 20 on either side → skip)
_LONG_USER = "I love Python and use it for all my backend work"
_LONG_AI   = "Great choice for backend development!"

_LONG_USER2 = "My tech stack includes Kubernetes and Terraform daily"
_LONG_AI2   = "That is a solid and battle-tested infrastructure stack"

_LONG_USER3 = "I use macOS for all my development work and prefer it"
_LONG_AI3   = "macOS is popular with developers for many good reasons"


class FakeAgent:
    def __init__(self, return_count=2):
        self._return_count = return_count
        self.calls = []

    async def _auto_memory_extract(self, user_input, ai_response, session_id=""):
        self.calls.append((user_input, ai_response, session_id))
        return self._return_count


@pytest.mark.asyncio
async def test_worker_processes_submission():
    agent = FakeAgent(return_count=2)
    worker = MemoryWorker(agent, maxsize=10)
    worker.start()

    worker.submit(_LONG_USER, _LONG_AI, "sess1")
    await worker.stop()

    assert len(agent.calls) == 1
    assert agent.calls[0][0] == _LONG_USER
    assert worker.stats["queued"] == 1
    assert worker.stats["stored"] == 2


@pytest.mark.asyncio
async def test_worker_accumulates_stored_count():
    agent = FakeAgent(return_count=3)
    worker = MemoryWorker(agent, maxsize=20)
    worker.start()

    worker.submit(_LONG_USER, _LONG_AI, "s1")
    worker.submit(_LONG_USER2, _LONG_AI2, "s2")
    await worker.stop()

    assert worker.stats["stored"] == 6   # 2 × 3
    assert worker.stats["queued"] == 2


@pytest.mark.asyncio
async def test_trivial_exchange_is_skipped():
    agent = FakeAgent(return_count=1)
    worker = MemoryWorker(agent, maxsize=10)
    worker.start()

    # Both sides < 20 chars → should be skipped
    worker.submit("hi", "hello")
    # One real submission (both sides >= 20 chars)
    worker.submit(_LONG_USER3, _LONG_AI3, "s1")
    await worker.stop()

    assert worker.stats["skipped"] == 1
    assert worker.stats["queued"] == 1
    assert len(agent.calls) == 1


@pytest.mark.asyncio
async def test_queue_full_increments_dropped():
    agent = FakeAgent(return_count=1)
    # Tiny queue; do NOT start the worker so it never drains
    worker = MemoryWorker(agent, maxsize=2)

    # Fill the queue
    worker.submit(_LONG_USER, _LONG_AI, "s1")
    worker.submit(_LONG_USER2, _LONG_AI2, "s2")
    # This one should overflow
    worker.submit(_LONG_USER3, _LONG_AI3, "s3")

    assert worker.stats["queued"] == 2
    assert worker.stats["dropped"] == 1

    # Start and stop cleanly to drain
    worker.start()
    await worker.stop()


@pytest.mark.asyncio
async def test_worker_handles_extraction_error_gracefully():
    class ErrorAgent:
        async def _auto_memory_extract(self, u, a, session_id=""):
            raise RuntimeError("extraction failed")

    worker = MemoryWorker(ErrorAgent(), maxsize=10)
    worker.start()

    worker.submit(_LONG_USER, _LONG_AI, "s1")
    await worker.stop()

    assert worker.stats["errors"] == 1
    assert worker.stats["stored"] == 0


@pytest.mark.asyncio
async def test_start_is_idempotent():
    agent = FakeAgent(return_count=1)
    worker = MemoryWorker(agent, maxsize=10)
    worker.start()
    task1 = worker._task
    worker.start()  # second call should be a no-op
    assert worker._task is task1
    await worker.stop()



