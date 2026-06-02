"""core/telemetry/metrics.py -- OTel span helpers for tool/LLM/session calls.

record_llm_call auto-delegates to active SessionReporter via ContextVar.
Recursion guard: reporter cleared from ContextVar before delegating to
reporter.track_llm() so the nested call hits the plain OTel path.
"""
from __future__ import annotations
import logging, time
from contextlib import contextmanager
from typing import Generator, Optional
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from .tracer import get_tracer
logger = logging.getLogger('beacon.telemetry.metrics')


@contextmanager
def record_tool_call(
    tool_name: str,
    *,
    session_id: Optional[str] = None,
) -> Generator[Span, None, None]:
    """OTel child span for one tool call."""
    tracer = get_tracer('beacon.tools')
    attrs: dict = {'tool.name': tool_name}
    if session_id:
        attrs['session.id'] = session_id
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


@contextmanager
def record_llm_call(
    model: str,
    *,
    session_id: Optional[str] = None,
) -> Generator[Span, None, None]:
    """OTel span for one LLM call. Delegates to SessionReporter if active.

    Recursion guard: clears reporter ContextVar before calling
    reporter.track_llm() so the nested record_llm_call uses plain OTel.
    """
    _active_reporter = None
    _cv_ref = None
    try:
        from core.telemetry.context import _cv_reporter as _cv_ref
        _active_reporter = _cv_ref.get()
    except Exception:
        pass
    t0 = time.perf_counter()
    if _active_reporter is not None:
        _guard_tok = None
        try:
            _guard_tok = _cv_ref.set(None)  # break recursion
        except Exception:
            pass
        _rep_cm = _active_reporter.track_llm(model)
        rep_span = _rep_cm.__enter__()
        try:
            yield rep_span
            _rep_cm.__exit__(None, None, None)
        except Exception as exc:
            _rep_cm.__exit__(type(exc), exc, exc.__traceback__)
            raise
        finally:
            if _guard_tok is not None:
                try:
                    _cv_ref.reset(_guard_tok)
                except Exception:
                    pass
    else:
        tracer = get_tracer('beacon.llm')
        attrs: dict = {'llm.model': model}
        if session_id:
            attrs['session.id'] = session_id
        with tracer.start_as_current_span(
            'llm/call', attributes=attrs, kind=trace.SpanKind.CLIENT
        ) as span:
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
