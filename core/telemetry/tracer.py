
"""
BEACON Telemetry — Distributed Tracing Module
==============================================
Provides a production-ready, configurable OpenTelemetry tracer for the BEACON agent.
Exports spans to Grafana Tempo via OTLP/gRPC (primary) and OTLP/HTTP (fallback).

Configuration is driven entirely by environment variables (12-factor app pattern):
  OTEL_SERVICE_NAME      — service name tag on every span (default: "beacon")
  OTEL_EXPORTER_ENDPOINT — OTLP/gRPC endpoint (default: "localhost:4317")
  OTEL_HTTP_ENDPOINT     — OTLP/HTTP fallback endpoint (default: "http://localhost:4318/v1/traces")
  OTEL_ENABLED           — set "false" to disable tracing entirely (default: "true")
"""

import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Iterator, Optional, Tuple

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as GrpcExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as HttpExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level session registry  (session_id -> (Span, Context))
# Thread-safe: protected by _session_spans_lock
# ---------------------------------------------------------------------------
_session_spans: Dict[str, Tuple[Span, Context]] = {}
_session_spans_lock: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal bootstrap helpers
# ---------------------------------------------------------------------------

def _build_resource(service_name: str) -> Resource:
    return Resource.create({SERVICE_NAME: service_name})


def _build_grpc_exporter(endpoint: str) -> GrpcExporter:
    return GrpcExporter(endpoint=endpoint, insecure=True)


def _build_http_exporter(http_endpoint: str) -> HttpExporter:
    return HttpExporter(endpoint=http_endpoint)


def _build_provider(
    resource: Resource,
    grpc_exporter: GrpcExporter,
    http_exporter: Optional[HttpExporter] = None,
    console: bool = False,
) -> TracerProvider:
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(grpc_exporter))
    if http_exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(http_exporter))
    if console:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    return provider


# ---------------------------------------------------------------------------
# Public API — provider lifecycle
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# OTel Logs SDK bootstrap  (logs -> otel-collector -> Loki)
# ---------------------------------------------------------------------------

_log_provider = None  # opentelemetry.sdk._logs.LoggerProvider


def _init_otel_logging(resource, endpoint: str) -> None:
    """Wire up the OTel Logs SDK so every Python log record is exported to Loki.

    Uses the same gRPC endpoint as traces so a single otel-collector pipeline
    receives both.  Each log record automatically carries trace_id + span_id
    so Grafana can correlate Log <-> Trace in both directions.
    """
    global _log_provider
    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.instrumentation.logging import LoggingInstrumentation

        _log_provider = LoggerProvider(resource=resource)
        _log_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=endpoint, insecure=True)
            )
        )
        set_logger_provider(_log_provider)

        # Bridge stdlib logging -> OTel LogRecords.
        # set_logging_format=True also injects traceID into console/file log lines.
        LoggingInstrumentation().instrument(set_logging_format=True)

        logger.info(
            "OTel Logs SDK initialised - Python logs -> Loki via gRPC %s", endpoint
        )
    except ImportError as exc:
        logger.warning(
            "OTel Logs SDK not available "
            "(pip install opentelemetry-instrumentation-logging): %s", exc
        )
    except Exception as exc:
        logger.warning("OTel Logs initialisation failed (non-fatal): %s", exc)


def _shutdown_otel_logging() -> None:
    """Flush all pending log records before process exit."""
    global _log_provider
    if _log_provider is not None:
        try:
            _log_provider.shutdown()
        except Exception:
            pass
        _log_provider = None

_provider: Optional[TracerProvider] = None


def init_tracer(
    service_name: Optional[str] = None,
    endpoint: Optional[str] = None,
    http_endpoint: Optional[str] = None,
    enabled: Optional[bool] = None,
    console: bool = False,
) -> TracerProvider:
    """Bootstrap the global OTel TracerProvider. Idempotent."""
    global _provider
    if _provider is not None:
        return _provider

    svc  = service_name or os.getenv("OTEL_SERVICE_NAME",      "beacon")
    ep   = endpoint      or os.getenv("OTEL_EXPORTER_ENDPOINT", "localhost:4317")
    h_ep = http_endpoint or os.getenv("OTEL_HTTP_ENDPOINT",     "http://localhost:4318/v1/traces")
    en   = enabled if enabled is not None else (
        os.getenv("OTEL_ENABLED", "true").lower() != "false"
    )

    if not en:
        logger.info("Tracing disabled — using NoOp provider.")
        from opentelemetry.trace import NoOpTracerProvider
        noop = NoOpTracerProvider()
        trace.set_tracer_provider(noop)
        _provider = noop  # type: ignore[assignment]
        return _provider  # type: ignore[return-value]

    resource = _build_resource(svc)
    g_exp    = _build_grpc_exporter(ep)
    h_exp    = _build_http_exporter(h_ep)
    provider = _build_provider(resource, g_exp, h_exp, console=console)
    trace.set_tracer_provider(provider)
    _provider = provider
    logger.info("Tracer initialised — service=%s gRPC=%s HTTP=%s console=%s", svc, ep, h_ep, console)
    return provider


