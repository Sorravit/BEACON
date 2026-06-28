"""Agent Task Mode + multi-agent orchestration routes."""

import asyncio
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from web import state
from web.schemas import AgentTaskRequest, OrchestrateRequest, TaskAnswerRequest
from web.helpers import _auto_title, _build_conversation, _schedule_save
from web.telemetry import (
    _TELEMETRY_AVAILABLE,
    SessionReporter,
    session_span_context,
    set_session_context,
    clear_session_context,
)
from web.logging_setup import logger

# AgentExecutor task routes
from api.agent_executor import AgentExecutor, Task, TaskStatus  # noqa: F401
from core.orchestration import Orchestrator

router = APIRouter()


async def _evict_task_later(task_id: str, delay: int = 600):
    """Phase 9 / #9: evict finished agent tasks from memory after `delay` seconds."""
    await asyncio.sleep(delay)
    state._agent_tasks.pop(task_id, None)
    logger.debug("Evicted agent task %s from memory", task_id)


def _safe_task_text(text):
    """Remove UTF-8 surrogates so session JSON serialises cleanly."""
    if not text:
        return ""
    try:
        return str(text).encode("utf-8", errors="replace").decode("utf-8")
    except Exception:
        return ""


def _make_task_callback(task_id, session_id=None):
    """Build the SSE/event callback shared by AgentExecutor and the Orchestrator.

    Buffers every phase event for streaming and persists only the final result
    (or failure) to the session's chat history.
    """
    state_obj = state._agent_tasks[task_id]

    def _cb(event, data):
        now = datetime.now().isoformat()
        # Ensure every event has a timestamp for real-time UI rendering
        event_data = {"type": event, "ts": now, **data}
        state_obj["event_buf"].append(event_data)
        if event in ("task_completed", "task_failed", "task_cancelled"):
            state_obj["done"] = True

        if not (session_id and session_id in state._sessions):
            return
        s = state._sessions[session_id]

        if event == "task_completed":
            result = _safe_task_text(data.get("result", ""))
            if result:
                s["messages"].append({
                    "role": "assistant",
                    "content": "🏁 **Task Complete**\n\n" + result,
                    "ts": now,
                })
                s["updated_at"] = now
                _schedule_save(session_id)
        elif event in ("task_failed", "task_cancelled"):
            error = _safe_task_text(data.get("error", "unknown error"))
            s["messages"].append({
                "role": "assistant",
                "content": "❌ **Task Failed**\n\n" + error,
                "ts": now,
            })
            s["updated_at"] = now
            _schedule_save(session_id)

    return _cb


def _make_agent_executor(task_id, session_id=None):
    cb = _make_task_callback(task_id, session_id)
    # Build session conversation context so Task Mode planning/execution is
    # aware of everything discussed in regular chat before activation.
    session_conv = None
    if session_id:
        session_conv = _build_conversation(state._shared_agent, session_id)

    return AgentExecutor(state._shared_agent, step_callback=cb, session_conversation=session_conv)


@router.post("/agent/task")
async def agent_submit_task(req: AgentTaskRequest):
    """DEPRECATED single-agent Task Mode (research→plan→act→verify with one model).

    Superseded by POST /agent/orchestrate, which runs the same pipeline as a
    multi-agent team with per-agent model selection, dynamic specialist spawning
    and spec-aware verification. Kept for backward compatibility / task recovery;
    the web UI no longer calls this endpoint.
    """
    if state._shared_agent is None: raise HTTPException(status_code=503, detail="Agent not initialised")
    task_id = "agt_" + uuid.uuid4().hex[:12]
    session_id = req.session_id or None
    state._agent_tasks[task_id] = {"event_buf": [], "done": False, "asyncio_task": None,
                              "description": req.description}

    # Save task command as a user message IMMEDIATELY on submission so it always
    # appears in chat history regardless of whether the task succeeds or fails.
    if session_id and session_id in state._sessions:
        s = state._sessions[session_id]
        now = datetime.now().isoformat()
        # Avoid duplicate if already present (e.g. page reload)
        already_saved = any(
            m.get("role") == "user" and m.get("content") == f"[Task Mode] {req.description}"
            for m in s["messages"][-3:] if s["messages"]
        )
        if not already_saved:
            s["messages"].append({
                "role": "user",
                "content": f"[Task Mode] {req.description}",
                "ts": now,
            })
            if not s.get("manually_named") and len([m for m in s["messages"] if m["role"] == "user"]) == 1:
                s["title"] = _auto_title(req.description)
            s["updated_at"] = now
            _schedule_save(session_id)
            logger.info(f"Saved task command to session {session_id}: {req.description[:60]}")

    executor = _make_agent_executor(task_id, session_id=session_id)
    state._agent_tasks[task_id]["executor"] = executor  # stored so /answer can call submit_answer()
    async def _run():
        try:
            await executor.execute_task(req.description, task_id=task_id)
        except Exception as exc:
            state._agent_tasks[task_id]["event_buf"].append({"type": "task_failed", "task_id": task_id, "error": str(exc)})
            state._agent_tasks[task_id]["done"] = True
        finally:
            buf = state._agent_tasks[task_id]["event_buf"]
            if not buf or buf[-1].get("type") != "stream_done": buf.append({"type": "stream_done", "task_id": task_id})
            state._agent_tasks[task_id]["done"] = True
            # Phase 9: schedule eviction after 10 minutes
            asyncio.create_task(_evict_task_later(task_id, delay=600))
    state._agent_tasks[task_id]["asyncio_task"] = asyncio.create_task(_run())
    return {"task_id": task_id, "status": "started"}


