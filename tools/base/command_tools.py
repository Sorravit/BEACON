import asyncio
import logging
import os
import subprocess

from opentelemetry import trace

logger = logging.getLogger(__name__)


class CommandToolsMixin:

    @staticmethod
    def _looks_like_self_kill(command: str) -> bool:
        """Block obvious commands that can kill this server process itself.

        This protects against agent plans like:
        `lsof -ti :8000 | xargs kill -9`
        which will terminate the currently running web_app process.
        """
        cmd = (command or "").lower()
        if not cmd:
            return False

        # Exact failure mode seen in production: kill whatever listens on :8000.
        if ":8000" in cmd and "kill" in cmd:
            return True

        # Block common direct process-kill patterns targeting this app runtime.
        kill_tokens = ("kill -9", "killall", "pkill")
        app_tokens = ("web_app.py", "uvicorn", "python web_app.py")
        if any(k in cmd for k in kill_tokens) and any(a in cmd for a in app_tokens):
            return True

        return False

    async def _execute_command(self, command: str):
        # Enrich the active OTel span with the actual command
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
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=30
            )
            output = result.stdout or result.stderr
            try:
                span.set_attribute("tool.exit_code", result.returncode)
                span.set_attribute("tool.output_bytes", len(output.encode("utf-8", errors="replace")))
            except Exception:
                pass
            return f'Output:\n{output}'
        except subprocess.TimeoutExpired:
            try:
                span.set_attribute("tool.exit_code", -1)
                span.set_attribute("tool.error", "TimeoutExpired(30s)")
            except Exception:
                pass
            return "Error: Command timed out after 30s"
        except Exception as exc:
            try:
                span.set_attribute("tool.error", str(exc)[:500])
            except Exception:
                pass
            return f"Error: {exc}"

    async def _execute_long_command(self, command: str):
        """Execute long-running commands with configurable timeout."""
        # Enrich the active OTel span with the actual command
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
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=effective_timeout,
                ),
            )
            output = result.stdout or result.stderr
            try:
                span.set_attribute("tool.exit_code", result.returncode)
                span.set_attribute("tool.output_bytes", len(output.encode("utf-8", errors="replace")))
            except Exception:
                pass
            return f'Output:\n{output}'
        except subprocess.TimeoutExpired:
            timeout_val = int(os.getenv("LONG_COMMAND_TIMEOUT", "7200"))
            try:
                span.set_attribute("tool.exit_code", -1)
                span.set_attribute("tool.error", f"TimeoutExpired({timeout_val}s / {timeout_val // 60}min)")
            except Exception:
                pass
            return (
                f"Error: Long command timed out after {timeout_val}s ({timeout_val//60} minutes). "
                "Set LONG_COMMAND_TIMEOUT env var to increase "
                "(e.g. LONG_COMMAND_TIMEOUT=14400 for 4 hours), or set "
                "LONG_COMMAND_TIMEOUT=0 to disable timeout."
            )
        except Exception as exc:
            try:
                span.set_attribute("tool.error", str(exc)[:500])
            except Exception:
                pass
            return f"Error: {exc}"
