"""Background CLI task routes: list/stop/clear/log-tail."""

import asyncio
import glob
import json
import os
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from web.routers.events import _build_tasks_payload, _collect_notifications
from web.logging_setup import logger

router = APIRouter()


# ── Background CLI task endpoints (NOTE: /tasks/notifications and /tasks/stop-all
#    must be registered BEFORE /tasks/{name}/... to avoid route shadowing) ─────
@router.get("/tasks")
async def list_tasks():
    payload = await _build_tasks_payload()
    return {"tasks": payload["tasks"]}


@router.get("/tasks/notifications")
async def get_notifications():
    notes = await _collect_notifications()
    return {"notifications": notes}


async def _kill_task(name: str) -> str:
    import signal as _signal
    lockfile = f"/tmp/bg_task_{name}.lock"
    killed = False

    if os.path.exists(lockfile):
        try:
            pid = int(Path(lockfile).read_text().strip())
            os.kill(pid, _signal.SIGTERM)
            killed = True
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        try:
            os.remove(lockfile)
        except Exception:
            pass

    # Step7: asyncio.create_subprocess_exec
    _p1 = await asyncio.create_subprocess_exec(
        "pkill", "-f", f"background_task.*--name.*{name}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await _p1.wait()
    _p2 = await asyncio.create_subprocess_exec(
        "pkill", "-TERM", "-f", f"bg_{name}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await _p2.wait()
    if _p1.returncode == 0 or _p2.returncode == 0:
        killed = True

    return "stopped" if killed else "already_stopped"


@router.post("/tasks/stop-all")
async def stop_all_tasks():
    names = set()
    for lf in glob.glob("/tmp/bg_task_*.lock"):
        names.add(Path(lf).stem.replace("bg_task_", ""))
    os.makedirs("logs", exist_ok=True)
    for lf in glob.glob("logs/bg_*.log"):
        names.add(Path(lf).stem[3:])
    results = {}
    for name in names:
        status = await _kill_task(name)
        lockfile = f"/tmp/bg_task_{name}.lock"
        log_file = Path(f"logs/bg_{name}.log")
        try:
            if os.path.exists(lockfile):
                os.remove(lockfile)
        except Exception:
            pass
        try:
            if log_file.exists():
                log_file.unlink()
        except Exception:
            pass
        results[name] = status
    return {"status": "done", "tasks": results}


# ── Per-task routes — must come AFTER all static /tasks/* routes ──────────────
@router.post("/tasks/{name}/stop")
async def stop_task(name: str):
    return {"status": await _kill_task(name), "name": name}


@router.delete("/tasks/{name}/log")
async def clear_task_log(name: str):
    log_file = Path(f"logs/bg_{name}.log")
    if not log_file.exists():
        return {"status": "not_found", "name": name}
    log_file.unlink()
    # Step7: asyncio.create_subprocess_exec
    _chk = await asyncio.create_subprocess_exec(
        "pgrep", "-f", f"background_task.*--name.*{name}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await _chk.wait()
    if _chk.returncode != 0:
        lockfile = f"/tmp/bg_task_{name}.lock"
        try:
            if os.path.exists(lockfile):
                os.remove(lockfile)
        except Exception:
            pass
    return {"status": "cleared", "name": name}


@router.post("/tasks/{name}/stop-and-clear")
async def stop_and_clear_task(name: str):
    lockfile = f"/tmp/bg_task_{name}.lock"
    log_file = Path(f"logs/bg_{name}.log")
    status = await _kill_task(name)
    try:
        if os.path.exists(lockfile):
            os.remove(lockfile)
    except Exception:
        pass
    try:
        if log_file.exists():
            log_file.unlink()
    except Exception:
        pass
    return {"status": status, "name": name}


@router.get("/tasks/{name}/logs")
async def stream_task_logs(name: str, request: Request):
    log_file = f"logs/bg_{name}.log"
    if not Path(log_file).exists():
        raise HTTPException(status_code=404, detail="Log file for task not found")
    return StreamingResponse(
        _tail_log(name, log_file, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _tail_log(name: str, log_file: str, request: Request) -> AsyncGenerator[str, None]:
    """FIX: properly closes file handles even on client disconnect."""
    f_existing = None
    f_tail = None
    try:
        # Send existing content first
        f_existing = open(log_file, "r", errors="replace")
        existing = f_existing.read()
        f_existing.close()
        f_existing = None

        for line in existing.splitlines():
            yield " " + json.dumps({"line": line, "done": False}) + "\n\n"

        # Tail new lines
        f_tail = open(log_file, "r", errors="replace")
        f_tail.seek(0, 2)  # seek to end
        while True:
            if await request.is_disconnected():
                logger.debug(f"Log tail client disconnected for task '{name}'")
                break
            lockfile = f"/tmp/bg_task_{name}.lock"
            line = f_tail.readline()
            if line:
                yield " " + json.dumps({"line": line.rstrip(), "done": False}) + "\n\n"
            else:
                if not os.path.exists(lockfile):
                    yield " " + json.dumps({"line": "", "done": True}) + "\n\n"
                    break
                await asyncio.sleep(0.5)
    except Exception as e:
        yield " " + json.dumps({"line": f"[error reading log: {e}]", "done": True}) + "\n\n"
    finally:
        # FIX: guarantee file handles are closed even if client disconnects mid-stream
        if f_existing:
            try:
                f_existing.close()
            except Exception:
                pass
        if f_tail:
            try:
                f_tail.close()
            except Exception:
                pass