@router.get("/agent/task/{task_id}")
async def agent_get_task(task_id: str):
    st = state._agent_tasks.get(task_id)
    if not st: raise HTTPException(status_code=404, detail="Task not found")
    buf = st["event_buf"]
    done = st["done"]
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


@router.get("/agent/task/{task_id}/stream")
async def agent_stream_task(task_id: str, request: Request):
    st = state._agent_tasks.get(task_id)
    if not st: raise HTTPException(status_code=404, detail="Task not found")
    async def _gen():
        cursor = 0
        idle_ticks = 0
        while True:
            if await request.is_disconnected(): break
            buf = st["event_buf"]
            while cursor < len(buf):
                yield "data: " + json.dumps(buf[cursor]) + "\n\n"
                cursor += 1
            if st["done"] and cursor >= len(st["event_buf"]): break
            await asyncio.sleep(0.2)
            idle_ticks += 1
            if idle_ticks % 15 == 0:  # keepalive every ~3 s
                yield ": keepalive\n\n"
    return StreamingResponse(_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/agent/task/{task_id}/cancel")
async def agent_cancel_task(task_id: str):
    st = state._agent_tasks.get(task_id)
    if not st: raise HTTPException(status_code=404, detail="Task not found")
    at = st.get("asyncio_task")
    if at and not at.done():
        # If the agent is paused waiting for an answer, release it first so the
        # awaiting coroutine unblocks before we cancel the task.
        target = st.get("executor") or st.get("orchestrator")
        if target is not None and getattr(target, "awaiting_answer", False):
            try:
                target.submit_answer(task_id, "")
            except Exception:
                pass
        at.cancel()
        # Surface a cancelled event so the UI stops showing "running".
        buf = st["event_buf"]
        buf.append({"type": "task_cancelled", "task_id": task_id,
                    "error": "Cancelled by user"})
        if not buf or buf[-1].get("type") != "stream_done":
            buf.append({"type": "stream_done", "task_id": task_id})
        state._agent_tasks[task_id]["done"] = True
        return {"status": "cancelled", "task_id": task_id}
    return {"status": "already_done", "task_id": task_id}


@router.post("/agent/task/{task_id}/answer")
async def agent_task_answer(task_id: str, req: TaskAnswerRequest):
    """
    Submit the user's answer to a clarifying question that the agent emitted
    via the ``task_question`` SSE event during the PLAN phase.

    The executor is paused on an asyncio.Event waiting for this call.
    On receipt, execution resumes immediately with the answer injected into
    the task context so subsequent phases (ACT, VERIFY) are aware of it.
    """
    st = state._agent_tasks.get(task_id)
    if not st:
        raise HTTPException(status_code=404, detail="Task not found")

    # Works for both single-agent Task Mode (executor) and multi-agent
    # Orchestrate / Agent Mode (orchestrator) — both expose submit_answer().
    target = st.get("executor") or st.get("orchestrator")
    if target is None:
        raise HTTPException(status_code=409, detail="No agent attached to task")

    resumed = target.submit_answer(task_id, req.answer.strip())
    if not resumed:
        raise HTTPException(
            status_code=409,
            detail="Task is not currently waiting for an answer"
        )

    return {"status": "resumed", "task_id": task_id}


# ── Multi-agent orchestration ─────────────────────────────────────────────────
def _save_task_command(session_id: str, description: str, prefix: str):
    """Persist the submitted task as a user message so it always shows in chat."""
    if not (session_id and session_id in state._sessions):
        return
    s = state._sessions[session_id]
    now = datetime.now().isoformat()
    content = f"{prefix} {description}"
    already = any(
        m.get("role") == "user" and m.get("content") == content
        for m in s["messages"][-3:] if s["messages"]
    )
    if not already:
        s["messages"].append({"role": "user", "content": content, "ts": now})
        if not s.get("manually_named") and len([m for m in s["messages"] if m["role"] == "user"]) == 1:
            s["title"] = _auto_title(description)
        s["updated_at"] = now
        _schedule_save(session_id)


@router.post("/agent/orchestrate")
async def agent_orchestrate(req: OrchestrateRequest):
    """Run a goal through the multi-agent orchestrator (research → plan →
    specialist → verify, looping on failure). Streams the same event types as
    Task Mode via /agent/task/{id}/stream."""
    if state._shared_agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialised")
    task_id = "orch_" + uuid.uuid4().hex[:12]
    session_id = req.session_id or None
    state._agent_tasks[task_id] = {"event_buf": [], "done": False, "asyncio_task": None,
                              "description": req.description}
    _save_task_command(session_id, req.description, "[Orchestrate]")

    cb = _make_task_callback(task_id, session_id)
    session_conv = _build_conversation(state._shared_agent, session_id) if session_id else None

    async def _run():
        tools = None
        # ── Telemetry ─────────────────────────────────────────────────────────
        _orch_reporter = SessionReporter(session_id=session_id) if (_TELEMETRY_AVAILABLE and session_id) else None
        _orch_ctx_tokens = None
        _orch_span_cm = None
        _orch_span = None
        if _TELEMETRY_AVAILABLE and session_id:
            if _orch_reporter:
                _orch_ctx_tokens = set_session_context(session_id, _orch_reporter)
            _orch_span_cm = session_span_context(session_id)
            try:
                _orch_span = _orch_span_cm.__enter__()
                _orch_span.set_attribute("session.mode", "orchestrate")
            except Exception:
                _orch_span_cm = None
        try:
            from main import ToolManager
            # Phase 6 / #2: per-session browser context from the pool
            _orch_browser_ctx = await state._shared_agent.browser_pool.get_context(session_id) if (session_id and state._shared_agent.browser_pool) else None
            tools = ToolManager(
                vector_memory=state._shared_agent.vector_memory,
                mcp_manager=state._shared_agent.mcp_manager,
                shared_context=_orch_browser_ctx,
                skill_manager=state._shared_agent.skill_manager,
            )
            await tools.initialize()
            if session_id:
                tools.session_id = session_id
            orchestrator = Orchestrator(
                state._shared_agent,
                tools=tools,
                max_rounds=max(1, min(req.max_rounds, 5)),
                model_overrides=req.model_overrides,
                emit=cb,
                session_conversation=session_conv,
            )
            # Store so POST /agent/task/{id}/answer can resume it on a question.
            state._agent_tasks[task_id]["orchestrator"] = orchestrator
            await orchestrator.run(req.description, task_id=task_id)
        except Exception as exc:
            state._agent_tasks[task_id]["event_buf"].append(
                {"type": "task_failed", "task_id": task_id, "error": str(exc)})
            state._agent_tasks[task_id]["done"] = True
        finally:
            if tools:
                try:
                    await tools.cleanup()
                except Exception:
                    pass
            # ── Telemetry teardown ────────────────────────────────────────────
            if _orch_span_cm:
                try:
                    _orch_span_cm.__exit__(None, None, None)
                except Exception:
                    pass
            if _TELEMETRY_AVAILABLE and _orch_ctx_tokens:
                try:
                    clear_session_context(*_orch_ctx_tokens)
                except Exception:
                    pass
            if _orch_reporter:
                try:
                    _orch_reporter.mark_ended()
                    _orch_reporter.log_summary()
                    _orch_reporter.save()
                except Exception:
                    pass
            buf = state._agent_tasks[task_id]["event_buf"]
            if not buf or buf[-1].get("type") != "stream_done":
                buf.append({"type": "stream_done", "task_id": task_id})
            state._agent_tasks[task_id]["done"] = True
            # Phase 9: schedule eviction after 10 minutes
            asyncio.create_task(_evict_task_later(task_id, delay=600))

    state._agent_tasks[task_id]["asyncio_task"] = asyncio.create_task(_run())
    return {"task_id": task_id, "status": "started"}
