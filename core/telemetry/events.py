"""core/telemetry/events.py

Structured TOOL_CALL telemetry event helpers.

Every tool invocation emits:
  - An OpenTelemetry span  (tool.call / <tool_name>)
  - A Prometheus histogram observation (tool_call_duration_ms)
  - A structured log line  (TOOL_CALL JSON)

The public API is intentionally narrow:

    ctx = record_tool_start(tool_name, session_id, params)
    ...run tool...
    record_tool_end(ctx, result=result)          # success
    record_tool_end(ctx, error=exc)              # failure

All functions are synchronous, thread-safe, and never raise — any
telemetry error is caught and logged as a warning so the tool
execution path is never interrupted.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .tracer import get_tracer
from .metrics import (
    TOOL_CALL_COUNTER,
    TOOL_CALL_DURATION_MS,
    TOOL_CALL_ERROR_COUNTER,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context object passed between start / end
# ---------------------------------------------------------------------------

@dataclass
class ToolCallContext:
    """Opaque context carried between record_tool_start and record_tool_end."""

    tool_name:   str
    session_id:  str
    start_time:  float          # Unix timestamp, seconds (from time.time())
    start_ns:    int            # monotonic nanoseconds (for accurate duration)
    span:        Any            # opentelemetry Span (or NoOpSpan)
    params_preview: str = ""   # first 200 chars of params JSON (for span attr)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def record_tool_start(
    tool_name: str,
    session_id: str,
    params: dict | None = None,
) -> ToolCallContext:
    """
    Record the beginning of a tool call.

    Returns a ToolCallContext that MUST be passed to record_tool_end.
    """
    try:
        start_time = time.time()
        start_ns   = time.monotonic_ns()

        tracer = get_tracer("beacon.tools")
        span   = tracer.start_span(
            name=f"tool.call/{tool_name}",
            attributes={
                "tool.name":       tool_name,
                "session.id":      session_id,
                "tool.start_time": _iso(start_time),
            },
        )

        params_preview = ""
        if params:
            try:
                params_preview = json.dumps(params, ensure_ascii=False, default=str)[:200]
                span.set_attribute("tool.params_preview", params_preview)
            except Exception:
                pass

        return ToolCallContext(
            tool_name=tool_name,
            session_id=session_id,
            start_time=start_time,
            start_ns=start_ns,
            span=span,
            params_preview=params_preview,
        )
    except Exception as exc:
        logger.warning("[telemetry] record_tool_start error: %s", exc)
        # Return a dummy context so record_tool_end doesn't crash
        return ToolCallContext(
            tool_name=tool_name,
            session_id=session_id,
            start_time=time.time(),
            start_ns=time.monotonic_ns(),
            span=trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT),
        )


def record_tool_end(
    ctx: ToolCallContext,
    *,
    result: Any = None,
    error: Optional[BaseException] = None,
) -> dict:
    """
    Record the end of a tool call.

    Computes duration, emits metrics + span, writes a structured log.
    Returns the telemetry payload dict (useful for tests / debugging).
    """
    try:
        end_ns   = time.monotonic_ns()
        end_time = time.time()
        duration_ms = (end_ns - ctx.start_ns) / 1_000_000.0

        status   = "error" if error else "success"
        tool     = ctx.tool_name
        sid      = ctx.session_id

        # ── Prometheus metrics ────────────────────────────────────────────
        try:
            TOOL_CALL_DURATION_MS.labels(tool_name=tool, status=status).observe(duration_ms)
            TOOL_CALL_COUNTER.labels(tool_name=tool, status=status).inc()
            if error:
                TOOL_CALL_ERROR_COUNTER.labels(
                    tool_name=tool,
                    error_type=type(error).__name__,
                ).inc()
        except Exception as m_exc:
            logger.warning("[telemetry] metrics error: %s", m_exc)

        # ── OTel span ─────────────────────────────────────────────────────
        try:
            ctx.span.set_attribute("tool.end_time",   _iso(end_time))
            ctx.span.set_attribute("tool.duration_ms", round(duration_ms, 3))
            ctx.span.set_attribute("tool.status",      status)
            if error:
                ctx.span.record_exception(error)
                ctx.span.set_status(Status(StatusCode.ERROR, str(error)))
            else:
                ctx.span.set_status(Status(StatusCode.OK))
                if result is not None:
                    try:
                        result_preview = str(result)[:200]
                        ctx.span.set_attribute("tool.result_preview", result_preview)
                    except Exception:
                        pass
            ctx.span.end()
        except Exception as s_exc:
            logger.warning("[telemetry] span error: %s", s_exc)

        # ── Structured TOOL_CALL log ──────────────────────────────────────
        payload = {
            "event":        "TOOL_CALL",
            "tool_name":    tool,
            "session_id":   sid,
            "start_time":   _iso(ctx.start_time),
            "end_time":     _iso(end_time),
            "duration_ms":  round(duration_ms, 3),
            "status":       status,
        }
        if error:
            payload["error"] = str(error)
            payload["error_type"] = type(error).__name__

        try:
            logger.info(
                "TOOL_CALL tool=%s session=%s duration_ms=%.3f status=%s",
                tool, sid, duration_ms, status,
                extra={"telemetry": payload},
            )
        except Exception:
            pass

        return payload

    except Exception as exc:
        logger.warning("[telemetry] record_tool_end error: %s", exc)
        return {
            "event":      "TOOL_CALL",
            "tool_name":  ctx.tool_name,
            "session_id": ctx.session_id,
            "status":     "error",
            "error":      str(exc),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _iso(ts: float) -> str:
    """Convert a Unix timestamp to an ISO-8601 string (UTC, milliseconds)."""
    import datetime
    dt = datetime.datetime.utcfromtimestamp(ts)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
