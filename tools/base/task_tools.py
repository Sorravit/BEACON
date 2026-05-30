"""Background task management tool handlers."""

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

            log_handle = open(f"logs/bg_{name}.log", "a", encoding="utf-8")
            proc = subprocess.Popen(
                args,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(project_root),
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
                result = subprocess.run(["ps", "-p", str(pid)], capture_output=True)
                alive = result.returncode == 0
                status = f"RUNNING (PID: {pid})" if alive else "STOPPED (stale lock)"
                lines.append(f"  {task_name}: {status} | log: logs/bg_{task_name}.log")
            except Exception:
                lines.append(f"  {task_name}: unknown")

        return "Background tasks:\n" + "\n".join(lines) if lines else f"No task named '{name}' found."

