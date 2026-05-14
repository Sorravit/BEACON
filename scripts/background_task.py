
#!/usr/bin/env python3
"""
Background Task Runner
======================
A generic long-running task executor that the main AI agent can delegate to.

The main agent calls this when a task requires:
  - An infinite loop (e.g. monitoring, watching, polling)
  - Repeated retries on a schedule
  - Running autonomously without blocking the chat interface

Usage (from terminal):
    python scripts/background_task.py --name "watch_course" --command "python scripts/auto_continue_course_simple.py https://..."
    python scripts/background_task.py --name "monitor_logs" --command "python scripts/run_agent.py 'monitor /var/log/app.log for errors every 5 minutes'"

Usage (from main.py via delegate_background_task tool):
    The AI agent calls delegate_background_task(name="...", command="...", interval=600)

The runner:
  1. Starts the command as a detached background process
  2. Writes its PID to /tmp/bg_task_<name>.lock
  3. Optionally re-runs it every <interval> seconds if it exits
  4. Logs all output to logs/bg_<name>.log

To stop a running task:
    python scripts/background_task.py --stop --name "watch_course"

To check status:
    python scripts/background_task.py --status --name "watch_course"
    python scripts/background_task.py --status  (shows all tasks)
"""

import os
import sys
import time
import signal
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

LOCK_DIR = "/tmp"
LOG_DIR = "logs"


def _write_notify(session_id: str, task_name: str, message: str, level: str = "info"):
    """Write a notification file for the web server to pick up within 3 seconds."""
    import json as _j, time as _t
    from datetime import datetime as _dt
    os.makedirs("logs", exist_ok=True)
    ts = _t.strftime("%Y%m%d_%H%M%S") + f"_{int(_t.time() * 1000) % 1_000_000:06d}"
    path = os.path.join("logs", f"notify_{ts}.json")
    try:
        with open(path, "w") as f:
            _j.dump({
                "session_id": session_id,
                "task_name": task_name,
                "message": message,
                "level": level,
                "ts": _dt.now().isoformat()
            }, f)
    except Exception:
        pass


def lockfile_path(name: str) -> str:
    return os.path.join(LOCK_DIR, f"bg_task_{name}.lock")


def logfile_path(name: str) -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    return os.path.join(LOG_DIR, f"bg_{name}.log")


def log(name: str, message: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{name}] {message}"
    print(line)
    with open(logfile_path(name), "a") as f:
        f.write(line + "\n")


def read_pid(name: str) -> int | None:
    lf = lockfile_path(name)
    if not os.path.exists(lf):
        return None
    try:
        with open(lf) as f:
            return int(f.read().strip())
    except Exception:
        return None


def write_pid(name: str, pid: int):
    with open(lockfile_path(name), "w") as f:
        f.write(str(pid))


def remove_lock(name: str):
    lf = lockfile_path(name)
    if os.path.exists(lf):
        os.remove(lf)


def is_running(name: str) -> tuple[bool, int | None]:
    """Returns (is_alive, pid)"""
    pid = read_pid(name)
    if pid is None:
        return False, None
    result = subprocess.run(["ps", "-p", str(pid)], capture_output=True)
    if result.returncode == 0:
        return True, pid
    # Stale lock
    remove_lock(name)
    return False, None


def run_once(name: str, command: str, session_id: str = None) -> int:
    """Run command, write PID, stream output line-by-line (intercept NOTIFY markers). Returns exit code."""
    log(name, f"▶ Running: {command}")
    lf = logfile_path(name)
    # Force unbuffered output so NOTIFY: lines are delivered line-by-line even when
    # the child process is a Python script writing to a pipe (which would otherwise
    # use 8 KB block-buffering, causing lines to be lost on SIGTERM).
    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(Path(__file__).parent.parent),
        env=child_env,
    )
    write_pid(name, proc.pid)
    log(name, f"  PID: {proc.pid}")

    # Stream output line-by-line: write to log AND intercept NOTIFY markers
    NOTIFY_PREFIXES = [
        ("ALERT:", "alert"),
        ("WARNING:", "warning"),
        ("SUCCESS:", "success"),
        ("NOTIFY:", "info"),
    ]
    with open(lf, "a") as log_f:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            log_f.write(line + "\n")
            log_f.flush()
            # Check for notification marker
            if session_id:
                for prefix, level in NOTIFY_PREFIXES:
                    if line.startswith(prefix):
                        msg = line[len(prefix):].strip()
                        _write_notify(session_id, name, msg, level)
                        break

    proc.wait()
    log(name, f"  Exited with code: {proc.returncode}")
    return proc.returncode


