#!/usr/bin/env python3
# ── gRPC fork-safety (must be set BEFORE any grpc/otlp import) ─────────────
# Crash: EXC_BREAKPOINT / BUG IN CLIENT OF LIBDISPATCH: trying to lock
# recursively.  Root cause: subprocess.fork() called after grpc spawned
# background threads that hold libdispatch / XPC dispatch_once locks.
# Fix: tell grpc to support fork and avoid the broken kqueue poll strategy.
import os as _grpc_os
_grpc_os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "1")
_grpc_os.environ.setdefault("GRPC_POLL_STRATEGY", "poll")
_grpc_os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
_grpc_os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
# ────────────────────────────────────────────────────────────────────────────
"""
AI Assistant - Web Application (app factory)
Serves a chat UI and exposes the AIAgent via FastAPI + SSE streaming.

Multi-session support: each session has its own AIAgent conversation history,
persisted to sessions/<id>.json so history survives server restarts.

Background-task architecture: the agent task is decoupled from the HTTP
connection lifetime.  When a client disconnects (refresh/close), the agent
keeps running.  On reconnect, the client replays the buffered event log and
then tails live events.
"""

import asyncio
import logging
import os
import sys
import warnings
from contextlib import asynccontextmanager
from pathlib import Path

import atexit
import signal as _signal
import sys as _sys
_sys.stdout.reconfigure(line_buffering=True)

_rt_warning_rule = "ignore:resource_tracker:UserWarning"
_py_warnings = os.environ.get("PYTHONWARNINGS", "")
if _rt_warning_rule not in [w.strip() for w in _py_warnings.split(",") if w.strip()]:
    os.environ["PYTHONWARNINGS"] = (
        f"{_py_warnings},{_rt_warning_rule}" if _py_warnings else _rt_warning_rule
    )

# Python 3.14 + native deps can emit a spurious resource_tracker semaphore
# warning on clean SIGTERM shutdown even after explicit executor teardown.
warnings.filterwarnings(
    "ignore",
    message=r"resource_tracker: There appear to be \d+ leaked semaphore objects to clean up at shutdown:.*",
    category=UserWarning,
    module=r"multiprocessing\.resource_tracker",
)

# ── Fix #1: Ignore SIGHUP so the server survives terminal disconnects ─────────
# When Fish shell / SSH closes the controlling terminal it sends SIGHUP to the
# process group. Python's default disposition is SIG_DFL (terminate).
# Ignoring it lets the server keep running — identical to nohup behaviour.
_signal.signal(_signal.SIGHUP, _signal.SIG_IGN)


# ── Fix #3: Belt-and-suspenders loky cleanup via atexit ───────────────────────
# sentence-transformers → joblib → loky creates a reusable process pool backed
# by a POSIX semaphore. If the loky executor is not explicitly shut down before
# the interpreter exits, Python's resource_tracker reports a leaked semaphore.
# This atexit handler guarantees cleanup even on abnormal exits (SIGTERM, etc.)
def _cleanup_loky():
    try:
        from joblib.externals.loky import get_reusable_executor
        executor = get_reusable_executor()
        try:
            # Newer loky versions accept kill_workers and release resources eagerly.
            executor.shutdown(wait=True, kill_workers=True)
        except TypeError:
            executor.shutdown(wait=True)
    except Exception:
        pass

    # Stop loky's own resource tracker process once all workers are down.
    try:
        from joblib.externals.loky.backend import resource_tracker as _loky_rt
        tracker = getattr(_loky_rt, "_resource_tracker", None)
        if tracker is not None:
            tracker._stop()
    except Exception:
        pass

atexit.register(_cleanup_loky)

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from main import AIAgent, Config

# Load .env BEFORE telemetry import so OTEL_EXPORTER_OTLP_ENDPOINT is set
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(dotenv_path='/Users/sorravit/sandbox/beacon/.env', override=True)

from web.telemetry import (
    _TELEMETRY_AVAILABLE,
    _init_tracer,
    _shutdown_tracer,
    get_tracer,
    install_print_bridge,
)

