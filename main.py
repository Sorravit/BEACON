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
AI Assistant - Agent with Skills
Version: 4.2.0 (MCP Integration)

A production-ready AI assistant with MCP support, browser automation, HTTP
requests, and file operations.

This module is now a thin entry point. The implementation lives in the
``core.agent`` package (split into runtime/config/base + topic mixins). The
names below are re-exported so existing imports keep working unchanged:

    from main import AIAgent, Config, ToolManager
"""

import asyncio
import sys

# ── Public API (re-exported for backward compatibility) ──────────────────────
from core.agent.runtime import (
    logger,
    setup_telemetry,
    install_print_bridge,
    _TELEMETRY_AVAILABLE,
    VERSION,
    LOG_FILE,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_BASE_URL,
    MAX_TOOL_ITERATIONS,
    MAX_CONVERSATION_TOKENS,
    MAX_MEMORY_CONTEXT_CHARS,
    MCP_CONFIG_FILE,
    ToolManager,
)
from core.agent.config import Config
from core.agent.base import AIAgent

__all__ = ["AIAgent", "Config", "ToolManager"]


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Autonomous AI Agent")
    parser.add_argument("--mode", choices=["chat", "agent"], default="chat", help="Execution mode")
    parser.add_argument("task", nargs="*", help="Task to run (only for agent mode)")

    args = parser.parse_args()
    task_str = " ".join(args.task) if args.task else None

    try:
        config = Config()
        if not config.validate():
            print("❌ API key not configured\nSet OPENAI_API_KEY in .env or environment")
            return 1

        # ── Boot telemetry BEFORE agent starts so every log/span is captured ──
        if _TELEMETRY_AVAILABLE:
            try:
                setup_telemetry(service_name="beacon")
                install_print_bridge()
                logger.info("🔭 Telemetry initialised (OTLP → localhost:14318)")
            except Exception as _tel_err:
                logger.warning("Telemetry init failed (non-fatal): %s", _tel_err)

        agent = AIAgent(config)
        if not await agent.initialize():
            print("❌ Failed to initialize")
            return 1

        await agent.run(mode=args.mode, task=task_str)
        return 0
    except Exception as e:
        logger.error(f"Fatal: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
