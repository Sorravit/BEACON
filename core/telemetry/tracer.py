"""Tracer setup for BEACON telemetry.

This module owns the OpenTelemetry *pipelines* (traces + logs) and the
per-session **root span** that every agent turn hangs off.

Export target (do NOT change)
-----------------------------
Traces and logs are shipped over **OTLP/gRPC** to the KRS otel-collector at
``localhost:4317``. Keeping ``service.name="beacon"`` lets Grafana/Loki label
BEACON logs distinctly from krs-service logs (``{service_name="beacon"}``).

Trace shape produced by the agent
---------------------------------
    session                 (root span, one per chat turn)
      |- llm/call           (one per LLM request - model, tokens, latency)
      |- tool/<name>        (one per tool call - parameters, latency, result)
      |- ...

Log<->trace correlation is provided by ``LoggingInstrumentor`` which stamps
every log record with the active ``trace_id``/``span_id``.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, Optional, Tuple

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

# gRPC exporters - endpoint is the base URL only (no /v1/... suffix).
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.instrumentation.logging import LoggingInstrumentor

logger = logging.getLogger(__name__)

# Default OTLP/gRPC endpoint of the KRS otel-collector. Overridable via the
# OTEL_EXPORTER_OTLP_ENDPOINT environment variable.
DEFAULT_OTLP_ENDPOINT = "http://localhost:4317"

# Per-export gRPC timeout (seconds). Kept short so a slow/unreachable collector
# can never block a flush — critical for fast Ctrl+C shutdown.
EXPORT_TIMEOUT_SECONDS = 3

# Hard ceiling (seconds) for the whole telemetry teardown. If the exporters are
# wedged, we give up and let the process exit rather than hang on Ctrl+C.
SHUTDOWN_TIMEOUT_SECONDS = 4

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
# The active TracerProvider, or None before init / after shutdown. Tests reset
# this to force a clean re-initialisation.
_provider: Optional[TracerProvider] = None
_logger_provider: Optional[LoggerProvider] = None

# Registry of live session root spans, keyed by session_id. Each value carries
# the span, its trace Context (for cross-thread child parenting) and the
# context-detach token. Guarded by a lock for thread-safety.
_session_spans: Dict[str, Tuple[Span, otel_context.Context, object]] = {}
_session_spans_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pipeline setup
# ---------------------------------------------------------------------------

def _suppress_recursive_loggers() -> None:
    """Silence loggers that would otherwise form an OTLP-export feedback loop.

    Without this, a log emitted by the exporter (e.g. a urllib3 retry) would be
    captured by the OTel LoggingHandler and exported again, recursively.
    """
    noisy = [
        "opentelemetry", "opentelemetry.sdk", "opentelemetry.sdk.trace",
        "opentelemetry.sdk.logs", "opentelemetry.exporter",
        "opentelemetry.exporter.otlp",
        "urllib3", "urllib3.connectionpool", "urllib3.util", "urllib3.util.retry",
        "httpx", "httpcore", "httpcore.http11", "requests",
        "uvicorn.access", "asyncio",
    ]
    for name in noisy:
        log = logging.getLogger(name)
        log.setLevel(logging.WARNING)
        log.propagate = False


def _resolve_endpoint(otlp_endpoint: str) -> str:
    """Let the OTEL_EXPORTER_OTLP_ENDPOINT env var override the default."""
    return os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", otlp_endpoint)


def setup_telemetry(
    service_name: str = "beacon",
    otlp_endpoint: str = DEFAULT_OTLP_ENDPOINT,
    *,
    enable_tracing: bool = True,
    enable_logging: bool = True,
) -> Optional[TracerProvider]:
    """Initialise the trace + log pipelines once and return the TracerProvider.

    Idempotent: repeated calls return the existing provider until ``shutdown``
    is called. Returns None if tracing is disabled.
    """
    global _provider, _logger_provider
    if _provider is not None:
        return _provider

    _suppress_recursive_loggers()

    endpoint = _resolve_endpoint(otlp_endpoint)
    service = os.getenv("OTEL_SERVICE_NAME", service_name)
    resource = Resource.create({
        "service.name": service,
        "service.version": os.getenv("APP_VERSION", "0.0.0"),
    })

    # -- Tracing pipeline ----------------------------------------------------
    if enable_tracing:
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, timeout=EXPORT_TIMEOUT_SECONDS)
            )
        )
        # Register as the global provider so auto-instrumentation (FastAPI, etc.)
        # shares the same pipeline. OTel ignores this if one is already set.
        trace.set_tracer_provider(provider)
        _provider = provider

    # -- Logging pipeline (log<->trace correlation) --------------------------
    if enable_logging:
        _logger_provider = LoggerProvider(resource=resource)
        _logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=endpoint, timeout=EXPORT_TIMEOUT_SECONDS),
                schedule_delay_millis=5000,
                max_export_batch_size=512,
                max_queue_size=2048,
            )
        )
        set_logger_provider(_logger_provider)

        root_logger = logging.getLogger()
        if not any(isinstance(h, LoggingHandler) for h in root_logger.handlers):
            root_logger.addHandler(
                LoggingHandler(level=logging.INFO, logger_provider=_logger_provider)
            )

        # Stamp every log record with trace_id/span_id. Guard against re-instrument.
        instrumentor = LoggingInstrumentor()
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.instrument(set_logging_format=True)

    logger.info(
        "OpenTelemetry initialised [grpc] service=%s endpoint=%s", service, endpoint
    )
    return _provider


def init_tracer(
    service_name: str = "beacon",
    otlp_endpoint: str = DEFAULT_OTLP_ENDPOINT,
) -> Optional[TracerProvider]:
    """Backward-compatible alias for :func:`setup_telemetry`."""
    return setup_telemetry(service_name=service_name, otlp_endpoint=otlp_endpoint)


def get_tracer(name: str = "beacon"):
    """Return a tracer from the BEACON provider (or the global one as fallback)."""
    if _provider is not None:
        return _provider.get_tracer(name)
    return trace.get_tracer(name)


def shutdown() -> None:
    """Flush and tear down the trace + log pipelines without ever hanging.

    The OTLP/gRPC exporters can block while flushing to a slow or unreachable
    collector. We therefore run the (potentially blocking) provider shutdowns on
    a daemon thread and wait at most ``SHUTDOWN_TIMEOUT_SECONDS`` for them. If
    they overrun, we abandon them and return so the process can exit promptly —
    which is what makes Ctrl+C feel instant again.
    """
    global _provider, _logger_provider
    provider, logger_provider = _provider, _logger_provider
    # Drop module references up front so a re-entrant call is a no-op.
    _provider = None
    _logger_provider = None

    def _teardown() -> None:
        for target in (provider, logger_provider):
            if target is None:
                continue
            try:
                target.shutdown()
            except Exception:
                pass

    worker = threading.Thread(target=_teardown, name="otel-shutdown", daemon=True)
    worker.start()
    worker.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    if worker.is_alive():
        logger.warning(
            "[telemetry] OTLP exporter slow to flush; abandoning teardown after %ss",
            SHUTDOWN_TIMEOUT_SECONDS,
        )


# ---------------------------------------------------------------------------
# print() -> OTel log bridge
# ---------------------------------------------------------------------------

def install_print_bridge(
    service_name: str = "beacon",
    otlp_endpoint: str = DEFAULT_OTLP_ENDPOINT,
) -> None:
    """Forward ``print()`` output to the OTel logging pipeline (line-buffered)."""
    if getattr(install_print_bridge, "_installed", False):
        return

    setup_telemetry(service_name=service_name, otlp_endpoint=otlp_endpoint)
    otel_logger = logging.getLogger("beacon.stdout")

    class _PrintBridge:
        """Transparent stdout proxy that also emits whole lines as log records.

        Kept as a plain proxy (not a TextIOWrapper subclass) so methods such as
        ``isatty()`` that uvicorn calls during startup still work.
        """

        def __init__(self, original: Any) -> None:
            self._original = original
            self._buf = ""

        def write(self, text: str) -> int:
            self._original.write(text)
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    otel_logger.info(line)
            return len(text)

        def flush(self) -> None:
            self._original.flush()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._original, name)

    sys.stdout = _PrintBridge(sys.stdout)
    install_print_bridge._installed = True


# ---------------------------------------------------------------------------
# Session root span
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_session_span(session_id: str) -> Tuple[Span, object]:
    """Start the root span for one agent turn and make it the current context.

    The span is created as a true root (no parent) so each chat turn is its own
    trace. The active context is attached so any ``llm/call`` or ``tool/<name>``
    spans created afterwards become children of this session span.
    """
    tracer = get_tracer("beacon.session")
    # Force a root span by basing it on an empty context (ignores any ambient
    # span such as an in-flight HTTP request span).
    span = tracer.start_span(
        "session",
        context=otel_context.Context(),
        kind=SpanKind.SERVER,
        attributes={
            "session.id": session_id,
            "session.start_time": _iso_now(),
        },
    )
    ctx = trace.set_span_in_context(span)
    token = otel_context.attach(ctx)
    with _session_spans_lock:
        _session_spans[session_id] = (span, ctx, token)
    return span, token


def end_session_span(span: Span, duration_ms: float, status: str = "ok") -> None:
    """End a session root span, recording duration + status. Never raises."""
    try:
        span.set_attribute("session.duration_ms", float(duration_ms))
        span.set_status(Status(StatusCode.OK if status == "ok" else StatusCode.ERROR))
        span.end()
    except Exception as exc:  # telemetry must never break the caller
        logger.debug("[telemetry] end_session_span error: %s", exc)

    # Detach context + drop the registry entry for whichever session owns it.
    with _session_spans_lock:
        for sid, (registered, _ctx, token) in list(_session_spans.items()):
            if registered is span:
                try:
                    otel_context.detach(token)
                except Exception:
                    pass
                _session_spans.pop(sid, None)
                break


def get_session_span(session_id: str) -> Optional[Span]:
    """Return the live root span for *session_id*, or None if not active."""
    with _session_spans_lock:
        entry = _session_spans.get(session_id)
        return entry[0] if entry else None


def get_session_context(session_id: str) -> Optional[otel_context.Context]:
    """Return the trace Context of a live session (for cross-thread parenting)."""
    with _session_spans_lock:
        entry = _session_spans.get(session_id)
        return entry[1] if entry else None


@contextmanager
def session_span_context(session_id: str) -> Generator[Span, None, None]:
    """Context-manager wrapper around start/end_session_span.

    Times the block, records ``session.duration_ms`` and sets ERROR status if
    the block raises. Always removes the session from the registry on exit.
    """
    span, _token = start_session_span(session_id)
    t0 = time.perf_counter()
    try:
        yield span
    except Exception as exc:
        span.record_exception(exc)
        end_session_span(span, (time.perf_counter() - t0) * 1000.0, status="error")
        raise
    else:
        end_session_span(span, (time.perf_counter() - t0) * 1000.0, status="ok")