# Bootstrap OTel immediately after .env is loaded
if _TELEMETRY_AVAILABLE:
    _init_tracer()
    install_print_bridge()

# Tool-call spans use this tracer when telemetry is enabled.
tracer = get_tracer("beacon.web") if _TELEMETRY_AVAILABLE else None

from web import state
from web.auth import auth_middleware
from web.helpers import (
    _load_all_sessions,
    _rotate_bg_logs,
    _rotate_log_if_needed,
    _save_session,
)
from web.routers.events import _events_producer
from web.routers import (
    agent_tasks as agent_tasks_router,
    chat as chat_router,
    events as events_router,
    sessions as sessions_router,
    system as system_router,
    tasks as tasks_router,
)

logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    state._config = Config()
    if not state._config.validate():
        logger.error("API key not configured - set OPENAI_API_KEY in .env")
        sys.exit(1)

    # FIX: initialize MCPManager ONCE here — main.py duplicated this
    state._shared_agent = AIAgent(state._config)
    ok = await state._shared_agent.initialize()
    if not ok:
        logger.error("Failed to initialize AI agent")
        sys.exit(1)
    logger.info("AI Agent ready")
    _load_all_sessions()
    logger.info(f"Loaded {len(state._sessions)} session(s) from disk")

    # Truncate any oversized background task log files on startup
    os.makedirs("logs", exist_ok=True)
    for lf in Path("logs").glob("bg_*.log"):
        _rotate_log_if_needed(lf)
    _rotate_bg_logs()

    # Phase 4 / #5: start the single global events producer
    state._events_producer_task = asyncio.create_task(
        _events_producer(), name="events-producer"
    )

    # OTel tracer already initialised at module load (top-level).
    logger.info("OTel tracer active ✓  (lifespan startup complete)")

    yield  # App runs here

    # ── Shutdown ──────────────────────────────────────────────────────────────
    # Stop the events producer
    if state._events_producer_task and not state._events_producer_task.done():
        state._events_producer_task.cancel()
        try:
            await asyncio.wait_for(state._events_producer_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Flush any pending debounced session saves
    for sid in list(state._save_pending.keys()):
        h = state._save_pending.pop(sid, None)
        if h:
            h.cancel()
        _save_session(sid)  # sync flush on shutdown

    # Pre-clean loky in case later shutdown steps are interrupted.
    _cleanup_loky()

    if state._shared_agent and hasattr(state._shared_agent, "shutdown"):
        await state._shared_agent.shutdown()
    elif state._shared_agent:
        if state._shared_agent.tools:
            await state._shared_agent.tools.cleanup()
        if state._shared_agent.vector_memory:
            state._shared_agent.vector_memory.close()

    # FIX: Flush all pending OTel spans before process exits
    _shutdown_tracer()

    # Ensure loky pool is torn down before interpreter atexit handlers run.
    _cleanup_loky()


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="Big's Personal AI Assistant", version="4.5.0", lifespan=lifespan)

# Instrument FastAPI for automatic HTTP span creation
if _TELEMETRY_AVAILABLE:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        # Exclude long-lived SSE / polling endpoints: their HTTP spans are
        # open for the entire stream and add noise without useful detail. The
        # meaningful work is captured by the per-turn `session` trace instead.
        FastAPIInstrumentor().instrument_app(
            app,
            excluded_urls="events,chat/reconnect,chat/status,tasks/stream,tasks/notifications,health",
        )
        import logging as _l; _l.getLogger(__name__).info("FastAPIInstrumentor active")
    except Exception as _e:
        import logging as _l; _l.getLogger(__name__).warning("FastAPIInstrumentor failed: %s", _e)

static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Auth middleware ───────────────────────────────────────────────────────────
app.middleware("http")(auth_middleware)

# ── Routers ───────────────────────────────────────────────────────────────────
# Registration order preserves original route-matching precedence.
app.include_router(system_router.router)
app.include_router(sessions_router.router)
app.include_router(chat_router.router)
app.include_router(events_router.router)
app.include_router(tasks_router.router)
app.include_router(agent_tasks_router.router)
