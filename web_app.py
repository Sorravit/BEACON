
#!/usr/bin/env python3
"""
AI Assistant - Web Application
Serves a chat UI and exposes the AIAgent via FastAPI + SSE streaming.

Multi-session support: each session has its own AIAgent conversation history,
persisted to sessions/<id>.json so history survives server restarts.
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
from typing import AsyncGenerator, Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from main import AIAgent, Config

logger = logging.getLogger(__name__)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="AI Assistant", version="4.3.0")

static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

# ── Global config + shared agent ─────────────────────────────────────────────
_config: Optional[Config] = None
_shared_agent: Optional[AIAgent] = None   # single pre-warmed agent, shared across sessions

# ── Session store ─────────────────────────────────────────────────────────────
# session_id -> { title, created_at, updated_at, messages }
# Each session stores its own conversation history; _shared_agent is loaded with
# the active session's history before each call.
_sessions: Dict[str, dict] = {}


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
    Load the shared agent with the stored conversation history for session_id.
    Returns the shared agent, or None if not available.
    """
    if _shared_agent is None:
        return None
    s = _sessions.get(session_id)
    if s is None:
        return None

    # Reset agent conversation to just the system message, then replay this session's history
    agent = _shared_agent
    # Keep only the system message (first message)
    if agent.conversation and agent.conversation[0]["role"] == "system":
        agent.conversation = [agent.conversation[0]]
    else:
        agent.conversation = []

    # Replay stored conversation for this session
    for msg in s["messages"]:
        role = msg.get("role")
        content = msg.get("content", "")
        if role in ("user", "assistant"):
            agent.conversation.append({"role": role, "content": content})

    return agent


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
    clean = re.sub(r'\s+', ' ', text).strip()
    return (clean[:48] + "\u2026") if len(clean) > 48 else clean


# ── Startup / shutdown ────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    global _config, _shared_agent
    _config = Config()
    if not _config.validate():
        logger.error("API key not configured - set OPENAI_API_KEY in .env")
        sys.exit(1)
    # Initialize the single shared agent at startup (like the original code)
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
    if _shared_agent:
        if _shared_agent.tools:
            await _shared_agent.tools.cleanup()
        if _shared_agent.vector_memory:
            _shared_agent.vector_memory.close()


# ── Request models ────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    session_id: str


class RenameRequest(BaseModel):
    title: str


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    html_file = Path("static/index.html")
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(), status_code=200)
    return HTMLResponse(content="<h1>Frontend not found</h1>", status_code=404)


# ── Session endpoints ─────────────────────────────────────────────────────────
@app.get("/sessions")
async def list_sessions():
    result = []
    for sid, s in _sessions.items():
        result.append({
            "id":            sid,
            "title":         s["title"],
            "created_at":    s["created_at"],
            "updated_at":    s["updated_at"],
            "message_count": len(s["messages"]),
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
    return {
        "id":         session_id,
        "title":      s["title"],
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "messages":   s["messages"],
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
    return {"status": "deleted", "id": session_id}


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    sid = req.session_id
    if sid not in _sessions:
        async def err_gen():
            yield " " + json.dumps({"type": "error", "content": "Session not found"}) + "\n\n"
        return StreamingResponse(err_gen(), media_type="text/event-stream")
    agent = await _prepare_agent_for_session(sid)
    if not agent:
        async def err_gen2():
            yield " " + json.dumps({"type": "error", "content": "Agent not initialised"}) + "\n\n"
        return StreamingResponse(err_gen2(), media_type="text/event-stream")
    return StreamingResponse(
        _stream_response(req.message, sid, agent),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/chat/clear")
async def chat_clear(req: ChatRequest):
    sid = req.session_id
    s = _sessions.get(sid)
    if s:
        s["messages"] = []
        s["updated_at"] = datetime.now().isoformat()
        _save_session(sid)
    return {"status": "cleared", "session_id": sid}


@app.get("/health")
async def health():
    return {"status": "ok", "sessions": len(_sessions)}


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
    try:
        result = subprocess.run(
            ["pkill", "-f", f"background_task.*--name.*{name}"], capture_output=True
        )
        subprocess.run(["pkill", "-f", f"bg_task_{name}"], capture_output=True)
        return "stopped" if result.returncode == 0 else "already_stopped"
    except Exception as e:
        return f"error: {e}"


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
        raise HTTPException(status_code=404, detail=f"Log file for task '{name}' not found")
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


async def _stream_response(user_input: str, session_id: str, agent: AIAgent) -> AsyncGenerator[str, None]:
    s = _sessions.get(session_id)
    try:
        queue: asyncio.Queue = asyncio.Queue()

        async def run_agent():
            original_execute = agent.tools.execute_tool if agent.tools else None
            if original_execute:
                async def instrumented_execute(name, args):
                    args_preview = ", ".join(
                        f"{k}={str(v)[:50]}" for k, v in args.items()
                    ) if args else ""
                    await queue.put({"type": "tool", "name": name, "args": args_preview})
                    result = await original_execute(name, args)
                    preview = str(result)
                    if len(preview) > 400:
                        preview = preview[:400] + "\u2026"
                    await queue.put({"type": "result", "name": name, "content": preview})
                    return result
                agent.tools.execute_tool = instrumented_execute
            try:
                response = await agent.get_response(user_input)
            finally:
                if original_execute and agent.tools:
                    agent.tools.execute_tool = original_execute
            await queue.put({"type": "__response__", "content": response or ""})
            await queue.put(None)

        task = asyncio.create_task(run_agent())

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=180.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event is None:
                break
            if event.get("type") == "__response__":
                content = event.get("content", "")
                words = content.split(" ")
                for i, word in enumerate(words):
                    chunk = word + (" " if i < len(words) - 1 else "")
                    yield " " + json.dumps({"type": "token", "content": chunk}) + "\n\n"
                    await asyncio.sleep(0.008)
                # Persist both user and assistant messages after response
                if s is not None:
                    now = datetime.now().isoformat()
                    s["messages"].append({"role": "user", "content": user_input, "ts": now})
                    s["messages"].append({"role": "assistant", "content": content, "ts": now})
                    if len([m for m in s["messages"] if m["role"] == "user"]) == 1:
                        s["title"] = _auto_title(user_input)
                    s["updated_at"] = now
                    _save_session(session_id)
            else:
                yield " " + json.dumps(event) + "\n\n"
                await asyncio.sleep(0)

        await task
        yield " " + json.dumps({"type": "done"}) + "\n\n"

    except Exception as e:
        logger.error(f"Stream error: {e}")
        import traceback
        traceback.print_exc()
        yield " " + json.dumps({"type": "error", "content": str(e)}) + "\n\n"


if __name__ == "__main__":
    uvicorn.run(
        "web_app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )