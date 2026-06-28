#!/usr/bin/env python3
# ── gRPC fork-safety (must be set BEFORE any grpc/otlp import) ─────────────
# Must run before web.app imports any grpc/otlp module. setdefault is
# idempotent, so re-applying it here (in addition to web/app.py) is safe and
# guarantees correct ordering when launched as `python web_app.py`.
import os as _grpc_os
_grpc_os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "1")
_grpc_os.environ.setdefault("GRPC_POLL_STRATEGY", "poll")
_grpc_os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
_grpc_os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
# ────────────────────────────────────────────────────────────────────────────
"""
AI Assistant - Web Application (entry point).

The implementation lives in the ``web`` package:
  - web/app.py        FastAPI instance, lifespan, middleware, router wiring
  - web/state.py      shared module-level state
  - web/routers/*     APIRouter modules (sessions, chat, events, tasks, agent)
  - web/helpers.py    session persistence + utility helpers
  - web/auth.py       token gate + login page + middleware
  - web/telemetry.py  telemetry import shim

This module is kept so `uvicorn web_app:app` and `from web_app import app`
continue to work unchanged.
"""

from web.app import app  # noqa: F401  (re-exported for `web_app:app`)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "web_app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
        # Per-request access logging (GET /events, /tasks, /sessions polling)
        # floods the log and buries real signal. Chat-turn lifecycle logs in
        # _run_agent_bg provide the meaningful per-request information instead.
        access_log=False,
        # Don't let long-lived SSE streams (/events, /chat/stream) stall Ctrl+C —
        # force connections closed a few seconds after shutdown begins.
        timeout_graceful_shutdown=3,
    )