def get_tracer(component: str = "beacon") -> trace.Tracer:
    """Return a named Tracer, bootstrapping the provider lazily if needed."""
    if _provider is None:
        init_tracer()
    return trace.get_tracer(component)


def shutdown_tracer() -> None:
    """Flush all pending spans + log records and shut down the providers. Idempotent."""
    global _provider
    # Flush logs first so final log lines are captured before spans close
    _shutdown_otel_logging()
    if _provider is not None:
        if hasattr(_provider, "shutdown"):
            try:
                _provider.shutdown()
            except Exception:
                pass
        _provider = None
        logger.info("TracerProvider shut down — all spans flushed.")


# Alias used by core.telemetry.__init__ and tests
shutdown = shutdown_tracer


@contextmanager
def traced_operation(
    name: str,
    component: str = "beacon",
    attributes: Optional[dict] = None,
) -> Iterator[Span]:
    """Context manager for a single traced operation."""
    tracer = get_tracer(component)
    with tracer.start_as_current_span(name) as span:
        if attributes:
            for k, v in attributes.items():
                span.set_attribute(k, v)
        yield span


# ---------------------------------------------------------------------------
# Session-root span API
# ---------------------------------------------------------------------------

def start_session_span(session_id: str) -> Tuple[Span, object]:
    """Create and register a root SERVER span for *session_id*.

    Returns (span, token).
    - span  : live Span — pass to end_session_span() when done
    - token : OTel context token — detach if you need to restore prior context
    """
    tracer = get_tracer("beacon.session")
    start_time_iso = datetime.now(timezone.utc).isoformat()

    # Empty Context() = fresh trace root (no parent)
    root_ctx = otel_context.Context()

    span = tracer.start_span(
        "session",
        context=root_ctx,
        kind=SpanKind.SERVER,
        attributes={
            "session.id": session_id,
            "session.start_time": start_time_iso,
        },
    )

    # Attach span as current context so child spans on same thread are parented
    span_ctx = trace.set_span_in_context(span)
    token = otel_context.attach(span_ctx)

    with _session_spans_lock:
        _session_spans[session_id] = (span, span_ctx)

    return span, token


def end_session_span(span: Span, duration_ms: float) -> None:
    """Finalise *span*: set session.duration_ms, status=OK, end it.
    Swallows all exceptions so callers never fail due to telemetry.
    """
    try:
        span.set_attribute("session.duration_ms", duration_ms)
        span.set_status(Status(StatusCode.OK))
        span.end()
    except Exception:
        pass

    with _session_spans_lock:
        keys_to_remove = [
            sid for sid, (s, _ctx) in _session_spans.items() if s is span
        ]
        for key in keys_to_remove:
            del _session_spans[key]


def get_session_span(session_id: str) -> Optional[Span]:
    """Return the active Span for *session_id*, or None if not registered."""
    with _session_spans_lock:
        entry = _session_spans.get(session_id)
    return entry[0] if entry is not None else None


def get_session_context(session_id: str) -> Optional[Context]:
    """Return the OTel Context for *session_id* for cross-thread child spans.
    Returns None if session_id is not currently registered.
    """
    with _session_spans_lock:
        entry = _session_spans.get(session_id)
    return entry[1] if entry is not None else None


@contextmanager
def session_span_context(session_id: str) -> Iterator[Span]:
    """Context-manager wrapping start_session_span/end_session_span.
    Measures wall-clock elapsed time automatically.
    Sets ERROR status + records exception if an exception propagates.
    Cleans the registry on both normal and exceptional exits.
    """
    span, token = start_session_span(session_id)
    t0 = time.perf_counter()
    try:
        yield span
        elapsed_ms = (time.perf_counter() - t0) * 1000
        end_session_span(span, duration_ms=elapsed_ms)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        try:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, description=str(exc)))
            span.set_attribute("session.duration_ms", elapsed_ms)
            span.end()
        except Exception:
            pass
        with _session_spans_lock:
            keys_to_remove = [
                sid for sid, (s, _ctx) in _session_spans.items() if s is span
            ]
            for key in keys_to_remove:
                del _session_spans[key]
        raise
    finally:
        try:
            otel_context.detach(token)
        except Exception:
            pass
