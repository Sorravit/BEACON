"""Stateful helper functions shared across routers.

All module-level mutable state lives in ``web.state``; these helpers reference
it via the ``state`` module so reassignments made in the lifespan are visible.
"""

import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

import aiofiles

from web import state
from web.telemetry import _TELEMETRY_AVAILABLE, record_llm_call
from web.logging_setup import logger


# ── Session file persistence ──────────────────────────────────────────────────

def _session_file(session_id: str) -> Path:
    return state.SESSIONS_DIR / f"{session_id}.json"


def _save_session(session_id: str):
    s = state._sessions.get(session_id)
    if not s:
        return
    data = {
        "id":             session_id,
        "title":          s["title"],
        "created_at":     s["created_at"],
        "updated_at":     s["updated_at"],
        "messages":       s["messages"],
        "manually_named": s.get("manually_named", False),
        "pinned":         s.get("pinned", False),
        "pin_order":      s.get("pin_order", 0),
    }
    try:
        _session_file(session_id).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning(f"Could not save session {session_id}: {e}")


async def _save_session_async(session_id: str):
    """Write the session file atomically via a tmp file (non-blocking)."""
    s = state._sessions.get(session_id)
    if not s:
        return
    data = {
        "id":             session_id,
        "title":          s["title"],
        "created_at":     s["created_at"],
        "updated_at":     s["updated_at"],
        "messages":       s["messages"],
        "manually_named": s.get("manually_named", False),
        "pinned":         s.get("pinned", False),
        "pin_order":      s.get("pin_order", 0),
    }
    lock = state._save_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        # Unique tmp name so concurrent saves to different sessions never collide
        tmp = _session_file(session_id).with_name(
            f"{session_id}.{uuid.uuid4().hex[:8]}.tmp"
        )
        try:
            async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))
            os.replace(tmp, _session_file(session_id))  # atomic on POSIX/macOS
        except Exception as e:
            logger.warning(f"Could not async-save session {session_id}: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


def _schedule_save(session_id: str):
    """Debounce session saves: coalesce rapid writes into one disk write."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        # No running loop (e.g. startup) — fall back to sync save
        _save_session(session_id)
        return
    h = state._save_pending.pop(session_id, None)
    if h:
        h.cancel()
    state._save_pending[session_id] = loop.call_later(
        state._SESSION_SAVE_DEBOUNCE,
        lambda: asyncio.create_task(_save_session_async(session_id)),
    )


def _load_all_sessions():
    for f in sorted(state.SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            data = json.loads(f.read_text())
            sid = data["id"]
            state._sessions[sid] = {
                "title":          data.get("title", "New Chat"),
                "created_at":     data.get("created_at", datetime.now().isoformat()),
                "updated_at":     data.get("updated_at", datetime.now().isoformat()),
                "messages":       data.get("messages", []),
                "manually_named": data.get("manually_named", False),
                "pinned":         data.get("pinned", False),
                "pin_order":      data.get("pin_order", 0),
            }
        except Exception as e:
            logger.warning(f"Could not load session file {f}: {e}")


def _create_session() -> str:
    sid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    state._sessions[sid] = {
        "title":          "New Chat",
        "created_at":     now,
        "updated_at":     now,
        "messages":       [],
        "manually_named": False,
        "pinned":         False,
        "pin_order":      0,
    }
    _save_session(sid)   # sync: ensure it exists immediately
    return sid


def _auto_title(text: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return (clean[:48] + "\u2026") if len(clean) > 48 else clean


async def _generate_smart_title(session_id: str, user_msg: str, assistant_msg: str):
    """Fire-and-forget: ask the model for a concise title after the first exchange."""
    try:
        agent = state._shared_agent
        if not agent or not agent.client:
            return
        params = dict(
            model=state._config.model,
            messages=[{
                "role": "user",
                "content": (
                    "Summarize this conversation as a short title (5 words max).\n"
                    f"User: {user_msg[:300]}\n"
                    f"Assistant: {assistant_msg[:300]}\n"
                    "Reply with ONLY the title. No quotes, no punctuation at end."
                )
            }],
            temperature=0.3,
            max_tokens=min(state._config.max_tokens, 100),
        )
        import contextlib as _cl_st
        _st_ctx = record_llm_call(state._config.model, session_id=session_id) if _TELEMETRY_AVAILABLE else _cl_st.nullcontext()
        with _st_ctx:
            # Phase 5: native async call
            response = await agent.client.chat.completions.create(**params)
        raw = (response.choices[0].message.content or "").strip()
        title = raw.splitlines()[0].strip().strip("'\"")
        if title and len(title) > 2:
            s = state._sessions.get(session_id)
            if s and not s.get("manually_named"):
                s["title"] = title[:60]
                s["updated_at"] = datetime.now().isoformat()
                _schedule_save(session_id)
                logger.info(f"Smart title for {session_id}: {title}")
    except Exception as e:
        logger.debug(f"Smart title generation skipped: {e}")


# ── Log rotation ──────────────────────────────────────────────────────────────

def _rotate_log_if_needed(log_path: Path, max_bytes: int = 1_000_000, backup_count: int = 2):
    """Rotate a log file if it exceeds max_bytes."""
    if not log_path.exists() or log_path.stat().st_size <= max_bytes:
        return
    for i in range(backup_count - 1, 0, -1):
        old = log_path.with_suffix(f".log.{i}")
        new = log_path.with_suffix(f".log.{i + 1}")
        if old.exists():
            old.rename(new)
    log_path.rename(log_path.with_suffix(".log.1"))
    log_path.write_text("")  # create fresh empty log


def _rotate_bg_logs():
    """Truncate background task log files that exceed 10 MB, keeping last 5000 lines."""
    os.makedirs("logs", exist_ok=True)
    for lf in Path("logs").glob("bg_*.log"):
        try:
            if lf.stat().st_size > 10 * 1024 * 1024:  # > 10 MB
                lines = lf.read_text(errors="replace").splitlines()
                lf.write_text("\n".join(lines[-5000:]) + "\n")
                logger.info(f"Rotated log {lf.name}: kept last 5000 lines")
        except Exception as e:
            logger.warning(f"Could not rotate {lf}: {e}")


# ── Conversation builder ──────────────────────────────────────────────────────

def _build_conversation(agent, session_id: str) -> list:
    """Build a fresh conversation list for this session (stateless per-request)."""
    s = state._sessions.get(session_id)
    conv = []

    if agent.conversation and agent.conversation[0]["role"] == "system":
        # Defensive copy: never share the agent's system-message dict by
        # reference across per-request conversations.
        conv.append(dict(agent.conversation[0]))

    if s:
        for msg in s["messages"]:
            role = msg.get("role")
            content = msg.get("content", "")
            if role in ("user", "assistant"):
                conv.append({"role": role, "content": content})

    return conv
