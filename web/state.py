"""Shared module-level state for the web app.

These names are imported by the router modules and the app factory. The
mutable singletons (`_config`, `_shared_agent`, `_events_producer_task`,
`_events_cache`) are REASSIGNED at runtime by the lifespan / events producer.
Routers must therefore reference them via attribute access on this module
(e.g. ``state._shared_agent``) so they always observe the current value
rather than a stale binding captured at import time.
"""

import asyncio
import os
from pathlib import Path
from typing import Dict, Optional

# Forward type hints only; avoid importing AIAgent/Config here to keep this
# module import-cheap and free of circular dependencies.

# ── Sessions on disk ──────────────────────────────────────────────────────────
SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

# ── Global config + shared agent (REASSIGNED in lifespan) ─────────────────────
_config = None
_shared_agent = None

# ── Session store ─────────────────────────────────────────────────────────────
_sessions: Dict[str, dict] = {}

# ── Per-session background state ──────────────────────────────────────────────
_bg: Dict[str, dict] = {}

# ── Tasks SSE: snapshot cache so we only push when something changes ──────────
_last_tasks_snapshot: Optional[str] = None

# ── Async session persistence ─────────────────────────────────────────────────
_save_locks: Dict[str, asyncio.Lock] = {}
_save_pending: Dict[str, asyncio.TimerHandle] = {}
_SESSION_SAVE_DEBOUNCE = float(os.getenv("SESSION_SAVE_DEBOUNCE_MS", "750")) / 1000.0

# ── Global events producer (task REASSIGNED in lifespan; cache reassigned) ────
_events_producer_task: Optional[asyncio.Task] = None
_events_subscribers: set = set()
_events_cache: dict = {"tasks": [], "notifications": [], "activity": {}}

# ── AgentExecutor / Orchestrator task registry ────────────────────────────────
_agent_tasks: Dict[str, dict] = {}


def _bg_state(session_id: str) -> dict:
    if session_id not in _bg:
        _bg[session_id] = {
            "task":        None,
            "event_buf":   [],
            "trim_offset": 0,     # monotonic index of event_buf[0]; cursors use logical indices
            "done_event":  None,   # FIX1: created lazily inside running event loop
            "activity":    "",
        }
    return _bg[session_id]
