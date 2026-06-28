"""Chat streaming routes + the decoupled background agent runner."""

import asyncio
import json
import time
from datetime import datetime
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from web import state
from web.schemas import ChatRequest
from web.helpers import (
    _auto_title,
    _build_conversation,
    _generate_smart_title,
    _schedule_save,
    _save_session_async,
)
from web.telemetry import (
    _TELEMETRY_AVAILABLE,
    SessionReporter,
    session_span_context,
    set_session_context,
    clear_session_context,
)
from web.logging_setup import logger

router = APIRouter()


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    sid = req.session_id
    if sid not in state._sessions:
        async def err_gen():
            yield " " + json.dumps({"type": "error", "content": "Session not found"}) + "\n\n"
        return StreamingResponse(err_gen(), media_type="text/event-stream")

    bg = state._bg_state(sid)
    if bg["task"] is not None and not bg["task"].done():
        return StreamingResponse(
            _reconnect_stream(sid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if state._shared_agent is None:
        async def err_gen2():
            yield " " + json.dumps({"type": "error", "content": "Agent not initialised"}) + "\n\n"
        return StreamingResponse(err_gen2(), media_type="text/event-stream")

    agent = state._shared_agent
    bg["event_buf"] = []
    bg["trim_offset"] = 0
    bg["done_event"] = asyncio.Event()
    bg["activity"] = ""
    bg["task"] = asyncio.create_task(_run_agent_bg(req.message, sid, agent, model=req.model))

    return StreamingResponse(
        _reconnect_stream(sid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/reconnect/{session_id}")
async def chat_reconnect(session_id: str):
    if session_id not in state._sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    return StreamingResponse(
        _reconnect_stream(session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/stop/{session_id}")
async def stop_chat(session_id: str):
    bg = state._bg.get(session_id)
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


@router.get("/chat/status/{session_id}")
async def chat_status(session_id: str):
    bg = state._bg.get(session_id, {})
    task = bg.get("task")
    running = task is not None and not task.done()
    activity = bg.get("activity", "") if running else ""
    return {"running": running, "activity": activity, "session_id": session_id}


@router.post("/chat/clear")
async def chat_clear(req: ChatRequest):
    sid = req.session_id
    s = state._sessions.get(sid)
    if s:
        s["messages"] = []
        s["updated_at"] = datetime.now().isoformat()
        await _save_session_async(sid)   # immediate for clear
    state._bg.pop(sid, None)
    return {"status": "cleared", "session_id": sid}


# ── Background agent task ─────────────────────────────────────────────────────

async def _run_agent_bg(user_input: str, session_id: str, agent, model: Optional[str] = None):
    """
    Run the agent completely decoupled from any HTTP connection.
    Each call gets its own ToolManager and conversation list.
    """
    bg = state._bg_state(session_id)
    s = state._sessions.get(session_id)

    # ── Telemetry: session root span + reporter + ContextVars ─────────────────
    import time as _tmod
    _t0_sess = _tmod.perf_counter()
    _reporter = SessionReporter(session_id=session_id) if _TELEMETRY_AVAILABLE else None
    _ctx_tokens = None
    if _TELEMETRY_AVAILABLE and _reporter:
        _ctx_tokens = set_session_context(session_id, _reporter)
    _sess_span_cm = session_span_context(session_id) if _TELEMETRY_AVAILABLE else None
    _sess_otel_span = None
    if _sess_span_cm:
        try:
            _sess_otel_span = _sess_span_cm.__enter__()
            _sess_otel_span.set_attribute("session.user_message_chars", len(user_input))
        except Exception:
            _sess_span_cm = None

    def _emit(ev: dict):
        bg["event_buf"].append(ev)

    # FIX 1: Build conversation BEFORE saving the new user message so that
    # get_response() does NOT receive user_input twice (once from history,
    # once when it appends it internally).  On message 2+ the old order
    # produced a duplicate user→user tail that caused silent LLM rejections
    # and session hangs with no server-side log output.
    conversation = _build_conversation(agent, session_id)

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
            _schedule_save(session_id)  # Phase 3: debounced async write

    # Resolve the model that will actually answer so the UI can show it and we
    # can persist it with the message.
    resolved_model = agent.config.resolve_model(model, role="chat")
    _model_info = agent.config.models.get(resolved_model)
    model_label = _model_info.label if _model_info else resolved_model
    _emit({"type": "model", "content": resolved_model, "label": model_label, "ts": datetime.now().isoformat()})

    # ── Chat-turn lifecycle logging ───────────────────────────────────────────
    # A single, scannable signal that the message was received and is being
    # worked, plus a summary when it finishes. Keeps logs informative without
    # the per-token / per-print noise.
    _sid8 = session_id[:8]
    _msg_preview = " ".join(user_input.split())
    if len(_msg_preview) > 80:
        _msg_preview = _msg_preview[:80] + "…"
    _turn_stats = {"tools": 0}
    logger.info(
        '▶ chat turn START [session=%s] model=%s msg="%s"',
        _sid8, model_label, _msg_preview,
    )

    try:
        from main import ToolManager
        # Phase 6 / #2: get per-session browser context from the pool
        _browser_ctx = await agent.browser_pool.get_context(session_id) if agent.browser_pool else None
        per_request_tools = ToolManager(
            vector_memory=agent.vector_memory,
            mcp_manager=agent.mcp_manager,
            shared_context=_browser_ctx,
            skill_manager=agent.skill_manager,
        )
        await per_request_tools.initialize()
        per_request_tools.session_id = session_id

        original_execute = per_request_tools.execute_tool

        async def instrumented_execute(name, args):
            # The canonical OTel span (tool/<name> with parameters, duration and
            # result) is created inside ToolManager.execute_tool. Here we only
            # add the SSE activity events and the per-session JSON bookkeeping —
            # no extra span, so each tool call shows up exactly once in a trace.
            args_preview = ", ".join(
                f"{k}={str(v)}" for k, v in args.items()
            ) if args else ""
            bg["activity"] = f"{name}({args_preview})"
            _turn_stats["tools"] += 1
            _args_log = args_preview if len(args_preview) <= 120 else args_preview[:120] + "…"
            logger.info("🔧 [session=%s] tool %s(%s)", _sid8, name, _args_log)
            _emit({"type": "tool", "name": name, "args": args_preview})
            # ── Skill indicator: tell the UI which skill is now active ──
            if name == "load_skill":
                _skill_name = args.get("name", "") if args else ""
                if _skill_name:
                    _emit({"type": "skill_active", "skill": _skill_name})

            t0_tool = time.monotonic()
            status = "ok"
            error_text = None
            # FIX 2: sentinel guards against UnboundLocalError in finally when
            # original_execute raises before `result` is assigned.
            _TOOL_UNSET = object()
            result = _TOOL_UNSET
            try:
                result = await original_execute(name, args)
                return result
            except Exception as exc:
                status = "error"
                error_text = str(exc)[:300]
                raise
            finally:
                duration_ms = (time.monotonic() - t0_tool) * 1000
                if _reporter:
                    _reporter.add_tool_call(
                        name, duration_ms, status=status,
                        error=error_text, parameters=args,
                    )
                if status == "ok" and result is not _TOOL_UNSET:
                    preview = str(result)
                    if len(preview) > 400:
                        preview = preview[:400] + "…"
                    bg["activity"] = f"Processing result from {name}..."
                    _emit({"type": "result", "name": name, "content": preview})
        per_request_tools.execute_tool = instrumented_execute

        bg["activity"] = "Thinking..."
        # Track whether any token actually reached the UI. Several get_response()
        # return paths (empty/timeout warning, iteration backstop, empty-after-
        # strip) produce content without streaming it. The frontend renders the
        # bubble purely from streamed tokens, so without this guard those turns
        # show a blank "no response" bubble. See the emit-fallback below.
        _streamed = {"any": False}
        try:
            def _token_cb(token: str):
                if token:
                    _streamed["any"] = True
                _emit({"type": "token", "content": token})
            response = await agent.get_response(user_input, conversation=conversation, tools=per_request_tools, token_callback=_token_cb, model=model)
            # ── Skill indicator Path-1: agent.py keyword dispatch ──
            _dispatched = getattr(agent, "_last_dispatched_skill", "")
            if _dispatched:
                _emit({"type": "skill_active", "skill": _dispatched})
            # ── Skill indicator: detect Path-1 agent.py dispatch (keyword match) ──
            if response and response.lstrip().startswith("\n\U0001f916") or (response and "**[" in response[:60] and response.lstrip().startswith("\n")):
                import re as _re_skill
                _m = _re_skill.search(r'\*\*\[([^\]]+)\]\*\*', response[:120])
                if _m:
                    _dispatched = _m.group(1).lower().replace(" agent", "").replace(" ", "_")
                    _emit({"type": "skill_active", "skill": _dispatched})
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

        # Safety net: if get_response produced content but streamed nothing (empty/
        # timeout warning, iteration backstop, empty-after-strip, etc.), emit it now
        # so the UI never shows a blank "no response" bubble.
        if content and not _streamed["any"]:
            _emit({"type": "token", "content": content})

        if s is not None:
            now = datetime.now().isoformat()
            s["messages"].append({"role": "assistant", "content": content, "ts": now,
                                   "model": resolved_model, "model_label": model_label})
            s["updated_at"] = now
            _schedule_save(session_id)  # Phase 3: debounced async write

        _emit({"type": "done"})

        _elapsed = _tmod.perf_counter() - _t0_sess
        logger.info(
            "■ chat turn DONE [session=%s] %.1fs · %d chars · %d tool call(s)",
            _sid8, _elapsed, len(content), _turn_stats["tools"],
        )

        # Generate smart title after first exchange (fire-and-forget)
        if s is not None and not s.get("manually_named"):
            user_msgs = [m for m in s["messages"] if m["role"] == "user"]
            if len(user_msgs) == 1:
                asyncio.create_task(
                    _generate_smart_title(session_id, user_input, content)
                )

    except asyncio.CancelledError:
        _elapsed = _tmod.perf_counter() - _t0_sess
        logger.info(
            "■ chat turn STOPPED [session=%s] %.1fs · %d tool call(s) (cancelled by user)",
            _sid8, _elapsed, _turn_stats["tools"],
        )
        _emit({"type": "stopped", "content": "Stopped by user"})
        if _sess_span_cm and _sess_otel_span:
            try:
                from opentelemetry.trace import Status, StatusCode
                _sess_otel_span.set_status(Status(StatusCode.ERROR, "Cancelled by user"))
                _sess_span_cm.__exit__(None, None, None)
                _sess_span_cm = None
            except Exception:
                pass
        raise
    except Exception as e:
        _elapsed = _tmod.perf_counter() - _t0_sess
        logger.error(
            "■ chat turn ERROR [session=%s] %.1fs · %d tool call(s): %s",
            _sid8, _elapsed, _turn_stats["tools"], e,
        )
        logger.debug("Agent BG traceback", exc_info=True)  # OTel-instrumented
        _emit({"type": "error", "content": str(e)})
        if _sess_span_cm and _sess_otel_span:
            try:
                _sess_span_cm.__exit__(type(e), e, e.__traceback__)
                _sess_span_cm = None
            except Exception:
                pass
    finally:
        # ── Close OTel root session span ──────────────────────────────────────
        if _sess_span_cm:
            try:
                _sess_span_cm.__exit__(None, None, None)
            except Exception:
                pass
        # ── Reset ContextVars ─────────────────────────────────────────────────
        if _TELEMETRY_AVAILABLE and _ctx_tokens:
            try:
                clear_session_context(*_ctx_tokens)
            except Exception:
                pass
        # ── Persist SessionReporter JSON to disk ──────────────────────────────
        if _reporter:
            try:
                _reporter.mark_ended()
                _reporter.log_summary()
                _reporter.save()
            except Exception as _re:
                logger.debug(f"SessionReporter save error: {_re}")
        bg["activity"] = ""
        # FIX3: guard against None (done_event set lazily in chat_stream)
        _dev = bg.get("done_event")
        if _dev is not None:
            _dev.set()
        # Cap event_buf to avoid unbounded growth. We only trim AFTER marking done
        # (done_event.set() above) so no active _reconnect_stream consumer can lose
        # unseen events. trim_offset tracks the logical start index so consumers
        # that reconnect after trimming still compute the correct list index.
        _buf = bg["event_buf"]
        if len(_buf) > 4000:
            keep = _buf[-2000:]
            bg["trim_offset"] = bg.get("trim_offset", 0) + (len(_buf) - len(keep))
            bg["event_buf"] = keep


async def _reconnect_stream(session_id: str) -> AsyncGenerator[str, None]:
    """
    Stream all buffered events then continue streaming live events until done.

    Uses a logical cursor (monotonic event index) combined with bg['trim_offset']
    so that post-turn event_buf trimming never causes unseen events to be skipped
    or already-seen events to be re-sent on reconnect.
    """
    bg = state._bg_state(session_id)
    done_event = bg.get("done_event")  # FIX2: snapshot once at entry
    # logical_cursor is the monotonic index of the next event to send.
    # buf[logical_cursor - trim_offset] gives the list position.
    logical_cursor = bg.get("trim_offset", 0)
    idle_ticks = 0

    while True:
        # FIX2: lazily pick up done_event if task not yet created at entry
        if done_event is None:
            done_event = bg.get("done_event")

        buf = bg["event_buf"]
        offset = bg.get("trim_offset", 0)

        # Drain any new events since last iteration
        while True:
            list_idx = logical_cursor - offset
            if list_idx < 0:
                # trim_offset advanced past our cursor (shouldn't happen since we
                # only trim after done, but guard defensively: jump to buf start).
                logical_cursor = offset
                list_idx = 0
            if list_idx >= len(buf):
                break
            ev = buf[list_idx]
            logical_cursor += 1
            yield " " + json.dumps(ev) + "\n\n"

        task = bg.get("task")
        is_done = (
            (task is None or task.done())
            and done_event is not None  # FIX2: guard stale-ref freeze
            and done_event.is_set()
        )
        # Re-check buffer length (logical) after refreshing offset
        buf_logical_end = bg.get("trim_offset", 0) + len(bg["event_buf"])
        if is_done and logical_cursor >= buf_logical_end:
            break

        await asyncio.sleep(0.25)
        idle_ticks += 1
        if idle_ticks % 8 == 0:
            yield ": keepalive\n\n"
