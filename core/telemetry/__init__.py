"""Telemetry package for the BEACON AI agent.

Public API (imported across the app and the test-suite):

    Pipeline setup / lifecycle
        init_tracer, setup_telemetry, install_print_bridge, get_tracer, shutdown

    Session root span
        start_session_span, end_session_span, get_session_span,
        get_session_context, session_span_context

    Per-call spans + JSON report
        record_tool_call, record_llm_call, record_session,
        set_tool_result, SessionReporter

    Async/thread-safe session context (ContextVars)
        set_session_context, clear_session_context, get_session_id, get_reporter
"""

# -- Tracer pipeline + session root span ----------------------------------------
from .tracer import (
    init_tracer,
    setup_telemetry,
    install_print_bridge,
    get_tracer,
    shutdown,
    start_session_span,
    end_session_span,
    get_session_span,
    get_session_context,
    session_span_context,
)

# -- Per-call span helpers ------------------------------------------------------
from .metrics import (
    record_tool_call,
    record_llm_call,
    record_session,
    set_tool_result,
)

# -- Session reporter (per-session JSON summary) --------------------------------
from .session_reporter import SessionReporter

# -- ContextVars carrying the active session id + reporter ----------------------
from .context import (
    set_session_context,
    clear_session_context,
    get_session_id,
    get_reporter,
)

__all__ = [
    # tracer pipeline
    "init_tracer",
    "setup_telemetry",
    "install_print_bridge",
    "get_tracer",
    "shutdown",
    # session root span
    "start_session_span",
    "end_session_span",
    "get_session_span",
    "get_session_context",
    "session_span_context",
    # per-call spans
    "record_tool_call",
    "record_llm_call",
    "record_session",
    "set_tool_result",
    "SessionReporter",
    # session context vars
    "set_session_context",
    "clear_session_context",
    "get_session_id",
    "get_reporter",
]