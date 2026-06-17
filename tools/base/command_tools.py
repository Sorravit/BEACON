import asyncio
import logging
import os
import subprocess

# ── gRPC fork-safety ─────────────────────────────────────────────────────────
# subprocess.run(shell=True) calls os.fork() internally.  If gRPC background
# threads are already running (Weaviate async client, OTLP exporter, etc.) at
# the time of the fork, gRPC logs:
#   "Other threads are currently calling into gRPC, skipping fork() handlers"
# This is harmless noise BUT it means gRPC's post-fork cleanup is skipped,
# which can cause stale file-descriptors / broken channels in the child.
#
# Fix: use asyncio.create_subprocess_shell instead of subprocess.run(shell=True)
# asyncio.create_subprocess_shell uses posix_spawn / vfork internally which
# does NOT trigger gRPC fork handlers at all — warning gone, no fd leaks.
# ─────────────────────────────────────────────────────────────────────────────

from opentelemetry import trace

logger = logging.getLogger(__name__)


class CommandToolsMixin:

    @staticmethod
    def _looks_like_self_kill(command: str) -> bool:
        """Block obvious commands that can kill this server process itself."""
        cmd = (command or "").lower()
        if not cmd:
            return False
        if ":8000" in cmd and "kill" in cmd:
            return True
        kill_tokens = ("kill -9", "killall", "pkill")
        app_tokens = ("web_app.py", "uvicorn", "python web_app.py")
        if any(k in cmd for k in kill_tokens) and any(a in cmd for a in app_tokens):
            return True
        return False

    async def _execute_command(self, command: str):
        span = trace.get_current_span()
        try:
            span.set_attribute("tool.command", command[:2000])
        except Exception:
            pass

        if os.getenv("ALLOW_SELF_KILL_COMMANDS", "0") != "1" and self._looks_like_self_kill(command):
            msg = (
                "Blocked potentially self-terminating command. "
                "This command appears to kill the running app process "
                "(for example by killing PID(s) on port 8000)."
            )
            try:
                span.set_attribute("tool.blocked", True)
                span.set_attribute("tool.block_reason", "self_kill_guard")
            except Exception:
                pass
            logger.warning("Command blocked by self-kill guard: %s", command)
            return f"Error: {msg}"

        try:
            # ✅ FIX: asyncio.create_subprocess_shell uses posix_spawn — no os.fork(),
            # no gRPC "skipping fork() handlers" warning.
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                try:
                    span.set_attribute("tool.exit_code", -1)
                    span.set_attribute("tool.error", "TimeoutExpired(30s)")
                except Exception:
                    pass
                return "Error: Command timed out after 30s"

            output = (stdout_bytes or b"").decode("utf-8", errors="replace") or \
                     (stderr_bytes or b"").decode("utf-8", errors="replace")
            try:
                span.set_attribute("tool.exit_code", proc.returncode)
                span.set_attribute("tool.output_bytes", len(output.encode("utf-8", errors="replace")))
            except Exception:
                pass
            return f"Output:\n{output}"

        except Exception as exc:
            try:
                span.set_attribute("tool.error", str(exc)[:500])
            except Exception:
                pass
            return f"Error: {exc}"

    async def _execute_long_command(self, command: str):
        """Execute long-running commands with configurable timeout."""
        span = trace.get_current_span()
        try:
            span.set_attribute("tool.command", command[:2000])
        except Exception:
            pass

        if os.getenv("ALLOW_SELF_KILL_COMMANDS", "0") != "1" and self._looks_like_self_kill(command):
            msg = (
                "Blocked potentially self-terminating long command. "
                "This command appears to kill the running app process."
            )
            try:
                span.set_attribute("tool.blocked", True)
                span.set_attribute("tool.block_reason", "self_kill_guard")
            except Exception:
                pass
            logger.warning("Long command blocked by self-kill guard: %s", command)
            return f"Error: {msg}"

        try:
            timeout_val = int(os.getenv("LONG_COMMAND_TIMEOUT", "7200"))
            effective_timeout = timeout_val if timeout_val > 0 else None

            # ✅ FIX: asyncio.create_subprocess_shell — no os.fork(), no gRPC warning.
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=effective_timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                try:
                    span.set_attribute("tool.exit_code", -1)
                    span.set_attribute("tool.error", f"TimeoutExpired({timeout_val}s / {timeout_val // 60}min)")
                except Exception:
                    pass
                return (
                    f"Error: Long command timed out after {timeout_val}s ({timeout_val // 60} minutes). "
                    "Set LONG_COMMAND_TIMEOUT env var to increase "
                    "(e.g. LONG_COMMAND_TIMEOUT=14400 for 4 hours), or set "
                    "LONG_COMMAND_TIMEOUT=0 to disable timeout."
                )

            output = (stdout_bytes or b"").decode("utf-8", errors="replace") or \
                     (stderr_bytes or b"").decode("utf-8", errors="replace")
            try:
                span.set_attribute("tool.exit_code", proc.returncode)
                span.set_attribute("tool.output_bytes", len(output.encode("utf-8", errors="replace")))
            except Exception:
                pass
            return f"Output:\n{output}"

        except Exception as exc:
            try:
                span.set_attribute("tool.error", str(exc)[:500])
            except Exception:
                pass
            return f"Error: {exc}"
