import asyncio
import logging
import os
import re
import signal
import time

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

    _TIMEOUT_KILL_GRACE_SECONDS = 2.0
    _TIMEOUT_KILL_WAIT_SECONDS = 3.0

    # ── Grace period (seconds) to wait after launching a background server ──
    # Short enough to catch an immediate crash, long enough for the process to
    # bind its port and write its first log line.
    _BG_LAUNCH_GRACE_SECONDS = 1.5

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

    @staticmethod
    def _looks_like_long_lived_process(command: str) -> bool:
        """
        Return True ONLY for commands that start a long-lived / never-returning
        server or daemon process.

        The detector is intentionally TIGHT:
          • Only clear server/daemon launch patterns are matched.
          • Common install / build / one-shot commands are explicitly excluded
            so that things like 'pip install uvicorn' are NOT mis-detected.

        Positive patterns (matched):
          uvicorn, gunicorn, flask run, manage.py runserver,
          python -m http.server, npm start, npm run dev, yarn dev, pnpm dev,
          next dev, serve (standalone binary), nohup …, trailing &

        Exclusions (never matched, even if a positive token appears):
          pip install …, apt …, brew …, echo …, cat …, grep …,
          any 'install' sub-command containing a server name (e.g. pip install uvicorn),
          kill …
        """
        cmd = (command or "").strip()
        if not cmd:
            return False

        cmd_lower = cmd.lower()

        # ── Hard exclusions ──────────────────────────────────────────────────
        # These are one-shot / install commands that must never be
        # auto-backgrounded even if they mention a server name.
        _exclude_prefixes = (
            "pip ",
            "pip3 ",
            "apt ",
            "apt-get ",
            "brew ",
            "echo ",
            "cat ",
            "grep ",
            "kill ",
            "pkill ",
            "killall ",
            "which ",
            "where ",
            "curl ",
            "wget ",
        )
        for excl in _exclude_prefixes:
            if cmd_lower.startswith(excl):
                return False

        # 'install' anywhere → one-shot package install, not a server launch
        if re.search(r'\binstall\b', cmd_lower):
            return False

        # ── Explicit trailing & (background marker put by the caller) ───────
        if cmd.rstrip().endswith("&"):
            return True

        # ── nohup prefix ────────────────────────────────────────────────────
        if cmd_lower.startswith("nohup "):
            return True

        # ── Known server / daemon launchers ─────────────────────────────────
        _server_patterns = [
            # Python ASGI / WSGI servers
            r'\buvicorn\b',
            r'\bgunicorn\b',
            # Flask dev server
            r'\bflask\s+run\b',
            # Django dev server
            r'\bmanage\.py\s+runserver\b',
            # Python stdlib HTTP server
            r'\bpython\b.*\-m\s+http\.server\b',
            r'\bpython3\b.*\-m\s+http\.server\b',
            # Node / JS ecosystem
            r'\bnpm\s+start\b',
            r'\bnpm\s+run\s+dev\b',
            r'\bnpm\s+run\s+start\b',
            r'\byarn\s+dev\b',
            r'\byarn\s+start\b',
            r'\bpnpm\s+dev\b',
            r'\bpnpm\s+start\b',
            r'\bnext\s+dev\b',
            r'\bnext\s+start\b',
            # Generic 'serve' binary (e.g. `serve -s build`)
            r'(?:^|\s)serve\s',
            r'(?:^|\s)serve$',
        ]

        for pattern in _server_patterns:
            if re.search(pattern, cmd_lower):
                return True

        return False

    async def _launch_as_background_process(self, command: str) -> str:
        """
        Detach *command* into its own process group so it keeps running after
        this coroutine returns.

        stdout / stderr are redirected to a timestamped log file under logs/
        (a plain file, not a PIPE → no buffer-deadlock, no SIGPIPE).
        We wait _BG_LAUNCH_GRACE_SECONDS to catch an immediate crash, then
        return with PID + log path regardless of whether the process is still
        running.
        """
        ts = int(time.time())
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"bg_cmd_{ts}.log")

        try:
            log_fd = open(log_path, "w")
        except OSError as exc:
            return f"Error: could not open log file {log_path}: {exc}"

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,   # own process group → not killed with parent
            )
        except Exception as exc:
            log_fd.close()
            return f"Error: failed to launch background process: {exc}"

        # Close our handle — the child keeps its own copy.
        log_fd.close()

        # Brief grace period: detect immediate crash (e.g. port already in use).
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._BG_LAUNCH_GRACE_SECONDS)
            # Process exited within grace period → almost certainly a crash.
            rc = proc.returncode
            try:
                with open(log_path) as f:
                    tail = f.read()[-800:]
            except OSError:
                tail = "(log unreadable)"
            return (
                f"Warning: background process exited immediately (rc={rc}).\n"
                f"Command : {command}\n"
                f"Log     : {log_path}\n"
                f"Output  :\n{tail}"
            )
        except asyncio.TimeoutError:
            # Still running — this is the normal happy path.
            pass

        return (
            f"Background process launched successfully.\n"
            f"PID     : {proc.pid}\n"
            f"Command : {command}\n"
            f"Log     : {log_path}\n"
            f"Note    : Process is running detached. "
            f"To stop it: kill {proc.pid}   "
            f"To follow logs: tail -f {log_path}"
        )

    async def _terminate_timed_out_process(self, proc: asyncio.subprocess.Process) -> None:
        """Terminate timed-out process safely, including spawned children.

        We launch shell commands with ``start_new_session=True`` so each command
        gets its own process group. On timeout, kill the whole group to avoid
        orphaned children holding stdout/stderr pipes open.
        """
        pgid = None
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = None

        try:
            if pgid:
                os.killpg(pgid, signal.SIGTERM)
            else:
                proc.terminate()
        except ProcessLookupError:
            return
        except Exception:
            try:
                proc.terminate()
            except Exception:
                return

        try:
            await asyncio.wait_for(proc.wait(), timeout=self._TIMEOUT_KILL_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        except Exception:
            return

        try:
            if pgid:
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            return
        except Exception:
            try:
                proc.kill()
            except Exception:
                return

        try:
            await asyncio.wait_for(proc.wait(), timeout=self._TIMEOUT_KILL_WAIT_SECONDS)
        except Exception:
            pass

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

        # ── Auto-background safety net ───────────────────────────────────────
        # If the command looks like a long-lived server/daemon, launch it
        # detached instead of blocking.  This prevents Task Mode from hanging
        # on a "start the server" step.
        if self._looks_like_long_lived_process(command):
            logger.info("Auto-backgrounding long-lived command: %s", command)
            try:
                span.set_attribute("tool.auto_backgrounded", True)
            except Exception:
                pass
            return await self._launch_as_background_process(command)

        try:
            # ✅ FIX: asyncio.create_subprocess_shell uses posix_spawn — no os.fork(),
            # no gRPC "skipping fork() handlers" warning.
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                await self._terminate_timed_out_process(proc)
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

        # ── Auto-background safety net ───────────────────────────────────────
        # Same guard as _execute_command: a server launch must never block the
        # task executor, regardless of which command tool the planner chose.
        if self._looks_like_long_lived_process(command):
            logger.info("Auto-backgrounding long-lived command (long path): %s", command)
            try:
                span.set_attribute("tool.auto_backgrounded", True)
            except Exception:
                pass
            return await self._launch_as_background_process(command)

        try:
            timeout_val = int(os.getenv("LONG_COMMAND_TIMEOUT", "7200"))
            effective_timeout = timeout_val if timeout_val > 0 else None

            # ✅ FIX: asyncio.create_subprocess_shell — no os.fork(), no gRPC warning.
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=effective_timeout
                )
            except asyncio.TimeoutError:
                await self._terminate_timed_out_process(proc)
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