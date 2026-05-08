#!/usr/bin/env python3
"""
AI Assistant - Web Application
Serves a chat UI and exposes the AIAgent via FastAPI + SSE streaming.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import AsyncGenerator, Optional

import uvicorn
from fastapi import FastAPI
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