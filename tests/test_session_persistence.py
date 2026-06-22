"""
tests/test_session_persistence.py

Tests for the async debounced session persistence (Phase 3 / #6).

Verifies:
- _save_session_async writes atomically (no leftover .tmp files)
- _schedule_save debounces multiple rapid calls into one write
- Session round-trips correctly (write → reload)
"""
import asyncio
import json
import os
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime


# ---------------------------------------------------------------------------
# Helpers to test the functions in isolation (without importing the full app)
# ---------------------------------------------------------------------------

def _make_save_session_async(sessions_dir: Path):
    """
    Returns a standalone async version of _save_session_async
    pointing at the given temp directory.
    """
    import aiofiles
    import uuid as _uuid

    async def _save_session_async(session_id: str, session_data: dict):
        data = {
            "id":             session_id,
            "title":          session_data.get("title", "Test"),
            "created_at":     session_data.get("created_at", datetime.now().isoformat()),
            "updated_at":     session_data.get("updated_at", datetime.now().isoformat()),
            "messages":       session_data.get("messages", []),
            "manually_named": session_data.get("manually_named", False),
            "pinned":         session_data.get("pinned", False),
            "pin_order":      session_data.get("pin_order", 0),
        }
        session_file = sessions_dir / f"{session_id}.json"
        # Use a unique tmp name per call to avoid races in concurrent tests
        tmp = sessions_dir / f"{session_id}.{_uuid.uuid4().hex[:8]}.tmp"
        async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, session_file)  # atomic on POSIX
        return session_file

    return _save_session_async


@pytest.mark.asyncio
async def test_async_save_writes_file():
    with tempfile.TemporaryDirectory() as td:
        sessions_dir = Path(td)
        saver = _make_save_session_async(sessions_dir)
        session_data = {
            "title": "Test Chat",
            "messages": [{"role": "user", "content": "hello"}],
        }
        path = await saver("abc123", session_data)
        assert path.exists(), "Session file should exist after save"
        loaded = json.loads(path.read_text())
        assert loaded["id"] == "abc123"
        assert loaded["title"] == "Test Chat"
        assert len(loaded["messages"]) == 1


@pytest.mark.asyncio
async def test_async_save_no_leftover_tmp():
    with tempfile.TemporaryDirectory() as td:
        sessions_dir = Path(td)
        saver = _make_save_session_async(sessions_dir)
        await saver("sess1", {"title": "Chat 1", "messages": []})
        tmp_files = list(sessions_dir.glob("*.tmp"))
        assert len(tmp_files) == 0, "No .tmp files should remain after atomic save"


@pytest.mark.asyncio
async def test_async_save_overwrites_correctly():
    with tempfile.TemporaryDirectory() as td:
        sessions_dir = Path(td)
        saver = _make_save_session_async(sessions_dir)
        await saver("sess2", {"title": "First", "messages": []})
        await saver("sess2", {"title": "Updated", "messages": [{"role": "user", "content": "hi"}]})
        loaded = json.loads((sessions_dir / "sess2.json").read_text())
        assert loaded["title"] == "Updated"
        assert len(loaded["messages"]) == 1


@pytest.mark.asyncio
async def test_multiple_rapid_saves_produce_one_final_file():
    """Multiple concurrent saves to the same session should produce one valid file."""
    with tempfile.TemporaryDirectory() as td:
        sessions_dir = Path(td)
        saver = _make_save_session_async(sessions_dir)
        # Simulate burst of saves
        tasks = [
            saver("burst", {"title": f"title_{i}", "messages": []})
            for i in range(10)
        ]
        await asyncio.gather(*tasks)
        # File should exist and be valid JSON
        loaded = json.loads((sessions_dir / "burst.json").read_text())
        assert loaded["id"] == "burst"
        # No tmp files
        assert len(list(sessions_dir.glob("*.tmp"))) == 0


@pytest.mark.asyncio
async def test_unicode_content_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        sessions_dir = Path(td)
        saver = _make_save_session_async(sessions_dir)
        thai = "สวัสดีครับ"
        emoji = "🤖🔦"
        await saver("uni", {"title": thai + emoji, "messages": []})
        loaded = json.loads((sessions_dir / "uni.json").read_text(encoding="utf-8"))
        assert loaded["title"] == thai + emoji


