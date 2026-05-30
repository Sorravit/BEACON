"""Shell command tool handlers."""

import asyncio
import os
import subprocess


class CommandToolsMixin:
    async def _execute_command(self, command: str):
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            return f"Output:\n{result.stdout or result.stderr}"
        except Exception as exc:
            return f"Error: {exc}"

    async def _execute_long_command(self, command: str):
        """Execute long-running commands with configurable timeout."""
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
            return f"Output:\n{result.stdout or result.stderr}"
        except subprocess.TimeoutExpired:
            timeout_val = int(os.getenv("LONG_COMMAND_TIMEOUT", "7200"))
            return (
                "Error: Long command timed out after "
                f"{timeout_val}s ({timeout_val//60} minutes). "
                "Set LONG_COMMAND_TIMEOUT env var to increase "
                "(e.g. LONG_COMMAND_TIMEOUT=14400 for 4 hours), or set "
                "LONG_COMMAND_TIMEOUT=0 to disable timeout."
            )
        except Exception as exc:
            return f"Error: {exc}"

