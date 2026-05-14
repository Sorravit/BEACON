#!/usr/bin/env python3
"""
AI Assistant - Web Application
Serves a chat UI and exposes the AIAgent via FastAPI + SSE streaming.

Multi-session support: each session has its own AIAgent conversation history,
persisted to sessions/<id>.json so history survives server restarts.

Background-task architecture: the agent task is decoupled from the HTTP
connection lifetime.  When a client disconnects (refresh/close), the agent
keeps running.  On reconnect, the client replays the buffered event log and
then tails live events.
"""

import asyncio
import glob
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from main import AIAgent, Config

logger = logging.getLogger(__name__)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="Big's Personal AI Assistant", version="4.4.0")

static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

# ── Global config + shared agent ─────────────────────────────────────────────
_config: Optional[Config] = None
_shared_agent: Optional[AIAgent] = None

# ── Session store ─────────────────────────────────────────────────────────────
_sessions: Dict[str, dict] = {}

# ── Per-session background state ─────────────────────────────────────────────
_bg: Dict[str, dict] = {}


def _bg_state(session_id: str) -> dict:
    if session_id not in _bg:
        _bg[session_id] = {
            "task":       None,
            "event_buf":  [],
            "done_event": asyncio.Event(),
            "activity":   "",
        }
    return _bg[session_id]


