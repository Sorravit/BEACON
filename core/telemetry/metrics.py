"""core/telemetry/metrics.py -- OTel span helpers for tool / LLM / session calls.

These context managers are the canonical way to wrap a single tool or LLM call
in a span. They record latency and (for tools) the call parameters + a result
preview, so a trace answers "which tool ran, with what arguments, how long did
it take, and did it succeed?".

``record_llm_call`` auto-delegates to the active SessionReporter (via a
ContextVar) so the same call is also captured in the per-session JSON report. A
recursion guard clears the reporter from the ContextVar before delegating, so
the nested call falls through to the plain OTel path.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Generator, Optional

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from .tracer import get_tracer

logger = logging.getLogger('beacon.telemetry.metrics')

# Maximum characters kept when serialising tool parameters / results onto spans.
_MAX_PARAM_CHARS = 1024
_MAX_RESULT_CHARS = 512


def _preview(value: Any, limit: int) -> str:
    """Render *value* as a compact string, truncated to *limit* characters."""
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value)
    except Exception:
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def set_tool_result(span: Span, result: Any) -> None:
    """Attach a truncated preview of a tool result to its span."""
    try:
        span.set_attribute("tool.result_preview", _preview(result, _MAX_RESULT_CHARS))
    except Exception:
        pass


@contextmanager
def record_tool_call(
    tool_name: str,
    *,
    session_id: Optional[str] = None,
    parameters: Optional[dict] = None,
) -> Generator[Span, None, None]:
    """OTel child span for one tool call.

    Records ``tool.name``, ``session.id``, the call ``tool.parameters`` and the
    measured ``tool.duration_ms``. On exception, records the error and sets the
    span status to ERROR.
    """
    tracer = get_tracer('beacon.tools')
    attrs: dict = {'tool.name': tool_name}
    if session_id:
        attrs['session.id'] = session_id
    if parameters:
        attrs['tool.parameters'] = _preview(parameters, _MAX_PARAM_CHARS)

    t0 = time.perf_counter()
    with tracer.start_as_current_span(
        f'tool/{tool_name}', attributes=attrs, kind=trace.SpanKind.INTERNAL
    ) as span:
        try:
            yield span
            span.set_attribute('tool.duration_ms', round((time.perf_counter() - t0) * 1000, 2))
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.set_attribute('tool.duration_ms', round((time.perf_counter() - t0) * 1000, 2))
            span.set_attribute('error.type', type(exc).__name__)
            span.set_attribute('error.message', str(exc)[:500])
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, description=str(exc)))
            raise


def _apply_llm_request_attrs(
    span: Span,
    model: str,
    session_id: Optional[str],
    temperature: Optional[float],
    max_tokens: Optional[int],
    message_count: Optional[int],
    streaming: Optional[bool],
) -> None:
    """Set the request-side attributes shared by both LLM span code paths."""
    try:
        span.set_attribute('llm.model', model)
        if session_id:
            span.set_attribute('session.id', session_id)
        if temperature is not None:
            span.set_attribute('llm.temperature', float(temperature))
        if max_tokens is not None:
            span.set_attribute('llm.max_tokens', int(max_tokens))
        if message_count is not None:
            span.set_attribute('llm.request.messages', int(message_count))
        if streaming is not None:
            span.set_attribute('llm.streaming', bool(streaming))
    except Exception:
        pass


@contextmanager
def record_llm_call(
    model: str,
    *,
    session_id: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    message_count: Optional[int] = None,
    streaming: Optional[bool] = None,
) -> Generator[Span, None, None]:
    """OTel span for one LLM call (model, request params, latency, tokens).

    Token counts are not known up front: the caller sets ``llm.input_tokens`` /
    ``llm.output_tokens`` on the yielded span once the response arrives.

    Delegates to the active SessionReporter when present. A recursion guard
    clears the reporter ContextVar before calling ``reporter.track_llm`` so the
    nested ``record_llm_call`` uses the plain OTel path.
    """
    active_reporter = None
    cv_ref = None
    try:
        from core.telemetry.context import _cv_reporter as cv_ref
        active_reporter = cv_ref.get()
    except Exception:
        pass

    t0 = time.perf_counter()

    if active_reporter is not None:
        guard_token = None
        try:
            guard_token = cv_ref.set(None)  # break recursion
        except Exception:
            pass
        reporter_cm = active_reporter.track_llm(model)
        span = reporter_cm.__enter__()
        _apply_llm_request_attrs(
            span, model, session_id, temperature, max_tokens, message_count, streaming
        )
        try:
            yield span
            reporter_cm.__exit__(None, None, None)
        except Exception as exc:
            reporter_cm.__exit__(type(exc), exc, exc.__traceback__)
            raise
        finally:
            if guard_token is not None:
                try:
                    cv_ref.reset(guard_token)
                except Exception:
                    pass
        return

    tracer = get_tracer('beacon.llm')
    with tracer.start_as_current_span(
        'llm/call', kind=trace.SpanKind.CLIENT
    ) as span:
        _apply_llm_request_attrs(
            span, model, session_id, temperature, max_tokens, message_count, streaming
        )
        try:
            yield span
            span.set_attribute('llm.duration_ms', round((time.perf_counter() - t0) * 1000, 2))
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.set_attribute('llm.duration_ms', round((time.perf_counter() - t0) * 1000, 2))
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, description=str(exc)))
            raise


def record_session(
    session_id: str,
    duration_ms: float,
    tool_call_count: int,
    *,
    status: str = 'ok',
) -> None:
    """Record aggregate session metrics as a log event."""
    logger.info(
        '[telemetry] session %s ended: duration=%.0fms tool_calls=%d status=%s',
        session_id, duration_ms, tool_call_count, status,
    )
