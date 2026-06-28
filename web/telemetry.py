"""Telemetry import shim for the web app.

Imports all telemetry symbols used by the web layer from ``core.telemetry``.
Falls back to no-op stubs when telemetry is unavailable so the rest of the
web package can import these names unconditionally.

IMPORTANT: ``.env`` must be loaded (so OTEL_EXPORTER_OTLP_ENDPOINT is set)
BEFORE this module is imported. ``web/app.py`` enforces that ordering.
"""

try:
    from core.telemetry import init_tracer as _init_tracer, shutdown as _shutdown_tracer, get_tracer
    from core.telemetry import session_span_context, record_llm_call
    from core.telemetry import SessionReporter
    from core.telemetry.context import set_session_context, clear_session_context
    from core.telemetry.tracer import install_print_bridge
    _TELEMETRY_AVAILABLE = True
except ImportError:
    def _init_tracer(): pass
    def _shutdown_tracer(): pass
    def get_tracer(*args, **kwargs): return None
    def install_print_bridge(): pass

    import contextlib as _cl

    def session_span_context(*args, **kwargs):
        return _cl.nullcontext()

    def record_llm_call(*args, **kwargs):
        return _cl.nullcontext()

    class SessionReporter:  # type: ignore
        def __init__(self, *args, **kwargs): pass
        def mark_ended(self): pass
        def log_summary(self): pass
        def save(self): pass
        def add_tool_call(self, *args, **kwargs): pass

    def set_session_context(*args, **kwargs):
        return None

    def clear_session_context(*args, **kwargs):
        pass

    _TELEMETRY_AVAILABLE = False
