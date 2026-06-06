"""core/telemetry/session_reporter.py
--------------------------------------
Per-session structured trace reporter for BEACON.

Builds a JSON-serialisable summary of every tool call and LLM invocation
captured during a single agent session and optionally saves it to disk.

Output schema (mirrors output/sample_trace.json)
------------------------------------------------
{
  "schema_version": "1",
  "session_id": "<uuid>",
  "started_at": "<ISO-8601>",
  "ended_at": "<ISO-8601>",
  "duration_ms": 12345.6,
  "status": "ok",
  "tool_calls": [
    {
      "seq": 1,
      "tool_name": "web_search",
      "started_at": "<ISO-8601>",
      "duration_ms": 341.2,
      "status": "ok",
      "error": null
    }
  ],
  "llm_calls": [
    {
      "seq": 1,
      "model": "claude-3-5-sonnet-20241022",
      "started_at": "<ISO-8601>",
      "duration_ms": 2103.4,
      "input_tokens": 1280,
      "output_tokens": 512,
      "status": "ok"
    }
  ],
  "summary": {
    "total_tool_calls": 5,
    "total_llm_calls": 3,
    "total_input_tokens": 3840,
    "total_output_tokens": 1536,
    "slowest_tool": {"tool_name": "execute_long_command", "duration_ms": 5432.1}
  }
}
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from .tracer import get_tracer
from .metrics import record_tool_call, record_llm_call

logger = logging.getLogger("beacon.telemetry.session")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SessionReporter:
    """Tracks all tool and LLM calls within a single agent session.

    Typical usage (inside ``web_app.py`` / ``_run_agent_bg``::

        reporter = SessionReporter(session_id=sid)
        with reporter.session_span(user_message=user_input):
            with reporter.track_tool("web_search") as span:
                result = await tool_manager.execute_tool("web_search", args)
            ...
        reporter.save()          # writes JSON to SESSION_REPORT_DIR
    """

    SCHEMA_VERSION = "1"

    def __init__(self, session_id: Optional[str] = None) -> None:
        self.session_id: str = session_id or str(uuid.uuid4())
        self._started_at: Optional[datetime] = None
        self._ended_at: Optional[datetime] = None
        self._status: str = "ok"
        self._tool_calls: List[Dict[str, Any]] = []
        self._llm_calls: List[Dict[str, Any]] = []
        self._tool_seq = 0
        self._llm_seq = 0
        # Auto-start timestamp
        self._started_at = _now()

    def mark_started(self) -> None:
        """Record session start timestamp explicitly."""
        self._started_at = _now()

    def mark_ended(self, status: str = "ok") -> None:
        """Record session end timestamp and final status."""
        self._ended_at = _now()
        self._status = status

    def mark_error(self) -> None:
        """Mark session as failed."""
        self._ended_at = _now()
        self._status = "error"

    # ------------------------------------------------------------------
    # Session root span
    # ------------------------------------------------------------------

    @contextmanager
    def session_span(self, user_message: str = "") -> Generator[Span, None, None]:
        """Root span that wraps the entire agent turn."""
        tracer = get_tracer("beacon.session")
        self._started_at = _now()
        attrs = {
            "session.id": self.session_id,
            "session.user_message_chars": len(user_message),
        }
        with tracer.start_as_current_span(
            "agent/session",
            attributes=attrs,
            kind=trace.SpanKind.SERVER,
        ) as span:
            try:
                yield span
                span.set_status(Status(StatusCode.OK))
            except Exception as exc:
                self._status = "error"
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, description=str(exc)))
                raise
            finally:
                self._ended_at = _now()

    # ------------------------------------------------------------------
    # Tool tracking
    # ------------------------------------------------------------------

    def add_tool_call(
        self,
        tool_name: str,
        duration_ms: float,
        status: str = "ok",
        error: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a completed tool call to the JSON report (no OTel span).

        Use this when the OTel span is already produced elsewhere (e.g. by
        ``ToolManager.execute_tool``) and you only need the session summary.
        """
        self._tool_seq += 1
        self._tool_calls.append({
            "seq": self._tool_seq,
            "tool_name": tool_name,
            "started_at": _now().isoformat(),
            "duration_ms": round(duration_ms, 2),
            "status": status,
            "error": error,
            "parameters": parameters,
        })

    @contextmanager
    def track_tool(
        self,
        tool_name: str,
    ) -> Generator[Span, None, None]:
        """Child span for a single tool call — records timing + status."""
        self._tool_seq += 1
        seq = self._tool_seq
        started = _now()
        t0 = time.perf_counter()

        entry: Dict[str, Any] = {
            "seq": seq,
            "tool_name": tool_name,
            "started_at": started.isoformat(),
            "duration_ms": 0.0,
            "status": "ok",
            "error": None,
        }
        with record_tool_call(tool_name, session_id=self.session_id) as span:
            try:
                yield span
                entry["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            except Exception as exc:
                entry["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
                entry["status"] = "error"
                entry["error"] = str(exc)[:300]
                raise
            finally:
                self._tool_calls.append(entry)

    # ------------------------------------------------------------------
    # LLM tracking
    # ------------------------------------------------------------------

    @contextmanager
    def track_llm(self, model: str) -> Generator[Span, None, None]:
        """Child span for a single LLM API call — records timing + tokens."""
        self._llm_seq += 1
        seq = self._llm_seq
        started = _now()
        t0 = time.perf_counter()

        entry: Dict[str, Any] = {
            "seq": seq,
            "model": model,
            "started_at": started.isoformat(),
            "duration_ms": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "status": "ok",
            "error": None,
        }
        with record_llm_call(model, session_id=self.session_id) as span:
            try:
                yield span
                entry["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
                # Caller may have set token counts on the span
                attrs = getattr(span, "attributes", None) or {}
                entry["input_tokens"] = int(attrs.get("llm.input_tokens", 0))
                entry["output_tokens"] = int(attrs.get("llm.output_tokens", 0))
            except Exception as exc:
                entry["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
                entry["status"] = "error"
                entry["error"] = str(exc)[:300]
                raise
            finally:
                self._llm_calls.append(entry)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Return the session report as a plain dict (JSON-serialisable)."""
        started = self._started_at or _now()
        ended = self._ended_at or _now()
        duration_ms = round((ended - started).total_seconds() * 1000, 2)

        total_input = sum(c.get("input_tokens", 0) for c in self._llm_calls)
        total_output = sum(c.get("output_tokens", 0) for c in self._llm_calls)

        slowest: Optional[Dict[str, Any]] = None
        if self._tool_calls:
            s = max(self._tool_calls, key=lambda c: c["duration_ms"])
            slowest = {"tool_name": s["tool_name"], "duration_ms": s["duration_ms"]}

        return {
            "schema_version": self.SCHEMA_VERSION,
            "session_id": self.session_id,
            "started_at": started.isoformat(),
            "ended_at": ended.isoformat(),
            "duration_ms": duration_ms,
            "status": self._status,
            "tool_calls": self._tool_calls,
            "llm_calls": self._llm_calls,
            "summary": {
                "total_tool_calls": len(self._tool_calls),
                "total_llm_calls": len(self._llm_calls),
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
                "slowest_tool": slowest,
            },
        }

    def save(self, report_dir: Optional[str] = None) -> Optional[str]:
        """Save report JSON to *report_dir* (default: SESSION_REPORT_DIR env var or /tmp/beacon_sessions).

        Returns the path written, or None on error.
        """
        directory = report_dir or os.getenv("SESSION_REPORT_DIR", "/tmp/beacon_sessions")
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            path = Path(directory) / f"{self.session_id}.json"
            path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))
            logger.info("[telemetry] Session report saved: %s", path)
            return str(path)
        except Exception as exc:
            logger.warning("[telemetry] Could not save session report: %s", exc)
            return None

    def log_summary(self) -> None:
        """Emit a one-line INFO summary of the session."""
        d = self.to_dict()
        sm = d["summary"]
        logger.info(
            "[telemetry] session=%s duration=%.0fms tools=%d llm_calls=%d "
            "input_tokens=%d output_tokens=%d status=%s",
            self.session_id,
            d["duration_ms"],
            sm["total_tool_calls"],
            sm["total_llm_calls"],
            sm["total_input_tokens"],
            sm["total_output_tokens"],
            d["status"],
        )