def _session_file(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.json"


def _save_session(session_id: str):
    s = _sessions.get(session_id)
    if not s:
        return
    data = {
        "id":         session_id,
        "title":      s["title"],
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "messages":   s["messages"],
    }
    try:
        _session_file(session_id).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning(f"Could not save session {session_id}: {e}")


def _load_all_sessions():
    for f in sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            data = json.loads(f.read_text())
            sid = data["id"]
            _sessions[sid] = {
                "title":      data.get("title", "New Chat"),
                "created_at": data.get("created_at", datetime.now().isoformat()),
                "updated_at": data.get("updated_at", datetime.now().isoformat()),
                "messages":   data.get("messages", []),
            }
        except Exception as e:
            logger.warning(f"Could not load session file {f}: {e}")


async def _prepare_agent_for_session(session_id: str) -> Optional[AIAgent]:
    """
    DEPRECATED — do not call from web paths.
    Previously mutated agent.conversation directly, which is unsafe for concurrent sessions.
    The web path now uses _build_conversation() + get_response(conversation=...) instead.
    Kept here only for backward-compatibility with any CLI callers; it is a no-op in web mode.
    """
    if _shared_agent is None:
        return None
    s = _sessions.get(session_id)
    if s is None:
        return None
    # Return the agent without touching agent.conversation — callers must use
    # _build_conversation() and pass the result as conversation= to get_response().
    return _shared_agent


def _create_session() -> str:
    sid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    _sessions[sid] = {
        "title":      "New Chat",
        "created_at": now,
        "updated_at": now,
        "messages":   [],
    }
    _save_session(sid)
    return sid


def _auto_title(text: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return (clean[:48] + "\u2026") if len(clean) > 48 else clean


@app.on_event("startup")
async def startup_event():
    global _config, _shared_agent
    _config = Config()
    if not _config.validate():
        logger.error("API key not configured - set OPENAI_API_KEY in .env")
        sys.exit(1)
    _shared_agent = AIAgent(_config)
    ok = await _shared_agent.initialize()
    if not ok:
        logger.error("Failed to initialize AI agent")
        sys.exit(1)
    logger.info("AI Agent ready")
    _load_all_sessions()
    logger.info(f"Loaded {len(_sessions)} session(s) from disk")


@app.on_event("shutdown")
async def shutdown_event():
    if _shared_agent and hasattr(_shared_agent, "shutdown"):
        await _shared_agent.shutdown()
    elif _shared_agent:
        if _shared_agent.tools:
            await _shared_agent.tools.cleanup()
        if _shared_agent.vector_memory:
            _shared_agent.vector_memory.close()


class ChatRequest(BaseModel):
    message: str
    session_id: str


class RenameRequest(BaseModel):
    title: str


# ── File upload ────────────────────────────────────────────────────────────────
@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a file and save it to temp/ folder. Returns the saved path."""
    import shutil

    dest_dir = Path("temp")
    dest_dir.mkdir(exist_ok=True)

    # Sanitise filename — strip directory, replace spaces
    safe_name = Path(file.filename).name.replace(" ", "_")
    if not safe_name:
        safe_name = "upload"
    dest = dest_dir / safe_name

    # Avoid overwriting existing files
    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    counter = 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    contents = await file.read()
    dest.write_bytes(contents)

    return {"path": str(dest), "name": dest.name, "size": dest.stat().st_size}


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    html_file = Path("static/index.html")
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(), status_code=200)
    return HTMLResponse(content="<h1>Frontend not found</h1>", status_code=404)


@app.get("/sessions")
async def list_sessions():
    result = []
    for sid, s in _sessions.items():
        bg = _bg.get(sid, {})
        task = bg.get("task")
        running = task is not None and not task.done()
        result.append({
            "id":            sid,
            "title":         s["title"],
            "created_at":    s["created_at"],
            "updated_at":    s["updated_at"],
            "message_count": len(s["messages"]),
            "running":       running,
        })
    result.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"sessions": result}


@app.post("/sessions")
async def create_session():
    sid = _create_session()
    return {"id": sid, "title": "New Chat"}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    bg = _bg.get(session_id, {})
    task = bg.get("task")
    running = task is not None and not task.done()
    return {
        "id":         session_id,
        "title":      s["title"],
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "messages":   s["messages"],
        "running":    running,
    }


@app.patch("/sessions/{session_id}/rename")
async def rename_session(session_id: str, req: RenameRequest):
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    s["title"] = req.title.strip() or "New Chat"
    s["updated_at"] = datetime.now().isoformat()
    _save_session(session_id)
    return {"id": session_id, "title": s["title"]}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    s = _sessions.pop(session_id, None)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    fp = _session_file(session_id)
    if fp.exists():
        fp.unlink()
    _bg.pop(session_id, None)
    return {"status": "deleted", "id": session_id}


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    sid = req.session_id
    if sid not in _sessions:
        async def err_gen():
            yield " " + json.dumps({"type": "error", "content": "Session not found"}) + "\n\n"
        return StreamingResponse(err_gen(), media_type="text/event-stream")

    bg = _bg_state(sid)
    # If a task is already running for this session, just reconnect to it
    if bg["task"] is not None and not bg["task"].done():
        return StreamingResponse(
            _reconnect_stream(sid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if _shared_agent is None:
        async def err_gen2():
            yield " " + json.dumps({"type": "error", "content": "Agent not initialised"}) + "\n\n"
        return StreamingResponse(err_gen2(), media_type="text/event-stream")

    agent = _shared_agent

    # Reset buffer for the new request
    bg["event_buf"] = []
    bg["done_event"] = asyncio.Event()
    bg["activity"] = ""

    # Start background task (NOT tied to this HTTP connection)
    bg["task"] = asyncio.create_task(_run_agent_bg(req.message, sid, agent))

    return StreamingResponse(
        _reconnect_stream(sid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/chat/reconnect/{session_id}")
async def chat_reconnect(session_id: str):
    """Reconnect to an in-progress or recently finished agent stream."""
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    return StreamingResponse(
        _reconnect_stream(session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/chat/stop/{session_id}")
async def stop_chat(session_id: str):
    bg = _bg.get(session_id)
    if bg:
        task = bg.get("task")
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        bg["activity"] = ""
        return {"status": "stopped", "session_id": session_id}
    return {"status": "not_running", "session_id": session_id}


@app.get("/chat/status/{session_id}")
async def chat_status(session_id: str):
    bg = _bg.get(session_id, {})
    task = bg.get("task")
    running = task is not None and not task.done()
    activity = bg.get("activity", "") if running else ""
    return {"running": running, "activity": activity, "session_id": session_id}


@app.post("/chat/clear")
async def chat_clear(req: ChatRequest):
    sid = req.session_id
    s = _sessions.get(sid)
    if s:
        s["messages"] = []
        s["updated_at"] = datetime.now().isoformat()
        _save_session(sid)
    _bg.pop(sid, None)
    return {"status": "cleared", "session_id": sid}


@app.get("/health")
async def health():
    return {"status": "ok", "sessions": len(_sessions)}


# ── Background agent task ─────────────────────────────────────────────────────
def _build_conversation(agent: AIAgent, session_id: str) -> list:
    """Build a fresh conversation list for this session (stateless per-request)."""
    s = _sessions.get(session_id)
    conv = []

    # Start with system message from agent
    if agent.conversation and agent.conversation[0]["role"] == "system":
        conv.append(agent.conversation[0])

    # Replay this session's messages
    if s:
        for msg in s["messages"]:
            role = msg.get("role")
            content = msg.get("content", "")
            if role in ("user", "assistant"):
                conv.append({"role": role, "content": content})

    return conv


async def _run_agent_bg(user_input: str, session_id: str, agent: AIAgent):
    """
    Run the agent completely decoupled from any HTTP connection.
    Each call gets its own ToolManager and conversation list, enabling
    true parallel multi-session execution without any shared lock.
    """
    bg = _bg_state(session_id)
    s = _sessions.get(session_id)

    def _emit(ev: dict):
        bg["event_buf"].append(ev)

    # Persist the user message immediately so it's visible even while AI is thinking
    if s is not None:
        now = datetime.now().isoformat()
        # Avoid duplicate if already saved (e.g. reconnect scenario)
        already_saved = any(
            m.get("role") == "user" and m.get("content") == user_input
            for m in s["messages"][-2:] if s["messages"]
        )
        if not already_saved:
            s["messages"].append({"role": "user", "content": user_input, "ts": now})
            if len([m for m in s["messages"] if m["role"] == "user"]) == 1:
                s["title"] = _auto_title(user_input)
            s["updated_at"] = now
            _save_session(session_id)

    # Build conversation from session history for stateless call
    conversation = _build_conversation(agent, session_id)

    try:
        # Create per-request ToolManager with shared browser
        from main import ToolManager
        per_request_tools = ToolManager(
            vector_memory=agent.vector_memory,
            mcp_manager=agent.mcp_manager,
            shared_browser=agent._shared_browser,
        )
        await per_request_tools.initialize()
        per_request_tools.session_id = session_id

        original_execute = per_request_tools.execute_tool

        async def instrumented_execute(name, args):
            args_preview = ", ".join(
                f"{k}={str(v)[:50]}" for k, v in args.items()
            ) if args else ""
            bg["activity"] = f"{name}({args_preview})"
            _emit({"type": "tool", "name": name, "args": args_preview})
            result = await original_execute(name, args)
            preview = str(result)
            if len(preview) > 400:
                preview = preview[:400] + "\u2026"
            bg["activity"] = f"Processing result from {name}..."
            _emit({"type": "result", "name": name, "content": preview})
            return result

        per_request_tools.execute_tool = instrumented_execute

        bg["activity"] = "Thinking..."
        try:
            # Pass per_request_tools directly — never swap agent.tools on the shared agent.
            # This is the key fix: concurrent sessions each have their own ToolManager and
            # pass it as tools= so get_response() uses it without touching self.tools.
            response = await agent.get_response(user_input, conversation=conversation, tools=per_request_tools)
        finally:
            await per_request_tools.cleanup()

        bg["activity"] = "Responding..."
        content = response or ""
        words = content.split(" ")
        for i, word in enumerate(words):
            chunk = word + (" " if i < len(words) - 1 else "")
            _emit({"type": "token", "content": chunk})
            await asyncio.sleep(0.008)

        if s is not None:
            now = datetime.now().isoformat()
            # Only append the assistant reply — user message was already saved above
            s["messages"].append({"role": "assistant", "content": content, "ts": now})
            s["updated_at"] = now
            _save_session(session_id)

        _emit({"type": "done"})

    except asyncio.CancelledError:
        _emit({"type": "stopped", "content": "Stopped by user"})
        raise
    except Exception as e:
        logger.error(f"Agent BG error: {e}")
        import traceback; traceback.print_exc()
        _emit({"type": "error", "content": str(e)})
    finally:
        bg["activity"] = ""
        bg["done_event"].set()


async def _reconnect_stream(session_id: str) -> AsyncGenerator[str, None]:
    """
    Stream all buffered events then continue streaming live events until done.
    Uses simple sleep-based polling so generator cancellation (client disconnect)
    never propagates into the background agent task.
    """
    bg = _bg_state(session_id)
    cursor = 0
    idle_ticks = 0

    while True:
        buf = bg["event_buf"]
        # Drain any new events since our cursor
        new_events = False
        while cursor < len(buf):
            ev = buf[cursor]
            cursor += 1
            new_events = True
            yield " " + json.dumps(ev) + "\n\n"

        # If task is done and we've delivered everything, we're finished
        task = bg.get("task")
        is_done = (task is None or task.done()) and bg["done_event"].is_set()
        if is_done and cursor >= len(bg["event_buf"]):
            break

        # Short sleep to avoid busy-looping; send periodic keepalives
        await asyncio.sleep(0.25)
        idle_ticks += 1
        if idle_ticks % 8 == 0:   # every ~2 seconds
            yield ": keepalive\n\n"


# ── Background tasks (CLI processes) ─────────────────────────────────────────
@app.get("/tasks")
async def list_tasks():
    names = set()
    for lf in glob.glob("/tmp/bg_task_*.lock"):
        names.add(Path(lf).stem.replace("bg_task_", ""))
    os.makedirs("logs", exist_ok=True)
    for lf in glob.glob("logs/bg_*.log"):
        name = Path(lf).stem[3:]
        names.add(name)
    tasks = []
    for name in sorted(names):
        check = subprocess.run(
            ["pgrep", "-f", f"background_task.*--name.*{name}"],
            capture_output=True
        )
        alive = check.returncode == 0
        log_file = f"logs/bg_{name}.log"
        tasks.append({
            "name": name,
            "running": alive,
            "log_file": log_file,
            "log_exists": Path(log_file).exists(),
        })
    return {"tasks": tasks}


def _kill_task(name: str) -> str:
    """
    Stop a background CLI task by reading its PID from the lock file and
    sending SIGTERM.  Falls back to pkill for any orphaned processes.
    """
    import signal as _signal
    lockfile = f"/tmp/bg_task_{name}.lock"
    killed = False

    # Primary: kill via stored PID
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

    # Fallback: pkill the wrapper script and any child process
    r1 = subprocess.run(["pkill", "-f", f"background_task.*--name.*{name}"], capture_output=True)
    r2 = subprocess.run(["pkill", "-TERM", "-f", f"bg_{name}"], capture_output=True)
    if r1.returncode == 0 or r2.returncode == 0:
        killed = True

    return "stopped" if killed else "already_stopped"


@app.post("/tasks/stop-all")
async def stop_all_tasks():
    """Stop and clear every background CLI task."""
    names = set()
    for lf in glob.glob("/tmp/bg_task_*.lock"):
        names.add(Path(lf).stem.replace("bg_task_", ""))
    os.makedirs("logs", exist_ok=True)
    for lf in glob.glob("logs/bg_*.log"):
        names.add(Path(lf).stem[3:])
    results = {}
    for name in names:
        status = _kill_task(name)
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


@app.get("/tasks/notifications")
async def get_notifications():
    """Read and consume all pending background task notification files, saving to session."""
    notes = []
    os.makedirs("logs", exist_ok=True)
    for f in sorted(Path("logs").glob("notify_*.json")):
        try:
            data = json.loads(f.read_text())
            notes.append(data)
            f.unlink()  # consume once — delete after reading

            # Save notification as assistant message in the session so it persists
            sid = data.get("session_id")
            msg = data.get("message", "")
            level = data.get("level", "info")
            ts = data.get("ts") or datetime.now().isoformat()

            if sid and sid in _sessions and msg:
                s = _sessions[sid]
                # Add a special marker so the frontend knows this is a notification
                s["messages"].append({
                    "role": "assistant",
                    "content": msg,
                    "ts": ts,
                    "notification": True,
                    "level": level,
                    "task_name": data.get("task_name", "")
                })
                s["updated_at"] = ts
                _save_session(sid)
        except Exception as e:
            logger.warning(f"Could not read notification {f}: {e}")
    return {"notifications": notes}


@app.post("/tasks/{name}/stop")
async def stop_task(name: str):
    return {"status": _kill_task(name), "name": name}


@app.delete("/tasks/{name}/log")
async def clear_task_log(name: str):
    log_file = Path(f"logs/bg_{name}.log")
    if not log_file.exists():
        return {"status": "not_found", "name": name}
    log_file.unlink()
    check = subprocess.run(
        ["pgrep", "-f", f"background_task.*--name.*{name}"], capture_output=True
    )
    if check.returncode != 0:
        lockfile = f"/tmp/bg_task_{name}.lock"
        try:
            if os.path.exists(lockfile):
                os.remove(lockfile)
        except Exception:
            pass
    return {"status": "cleared", "name": name}


@app.post("/tasks/{name}/stop-and-clear")
async def stop_and_clear_task(name: str):
    lockfile = f"/tmp/bg_task_{name}.lock"
    log_file = Path(f"logs/bg_{name}.log")
    status = _kill_task(name)
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


@app.get("/tasks/{name}/logs")
async def stream_task_logs(name: str):
    log_file = f"logs/bg_{name}.log"
    if not Path(log_file).exists():
        raise HTTPException(status_code=404, detail=f"Log file for task not found")
    return StreamingResponse(
        _tail_log(name, log_file),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _tail_log(name: str, log_file: str) -> AsyncGenerator[str, None]:
    try:
        with open(log_file, "r", errors="replace") as f:
            existing = f.read()
        for line in existing.splitlines():
            yield " " + json.dumps({"line": line, "done": False}) + "\n\n"
        with open(log_file, "r", errors="replace") as f:
            f.seek(0, 2)
            while True:
                lockfile = f"/tmp/bg_task_{name}.lock"
                line = f.readline()
                if line:
                    yield " " + json.dumps({"line": line.rstrip(), "done": False}) + "\n\n"
                else:
                    if not os.path.exists(lockfile):
                        yield " " + json.dumps({"line": "", "done": True}) + "\n\n"
                        break
                    await asyncio.sleep(0.5)
    except Exception as e:
        yield " " + json.dumps({"line": f"[error reading log: {e}]", "done": True}) + "\n\n"


if __name__ == "__main__":
    uvicorn.run(
        "web_app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