def start_loop(name: str, command: str, interval: int, max_runs: int, session_id: str = None):
    """
    Run command in a loop.
    - interval=0  → run once, no repeat
    - interval>0  → wait <interval> seconds after each run, then re-run
    - max_runs=-1 → loop forever
    """
    signal.signal(signal.SIGINT, lambda s, f: (remove_lock(name), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda s, f: (remove_lock(name), sys.exit(0)))

    log(name, "=" * 50)
    log(name, f"Task '{name}' started")
    log(name, f"Command: {command}")
    log(name, f"Interval: {interval}s | Max runs: {'∞' if max_runs == -1 else max_runs}")
    log(name, "=" * 50)

    runs = 0
    try:
        while True:
            log(name, f"─── Run #{runs + 1} {'(loop forever)' if max_runs == -1 else f'of {max_runs}'} ───")
            log(name, f"📋 Prompt/Command: {command}")
            run_once(name, command, session_id=session_id)
            runs += 1

            if max_runs != -1 and runs >= max_runs:
                log(name, f"Reached max runs ({max_runs}). Stopping.")
                break

            if interval <= 0:
                log(name, "Single run complete.")
                break

            log(name, f"💤 Next run in {interval}s...")
            time.sleep(interval)
    finally:
        remove_lock(name)
        log(name, f"Task '{name}' stopped. Total runs: {runs}")
        if session_id:
            _write_notify(session_id, name,
                          f"✅ Background task '{name}' completed after {runs} run(s).",
                          "success")


def cmd_start(name: str, command: str, interval: int, max_runs: int, detach: bool, session_id: str = None):
    """Start the task, optionally detached."""
    alive, pid = is_running(name)
    if alive:
        print(f"⚠️  Task '{name}' is already running (PID: {pid})")
        return

    if detach:
        # Launch this same script in background as a new process
        lf = logfile_path(name)
        args = [
            sys.executable, __file__,
            "--name", name,
            "--command", command,
            "--interval", str(interval),
            "--max-runs", str(max_runs),
            "--no-detach"
        ]
        if session_id:
            args += ["--session-id", session_id]
        proc = subprocess.Popen(
            args,
            stdout=open(lf, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(Path(__file__).parent.parent)
        )
        write_pid(name, proc.pid)
        print(f"✅ Task '{name}' started in background (PID: {proc.pid})")
        print(f"   Log: {lf}")
        print(f"   Stop: python scripts/background_task.py --stop --name {name}")
    else:
        start_loop(name, command, interval, max_runs, session_id=session_id)


def cmd_stop(name: str):
    alive, pid = is_running(name)
    if not alive:
        print(f"Task '{name}' is not running.")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        remove_lock(name)
        print(f"✅ Task '{name}' stopped (PID: {pid})")
    except Exception as e:
        print(f"Error stopping '{name}': {e}")


def cmd_status(name: str | None):
    if name:
        alive, pid = is_running(name)
        lf = logfile_path(name)
        status = f"RUNNING (PID: {pid})" if alive else "STOPPED"
        print(f"Task '{name}': {status}")
        print(f"  Log: {lf}")
    else:
        # Show all tasks by scanning lock files
        locks = list(Path(LOCK_DIR).glob("bg_task_*.lock"))
        if not locks:
            print("No background tasks found.")
            return
        for lf in locks:
            task_name = lf.stem.replace("bg_task_", "")
            alive, pid = is_running(task_name)
            status = f"RUNNING (PID: {pid})" if alive else "STOPPED (stale lock)"
            print(f"  {task_name}: {status}")


def main():
    parser = argparse.ArgumentParser(description="Background Task Runner")
    parser.add_argument("--name", required=False, help="Task name (used for lock/log files)")
    parser.add_argument("--command", help="Shell command to run")
    parser.add_argument("--interval", type=int, default=0,
                        help="Seconds between re-runs (0 = run once, default: 0)")
    parser.add_argument("--max-runs", type=int, default=-1,
                        help="Max number of runs (-1 = infinite, default: -1)")
    parser.add_argument("--stop", action="store_true", help="Stop a running task")
    parser.add_argument("--status", action="store_true", help="Show task status")
    parser.add_argument("--no-detach", action="store_true",
                        help="Run in foreground (used internally for detached launch)")
    parser.add_argument("--session-id", default=None,
                        help="Chat session ID to notify when task completes")
    args = parser.parse_args()

    if args.status:
        cmd_status(args.name)
    elif args.stop:
        if not args.name:
            print("Error: --name required with --stop")
            sys.exit(1)
        cmd_stop(args.name)
    elif args.command:
        if not args.name:
            print("Error: --name required with --command")
            sys.exit(1)
        detach = not args.no_detach
        cmd_start(args.name, args.command, args.interval, args.max_runs, detach, session_id=args.session_id)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()