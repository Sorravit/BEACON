"""Background task management tool handlers."""

# ── gRPC fork-safety ─────────────────────────────────────────────────────────
# subprocess.Popen(start_new_session=True) calls os.fork() which triggers:
#   "Other threads are currently calling into gRPC, skipping fork() handlers"
# Fix: explicitly pass gRPC fork-safety env vars into the child process env
# so gRPC in the child runs its post-fork handlers cleanly.
# ─────────────────────────────────────────────────────────────────────────────

import glob
import os
import subprocess
import sys
from pathlib import Path


class TaskToolsMixin:
    async def _delegate_background_task(self, name: str, command: str, interval_seconds: str = "0"):
        project_root = Path(__file__).resolve().parents[2]
        script = project_root / "scripts" / "background_task.py"
        if not script.exists():
            return f"Error: background_task.py not found at {script}"

        try:
            interval = int(interval_seconds)
        except ValueError:
            interval = 0

        try:
            os.makedirs("logs", exist_ok=True)
            args = [
                sys.executable,
                str(script),
                "--name",
                name,
                "--command",
                command,
                "--interval",
                str(interval),
                "--max-runs",
                "-1" if interval > 0 else "1",
                "--no-detach",
            ]
            if self.session_id:
                args += ["--session-id", self.session_id]

            # ✅ FIX: pass gRPC fork-safety env vars explicitly to the child process.
            # Without this, the child process inherits the parent's gRPC thread state
            # and gRPC skips its fork() handlers, logging the warning flood.
            child_env = os.environ.copy()
            child_env["GRPC_ENABLE_FORK_SUPPORT"] = "1"
            child_env["GRPC_POLL_STRATEGY"] = "poll"
            child_env["GRPC_VERBOSITY"] = "ERROR"
            child_env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"

            log_handle = open(f"logs/bg_{name}.log", "a", encoding="utf-8")
            proc = subprocess.Popen(
                args,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(project_root),
                env=child_env,
            )
            log_handle.close()
            return (
                f"✅ Background task '{name}' started (PID: {proc.pid})\n"
                f"   Command: {command}\n"
                f"   Interval: {interval}s ({'loop forever' if interval > 0 else 'run once'})\n"
                f"   Log: logs/bg_{name}.log\n"
                f"   Stop: use stop_background_task(name='{name}')"
            )
        except Exception as exc:
            return f"Error starting background task: {exc}"

    async def _stop_background_task(self, name: str):
        lockfile = f"/tmp/bg_task_{name}.lock"
        if not os.path.exists(lockfile):
            return f"Task '{name}' is not running."
        try:
            with open(lockfile) as handle:
                pid = int(handle.read().strip())
            import signal as sig
            os.kill(pid, sig.SIGTERM)
            os.remove(lockfile)
            return f"✅ Background task '{name}' stopped (PID: {pid})."
        except Exception as exc:
            return f"Error stopping '{name}': {exc}"

    async def _background_task_status(self, name: str = ""):
        locks = glob.glob("/tmp/bg_task_*.lock")
        if not locks:
            return "No background tasks found."

        lines = []
        for lock in locks:
            task_name = Path(lock).stem.replace("bg_task_", "")
            if name and task_name != name:
                continue
            try:
                with open(lock) as handle:
                    pid = int(handle.read().strip())
                # ✅ FIX: asyncio.create_subprocess_exec — no os.fork(), no gRPC warning.
                import asyncio as _asyncio
                _ps = await _asyncio.create_subprocess_exec(
                    "ps", "-p", str(pid),
                    stdout=_asyncio.subprocess.DEVNULL,
                    stderr=_asyncio.subprocess.DEVNULL,
                )
                await _ps.wait()
                alive = _ps.returncode == 0
                status = f"RUNNING (PID: {pid})" if alive else "STOPPED (stale lock)"
                lines.append(f"  {task_name}: {status} | log: logs/bg_{task_name}.log")
            except Exception:
                lines.append(f"  {task_name}: unknown")

        return "Background tasks:\n" + "\n".join(lines) if lines else f"No task named '{name}' found."
