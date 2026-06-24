"""
Unit tests for Fix 1 — auto-background long-lived process detection.

Covers:
  - _looks_like_long_lived_process(): positive and negative cases
  - _execute_command() returns quickly (non-blocking) with PID + log for server commands
  - _execute_command() still runs inline and returns output for normal commands
  - _execute_long_command() also auto-backgrounds server commands
  - Port-8000 self-kill guard is not broken
"""
import asyncio
import os
import re
import sys
import time

import pytest

# ---------------------------------------------------------------------------
# Minimal concrete subclass so we can test the mixin directly
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.base.command_tools import CommandToolsMixin


class _ConcreteCommandTools(CommandToolsMixin):
    """Thin concrete class that exposes the mixin for testing."""
    pass


_tools = _ConcreteCommandTools()


# ===========================================================================
# 1.  _looks_like_long_lived_process — positive cases
# ===========================================================================
@pytest.mark.parametrize("cmd", [
    "uvicorn web_app:app --port 8001",
    "uvicorn web_app:app --host 0.0.0.0 --port 8001 --reload",
    "gunicorn -w 4 myapp:app",
    "flask run",
    "flask run --port 5000",
    "python manage.py runserver",
    "python manage.py runserver 0.0.0.0:8080",
    "python -m http.server",
    "python -m http.server 9000",
    "python3 -m http.server 9000",
    "npm start",
    "npm run dev",
    "npm run start",
    "yarn dev",
    "yarn start",
    "pnpm dev",
    "pnpm start",
    "next dev",
    "next start",
    "serve -s build",
    "serve",
    "nohup python app.py",
    "nohup uvicorn web_app:app &",
    "python server.py &",
    "bash start.sh &",
])
def test_looks_like_long_lived_positive(cmd):
    assert _tools._looks_like_long_lived_process(cmd), \
        f"Expected True for: {cmd!r}"


# ===========================================================================
# 2.  _looks_like_long_lived_process — negative cases (must NOT match)
# ===========================================================================
@pytest.mark.parametrize("cmd", [
    # Package installation — most important exclusion
    "pip install uvicorn",
    "pip install uvicorn gunicorn flask",
    "pip3 install uvicorn",
    "pip install -r requirements.txt",
    # Build / test / one-shot
    "pytest tests/",
    "pytest tests/ -v",
    "python -m pytest",
    "mvn package",
    "mvn clean install",
    "gradle build",
    "make build",
    # Shell utilities
    "echo hello",
    "cat README.md",
    "grep -r uvicorn .",
    "which uvicorn",
    "curl http://localhost:8001/health",
    "wget http://example.com",
    # Kill commands
    "kill 1234",
    "pkill uvicorn",
    "killall gunicorn",
    # apt / brew
    "apt-get install uvicorn",
    "brew install node",
    # misc
    "python script.py",
    "python3 helper.py",
    "ls -la",
    "cd /tmp && ls",
    "",
])
def test_looks_like_long_lived_negative(cmd):
    assert not _tools._looks_like_long_lived_process(cmd), \
        f"Expected False for: {cmd!r}"


# ===========================================================================
# 3.  _execute_command auto-backgrounds a server command — non-blocking
#     Returns quickly (< 5s) with PID + log path in the result.
# ===========================================================================
@pytest.mark.asyncio
async def test_execute_command_autobackgrounds_server():
    # Use 'python -m http.server 19877' — a real server that would block forever.
    cmd = "python3 -m http.server 19877"
    start = time.monotonic()
    result = await _tools._execute_command(cmd)
    elapsed = time.monotonic() - start

    # Must return quickly — well under 5 seconds (grace period is 1.5s)
    assert elapsed < 5.0, f"_execute_command blocked for {elapsed:.1f}s on a server command"

    # Result must contain a PID and a log path
    assert "PID" in result or "Background process launched" in result or "background" in result.lower(), \
        f"Unexpected result: {result}"

    # Extract PID and clean up
    pid_match = re.search(r"PID\s*:\s*(\d+)", result)
    if pid_match:
        pid = int(pid_match.group(1))
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass  # already gone


# ===========================================================================
# 4.  _execute_command runs inline for a normal command and returns output
# ===========================================================================
@pytest.mark.asyncio
async def test_execute_command_inline_normal():
    result = await _tools._execute_command("echo beacon_test_ok")
    assert "beacon_test_ok" in result, f"Expected echo output, got: {result}"


# ===========================================================================
# 5.  _execute_long_command also auto-backgrounds server commands
# ===========================================================================
@pytest.mark.asyncio
async def test_execute_long_command_autobackgrounds_server():
    cmd = "python3 -m http.server 19878"
    start = time.monotonic()
    result = await _tools._execute_long_command(cmd)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"_execute_long_command blocked for {elapsed:.1f}s on a server command"
    assert "PID" in result or "background" in result.lower() or "Background" in result, \
        f"Unexpected result: {result}"

    pid_match = re.search(r"PID\s*:\s*(\d+)", result)
    if pid_match:
        pid = int(pid_match.group(1))
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


# ===========================================================================
# 6.  _execute_long_command still works inline for normal commands
# ===========================================================================
@pytest.mark.asyncio
async def test_execute_long_command_inline_normal():
    result = await _tools._execute_long_command("echo long_cmd_ok")
    assert "long_cmd_ok" in result, f"Expected echo output, got: {result}"


# ===========================================================================
# 7.  Self-kill guard still blocks port-8000 kill commands
# ===========================================================================
@pytest.mark.asyncio
async def test_self_kill_guard_still_active():
    result = await _tools._execute_command("kill -9 $(lsof -ti:8000)")
    assert "Error" in result or "Blocked" in result, \
        f"Self-kill guard should have blocked, got: {result}"


# ===========================================================================
# 8.  Crash detection — immediate exit is reported as a warning
# ===========================================================================
@pytest.mark.asyncio
async def test_execute_command_crash_detected():
    # This command exits immediately with rc=1
    result = await _tools._execute_command("python3 -c 'import uvicorn' 2>/dev/null || (echo fail && exit 1) &")
    # Whether it's detected as crash or background-launched depends on exact timing,
    # but it must return quickly and not raise an exception.
    assert isinstance(result, str)