#!/usr/bin/env python3
"""
AI Assistant - Agent with Skills
Version: 4.2.0 (MCP Integration)

A production-ready AI assistant with MCP support, browser automation, HTTP requests, and file operations.
"""

import asyncio
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Suppress noisy deprecation warnings from third-party libraries
warnings.filterwarnings("ignore", category=DeprecationWarning, module="authlib")
warnings.filterwarnings("ignore", message=".*authlib.jose.*")

from openai import OpenAI
from core.mcp_client import MCPManager
from core.vector_memory import VectorMemory

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
DEFAULT_MAX_TOKENS = 4096
DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Tool Execution Limits
# MAX_TOOL_ITERATIONS: Maximum number of tool calls per user message
# - 1000 = ~8-10 hours of continuous execution (recommended for courses)
# - 2000 = ~16-20 hours (for very long courses)
# - 5000 = ~40-50 hours (for multi-day tasks)
MAX_TOOL_ITERATIONS = 1000

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


class ToolManager:
    """Manages all agent tools including file, browser, and HTTP operations."""
    
    def __init__(self, vector_memory=None, mcp_manager=None, shared_browser=None):
        """Initialize the tool manager."""
        self.tools: List[Dict[str, Any]] = []
        self.tool_handlers: Dict[str, Callable] = {}
        self._shared_browser = shared_browser  # optionally receive a shared browser process
        self.browser = None        # the browser process (can be shared)
        self.page = None           # per-request page (from a per-request context)
        self._context = None       # per-request browser context
        self.playwright = None
        self.vector_memory = vector_memory
        self.mcp_manager = mcp_manager
        self.session_id = None     # set by web_app.py before each request
        
    async def initialize(self):
        """Initialize tools"""
        try:
            await self._register_tools()
            logger.info(f"Initialized with {len(self.tools)} tools")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize: {e}")
            return False
    
    async def _register_tools(self):
        """Register all available tools"""
        tools_config = [
            # System tools
            ("get_current_time", "Returns the current date and time. Use this whenever the user asks about time, date, today, now, etc.", {}, self._get_current_time),
            ("execute_command", "Executes a shell command and returns its output. Use this to get system info, run programs, etc.", {"command": "string"}, self._execute_command),
            ("execute_long_command", "Execute a long-running shell command such as Maven builds (mvn test, mvn package), Gradle builds, Docker image builds, full test suite runs, or any command expected to take more than 1 minutes. Uses LONG_COMMAND_TIMEOUT env var (default: 7200s = 2 hours).", {"command": "string"}, self._execute_long_command),
            
            # File tools
            ("read_file", "Returns the contents of a file at the specified path", {"file_path": "string"}, self._read_file),
            ("write_file", "Writes content to a file at the specified path", {"file_path": "string", "content": "string"}, self._write_file),
            ("list_files", "Returns a list of files and directories in the specified directory path", {"directory": "string"}, self._list_files),
            
            # Web search tool
            ("web_search", "Searches DuckDuckGo and returns results. Use for current info, news, facts, definitions.", {"query": "string"}, self._web_search),
            
            # Browser tools
            ("browser_navigate", "Opens a web browser and navigates to the specified URL", {"url": "string"}, self._browser_navigate),
            ("browser_click", "Clicks on an element in the browser using a CSS selector", {"selector": "string"}, self._browser_click),
            ("browser_type", "Types text into an input field in the browser using a CSS selector", {"selector": "string", "text": "string"}, self._browser_type),
            ("browser_screenshot", "Takes a screenshot of the current browser window and saves it to a file", {"filename": "string"}, self._browser_screenshot),
            ("browser_get_text", "Gets the text content from an element in the browser using a CSS selector", {"selector": "string"}, self._browser_get_text),
            ("browser_close", "Closes the browser window", {}, self._browser_close),
            
            # HTTP tools
            ("http_get", "Makes an HTTP GET request to the specified URL and returns the response", {"url": "string"}, self._http_get),
            ("http_post", "Makes an HTTP POST request to the specified URL with data and returns the response", {"url": "string", "data": "string"}, self._http_post),

            # Background task delegation — for long-running / infinite-loop tasks
            ("delegate_background_task", "Delegates a long-running or infinite-loop task to a background process. Use this when a task needs to run continuously (e.g. monitoring, watching a course, polling). The task runs independently and won't block the chat.", {"name": "string", "command": "string", "interval_seconds": "string"}, self._delegate_background_task),
            ("stop_background_task", "Stops a running background task by name.", {"name": "string"}, self._stop_background_task),
            ("background_task_status", "Shows status of all background tasks or a specific one by name.", {"name": "string"}, self._background_task_status),

            # MCP server management tools
            ("mcp_list_servers", "Lists all MCP servers and their current status (running/stopped) and tool count. Use when asked about MCP servers.", {}, self._mcp_list_servers),
            ("mcp_restart_server", "Restarts a specific MCP server by name. Use when an MCP server is unresponsive or the user asks to restart it.", {"server_name": "string"}, self._mcp_restart_server),
            ("mcp_restart_all", "Restarts ALL MCP servers. Use when the user asks to restart all MCP servers or when multiple servers are unresponsive.", {}, self._mcp_restart_all),

            # Memory management tools
            ("memory_list_facts", "Lists all personal facts stored in memory about the user. Use this when asked 'what do you know about me' or 'show my memory'.", {}, self._memory_list_facts),
            ("memory_add_fact", "Manually adds a personal fact to memory. Use this when the user explicitly asks you to remember something specific about them.", {"topic": "string", "fact": "string"}, self._memory_add_fact),
            ("memory_delete_fact", "Deletes personal facts from memory that match a keyword. Use this when the user asks to forget or remove something about themselves.", {"keyword": "string"}, self._memory_delete_fact),
            ("memory_delete_research", "Deletes research memory entries that match a keyword. Use this when the user asks to forget research about a topic.", {"keyword": "string"}, self._memory_delete_research),
            ("memory_clear_research", "Clears ALL research memory entries. Use only when user explicitly asks to clear all research memory.", {}, self._memory_clear_research),
        ]
        
        for name, desc, params, handler in tools_config:
            self.tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": {k: {"type": v, "description": k.replace("_", " ")} for k, v in params.items()},
                        "required": list(params.keys())
                    }
                }
            })
            self.tool_handlers[name] = handler
    
    # System tools
    async def _get_current_time(self):
        """Get current date and time"""
        try:
            from datetime import datetime
            now = datetime.now()
            return f"Current time: {now.strftime('%A, %B %d, %Y at %I:%M:%S %p')}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _web_search(self, query: str):
        """Search with smart routing.

        The DDG Instant Answer API only returns results for Wikipedia-style
        factual queries. News/current-event queries always return empty JSON.
        We detect those and skip straight to browser-based DuckDuckGo search.

        Strategy:
          1. Factual queries -> DDG Instant Answer API (fast, no browser)
          2. News/current   -> DuckDuckGo via browser (type in search box)
          3. Fallback       -> Google via browser (type in search box)
        """
        import urllib.request as _ureq, json as _json
        from urllib.parse import quote

        _NEWS_KW = [
            "latest", "recent", "news", "today", "tonight", "this week",
            "this month", "right now", "currently", "breaking", "just ",
            "happened", "trending", "update", "2024", "2025", "2026",
        ]
        skip_api = any(kw in query.lower() for kw in _NEWS_KW)

        # Step 1: DDG Instant Answer API (factual queries only)
        if not skip_api:
            try:
                req = _ureq.Request(
                    f"https://api.duckduckgo.com/?q={quote(query)}&format=json&no_redirect=1&no_html=1",
                    headers={"User-Agent": "Mozilla/5.0 (compatible; BigAI/1.0)"}
                )
                with _ureq.urlopen(req, timeout=8) as r:
                    data = _json.loads(r.read().decode())
                rows = []
                if data.get("AbstractText"):
                    rows.append(f"**{data.get('Heading','Answer')}**: {data['AbstractText']}")
                    if data.get("AbstractURL"):
                        rows.append(f"Source: {data['AbstractURL']}")
                    rows.append("")
                for i, t in enumerate(data.get("RelatedTopics", [])[:8]):
                    if "Topics" in t:
                        for s in t.get("Topics", [])[:3]:
                            if s.get("Text"):
                                rows.append(f"{len(rows)+1}. {s['Text']}")
                                if s.get("FirstURL"):
                                    rows.append(f"   {s['FirstURL']}")
                    else:
                        if t.get("Text"):
                            rows.append(f"{i+1}. {t['Text']}")
                            if t.get("FirstURL"):
                                rows.append(f"   {t['FirstURL']}")
                for item in data.get("Results", [])[:5]:
                    if item.get("Text"):
                        rows.append(f"- {item['Text']}")
                        if item.get("FirstURL"):
                            rows.append(f"  {item['FirstURL']}")
                if rows:
                    return ("DuckDuckGo results for '" + query + "':\n\n" + "\n".join(rows))
            except Exception:
                pass  # fall through to browser search

        # Step 2: Browser-based DuckDuckGo search (handles news, less bot-detection)
        try:
            page = await self._ensure_browser()
            await page.goto("https://duckduckgo.com", wait_until="domcontentloaded")
            await page.wait_for_selector('input[name="q"]', timeout=6000)
            await page.click('input[name="q"]')
            await page.fill('input[name="q"]', query)
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
            current_url = page.url
            text_result = await self._browser_get_text("body")
            safe = query[:20].replace(" ", "_")
            await self._browser_screenshot(f"output/search_{safe}.png")
            return ("DuckDuckGo browser search for '" + query + "':\n"
                    + "URL: " + str(current_url) + "\n\n"
                    + "Page text (first 2000 chars):\n" + str(text_result)[:2000])
        except Exception as ddg_err:
            pass  # fall through to Google

        # Step 3: Google fallback (type in search box, not URL params)
        try:
            page = await self._ensure_browser()
            await page.goto("https://www.google.com", wait_until="domcontentloaded")
            await page.wait_for_selector('textarea[name="q"], input[name="q"]', timeout=6000)
            await page.click('textarea[name="q"], input[name="q"]')
            await page.fill('textarea[name="q"], input[name="q"]', query)
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
            current_url = page.url
            text_result = await self._browser_get_text("body")
            safe = query[:20].replace(" ", "_")
            await self._browser_screenshot(f"output/search_{safe}.png")
            return ("Google search for '" + query + "':\n"
                    + "URL: " + str(current_url) + "\n\n"
                    + "Page text (first 2000 chars):\n" + str(text_result)[:2000])
        except Exception as google_err:
            return f"All search methods failed for '{query}'. Last error: {google_err}"


    # File tools
    async def _read_file(self, file_path: str):
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                return f"Content of {file_path}:\n{f.read()}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _write_file(self, file_path: str, content: str):
        try:
            p = Path(file_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return f"Wrote to {file_path}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _list_files(self, directory: str):
        try:
            files = [f.name for f in Path(directory).iterdir()]
            return f"Files in {directory}:\n" + "\n".join(files)
        except Exception as e:
            return f"Error: {e}"
    
    async def _execute_command(self, command: str):
        try:
            import subprocess
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            return f"Output:\n{result.stdout or result.stderr}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _execute_long_command(self, command: str):
        """Execute a long-running command (builds, test suites, etc.) with extended timeout."""
        try:
            timeout_val = int(os.getenv("LONG_COMMAND_TIMEOUT", "7200"))  # default 2 hours
            effective_timeout = timeout_val if timeout_val > 0 else None
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    command, shell=True, capture_output=True, text=True,
                    timeout=effective_timeout
                )
            )
            return f"Output:\n{result.stdout or result.stderr}"
        except subprocess.TimeoutExpired:
            timeout_val = int(os.getenv("LONG_COMMAND_TIMEOUT", "7200"))
            return f"Error: Long command timed out after {timeout_val}s ({timeout_val//60} minutes). Set LONG_COMMAND_TIMEOUT env var to increase (e.g. LONG_COMMAND_TIMEOUT=14400 for 4 hours), or set LONG_COMMAND_TIMEOUT=0 to disable timeout."
        except Exception as e:
            return f"Error: {e}"

    # Browser tools
    async def _ensure_browser(self):
        """Get or create a per-request browser context and page."""
        if self._shared_browser:
            # Use shared browser process, create isolated context
            self.browser = self._shared_browser
        elif not self.browser:
            # Create our own browser process
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=False)
            logger.info("Browser launched")

        # Create a per-request context if not already done
        if not self._context:
            self._context = await self.browser.new_context()
            self.page = await self._context.new_page()
            logger.info("Browser context created")
        return self.page
    
    async def _browser_navigate(self, url: str):
        try:
            page = await self._ensure_browser()
            await page.goto(url, wait_until="domcontentloaded")
            return f"Navigated to {url}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _browser_click(self, selector: str):
        try:
            page = await self._ensure_browser()
            await page.click(selector)
            return f"Clicked {selector}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _browser_type(self, selector: str, text: str):
        try:
            page = await self._ensure_browser()
            await page.fill(selector, text)
            return f"Typed '{text}' into {selector}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _browser_screenshot(self, filename: str):
        try:
            page = await self._ensure_browser()
            # Redirect bare filenames (no directory) to output/ folder
            p = Path(filename)
            if not p.parent or str(p.parent) == ".":
                Path("output").mkdir(exist_ok=True)
                filename = str(Path("output") / p.name)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=filename)
            return f"Screenshot saved to {filename}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _browser_get_text(self, selector: str):
        try:
            page = await self._ensure_browser()
            text = await page.text_content(selector)
            return f"Text: {text}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _browser_close(self):
        try:
            if self.browser:
                await self.browser.close()
                await self.playwright.stop()
                self.browser = None
                self.page = None
                return "Browser closed"
            return "Browser was not open"
        except Exception as e:
            return f"Error: {e}"
    
    # HTTP tools
    async def _http_get(self, url: str):
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                return f"Status: {response.status_code}\nBody: {response.text[:500]}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _http_post(self, url: str, data: str):
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.post(url, content=data)
                return f"Status: {response.status_code}\nBody: {response.text[:500]}"
        except Exception as e:
            return f"Error: {e}"
    
    # Background task delegation handlers
    async def _delegate_background_task(self, name: str, command: str, interval_seconds: str = "0"):
        script = Path(__file__).parent / "scripts" / "background_task.py"
        if not script.exists():
            return f"Error: background_task.py not found at {script}"
        try:
            interval = int(interval_seconds)
        except ValueError:
            interval = 0
        try:
            os.makedirs("logs", exist_ok=True)
            args = [
                sys.executable, str(script),
                "--name", name,
                "--command", command,
                "--interval", str(interval),
                "--max-runs", "-1" if interval > 0 else "1",
                "--no-detach",   # already launching detached below; skip the double-fork
            ]
            if self.session_id:
                args += ["--session-id", self.session_id]
            _log_handle = open(f"logs/bg_{name}.log", "a", encoding='utf-8')
            proc = subprocess.Popen(
                args,
                stdout=_log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # detach from main.py's process group
                cwd=str(Path(__file__).parent),  # ensure child resolves logs/ relative to project root
            )
            _log_handle.close()  # close parent copy; subprocess holds its own fd
            return (
                f"✅ Background task '{name}' started (PID: {proc.pid})\n"
                f"   Command: {command}\n"
                f"   Interval: {interval}s ({'loop forever' if interval > 0 else 'run once'})\n"
                f"   Log: logs/bg_{name}.log\n"
                f"   Stop: use stop_background_task(name='{name}')"
            )
        except Exception as e:
            return f"Error starting background task: {e}"

    async def _stop_background_task(self, name: str):
        lockfile = f"/tmp/bg_task_{name}.lock"
        if not os.path.exists(lockfile):
            return f"Task '{name}' is not running."
        try:
            with open(lockfile) as f:
                pid = int(f.read().strip())
            import signal as sig
            os.kill(pid, sig.SIGTERM)
            os.remove(lockfile)
            return f"✅ Background task '{name}' stopped (PID: {pid})."
        except Exception as e:
            return f"Error stopping '{name}': {e}"

    async def _background_task_status(self, name: str = ""):
        import glob
        locks = glob.glob("/tmp/bg_task_*.lock")
        if not locks:
            return "No background tasks found."
        lines = []
        for lf in locks:
            task_name = Path(lf).stem.replace("bg_task_", "")
            if name and task_name != name:
                continue
            try:
                with open(lf) as f:
                    pid = int(f.read().strip())
                result = subprocess.run(["ps", "-p", str(pid)], capture_output=True)
                alive = result.returncode == 0
                status = f"RUNNING (PID: {pid})" if alive else "STOPPED (stale lock)"
                lines.append(f"  {task_name}: {status} | log: logs/bg_{task_name}.log")
            except Exception:
                lines.append(f"  {task_name}: unknown")
        return "Background tasks:\n" + "\n".join(lines) if lines else f"No task named '{name}' found."

    # MCP server management tool handlers
    async def _mcp_list_servers(self):
        if not self.mcp_manager:
            return "No MCP manager available."
        servers = await self.mcp_manager.list_servers()
        if not servers:
            return "No MCP servers registered."
        lines = [f"  {s['name']}: {s['status']} ({s['tools']} tools)" for s in servers]
        return "MCP Servers:\n" + "\n".join(lines)

    async def _mcp_restart_server(self, server_name: str):
        if not self.mcp_manager:
            return "No MCP manager available."
        ok = await self.mcp_manager.restart_server(server_name)
        if ok:
            servers = await self.mcp_manager.list_servers()
            info = next((s for s in servers if s["name"] == server_name), None)
            tools = info["tools"] if info else "?"
            return f"MCP server '{server_name}' restarted successfully ({tools} tools loaded)."
        return f"Failed to restart MCP server '{server_name}'. Check logs for details."

    async def _mcp_restart_all(self):
        if not self.mcp_manager:
            return "No MCP manager available."
        servers = await self.mcp_manager.list_servers()
        if not servers:
            return "No MCP servers to restart."
        results = []
        for s in servers:
            ok = await self.mcp_manager.restart_server(s["name"])
            results.append(f"  {s['name']}: {'✅ restarted' if ok else '❌ failed'}")
        return "MCP server restart results:\n" + "\n".join(results)

    # Memory management tool handlers
    async def _memory_add_fact(self, topic: str, fact: str):
        if not self.vector_memory:
            return "Memory system is not available."
        if not await self.vector_memory.ensure_ready():
            return "Memory system is currently unavailable."
        stored = await self.vector_memory.store_personal_fact(topic, fact)
        if stored:
            return f"Remembered: [{topic}] {fact}"
        return f"Failed to store fact."

    async def _memory_list_facts(self):
        if not self.vector_memory:
            return "Memory system is not available."
        if not await self.vector_memory.ensure_ready():
            return "Memory system is currently unavailable."
        facts = await self.vector_memory.get_all_personal_facts()
        if not facts:
            return "No personal facts stored in memory."
        lines = [f"[{f.get('topic')}] {f.get('fact')} (saved: {f.get('stored_at', '')[:10]})" for f in facts]
        return f"Personal facts in memory ({len(facts)}):\n" + "\n".join(lines)

    async def _memory_delete_fact(self, keyword: str):
        if not self.vector_memory:
            return "Memory system is not available."
        if not await self.vector_memory.ensure_ready():
            return "Memory system is currently unavailable."
        count = await self.vector_memory.delete_personal_facts(keyword)
        if count == 0:
            return f"No personal facts found matching '{keyword}'."
        return f"Deleted {count} personal fact(s) matching '{keyword}'."

    async def _memory_delete_research(self, keyword: str):
        if not self.vector_memory:
            return "Memory system is not available."
        if not await self.vector_memory.ensure_ready():
            return "Memory system is currently unavailable."
        count = await self.vector_memory.delete_research(keyword)
        if count == 0:
            return f"No research entries found matching '{keyword}'."
        return f"Deleted {count} research entry/entries matching '{keyword}'."

    async def _memory_clear_research(self):
        if not self.vector_memory:
            return "Memory system is not available."
        if not await self.vector_memory.ensure_ready():
            return "Memory system is currently unavailable."
        count = await self.vector_memory.clear_all_research()
        return f"Cleared all {count} research memory entries."

    async def execute_tool(self, name: str, args: Dict):
        if name not in self.tool_handlers:
            # Fall through to MCP manager for MCP tools not in built-in tool_handlers
            if self.mcp_manager:
                result = await self.mcp_manager.call_tool(name, args)
                if result is None:
                    return json.dumps({"error": f"Unknown tool: {name}"})
                return result
            return f"Unknown tool: {name}"
        
        # Normalize parameter names to handle AI model variations
        normalized_args = self._normalize_tool_args(name, args)

        # Validate required parameters and strip unexpected ones before calling handler
        import inspect
        handler = self.tool_handlers[name]
        sig = inspect.signature(handler)

        # Check for missing required parameters
        missing = [
            p for p, v in sig.parameters.items()
            if p != "self"
            and v.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
            and v.default is inspect.Parameter.empty
            and p not in normalized_args
        ]
        if missing:
            return f"Error: tool '{name}' missing required parameter(s): {', '.join(missing)}"

        # Strip unexpected keyword args the model may hallucinate (e.g. 'timeout' on tools
        # that don't accept it) to prevent TypeError: unexpected keyword argument.
        accepted = {
            p for p, v in sig.parameters.items()
            if p != "self" and v.kind not in (inspect.Parameter.VAR_POSITIONAL,)
        }
        has_var_keyword = any(
            v.kind == inspect.Parameter.VAR_KEYWORD
            for v in sig.parameters.values()
        )
        if not has_var_keyword:
            normalized_args = {k: v for k, v in normalized_args.items() if k in accepted}

        return await self.tool_handlers[name](**normalized_args)
    
    def _normalize_tool_args(self, tool_name: str, args: Dict) -> Dict:
        """
        Normalize parameter names to match tool function signatures.
        Handles cases where AI uses generic names like 'path', 'filename' instead of specific parameter names.
        """
        # Parameter mapping for each tool - maps AI's parameter names to actual function parameter names
        param_mappings = {
            'read_file': {
                'path': 'file_path',
                'filename': 'file_path',
                'file': 'file_path'
            },
            'write_file': {
                'path': 'file_path',
                'filename': 'file_path',
                'file': 'file_path'
            },
            'list_files': {
                'path': 'directory',
                'dir': 'directory',
                'folder': 'directory'
            },
            'execute_command': {
                'cmd': 'command',
                'shell_command': 'command',
                'shell': 'command',
                'code': 'command',
                'script': 'command',
                'bash': 'command',
                'input': 'command',
            },
            'execute_long_command': {
                'cmd': 'command',
                'shell_command': 'command',
                'shell': 'command',
                'code': 'command',
                'script': 'command',
                'bash': 'command',
                'input': 'command',
            },
        }
        
        if tool_name not in param_mappings:
            return args
        
        normalized = {}
        mapping = param_mappings[tool_name]
        
        for key, value in args.items():
            # If the key needs to be mapped, use the mapped name
            normalized_key = mapping.get(key, key)
            normalized[normalized_key] = value
        
        return normalized
    
    async def cleanup(self):
        """Clean up per-request resources (context/page only, not shared browser)."""
        try:
            if self._context:
                await self._context.close()
                self._context = None
                self.page = None
        except:
            pass
        # Only close the browser process if WE own it (not shared)
        if not self._shared_browser:
            try:
                if self.browser:
                    await self.browser.close()
                    self.browser = None
            except:
                pass
            try:
                if self.playwright:
                    await self.playwright.stop()
                    self.playwright = None
            except:
                pass


