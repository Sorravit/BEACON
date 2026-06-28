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
core/agent/runtime.py — shared foundation for the AIAgent package.

Holds the module-level constants, logging setup, telemetry shims and external
collaborator imports that the AIAgent mixins and the CLI entry point all share.
Centralising them here keeps the mixin modules free of duplicated boilerplate
while preserving the exact behaviour of the original monolithic ``main.py``.
"""

import asyncio
import json
import logging
import logging.handlers
import os
import re
import sys
import time
import warnings
from contextlib import nullcontext
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

# Suppress noisy deprecation warnings from third-party libraries
warnings.filterwarnings("ignore", category=DeprecationWarning, module="authlib")
warnings.filterwarnings("ignore", message=".*authlib.jose.*")
_rt_warning_rule = "ignore:resource_tracker:UserWarning"
_py_warnings = os.environ.get("PYTHONWARNINGS", "")
if _rt_warning_rule not in [w.strip() for w in _py_warnings.split(",") if w.strip()]:
    os.environ["PYTHONWARNINGS"] = (
        f"{_py_warnings},{_rt_warning_rule}" if _py_warnings else _rt_warning_rule
    )
warnings.filterwarnings(
    "ignore",
    message=r"resource_tracker: There appear to be \d+ leaked semaphore objects to clean up at shutdown:.*",
    category=UserWarning,
    module=r"multiprocessing\.resource_tracker",
)

from openai import AsyncOpenAI, OpenAI
from core.mcp_client import MCPManager
from core.models import ModelRegistry
from core.skills import SkillManager
from core.vector_memory import VectorMemory
from tools.manager import ToolManager

# ── Telemetry ─────────────────────────────────────────────────────────────────
try:
    from core.telemetry import record_llm_call, setup_telemetry, install_print_bridge
    from core.telemetry.context import get_session_id, get_reporter

    _TELEMETRY_AVAILABLE = True
except ImportError:
    _TELEMETRY_AVAILABLE = False


    def get_session_id():
        return None  # type: ignore


    def get_reporter():
        return None  # type: ignore


    def setup_telemetry(*a, **kw):
        pass  # type: ignore


    def install_print_bridge(*a, **kw):
        pass  # type: ignore


    def record_llm_call(*a, **kw):  # type: ignore
        # Never invoked when telemetry is unavailable (all call sites guard on
        # _TELEMETRY_AVAILABLE); defined only so unconditional imports resolve.
        return nullcontext()

# ============================================================================
# CONFIGURATION CONSTANTS - Modify these to customize behavior
# ============================================================================

# Version
VERSION = "4.2.0"

# Logging
os.makedirs("logs", exist_ok=True)
LOG_FILE = "logs/ai_assistant.log"

# AI Model Configuration
DEFAULT_MODEL = "gpt-3.5-turbo"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 64000
DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Tool Execution Limits
# MAX_TOOL_ITERATIONS: Maximum number of tool calls per user message
# - 10_000_000 = effectively unlimited (token is free — run as long as needed)
# - Previous value was 1000 (~8-10 hours). Raised per user request.
# - This is a safety backstop only; agent exits normally via the 'no tool calls' branch.
MAX_TOOL_ITERATIONS = 10_000_000  # effectively unlimited — run as long as needed (token is free)

# Conversation Management
# MAX_CONVERSATION_TOKENS: Maximum tokens to keep in conversation history
# - Prevents "prompt too long" errors (200k token limit)
# - Dynamically trims based on actual token count, not message count
# - Keeps as many recent messages as possible within token limit
# - System message is always kept
# tiktoken gives accurate counts now — raised from 80k. Actual Claude limit is 200k.
# and the system message (with tools XML) + memory context add thousands of untracked tokens.
MAX_CONVERSATION_TOKENS = 80000
# Max characters for the memory context block injected per user message
MAX_MEMORY_CONTEXT_CHARS = 3000

# MCP Configuration
MCP_CONFIG_FILE = "mcp_config.json"

# ============================================================================

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,
            encoding="utf-8",
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ── Module-level cached tiktoken encoder (Phase 1 / #7) ──────────────────────
@lru_cache(maxsize=1)
def _get_encoder():
    """Return the cached cl100k_base tiktoken encoder (loaded once per process)."""
    import tiktoken
    return tiktoken.get_encoding("cl100k_base")


# ── LLM concurrency ──────────────────────────────────────────────────────────
# The previous global semaphore (LLM_MAX_CONCURRENCY) has been removed: this app
# serves a single local user, the per-session background-task guard already
# serialises a session's own turns, and the artificial cap added no protection
# that the per-call wall-clock timeout does not already provide. Every LLM call
# MUST still be bounded by a timeout (see get_response) so a stalled request can
# never hang forever.
#
# nullcontext keeps the existing ``async with _get_llm_sem():`` call sites
# working without re-indenting, while imposing no concurrency limit.

def _get_llm_sem():
    """No-op async context manager (LLM concurrency is unbounded)."""
    return nullcontext()
