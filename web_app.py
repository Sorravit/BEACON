#!/usr/bin/env python3
"""
AI Assistant - Web Application
Serves a chat UI and exposes the AIAgent via FastAPI + SSE streaming.
"""

import asyncio
import glob
import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import AsyncGenerator, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from main import AIAgent, Config

logger = logging.getLogger(__name__)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="AI Assistant", version="4.2.0")

static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Global agent ─────────────────────────────────────────────────────────────
_agent: Optional[AIAgent] = None


@app.on_event("startup")
async def startup_event():
    global _agent
    config = Config()
    if not config.validate():
        logger.error("❌ API key not configured — set OPENAI_API_KEY in .env")
        sys.exit(1)
    _agent = AIAgent(config)
    ok = await _agent.initialize()
    if not ok:
        logger.error("❌ Failed to initialize AI agent")
        sys.exit(1)
    logger.info("✅ AI Agent ready")


@app.on_event("shutdown")
async def shutdown_event():
    global _agent
    if _agent and _agent.tools:
        await _agent.tools.cleanup()
    if _agent and _agent.vector_memory:
        _agent.vector_memory.close()


# ── Request models ────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    html_file = Path("static/index.html")
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(), status_code=200)
    return HTMLResponse(content="<h1>Frontend not found — static/index.html missing</h1>", status_code=404)


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    Stream agent responses via Server-Sent Events.

    SSE event types:
      {"type": "tool",   "name": "...", "args": "..."}   — tool is being called
      {"type": "result", "name": "...", "content": "..."}  — tool result preview
      {"type": "token",  "content": "..."}                 — response text chunk
      {"type": "done"}                                      — stream finished
      {"type": "error",  "content": "..."}                 — error occurred
    """
    if not _agent:
        async def err_gen():
            yield " " + json.dumps({"type": "error", "content": "Agent not initialised"}) + "\n\n"
        return StreamingResponse(err_gen(), media_type="text/event-stream")

    return StreamingResponse(
        _stream_response(req.message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/chat/clear")
async def chat_clear():
    """Clear conversation history."""
    if _agent:
        _agent.clear()
    return {"status": "cleared"}


@app.get("/health")
async def health():
    return {"status": "ok", "agent_ready": _agent is not None}


# ── Background task endpoints ─────────────────────────────────────────────────

@app.get("/tasks")
async def list_tasks():
    """
    List all background tasks.
    Shows tasks that have a lock file OR a log file so stopped tasks remain visible.
    """
    # Collect names from lock files AND log files
    names = set()
    for lf in glob.glob("/tmp/bg_task_*.lock"):
        names.add(Path(lf).stem.replace("bg_task_", ""))
    os.makedirs("logs", exist_ok=True)
    for lf in glob.glob("logs/bg_*.log"):
        name = Path(lf).stem[3:]  # strip "bg_" prefix
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
    """Kill all processes related to a background task by name using pkill."""
    try:
        # Kill the background_task runner and any child using the task name as pattern
        result = subprocess.run(
            ["pkill", "-f", f"background_task.*--name.*{name}"],
            capture_output=True
        )
        # Also try killing by lock file pattern
        subprocess.run(
            ["pkill", "-f", f"bg_task_{name}"],
            capture_output=True
        )
        return "stopped" if result.returncode == 0 else "already_stopped"
    except Exception as e:
        return f"error: {e}"


@app.post("/tasks/{name}/stop")
async def stop_task(name: str):
    """Stop a running background task. Keeps BOTH the lock file and the log so the task stays visible."""
    status = _kill_task(name)
    return {"status": status, "name": name}


@app.delete("/tasks/{name}/log")
async def clear_task_log(name: str):
    """
    Delete the log file for a task.
    If the task is not running, also removes the lock file so it disappears from the panel.
    """
    log_file = Path(f"logs/bg_{name}.log")
    if not log_file.exists():
        return {"status": "not_found", "name": name}
    log_file.unlink()
    # Check if task is still running
    check = subprocess.run(
        ["pgrep", "-f", f"background_task.*--name.*{name}"],
        capture_output=True
    )
    if check.returncode != 0:
        # Task is not running — also remove the lock file so it leaves the panel
        lockfile = f"/tmp/bg_task_{name}.lock"
        try:
            if os.path.exists(lockfile):
                os.remove(lockfile)
        except Exception:
            pass
    return {"status": "cleared", "name": name}


@app.post("/tasks/{name}/stop-and-clear")
async def stop_and_clear_task(name: str):
    """Stop the task AND delete its log file."""
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
    """
    Stream the log file of a background task via SSE.
    Sends existing content first, then tails for new lines.
    Each event: data: {"line": "...", "done": false}
    Final event when task ends:  {"line": "", "done": true}
    """
    log_file = f"logs/bg_{name}.log"
    if not Path(log_file).exists():
        raise HTTPException(status_code=404, detail=f"Log file for task '{name}' not found")

    return StreamingResponse(
        _tail_log(name, log_file),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _tail_log(name: str, log_file: str) -> AsyncGenerator[str, None]:
    """Yield SSE events from a log file, tailing for new content."""
    try:
        # Send all existing content first
        with open(log_file, "r", errors="replace") as f:
            existing = f.read()
        for line in existing.splitlines():
            yield " " + json.dumps({"line": line, "done": False}) + "\n\n"

        # Now tail for new lines
        with open(log_file, "r", errors="replace") as f:
            f.seek(0, 2)  # seek to end
            while True:
                lockfile = f"/tmp/bg_task_{name}.lock"
                line = f.readline()
                if line:
                    yield " " + json.dumps({"line": line.rstrip(), "done": False}) + "\n\n"
                else:
                    # Check if task is still running
                    if not os.path.exists(lockfile):
                        yield "data: " + json.dumps({"line": "", "done": True}) + "\n\n"
                        break
                    await asyncio.sleep(0.5)
    except Exception as e:
        yield " " + json.dumps({"line": f"[error reading log: {e}]", "done": True}) + "\n\n"


# ── SSE streaming helper ──────────────────────────────────────────────────────
async def _stream_response(user_input: str) -> AsyncGenerator[str, None]:
    """Run the agent and yield SSE events."""
    try:
        queue: asyncio.Queue = asyncio.Queue()

        async def run_agent():
            original_execute = _agent.tools.execute_tool if _agent.tools else None

            if original_execute:
                async def instrumented_execute(name, args):
                    args_preview = ", ".join(
                        f"{k}={str(v)[:50]}" for k, v in args.items()
                    ) if args else ""
                    await queue.put({"type": "tool", "name": name, "args": args_preview})
                    result = await original_execute(name, args)
                    preview = str(result)
                    if len(preview) > 400:
                        preview = preview[:400] + "…"
                    await queue.put({"type": "result", "name": name, "content": preview})
                    return result

                _agent.tools.execute_tool = instrumented_execute

            try:
                response = await _agent.get_response(user_input)
            finally:
                if original_execute and _agent.tools:
                    _agent.tools.execute_tool = original_execute

            await queue.put({"type": "__response__", "content": response or ""})
            await queue.put(None)  # sentinel

        task = asyncio.create_task(run_agent())

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=180.0)
            except asyncio.TimeoutError:
                yield "data: " + json.dumps({"type": "error", "content": "Response timed out after 3 minutes"}) + "\n\n"
                break

            if event is None:
                break

            if event.get("type") == "__response__":
                content = event.get("content", "")
                # Stream text word-by-word for a typing effect
                words = content.split(" ")
                for i, word in enumerate(words):
                    chunk = word + (" " if i < len(words) - 1 else "")
                    yield " " + json.dumps({"type": "token", "content": chunk}) + "\n\n"
                    await asyncio.sleep(0.008)
            else:
                yield " " + json.dumps(event) + "\n\n"

        await task
        yield " " + json.dumps({"type": "done"}) + "\n\n"

    except Exception as e:
        logger.error(f"Stream error: {e}")
        import traceback
        traceback.print_exc()
        yield "data: " + json.dumps({"type": "error", "content": str(e)}) + "\n\n"


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "web_app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )