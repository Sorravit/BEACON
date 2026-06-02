"""BEACON Telemetry Package — OpenTelemetry SDK bootstrap + helpers.

Public surface
--------------
    from core.telemetry import init_tracer, get_tracer, shutdown
    from core.telemetry import record_tool_call, record_llm_call, SessionReporter
    from core.telemetry import start_session_span, end_session_span
    from core.telemetry import get_session_span, get_session_context
    from core.telemetry import session_span_context
    from core.telemetry import set_session_context, clear_session_context
    from core.telemetry import get_session_id, get_reporter

Environment variables (all optional)
-------------------------------------
    OTEL_SERVICE_NAME          : string  (default: beacon-agent)
    OTEL_SERVICE_VERSION       : string  (default: 4.2.0)
    OTEL_EXPORTER_OTLP_ENDPOINT: http://host:port  (default: http://localhost:4317)
    BEACON_OTEL_HTTP_ENDPOINT  : http://host:port  (second exporter, optional)
    BEACON_OTEL_CONSOLE        : true|false  (default: false)
    BEACON_ENV                 : string  (default: development)
    OTEL_ENABLED               : true|false  (default: true)
"""

from .tracer import (
    init_tracer,
    get_tracer,
    shutdown,
    start_session_span,
    end_session_span,
    get_session_span,
    get_session_context,
    session_span_context,
)
from .metrics import record_tool_call, record_llm_call, record_session
from .session_reporter import SessionReporter
from .context import (
    set_session_context,
    clear_session_context,
    get_session_id,
    get_reporter,
)

__all__ = [
    # TracerProvider lifecycle
    "init_tracer",
    "get_tracer",
    "shutdown",
    # Session-root span API
    "start_session_span",
    "end_session_span",
    "get_session_span",
    "get_session_context",
    "session_span_context",
    # Metric context-managers
    "record_tool_call",
    "record_llm_call",
    "record_session",
    # Per-session structured reporter
    "SessionReporter",
    # ContextVar helpers (no explicit param threading)
    "set_session_context",
    "clear_session_context",
    "get_session_id",
    "get_reporter",
]
