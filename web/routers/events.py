"""SSE events feed + tasks payload builder + notifications collector."""

import asyncio
import glob
import json
import os
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from web import state
from web.helpers import _rotate_log_if_needed, _schedule_save
from web.logging_setup import logger

router = APIRouter()


# ── Phase 4 / #5: single global events producer ──────────────────────────────

async def _events_producer():
    """
    Single background task that builds the tasks/notifications/activity
    snapshot ONCE every 3 seconds and fans it out to all connected SSE clients.

    Fixes two issues in the old per-client generator:
    1. _build_tasks_payload (glob + pgrep) ran once per client per tick.
    2. _collect_notifications unlinked files inside each client's loop →
       with 2+ open /events connections only one client received each note.
    """
    while True:
        try:
            payload = await _build_tasks_payload()   # globs + pgrep exactly ONCE
            activity = {
                sid: bg["activity"]
                for sid, bg in state._bg.items()
                if bg.get("task") and not bg["task"].done() and bg.get("activity")
            }
            state._events_cache = {
                "tasks":         payload["tasks"],
                "notifications": payload["notifications"],
                "activity":      activity,
            }
            snap = json.dumps(state._events_cache)
            for q in list(state._events_subscribers):
                try:
                    if not q.full():
                        q.put_nowait(snap)
                except Exception:
                    pass
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("events producer error: %s", exc)
        await asyncio.sleep(3)


# ── Background tasks payload builder ─────────────────────────────────────────

async def _build_tasks_payload() -> dict:
    """Build the current tasks + notifications payload (used by both REST and SSE)."""
    names = set()
    for lf in glob.glob("/tmp/bg_task_*.lock"):
        names.add(Path(lf).stem.replace("bg_task_", ""))
    os.makedirs("logs", exist_ok=True)
    for lf in glob.glob("logs/bg_*.log"):
        name = Path(lf).stem[3:]
        names.add(name)

    # FIX4: parallelise pgrep — sequential awaits starved event loop
    async def _check_alive(name: str) -> bool:
        try:
            _proc = await asyncio.create_subprocess_exec(
                "pgrep", "-f", f"background_task.*--name.*{name}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await _proc.wait()
            return _proc.returncode == 0
        except Exception:
            return False

    sorted_names = sorted(names)
    alive_results = await asyncio.gather(*[_check_alive(n) for n in sorted_names])

    tasks = []
    for name, alive in zip(sorted_names, alive_results):
        log_file = f"logs/bg_{name}.log"
        _rotate_log_if_needed(Path(log_file))
        tasks.append({
            "name": name,
            "running": alive,
            "log_file": log_file,
            "log_exists": Path(log_file).exists(),
        })

    # Collect pending notifications
    notes = await _collect_notifications()

    return {"tasks": tasks, "notifications": notes}


async def _collect_notifications() -> list:
    """Read and consume all pending notification JSON files."""
    notes = []
    os.makedirs("logs", exist_ok=True)
    for f in sorted(Path("logs").glob("notify_*.json")):
        try:
            data = json.loads(f.read_text())
            notes.append(data)
            f.unlink()  # consume once

            sid = data.get("session_id")
            msg = data.get("message", "")
            level = data.get("level", "info")
            ts = data.get("ts") or datetime.now().isoformat()

            if sid and sid in state._sessions and msg:
                s = state._sessions[sid]
                s["messages"].append({
                    "role": "assistant",
                    "content": msg,
                    "ts": ts,
                    "notification": True,
                    "level": level,
                    "task_name": data.get("task_name", "")
                })
                s["updated_at"] = ts
                _schedule_save(sid)
        except Exception as e:
            logger.warning(f"Could not read notification {f}: {e}")
    return notes


# ── SSE event stream: single persistent feed replacing polling ────────────────
@router.get("/events")
async def event_stream(request: Request):
    """Single SSE stream replacing polling of /tasks and /tasks/notifications."""
    return StreamingResponse(
        _events_generator(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _events_generator(request: Request) -> AsyncGenerator[str, None]:
    """
    Phase 4 / #5: thin SSE subscriber.
    Subscribes to the single global _events_producer via an asyncio.Queue.
    Notifications are consumed once by the producer and broadcast to all
    connected clients, so every tab receives every notification exactly once.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=4)
    state._events_subscribers.add(q)
    try:
        # Send current snapshot immediately on connect
        yield "data: " + json.dumps(state._events_cache) + "\n\n"
        while True:
            if await request.is_disconnected():
                logger.debug("SSE /events client disconnected — stopping generator")
                return
            try:
                snap = await asyncio.wait_for(q.get(), timeout=15)
                yield "data: " + snap + "\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
            except asyncio.CancelledError:
                break
    finally:
        state._events_subscribers.discard(q)


# ── Legacy SSE endpoint kept for backward compat ──────────────────────────────
@router.get("/tasks/stream")
async def tasks_stream(request: Request):
    """
    SSE stream (legacy — kept for backward compat).
    New clients should use GET /events instead.
    """
    return StreamingResponse(
        _events_generator(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
