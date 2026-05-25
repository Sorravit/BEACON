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
import logging.handlers
import os
import re
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Dict, List, Optional

import atexit
import signal as _signal
import sys as _sys
_sys.stdout.reconfigure(line_buffering=True)

# ── Fix #1: Ignore SIGHUP so the server survives terminal disconnects ─────────
# When Fish shell / SSH closes the controlling terminal it sends SIGHUP to the
# process group. Python's default disposition is SIG_DFL (terminate).
# Ignoring it lets the server keep running — identical to nohup behaviour.
_signal.signal(_signal.SIGHUP, _signal.SIG_IGN)

# ── Fix #3: Belt-and-suspenders loky cleanup via atexit ───────────────────────
# sentence-transformers → joblib → loky creates a reusable process pool backed
# by a POSIX semaphore. If the loky executor is not explicitly shut down before
# the interpreter exits, Python's resource_tracker reports a leaked semaphore.
# This atexit handler guarantees cleanup even on abnormal exits (SIGTERM, etc.)
def _cleanup_loky():
    try:
        from joblib.externals.loky import get_reusable_executor
        get_reusable_executor().shutdown(wait=False)
    except Exception:
        pass

atexit.register(_cleanup_loky)

import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from main import AIAgent, Config

logger = logging.getLogger(__name__)

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _shared_agent
    # ── Startup ───────────────────────────────────────────────────────────────
    _config = Config()
    if not _config.validate():
        logger.error("API key not configured - set OPENAI_API_KEY in .env")
        sys.exit(1)

    # FIX: initialize MCPManager ONCE here — main.py duplicated this
    _shared_agent = AIAgent(_config)
    ok = await _shared_agent.initialize()
    if not ok:
        logger.error("Failed to initialize AI agent")
        sys.exit(1)
    logger.info("AI Agent ready")
    _load_all_sessions()
    logger.info(f"Loaded {len(_sessions)} session(s) from disk")

    # Truncate any oversized background task log files on startup
    os.makedirs("logs", exist_ok=True)
    for lf in Path("logs").glob("bg_*.log"):
        _rotate_log_if_needed(lf)
    _rotate_bg_logs()

    yield  # App runs here

    # ── Shutdown ──────────────────────────────────────────────────────────────
    if _shared_agent and hasattr(_shared_agent, "shutdown"):
        await _shared_agent.shutdown()
    elif _shared_agent:
        if _shared_agent.tools:
            await _shared_agent.tools.cleanup()
        if _shared_agent.vector_memory:
            _shared_agent.vector_memory.close()


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="Big's Personal AI Assistant", version="4.5.0", lifespan=lifespan)

static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Auth middleware ───────────────────────────────────────────────────────────
_AUTH_TOKEN = os.getenv("AUTH_TOKEN", "").strip()

def _login_page_html() -> str:
    return '''<!DOCTYPE html><html><head><title>BEACON Login</title>
<style>*{box-sizing:border-box}body{background:#0f0f17;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;font-family:system-ui,sans-serif;}
.box{background:#1a1a2e;padding:2.5rem;border-radius:16px;width:320px;box-shadow:0 8px 32px #0006;}
h2{color:#cdd6f4;margin:0 0 1.5rem;text-align:center;font-size:1.4rem;}  
input{width:100%;padding:.7rem 1rem;background:#252540;border:1px solid #44446a;border-radius:8px;color:#cdd6f4;font-size:14px;margin-bottom:1rem;outline:none;}
input:focus{border-color:#6c63ff;}
button{width:100%;padding:.75rem;background:#6c63ff;border:none;border-radius:8px;color:#fff;font-size:15px;cursor:pointer;font-weight:600;}
button:hover{background:#5a52d5;}
.err{color:#f38ba8;font-size:13px;text-align:center;margin-top:.5rem;display:none;}</style></head>
<body><div class="box"><h2>🤖 BEACON</h2>
<form onsubmit="login(event)">
<input type="password" id="tok" placeholder="Access token" autofocus>
<button type="submit">Login</button>
<p class="err" id="err">Invalid token — try again</p>
</form></div>
<script>
async function login(e){
  e.preventDefault();
  const tok=document.getElementById("tok").value.trim();
  if(!tok)return;
  const r=await fetch("/health",{headers:{"Authorization":"Bearer "+tok}});
  if(r.ok){document.cookie="auth_token="+encodeURIComponent(tok)+";path=/;max-age=86400";location.reload();}
  else{document.getElementById("err").style.display="block";}}
</script></body></html>'''

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Auth disabled if no token configured (dev mode)
    if not _AUTH_TOKEN:
        return await call_next(request)
    # Always allow health check (used by login page to verify token)
    skip_paths = ["/health", "/static", "/favicon"]
    if any(request.url.path.startswith(p) for p in skip_paths):
        return await call_next(request)
    # Extract token from Authorization header, cookie, or query param
    token = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    if not token:
        token = request.cookies.get("auth_token", "").strip()
    if not token:
        token = request.query_params.get("token", "").strip()
    if token == _AUTH_TOKEN:
        return await call_next(request)
    # Unauthorized — return login page for browser, JSON for API
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        from fastapi.responses import HTMLResponse as _HR
        return _HR(content=_login_page_html(), status_code=401)
    from fastapi.responses import JSONResponse as _JR
    return _JR(status_code=401, content={"error": "Unauthorized — set AUTH_TOKEN in .env"})

SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

# ── Global config + shared agent ─────────────────────────────────────────────
_config: Optional[Config] = None
_shared_agent: Optional[AIAgent] = None

# ── Session store ─────────────────────────────────────────────────────────────
_sessions: Dict[str, dict] = {}

# ── Per-session background state ─────────────────────────────────────────────
_bg: Dict[str, dict] = {}

# ── Tasks SSE: snapshot cache so we only push when something changes ──────────
_last_tasks_snapshot: Optional[str] = None


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


def _load_all_sessions():
    for f in sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            data = json.loads(f.read_text())
            sid = data["id"]
            _sessions[sid] = {
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
    _sessions[sid] = {
        "title":          "New Chat",
        "created_at":     now,
        "updated_at":     now,
        "messages":       [],
        "manually_named": False,
        "pinned":         False,
        "pin_order":      0,
    }
    _save_session(sid)
    return sid


def _auto_title(text: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return (clean[:48] + "\u2026") if len(clean) > 48 else clean



async def _generate_smart_title(session_id: str, user_msg: str, assistant_msg: str):
    """Fire-and-forget: ask the model for a concise title after the first exchange."""
    try:
        agent = _shared_agent
        if not agent or not agent.client:
            return
        loop = asyncio.get_running_loop()
        params = dict(
            model=_config.model,
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
            max_tokens=_config.max_tokens,
        )
        response = await loop.run_in_executor(
            None, lambda: agent.client.chat.completions.create(**params)
        )
        raw = (response.choices[0].message.content or "").strip()
        title = raw.splitlines()[0].strip().strip("'\"")
        if title and len(title) > 2:
            s = _sessions.get(session_id)
            if s and not s.get("manually_named"):
                s["title"] = title[:60]
                s["updated_at"] = datetime.now().isoformat()
                _save_session(session_id)
                logger.info(f"Smart title for {session_id}: {title}")
    except Exception as e:
        logger.debug(f"Smart title generation skipped: {e}")

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


class ChatRequest(BaseModel):
    message: str
    session_id: str


class RenameRequest(BaseModel):
    title: str


class ReorderRequest(BaseModel):
    order: list


# ── File upload ───────────────────────────────────────────────────────────────
@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a file and save it to temp/ folder. Returns the saved path."""
    import shutil

    dest_dir = Path("temp")
    dest_dir.mkdir(exist_ok=True)

    safe_name = Path(file.filename).name.replace(" ", "_")
    if not safe_name:
        safe_name = "upload"
    dest = dest_dir / safe_name

    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    counter = 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    contents = await file.read()
    dest.write_bytes(contents)

    return {"path": str(dest), "name": dest.name, "size": dest.stat().st_size}


# ── Static pages ──────────────────────────────────────────────────────────────
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
            "pinned":        s.get("pinned", False),
            "pin_order":     s.get("pin_order", 0),
        })
    pinned   = [x for x in result if x["pinned"]]
    unpinned = [x for x in result if not x["pinned"]]
    pinned.sort(key=lambda x: x.get("pin_order", 0))
    unpinned.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"sessions": pinned + unpinned}


@app.post("/sessions")
async def create_session():
    sid = _create_session()
    return {"id": sid, "title": "New Chat"}


# NOTE: /sessions/reorder-pins must be registered BEFORE /sessions/{session_id}
# so FastAPI does not treat "reorder-pins" as a session_id path parameter.
@app.patch("/sessions/reorder-pins")
async def reorder_pins(req: ReorderRequest):
    for i, sid in enumerate(req.order):
        s = _sessions.get(sid)
        if s and s.get("pinned"):
            s["pin_order"] = i
            _save_session(sid)
    return {"status": "ok"}


@app.patch("/sessions/{session_id}/pin")
async def pin_session(session_id: str):
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    s["pinned"] = not s.get("pinned", False)
    if s["pinned"]:
        max_order = max(
            (v.get("pin_order", 0) for v in _sessions.values() if v.get("pinned")),
            default=-1,
        )
        s["pin_order"] = max_order + 1
    else:
        s["pin_order"] = 0
    s["updated_at"] = datetime.now().isoformat()
    _save_session(session_id)
    return {"id": session_id, "pinned": s["pinned"], "pin_order": s["pin_order"]}


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
    s["manually_named"] = True
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


@app.post("/sessions/{session_id}/delete-if-empty")
async def delete_session_if_empty(session_id: str):
    """Delete a session only if it has no messages. Called on page unload."""
    s = _sessions.get(session_id)
    if not s:
        return {"status": "not_found"}
    if len(s.get("messages", [])) == 0:
        _sessions.pop(session_id, None)
        fp = _session_file(session_id)
        if fp.exists():
            fp.unlink()
        _bg.pop(session_id, None)
        return {"status": "deleted"}
    return {"status": "kept", "message_count": len(s["messages"])}


# ── Chat streaming endpoints ──────────────────────────────────────────────────
@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    sid = req.session_id
    if sid not in _sessions:
        async def err_gen():
            yield " " + json.dumps({"type": "error", "content": "Session not found"}) + "\n\n"
        return StreamingResponse(err_gen(), media_type="text/event-stream")

    bg = _bg_state(sid)
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
    bg["event_buf"] = []
    bg["done_event"] = asyncio.Event()
    bg["activity"] = ""
    bg["task"] = asyncio.create_task(_run_agent_bg(req.message, sid, agent))

    return StreamingResponse(
        _reconnect_stream(sid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/chat/reconnect/{session_id}")
async def chat_reconnect(session_id: str):
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


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "sessions": len(_sessions)}


# ── Background agent task ─────────────────────────────────────────────────────
def _build_conversation(agent: AIAgent, session_id: str) -> list:
    """Build a fresh conversation list for this session (stateless per-request)."""
    s = _sessions.get(session_id)
    conv = []

    if agent.conversation and agent.conversation[0]["role"] == "system":
        conv.append(agent.conversation[0])

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
    Each call gets its own ToolManager and conversation list.
    """
    bg = _bg_state(session_id)
    s = _sessions.get(session_id)

    def _emit(ev: dict):
        bg["event_buf"].append(ev)

    if s is not None:
        now = datetime.now().isoformat()
        already_saved = any(
            m.get("role") == "user" and m.get("content") == user_input
            for m in s["messages"][-2:] if s["messages"]
        )
        if not already_saved:
            s["messages"].append({"role": "user", "content": user_input, "ts": now})
            if not s.get("manually_named") and len([m for m in s["messages"] if m["role"] == "user"]) == 1:
                s["title"] = _auto_title(user_input)
            s["updated_at"] = now
            _save_session(session_id)

    conversation = _build_conversation(agent, session_id)

    try:
        from main import ToolManager
        per_request_tools = ToolManager(
            vector_memory=agent.vector_memory,
            mcp_manager=agent.mcp_manager,
            shared_browser=await agent._get_shared_browser(),  # lazy init — one Chromium process shared
        )
        await per_request_tools.initialize()
        per_request_tools.session_id = session_id

        original_execute = per_request_tools.execute_tool

        async def instrumented_execute(name, args):
            args_preview = ", ".join(
                f"{k}={str(v)}" for k, v in args.items()
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
            def _token_cb(token: str):
                _emit({"type": "token", "content": token})
            response = await agent.get_response(user_input, conversation=conversation, tools=per_request_tools, token_callback=_token_cb)
        except Exception as api_err:
            err_msg = str(api_err)
            # Emit user-friendly error to the SSE stream
            if "too long" in err_msg.lower() or "token" in err_msg.lower():
                friendly = f"⚠️ Conversation too long for AI model. The context window was exceeded ({err_msg}). Try starting a new chat or clearing this one."
            else:
                friendly = f"⚠️ AI error: {err_msg}"
            _emit({"type": "error", "content": friendly})
            raise  # re-raise so the outer except CancelledError / except Exception handles it
        finally:
            await per_request_tools.cleanup()

        # Tokens already streamed live via token_callback during LLM inference.
        content = response or ""

        if s is not None:
            now = datetime.now().isoformat()
            s["messages"].append({"role": "assistant", "content": content, "ts": now})
            s["updated_at"] = now
            _save_session(session_id)

        _emit({"type": "done"})

        # Generate smart title after first exchange (fire-and-forget)
        if s is not None and not s.get("manually_named"):
            user_msgs = [m for m in s["messages"] if m["role"] == "user"]
            if len(user_msgs) == 1:
                asyncio.create_task(
                    _generate_smart_title(session_id, user_input, content)
                )

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
    """
    bg = _bg_state(session_id)
    cursor = 0
    idle_ticks = 0

    while True:
        buf = bg["event_buf"]
        while cursor < len(buf):
            ev = buf[cursor]
            cursor += 1
            yield " " + json.dumps(ev) + "\n\n"

        task = bg.get("task")
        is_done = (task is None or task.done()) and bg["done_event"].is_set()
        if is_done and cursor >= len(bg["event_buf"]):
            break

        await asyncio.sleep(0.25)
        idle_ticks += 1
        if idle_ticks % 8 == 0:
            yield ": keepalive\n\n"


# ── Background tasks (CLI processes) ─────────────────────────────────────────

async def _build_tasks_payload() -> dict:
    """Build the current tasks + notifications payload (used by both REST and SSE)."""
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
        # Rotate oversized logs in the background
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

            if sid and sid in _sessions and msg:
                s = _sessions[sid]
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
    return notes


# ── SSE event stream: single persistent feed replacing polling ────────────────
@app.get("/events")
async def event_stream(request: Request):
    """Single SSE stream replacing polling of /tasks and /tasks/notifications."""
    return StreamingResponse(
        _events_generator(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _events_generator(request: Request) -> AsyncGenerator[str, None]:
    """Yield task status + notifications + chat activity as SSE events every 3 seconds."""
    tick = 0
    while True:
        if await request.is_disconnected():
            logger.debug("SSE /events client disconnected — stopping generator")
            return
        try:
            payload = await _build_tasks_payload()

            # Build activity dict: session_id → current activity string for running sessions
            activity = {}
            for sid, bg in _bg.items():
                task = bg.get("task")
                running = task is not None and not task.done()
                if running and bg.get("activity"):
                    activity[sid] = bg["activity"]

            full_payload = {
                "tasks": payload["tasks"],
                "notifications": payload["notifications"],
                "activity": activity,
            }
            yield "data: " + json.dumps(full_payload) + "\n\n"

            tick += 1
            await asyncio.sleep(3)
            if tick % 5 == 0:  # keepalive every 15 s
                yield ": keepalive\n\n"
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"_events_generator error: {e}")
            await asyncio.sleep(3)


# ── Legacy SSE endpoint kept for backward compat ──────────────────────────────
@app.get("/tasks/stream")
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


# ── Background CLI task endpoints (NOTE: /tasks/notifications and /tasks/stop-all
#    must be registered BEFORE /tasks/{name}/... to avoid route shadowing) ─────
@app.get("/tasks")
async def list_tasks():
    payload = await _build_tasks_payload()
    return {"tasks": payload["tasks"]}


@app.get("/tasks/notifications")
async def get_notifications():
    notes = await _collect_notifications()
    return {"notifications": notes}


def _kill_task(name: str) -> str:
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

    r1 = subprocess.run(["pkill", "-f", f"background_task.*--name.*{name}"], capture_output=True)
    r2 = subprocess.run(["pkill", "-TERM", "-f", f"bg_{name}"], capture_output=True)
    if r1.returncode == 0 or r2.returncode == 0:
        killed = True

    return "stopped" if killed else "already_stopped"


@app.post("/tasks/stop-all")
async def stop_all_tasks():
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


# ── Per-task routes — must come AFTER all static /tasks/* routes ──────────────
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


# AgentExecutor task routes
from agent_executor import AgentExecutor, Task, TaskStatus
_agent_tasks = {}

class AgentTaskRequest(BaseModel):
    description: str
    session_id: str = None

def _make_agent_executor(task_id, session_id=None):
    state = _agent_tasks[task_id]

    def _cb(event, data):
        state["event_buf"].append({"type": event, **data})
        if event == "task_completed":
            state["done"] = True
            # Save task description (user turn) + result (assistant turn) into
            # the session's message history so regular chat can continue the
            # conversation with full awareness of what Task Mode did.
            if session_id and session_id in _sessions:
                s = _sessions[session_id]
                now = datetime.now().isoformat()
                task_desc = data.get("description") or state.get("description", "")
                result = data.get("result", "")
                if task_desc:
                    s["messages"].append({
                        "role": "user",
                        "content": f"[Task Mode] {task_desc}",
                        "ts": now,
                    })
                if result:
                    s["messages"].append({
                        "role": "assistant",
                        "content": f"[Task Mode completed]\n\n{result}",
                        "ts": now,
                    })
                s["updated_at"] = now
                _save_session(session_id)
        elif event in ("task_failed", "task_cancelled"):
            state["done"] = True

    # Build session conversation context so Task Mode planning/execution is
    # aware of everything discussed in regular chat before activation.
    session_conv = None
    if session_id:
        session_conv = _build_conversation(_shared_agent, session_id)

    return AgentExecutor(_shared_agent, step_callback=_cb, session_conversation=session_conv)

@app.post("/agent/task")
async def agent_submit_task(req: AgentTaskRequest):
    if _shared_agent is None: raise HTTPException(status_code=503, detail="Agent not initialised")
    task_id = "agt_" + uuid.uuid4().hex[:12]
    session_id = req.session_id or None
    _agent_tasks[task_id] = {"event_buf": [], "done": False, "asyncio_task": None,
                              "description": req.description}
    executor = _make_agent_executor(task_id, session_id=session_id)
    async def _run():
        try:
            await executor.execute_task(req.description, task_id=task_id)
        except Exception as exc:
            _agent_tasks[task_id]["event_buf"].append({"type": "task_failed", "task_id": task_id, "error": str(exc)})
            _agent_tasks[task_id]["done"] = True
        finally:
            buf = _agent_tasks[task_id]["event_buf"]
            if not buf or buf[-1].get("type") != "stream_done": buf.append({"type": "stream_done", "task_id": task_id})
            _agent_tasks[task_id]["done"] = True
    _agent_tasks[task_id]["asyncio_task"] = asyncio.create_task(_run())
    return {"task_id": task_id, "status": "started"}

@app.get("/agent/task/{task_id}")
async def agent_get_task(task_id: str):
    state = _agent_tasks.get(task_id)
    if not state: raise HTTPException(status_code=404, detail="Task not found")
    buf = state["event_buf"]
    done = state["done"]
    # Derive a human-readable status string
    if done:
        last_type = buf[-1].get("type") if buf else None
        if last_type == "task_completed":
            status = "completed"
        elif last_type in ("task_failed", "task_cancelled"):
            status = "failed"
        else:
            status = "done"
    else:
        status = "running"
    # Extract steps list from the task_planned event if present
    steps = []
    result = None
    current_step = None
    for ev in buf:
        if ev.get("type") == "task_planned" and ev.get("steps"):
            steps = ev["steps"]
        if ev.get("type") == "step_started":
            current_step = ev.get("step_id") or (ev.get("step", {}) or {}).get("step_id")
        if ev.get("type") == "task_completed":
            result = ev.get("result")
    return {
        "task_id": task_id,
        "done": done,
        "status": status,
        "steps": steps,
        "current_step": current_step,
        "result": result,
        "event_count": len(buf),
    }

@app.get("/agent/task/{task_id}/stream")
async def agent_stream_task(task_id: str, request: Request):
    state = _agent_tasks.get(task_id)
    if not state: raise HTTPException(status_code=404, detail="Task not found")
    async def _gen():
        cursor = 0
        idle_ticks = 0
        while True:
            if await request.is_disconnected(): break
            buf = state["event_buf"]
            while cursor < len(buf):
                yield "data: " + json.dumps(buf[cursor]) + "\n\n"
                cursor += 1
            if state["done"] and cursor >= len(state["event_buf"]): break
            await asyncio.sleep(0.2)
            idle_ticks += 1
            if idle_ticks % 15 == 0:  # keepalive every ~3 s
                yield ": keepalive\n\n"
    return StreamingResponse(_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/agent/task/{task_id}/cancel")
async def agent_cancel_task(task_id: str):
    state = _agent_tasks.get(task_id)
    if not state: raise HTTPException(status_code=404, detail="Task not found")
    at = state.get("asyncio_task")
    if at and not at.done():
        at.cancel(); _agent_tasks[task_id]["done"] = True
        return {"status": "cancelled", "task_id": task_id}
    return {"status": "already_done", "task_id": task_id}

if __name__ == "__main__":
    uvicorn.run(
        "web_app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