class Config:
    """Configuration manager for the AI assistant."""
    
    def __init__(self):
        """Initialize configuration from environment variables and .env file."""
        self._load_env_file()
        self._load_config()
    
    def _load_env_file(self):
        """Load environment variables from .env file if it exists."""
        env_file = Path(".env")
        if not env_file.exists():
            return
        
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip("\"'")
    
    def _load_config(self):
        """Load configuration values from environment."""
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL)
        self.model = os.getenv("AI_MODEL", DEFAULT_MODEL)
        self.temperature = float(os.getenv("AI_TEMPERATURE", str(DEFAULT_TEMPERATURE)))
        self.max_tokens = int(os.getenv("AI_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
        self.enable_tools = os.getenv("ENABLE_TOOLS", "true").lower() == "true"
    
    def validate(self) -> bool:
        """Validate that required configuration is present."""
        return bool(self.api_key)
    
    def display(self):
        """Display current configuration."""
        print(f"Model: {self.model}")
        print(f"Endpoint: {self.base_url}")
        print(f"Tools: {self.enable_tools}")


class AIAgent:
    """Main AI agent that handles conversations and tool execution."""
    
    def __init__(self, config: Config):
        """Initialize the AI agent with configuration."""
        self.config = config
        self.client: Optional[OpenAI] = None
        self.conversation: List[Dict[str, Any]] = []
        self.tools: Optional[ToolManager] = None
        self.mcp_manager: Optional[MCPManager] = None
        self.tools_available = False
        self.vector_memory: Optional[VectorMemory] = None
        self.memory_available = False
        self._shared_browser = None  # shared Playwright browser process
        self._playwright = None

    async def _get_shared_browser(self):
        """Lazily create ONE shared Chromium process for all per-request ToolManagers.
        Each ToolManager creates its own isolated BrowserContext from this browser.
        """
        if self._shared_browser is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._shared_browser = await self._playwright.chromium.launch(headless=False)
            logger.info("Shared browser process started (lazy init)")
        return self._shared_browser
    
    async def initialize(self) -> bool:
        """
        Initialize the AI agent and its tools.
        
        Returns:
            bool: True if initialization successful, False otherwise.
        """
        try:
            self.client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)
            
            # Initialize MCP manager
            self.mcp_manager = MCPManager()
            
            # Load MCP servers from config
            await self._load_mcp_servers()
            
            # Initialize vector memory (Weaviate) — optional, graceful if unavailable
            weaviate_port = int(os.getenv("WEAVIATE_PORT", "8090"))
            self.vector_memory = VectorMemory(
                openai_api_key=self.config.api_key,
                weaviate_url=f"http://localhost:{weaviate_port}",
                openai_base_url=self.config.base_url,
            )
            self.memory_available = await self.vector_memory.initialize()
            if self.memory_available:
                logger.info("✅ Vector memory (Weaviate) available")
            else:
                logger.info("ℹ️  Vector memory unavailable — running without persistent memory")

            if self.config.enable_tools:
                self.tools = ToolManager(vector_memory=self.vector_memory, mcp_manager=self.mcp_manager)
                self.tools_available = await self.tools.initialize()

                # mcp_manager already loaded above — update reference in ToolManager
                if self.tools:
                    self.tools.mcp_manager = self.mcp_manager
                
                # Add system message establishing tool usage context
                if self.tools_available:
                    # Ensure output/temp directories exist for AI-generated files
                    Path("output").mkdir(exist_ok=True)
                    Path("temp").mkdir(exist_ok=True)
                    system_message = {
                        "role": "system",
                        "content": (
                            "You are an AUTONOMOUS AI AGENT. Your job is to TAKE ACTION, not explain.\n\n"
                            "CRITICAL RULES:\n"
                            "1. When user asks a question → USE TOOLS IMMEDIATELY to get the answer\n"
                            "2. NEVER say 'I cannot' or 'I don't have access' - you have tools, USE THEM\n"
                            "3. NEVER explain how to do something - JUST DO IT\n"
                            "4. For time/date questions → use get_current_time\n"
                            "5. For recent events/news/current info → use web_search\n"
                            "6. For system info → use execute_command\n"
                            "7. Chain multiple tools when needed\n"
                            "8. Only respond in text AFTER you've used tools to get the answer\n\n"
                            "FILE CREATION RULES (ALWAYS FOLLOW):\n"
                            "9. ALWAYS save screenshots, images, and temp files to the 'output/' folder (e.g. output/screenshot.png)\n"
                            "10. ALWAYS save intermediate/scratch files to the 'temp/' folder (e.g. temp/work.txt)\n"
                            "11. NEVER create temp/image files in the project root directory\n"
                            "12. If a filename has no directory prefix, prepend 'output/' automatically\n\n"
                            "BACKGROUND TASK CAPABILITIES — CRITICAL:\n"
                            "You CAN proactively send messages into this chat using background tasks.\n"
                            "'Send me', 'tell me', 'notify me', 'alert me', 'say X to me' = use delegate_background_task.\n\n"
                            "HOW TO SEND MESSAGES TO CHAT FROM A BACKGROUND TASK:\n"
                            "Any line printed by the background script that starts with a special prefix\n"
                            "is AUTOMATICALLY injected as a message into THIS chat within 3 seconds.\n\n"
                            "The prefixes are:\n"
                            "  print('NOTIFY: <text>')   → appears as info message in chat\n"
                            "  print('SUCCESS: <text>')  → appears as green success message in chat\n"
                            "  print('WARNING: <text>')  → appears as yellow warning in chat\n"
                            "  print('ALERT: <text>')    → appears as red urgent alert in chat\n\n"
                            "IMPORTANT: Plain print() WITHOUT the prefix only goes to the log file, NOT the chat.\n"
                            "You MUST use print('NOTIFY: ...') or similar prefix to send to chat.\n\n"
                            "EXAMPLE — 'say hi to me every 10 seconds':\n"
                            "Step 1: Write this to temp/say_hi.py:\n"
                            "  import time\n"
                            "  for i in range(6):\n"
                            "      print('NOTIFY: Hi! This is message number', i+1)\n"
                            "      time.sleep(10)\n"
                            "Step 2: Call delegate_background_task(name='say_hi', command='python temp/say_hi.py', interval_seconds='0')\n"
                            "Result: 'NOTIFY: Hi! This is message number 1' appears in THIS chat after 3 seconds.\n\n"
                            "EXAMPLE — 'alert me every minute with CPU usage':\n"
                            "  import time, subprocess\n"
                            "  while True:\n"
                            "      cpu = subprocess.check_output(['top', '-l1', '-n0']).decode()\n"
                            "      print('NOTIFY: CPU status -', cpu[:100])\n"
                            "      time.sleep(60)\n\n"
                            "NEVER use plain print() when you want to send something to the chat.\n"
                            "ALWAYS use print('NOTIFY: ...') to send messages to the chat.\n"
                            "NEVER say 'I cannot send messages to this chat' — you CAN via delegate_background_task.\n\n"
                            "Example: User asks 'what time is it?' → Call get_current_time() → Return the time\n"
                            "Example: User asks 'latest aviation accident' → Call web_search('latest aviation accident') → Summarize results\n"
                            "Example: Taking a screenshot → use filename 'output/screenshot.png' not 'screenshot.png'\n\n"
                            "LARGE FILE HANDLING RULES:\n"
                            "17. If read_file returns a very large result, DO NOT try to read it again — it will be the same size.\n"
                            "18. For large files, use execute_command with grep/head/tail/sed to navigate:\n"
                            "    - Search: grep -n 'keyword' filename\n"
                            "    - First N lines: head -100 filename\n"
                            "    - Last N lines: tail -100 filename\n"
                            "    - Line range: sed -n '100,200p' filename\n"
                            "    - Count lines: wc -l filename\n"
                            "19. Never include raw binary or huge JSON blobs in your response — summarise them.\n\n"
                            "BE PROACTIVE. ACT FIRST. EXPLAIN AFTER."
                        )
                    }
                    self.conversation.append(system_message)
                    
            logger.info("Agent initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Agent initialization failed: {e}")
            return False
    
    async def _load_mcp_servers(self):
        """Load MCP servers from mcp_config.json"""
        try:
            config_file = Path("mcp_config.json")
            if not config_file.exists():
                logger.info("No mcp_config.json found, skipping MCP servers")
                return
            
            with open(config_file) as f:
                mcp_config = json.load(f)
            
            servers = mcp_config.get("servers", {})
            for server_name, server_config in servers.items():
                command = server_config.get("command")
                args = server_config.get("args", [])
                env = server_config.get("env", None)

                if command and self.mcp_manager:
                    logger.info(f"Loading MCP server: {server_name}")
                    success = await self.mcp_manager.add_server(server_name, command, args, env=env)
                    if success:
                        logger.info(f"✅ MCP server {server_name} loaded")
                    else:
                        logger.warning(f"⚠️  Failed to load MCP server {server_name}")
        
        except Exception as e:
            logger.error(f"Error loading MCP servers: {e}")
    
    async def _extract_and_store_facts(self, user_input: str):
        """
        Detect personal facts in user input and store them in vector memory.
        Examples: "I have a Samsung oven", "my name is John", "I live in Bangkok"
        """
        # Simple heuristic patterns that suggest personal facts
        fact_patterns = [
            "i have", "i own", "i use", "i am", "i'm", "my name is",
            "i live", "i work", "my ", "i prefer", "i like", "i hate",
            "i drive", "i eat", "i drink", "i take", "i need",
        ]
        lower = user_input.lower()
        if not any(p in lower for p in fact_patterns):
            return

        # Ask the AI to extract the fact cleanly
        try:
            extraction_prompt = (
                f'Extract personal facts from this message as JSON.\n'
                f'Message: "{user_input}"\n'
                f'Respond ONLY with JSON like: {{"topic": "oven", "fact": "I have a Samsung NV75K5571RS oven"}}\n'
                f'If no personal fact found, respond: {{"topic": null, "fact": null}}'
            )
            _loop = asyncio.get_running_loop()
            _extract_params = dict(
                model=self.config.model,
                messages=[{"role": "user", "content": extraction_prompt}],
                temperature=0,
                max_tokens=100
            )
            extraction_response = await _loop.run_in_executor(
                None, lambda: self.client.chat.completions.create(**_extract_params)
            )
            text = extraction_response.choices[0].message.content or ""
            # Parse JSON
            json_start = text.find('{')
            json_end = text.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                data = json.loads(text[json_start:json_end])
                topic = data.get("topic")
                fact = data.get("fact")
                if topic and fact and self.vector_memory:
                    await self.vector_memory.store_personal_fact(topic, fact)
                    print(f"  🧠 Remembered: [{topic}] {fact}")
        except Exception as e:
            logger.debug(f"Fact extraction skipped: {e}")


    async def _auto_memory_hook(self, user_input: str, ai_response: str):
        """
        Auto-learning memory hook — fires after every final AI response.
        Sends the exchange to Claude Sonnet to extract meaningful facts about
        the user, then stores them in the AutoLearned Weaviate collection.
        Completely separate from PersonalFacts (explicit memory).
        Non-blocking: always called via asyncio.create_task().
        """
        if not self.memory_available or not self.vector_memory:
            return
        # Skip trivial exchanges (one-liners, time queries, greetings)
        if len(user_input.strip()) < 20 or len(ai_response.strip()) < 20:
            return
        try:
            # Get existing auto-facts topics to avoid duplication
            existing = await self.vector_memory.get_all_auto_facts()
            existing_topics = [f.get('topic', '').lower() for f in existing]
            existing_summary = ', '.join(existing_topics[:50]) if existing_topics else 'none yet'

            # Get existing personal facts topics too
            personal = await self.vector_memory.get_all_personal_facts()
            personal_topics = [f.get('topic', '').lower() for f in personal]
            personal_summary = ', '.join(personal_topics[:50]) if personal_topics else 'none'

            extraction_prompt = f"""You are a memory extraction assistant. Analyze this conversation exchange and extract meaningful facts about the USER (not about AI, not tool outputs).

Conversation:
User: {user_input[:1000]}
Assistant: {ai_response[:800]}

Already known personal facts (do NOT re-extract these topics): {personal_summary}
Already auto-learned topics (update if changed, skip if same): {existing_summary}

Extract facts about:
- User preferences, dislikes, opinions
- Projects they work on, tools they use
- People they mention (names, roles, relationships)
- Technical environment (OS, languages, stack)
- Decisions they made
- Problems they solved or encountered
- Things they corrected the AI about
- Work context and habits

Do NOT extract:
- Questions the user asked
- AI responses or tool results
- One-time lookups (time, weather)
- Greetings or small talk
- Anything already in personal facts

Respond ONLY with a JSON array. Each item: {{"topic": "short_snake_case_key", "fact": "concise fact sentence", "confidence": "high|medium|low"}}
If nothing meaningful to extract, respond with: []

JSON:"""

            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.client.chat.completions.create(
                    model=self.config.model,
                    messages=[{"role": "user", "content": extraction_prompt}],
                    temperature=0.2,
                    max_tokens=800
                )
            )

            raw = result.choices[0].message.content.strip()
            # Strip markdown code fences if present
            if raw.startswith('```'):
                raw = raw.split('```')[1]
                if raw.startswith('json'):
                    raw = raw[4:]
            raw = raw.strip()

            import json as _json
            facts = _json.loads(raw)
            if not isinstance(facts, list):
                return

            stored_count = 0
            for item in facts:
                topic = item.get('topic', '').strip()
                fact = item.get('fact', '').strip()
                confidence = item.get('confidence', 'medium')
                if topic and fact and len(fact) > 5:
                    ok = await self.vector_memory.store_auto_fact(topic, fact, confidence)
                    if ok:
                        stored_count += 1

            if stored_count > 0:
                logger.info(f"Auto-memory hook stored {stored_count} new/updated facts")

        except Exception as e:
            logger.debug(f"Auto-memory hook failed (non-critical): {e}")

    def _build_tools_prompt(self) -> str:
        """Build system prompt with tool descriptions"""
        prompt = "\n\nYou have access to the following tools:\n\n"
        
        # Add built-in tools
        if self.tools_available and self.tools:
            for tool in self.tools.tools:
                func = tool['function']
                prompt += f"<tool name=\"{func['name']}\">\n"
                prompt += f"Description: {func['description']}\n"
                if func.get('parameters', {}).get('properties'):
                    prompt += "Parameters:\n"
                    for param_name, param_info in func['parameters']['properties'].items():
                        param_type = param_info.get('type', 'string')
                        param_desc = param_info.get('description', '')
                        required = param_name in func['parameters'].get('required', [])
                        req_str = " (required)" if required else " (optional)"
                        prompt += f"  - {param_name} ({param_type}){req_str}: {param_desc}\n"
                else:
                    prompt += "Parameters: none\n"
                prompt += "</tool>\n\n"
        
        # Add MCP tools
        if self.mcp_manager:
            for tool in self.mcp_manager.get_all_tools_for_openai():
                func = tool['function']
                prompt += f"<tool name=\"{func['name']}\">\n"
                prompt += f"Description: {func['description']}\n"
                if func.get('parameters', {}).get('properties'):
                    prompt += "Parameters:\n"
                    for param_name, param_info in func['parameters']['properties'].items():
                        param_type = param_info.get('type', 'string')
                        param_desc = param_info.get('description', '')
                        required = param_name in func['parameters'].get('required', [])
                        req_str = " (required)" if required else " (optional)"
                        prompt += f"  - {param_name} ({param_type}){req_str}: {param_desc}\n"
                else:
                    prompt += "Parameters: none\n"
                prompt += "</tool>\n\n"
        
        prompt += """To use a tool, respond with:
<tool_use>
<tool_name>tool_name_here</tool_name>
<parameters>
{
  "param1": "value1",
  "param2": "value2"
}
</parameters>
</tool_use>

You can use multiple tools in sequence. After I execute each tool, I'll provide the result and you can continue or use another tool.
IMPORTANT: When you use a tool, ONLY output the <tool_use> block, nothing else. After I give you the result, then provide your final answer."""
        
        return prompt
    
    def _parse_tool_calls(self, response_text: str) -> List[Dict[str, Any]]:
        """Parse tool calls from Claude's XML response"""
        import re
        
        tool_calls = []
        
        # ── Format 1: <tool_use>...<tool_name>NAME</tool_name>...</tool_use> ──
        # Also tolerates mismatched closing tags: </tool_invoke>, </tool_call>
        pattern = r'<tool_use>(.*?)</(?:tool_use|tool_invoke|tool_call|use)>'
        matches = re.findall(pattern, response_text, re.DOTALL)

        # ── Format 2: <function_calls><invoke name="NAME">...</invoke></function_calls> ──
        # Claude sometimes uses this anthropic-native format for MCP tools.
        invoke_pattern = r'<invoke\s+name=["\']([^"\']+)["\']>(.*?)</invoke>'
        for inv_name, inv_body in re.findall(invoke_pattern, response_text, re.DOTALL):
            # Convert <parameter name="key">value</parameter> → JSON dict
            params = {}
            for p_name, p_val in re.findall(r'<parameter\s+name=["\']([^"\']+)["\']>(.*?)</parameter>', inv_body, re.DOTALL):
                params[p_name] = p_val.strip()
            tool_calls.append({"tool_name": inv_name.strip(), "parameters": params})
            logger.warning(f"Parsed function_calls/invoke format for tool: {inv_name.strip()}")

        for match in matches:
            try:
                # Parse tool name
                tool_name_match = re.search(r'<tool_name>(.*?)</tool_name>', match)
                if not tool_name_match:
                    continue
                tool_name = tool_name_match.group(1).strip()
                
                # Parse parameters
                params_match = re.search(r'<parameters>(.*?)</parameters>', match, re.DOTALL)
                if params_match:
                    params_text = params_match.group(1).strip()
                    if params_text:
                        try:
                            parameters = json.loads(params_text)
                        except json.JSONDecodeError:
                            # Try 1: repair truncated JSON (max_tokens cut off closing brace)
                            open_count = params_text.count('{')
                            close_count = params_text.count('}')
                            if open_count > close_count:
                                repaired = params_text.rstrip() + '}' * (open_count - close_count)
                                try:
                                    parameters = json.loads(repaired)
                                    logger.warning(f"Repaired truncated parameters JSON")
                                    # Successfully repaired — skip XML fallback
                                except json.JSONDecodeError:
                                    parameters = None
                            else:
                                parameters = None

                            # Try 2: model used XML-style params <key>value</key> instead of JSON
                            if parameters is None:
                                xml_params = re.findall(r'<(\w+)>(.*?)</\1>', params_text, re.DOTALL)
                                if xml_params:
                                    parameters = {k: v.strip() for k, v in xml_params}
                                    logger.warning(f"Parsed XML-style parameters: {list(parameters.keys())}")
                                else:
                                    logger.error(f"Failed to parse parameters: {params_text[:200]}")
                                    parameters = {}
                    else:
                        parameters = {}
                else:
                    parameters = {}
                
                tool_calls.append({
                    "tool_name": tool_name,
                    "parameters": parameters
                })
            except Exception as e:
                logger.error(f"Error parsing tool call: {e}")
                continue
        
        return tool_calls
    
    async def get_response(self, user_input: str, conversation: Optional[List[Dict]] = None, tools: Optional["ToolManager"] = None, token_callback=None) -> Optional[str]:
        """
        Get AI response with prompt-based tool execution loop (Cline/Roo style).
        
        Args:
            user_input: User's message
            conversation: Optional external conversation list (stateless/per-request mode).
                          If None, falls back to self.conversation (CLI/backward-compat mode).
            tools: Optional per-request ToolManager. If provided, used instead of self.tools so
                   concurrent requests never share or mutate the agent's tool reference.
            
        Returns:
            Optional[str]: AI response or None if error occurred
        """
        if not self.client:
            return None

        # Resolve which ToolManager to use for this call — never mutate self.tools
        effective_tools = tools if tools is not None else self.tools

        # Use passed-in conversation or fall back to self.conversation (backward compat)
        if conversation is not None:
            conv = conversation  # stateless mode: caller owns this list
        else:
            conv = self.conversation  # CLI mode: mutate self.conversation as before
        
        # Build tools prompt once and store it for use in the first iteration only.
        # We do NOT permanently inject it into conv[0] because:
        #   1. conv[0] is a shared reference to agent.conversation[0] — mutating it
        #      would corrupt the shared system message across sessions.
        #   2. Including the full 80k-char tools prompt in EVERY iteration (including
        #      the tool-results feedback round-trip) bloats the payload and causes
        #      IBM ICA (and other APIs with tight context limits) to return 400.
        # Instead, we pass a _first-iteration-only_ system message with the tools
        # description, and strip it back to the base system content for subsequent
        # iterations.
        _base_system_content = conv[0]["content"] if conv and conv[0]["role"] == "system" else ""
        _tools_prompt = self._build_tools_prompt() if self.tools_available else ""

        try:
            # (tools prompt is applied per-iteration below, not permanently here)
            pass

            # --- Vector memory: detect and store personal facts from user input ---
            if self.memory_available and self.vector_memory:
                await self._extract_and_store_facts(user_input)

            # --- Vector memory: inject relevant context as prefix (capped size) ---
            memory_prefix = ""
            if self.memory_available and self.vector_memory:
                raw_prefix = await self.vector_memory.build_context_prompt(user_input)
                # Cap memory context to avoid token overflow
                if raw_prefix and len(raw_prefix) > MAX_MEMORY_CONTEXT_CHARS:
                    raw_prefix = raw_prefix[:MAX_MEMORY_CONTEXT_CHARS] + "\n...[memory truncated]\n\n"
                memory_prefix = raw_prefix

            user_message = (memory_prefix + user_input) if memory_prefix else user_input
            conv.append({"role": "user", "content": user_message})

            # Trim AFTER adding the new message so trimming sees the full picture
            self._trim_conversation(conv)
            
            # Agent loop - allow multiple tool calls
            # For long-running tasks (courses, etc.), use a very high limit
            # Configured via MAX_TOOL_ITERATIONS constant at top of file
            for iteration in range(MAX_TOOL_ITERATIONS):
                # On the first iteration: inject tools prompt into system message so
                # the model knows the tool format.  On subsequent iterations (tool
                # results feedback): use the base system content only to keep the
                # payload small and avoid 400 errors from context-size limits.
                if conv and conv[0]["role"] == "system":
                    if iteration == 0 and _tools_prompt:
                        conv[0] = {"role": "system", "content": _base_system_content + _tools_prompt}
                    else:
                        conv[0] = {"role": "system", "content": _base_system_content}

                params = {
                    "model": self.config.model,
                    "messages": conv,
                    "temperature": self.config.temperature,
                    "max_tokens": self.config.max_tokens
                }
                
                # NO tools parameter - use prompt-based approach instead
                
                _loop = asyncio.get_running_loop()
                if token_callback:
                    # STREAMING MODE — entire chunk iteration runs in executor thread
                    # so the async event loop stays free to flush SSE tokens to the
                    # browser in real time. This fixes the UI freeze / step-112 lockup
                    # where tokens were generated but the browser never received them
                    # because the sync for-loop was blocking the event loop.
                    import queue as _queue
                    _token_q: _queue.Queue = _queue.Queue()
                    stream_params = {**params, 'stream': True}

                    def _stream_in_thread():
                        try:
                            stream = self.client.chat.completions.create(**stream_params)
                            for chunk in stream:
                                delta = chunk.choices[0].delta.content if chunk.choices else None
                                if delta:
                                    _token_q.put(delta)
                        except Exception as _e:
                            _token_q.put(_e)
                        finally:
                            _token_q.put(None)  # sentinel — stream finished

                    # Launch blocking stream iteration in thread pool
                    _stream_future = _loop.run_in_executor(None, _stream_in_thread)

                    response_text = ''
                    while True:
                        # Poll queue; yield to event loop between polls so SSE can flush
                        try:
                            item = _token_q.get_nowait()
                        except _queue.Empty:
                            await asyncio.sleep(0)  # yield → event loop flushes SSE
                            continue
                        if item is None:
                            break  # sentinel: stream done
                        if isinstance(item, Exception):
                            raise item
                        response_text += item
                        # Do NOT emit tokens yet — buffer until we know this is
                        # the final response (no tool calls). Replayed below.

                    await _stream_future  # ensure thread is fully joined
                else:
                    # NON-STREAMING MODE: CLI / planning / background tasks
                    response = await _loop.run_in_executor(
                        None, lambda: self.client.chat.completions.create(**params)
                    )
                    response_text = response.choices[0].message.content
                
                if not response_text:
                    return "No response from AI"
                
                # Parse tool calls from response
                tool_calls = self._parse_tool_calls(response_text)
                
                if tool_calls:
                    # Add assistant's response with tool calls
                    conv.append({
                        "role": "assistant",
                        "content": response_text
                    })
                    
                    # Execute all tool calls
                    tool_results = []
                    for tool_call in tool_calls:
                        tool_name = tool_call["tool_name"]
                        tool_args = tool_call["parameters"]
                        
                        print(f"\033[92m  🔧 Executing: {tool_name}({', '.join(f'{k}={str(v)}' for k, v in tool_args.items()) if tool_args else ''})\033[0m")
                        
                        # Route ALL tool calls (built-in and MCP) through effective_tools.execute_tool()
                        # so web_app.py's instrumented wrapper emits SSE events for every call,
                        # including MCP tools. execute_tool() now falls through to mcp_manager for
                        # tools not in tool_handlers.
                        if effective_tools:
                            tool_result = await effective_tools.execute_tool(tool_name, tool_args)
                        else:
                            tool_result = json.dumps({"error": "No tool manager available"})
                        
                        # Print tool result in cyan
                        result_preview = str(tool_result)
                        if len(result_preview) > 500:
                            result_preview = result_preview[:500] + "... [truncated]"
                        print(f"\033[96m  📤 Result: {result_preview}\033[0m")
                        
                        # --- Store tool result in vector memory ---
                        if self.memory_available and self.vector_memory:
                            query_hint = str(list(tool_args.values())[0]) if tool_args else tool_name
                            await self.vector_memory.store_research(
                                tool_name=tool_name,
                                query=query_hint[:200],
                                result=str(tool_result)
                            )

                        tool_results.append(f"Tool: {tool_name}\nResult: {tool_result}")
                    
                    # Notify instead of silently truncate — tell the AI the result is large
                    MAX_RESULT_CHARS = 80000  # ~26k tokens — warn if larger but don't silently cut off
                    notified_results = []
                    for tr in tool_results:
                        if len(tr) > MAX_RESULT_CHARS:
                            # Don't silently truncate — tell the AI the result is large so it can navigate it
                            preview = tr[:3000]
                            size_kb = len(tr) // 1024
                            notified_results.append(
                                f"[RESULT TOO LARGE: {size_kb}KB — only first 3KB shown below. "
                                f"Use execute_command with grep/head/tail/sed to search/navigate this content "
                                f"rather than reading the whole thing at once.]\n\n{preview}\n\n[...{size_kb-3}KB more not shown]"
                            )
                        else:
                            notified_results.append(tr)
                    results_text = "\n\n".join(notified_results)
                    conv.append({
                        "role": "user",
                        "content": f"Tool execution results:\n\n{results_text}\n\nPlease provide your final response based on these results."
                    })
                    
                    # Continue loop to let AI process tool results
                    continue
                else:
                    # No tool calls — final response. NOW stream tokens to UI.
                    # This means the browser never sees raw <tool_use> XML blocks
                    # from intermediate iterations — only the clean final answer.
                    if token_callback:
                        import re as _re
                        clean = _re.sub(r'<tool_use>.*?</tool_use>', '', response_text, flags=_re.DOTALL).strip()
                        for ch in clean:
                            try:
                                token_callback(ch)
                            except Exception:
                                pass
                    conv.append({"role": "assistant", "content": response_text})
                    # Fire auto-learning hook (non-blocking)
                    try:
                        asyncio.create_task(self._auto_memory_hook(user_input, response_text))
                    except Exception:
                        pass
                    return response_text
            
            # If we hit max iterations, inform user but don't fail
            logger.warning(f"Reached maximum iterations ({MAX_TOOL_ITERATIONS}). Task may not be complete.")
            return f"I've executed {MAX_TOOL_ITERATIONS} tool calls. The task may need to be continued. Please check the current state and let me know if you want me to continue."
                
        except Exception as e:
            logger.error(f"Error: {e}")
            import traceback
            traceback.print_exc()
            if conv and conv[-1]["role"] == "user":
                conv.pop()
            raise  # Re-raise so web_app.py can surface it to the frontend
    
    def _estimate_tokens(self, text: str) -> int:
        """
        Accurate token count using tiktoken (cl100k_base covers gpt-4, gpt-4o, claude via proxy).
        Falls back to conservative char-based estimate if tiktoken unavailable.
        """
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            # Fallback: len//2 is safer than //3 for Thai/CJK text
            return max(1, len(text) // 2)
    
    def _get_conversation_tokens(self, conv: List[Dict]) -> int:
        """Calculate total tokens in a conversation list"""
        total = 0
        for msg in conv:
            content = msg.get("content", "")
            total += self._estimate_tokens(content)
        return total
    
    def _trim_conversation(self, conv: List[Dict]):
        """
        Dynamically trim conversation history based on token count.
        Keeps system message and as many recent messages as possible within token limit.
        Operates on the passed-in conversation list (works for both self.conversation and
        per-request conversation lists).
        """
        total_tokens = self._get_conversation_tokens(conv)
        
        # If under limit, no trimming needed
        if total_tokens <= MAX_CONVERSATION_TOKENS:
            return
        
        # Keep system message (first message with tools description)
        system_msg = None
        start_idx = 0
        if conv and conv[0]["role"] == "system":
            system_msg = conv[0]
            start_idx = 1
        
        # Calculate tokens for system message
        system_tokens = self._estimate_tokens(system_msg["content"]) if system_msg else 0
        available_tokens = MAX_CONVERSATION_TOKENS - system_tokens
        
        # Keep as many recent messages as possible within token limit
        kept_messages = []
        current_tokens = 0
        
        # Iterate from most recent to oldest
        for msg in reversed(conv[start_idx:]):
            msg_tokens = self._estimate_tokens(msg.get("content", ""))
            if current_tokens + msg_tokens <= available_tokens:
                kept_messages.insert(0, msg)  # Insert at beginning to maintain order
                current_tokens += msg_tokens
            else:
                break  # Stop when we would exceed limit
        
        # Rebuild conversation in-place
        conv.clear()
        if system_msg:
            conv.append(system_msg)

        # Guard: OpenAI requires first non-system message to be 'user'
        # Trimming can leave an orphaned 'assistant' message at the front
        while kept_messages and kept_messages[0]["role"] != "user":
            logger.debug(f"Trim: dropping orphaned leading '{kept_messages[0]['role']}' message to satisfy OpenAI role ordering")
            kept_messages.pop(0)

        conv.extend(kept_messages)
        
        new_total = self._get_conversation_tokens(conv)
        logger.info(f"Trimmed conversation: {total_tokens} → {new_total} tokens ({len(kept_messages)} messages kept)")
    
    def clear(self):
        """Clear all conversation history"""
        self.conversation.clear()

    async def shutdown(self):
        """Shut down agent resources including the shared browser process."""
        if self.tools:
            await self.tools.cleanup()
        # Close shared browser process owned by this agent
        try:
            if self._shared_browser:
                await self._shared_browser.close()
                self._shared_browser = None
        except:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
        except:
            pass
        if self.vector_memory:
            self.vector_memory.close()

    async def run(self, mode: str = "chat"):
        """
        Run the agent in specified mode.
        
        Args:
            mode: "chat" for interactive chat, "agent" for programmatic agent mode
        """
        if mode == "agent":
            # Agent mode - programmatic interface
            from agent_api import AgentAPI
            api = AgentAPI(self)
            print("="*60)
            print("🤖 AGENT MODE - Programmatic Interface")
            print("="*60)
            print("Agent is ready for programmatic task execution.")
            print("Use the AgentAPI to run tasks programmatically.")
            print("See example_agent_usage.py for examples.")
            print("-"*60)
            return api
        
        # Chat mode - interactive conversation
        print("="*60)
        print("🤖 AUTONOMOUS AI AGENT (v4.1.0) - CHAT MODE")
        print("="*60)
        self.config.display()
        if self.tools_available:
            tool_count = 0
            if self.tools:
                tool_count += len(self.tools.tools)
            if self.mcp_manager:
                tool_count += len(self.mcp_manager.get_all_tools_for_openai())
            print(f"🔧 Tools: {tool_count} available")
            print("⚡ I take action immediately - just ask!")
            print("🕐 Time/Date | 🔍 Web Search | 📁 Files | 🌐 Browser | 💻 Commands")
        print("\nCommands: help, clear, quit, agent")
        print("-"*60)

        # Use prompt_toolkit for rich input: arrow keys, history recall (↑), no length limit
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory
            session = PromptSession(history=FileHistory(".chat_history"))
        except ImportError:
            session = None

        while True:
            try:
                if session:
                    user_input = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: session.prompt("\n👤 You: ")
                    )
                else:
                    user_input = input("\n👤 You: ")
                user_input = user_input.strip()
                if not user_input:
                    continue
                
                if user_input.lower() in ['quit', 'exit', 'q']:
                    print("\n👋 Goodbye!")
                    break
                
                if user_input.lower() in ['clear', 'reset']:
                    self.clear()
                    print("🔄 Cleared!")
                    continue
                
                if user_input.lower() == 'agent':
                    print("\n🤖 Switching to Agent Mode...")
                    print("To use agent mode programmatically, see example_agent_usage.py")
                    print("Agent mode allows you to:")
                    print("  - Run tasks programmatically")
                    print("  - Execute multi-step workflows")
                    print("  - Monitor task progress")
                    print("  - Get detailed reports")
                    continue
                
                if user_input.lower() == 'help':
                    print("\n📖 I'm an autonomous agent - I act immediately!")
                    print("\nExamples:")
                    print("  'what time is it?' → I'll get the time")
                    print("  'latest aviation accidents' → I'll search Google")
                    print("  'list files here' → I'll list directory")
                    print("  'open google.com' → I'll launch browser")
                    print("\nCommands:")
                    print("  quit/exit - Exit")
                    print("  clear - Clear history")
                    print("  agent - Info about agent mode")
                    continue
                
                print("\n🔄 Agent working...")
                response = await self.get_response(user_input)
                if response:
                    print(f"\n✅ {response}")
                else:
                    print("❌ Failed to get response")
                    
            except (KeyboardInterrupt, EOFError):
                print("\n\n👋 Goodbye!")
                break
            except Exception as e:
                logger.error(f"Error: {e}")
                print("❌ An error occurred")
        
        await self.shutdown()


async def main():
    try:
        config = Config()
        if not config.validate():
            print("❌ API key not configured\nSet OPENAI_API_KEY in .env or environment")
            return 1
        
        agent = AIAgent(config)
        if not await agent.initialize():
            print("❌ Failed to initialize")
            return 1
        
        await agent.run()
        return 0
    except Exception as e:
        logger.error(f"Fatal: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))