"""Central tool manager composed from category-specific tool modules."""

import inspect
import json
import logging
from typing import Any, Callable, Dict, List

from tools.base.command_tools import CommandToolsMixin
from tools.base.system_tools import SystemToolsMixin
from tools.base.task_tools import TaskToolsMixin
from tools.browser.browser_tools import BrowserToolsMixin
from tools.file.file_tools import FileToolsMixin
from tools.mcp.mcp_tools import MCPToolsMixin
from tools.memory.memory_tools import MemoryToolsMixin
from tools.web.http_tools import HttpToolsMixin

logger = logging.getLogger(__name__)


class ToolManager(
    SystemToolsMixin,
    CommandToolsMixin,
    FileToolsMixin,
    BrowserToolsMixin,
    HttpToolsMixin,
    TaskToolsMixin,
    MCPToolsMixin,
    MemoryToolsMixin,
):
    """Manages all agent tools, split by domain modules."""

    def __init__(self, vector_memory=None, mcp_manager=None, shared_browser=None):
        self.tools: List[Dict[str, Any]] = []
        self.tool_handlers: Dict[str, Callable] = {}
        self._shared_browser = shared_browser
        self.browser = None
        self.page = None
        self._context = None
        self.playwright = None
        self.vector_memory = vector_memory
        self.mcp_manager = mcp_manager
        self.session_id = None

    async def initialize(self):
        try:
            await self._register_tools()
            logger.info("Initialized with %d tools", len(self.tools))
            return True
        except Exception as exc:
            logger.error("Failed to initialize: %s", exc)
            return False

    async def _register_tools(self):
        tools_config = [
            ("get_current_time", "Returns the current date and time. Use this whenever the user asks about time, date, today, now, etc.", {}, self._get_current_time),
            ("execute_command", "Executes a shell command and returns its output. Use this to get system info, run programs, etc.", {"command": "string"}, self._execute_command),
            ("execute_long_command", "Execute a long-running shell command such as Maven builds (mvn test, mvn package), Gradle builds, Docker image builds, full test suite runs, or any command expected to take more than 1 minutes. Uses LONG_COMMAND_TIMEOUT env var (default: 7200s = 2 hours).", {"command": "string"}, self._execute_long_command),
            ("read_file", "Returns the contents of a file at the specified path", {"file_path": "string"}, self._read_file),
            ("write_file", "Writes content to a file at the specified path", {"file_path": "string", "content": "string"}, self._write_file),
            ("list_files", "Returns a list of files and directories in the specified directory path", {"directory": "string"}, self._list_files),
            ("web_search", "Searches DuckDuckGo and returns results. Use for current info, news, facts, definitions.", {"query": "string"}, self._web_search),
            ("browser_navigate", "Opens a web browser and navigates to the specified URL", {"url": "string"}, self._browser_navigate),
            ("browser_click", "Clicks on an element in the browser using a CSS selector", {"selector": "string"}, self._browser_click),
            ("browser_type", "Types text into an input field in the browser using a CSS selector", {"selector": "string", "text": "string"}, self._browser_type),
            ("browser_screenshot", "Takes a screenshot of the current browser window and saves it to a file", {"filename": "string"}, self._browser_screenshot),
            ("browser_get_text", "Gets the text content from an element in the browser using a CSS selector", {"selector": "string"}, self._browser_get_text),
            ("browser_close", "Closes the browser window", {}, self._browser_close),
            ("http_get", "Makes an HTTP GET request to the specified URL and returns the response", {"url": "string"}, self._http_get),
            ("http_post", "Makes an HTTP POST request to the specified URL with data and returns the response", {"url": "string", "data": "string"}, self._http_post),
            ("delegate_background_task", "Delegates a long-running or infinite-loop task to a background process. Use this when a task needs to run continuously (e.g. monitoring, watching a course, polling). The task runs independently and won't block the chat.", {"name": "string", "command": "string", "interval_seconds": "string"}, self._delegate_background_task),
            ("stop_background_task", "Stops a running background task by name.", {"name": "string"}, self._stop_background_task),
            ("background_task_status", "Shows status of all background tasks or a specific one by name.", {"name": "string"}, self._background_task_status),
            ("mcp_list_servers", "Lists all MCP servers and their current status (running/stopped) and tool count. Use when asked about MCP servers.", {}, self._mcp_list_servers),
            ("mcp_restart_server", "Restarts a specific MCP server by name. Use when an MCP server is unresponsive or the user asks to restart it.", {"server_name": "string"}, self._mcp_restart_server),
            ("mcp_restart_all", "Restarts ALL MCP servers. Use when the user asks to restart all MCP servers or when multiple servers are unresponsive.", {}, self._mcp_restart_all),
            ("memory_list_facts", "Lists all personal facts stored in memory about the user. Use this when asked 'what do you know about me' or 'show my memory'.", {}, self._memory_list_facts),
            ("memory_add_fact", "Manually adds a personal fact to memory. Use this when the user explicitly asks you to remember something specific about them.", {"topic": "string", "fact": "string"}, self._memory_add_fact),
            ("memory_delete_fact", "Deletes personal facts from memory that match a keyword. Use this when the user asks to forget or remove something about themselves.", {"keyword": "string"}, self._memory_delete_fact),
            ("memory_delete_research", "Deletes research memory entries that match a keyword. Use this when the user asks to forget research about a topic.", {"keyword": "string"}, self._memory_delete_research),
            ("memory_clear_research", "Clears ALL research memory entries. Use only when user explicitly asks to clear all research memory.", {}, self._memory_clear_research),
        ]

        for name, desc, params, handler in tools_config:
            self.tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": desc,
                        "parameters": {
                            "type": "object",
                            "properties": {k: {"type": v, "description": k.replace("_", " ")} for k, v in params.items()},
                            "required": list(params.keys()),
                        },
                    },
                }
            )
            self.tool_handlers[name] = handler

    async def execute_tool(self, name: str, args: Dict):
        if name not in self.tool_handlers:
            if self.mcp_manager:
                result = await self.mcp_manager.call_tool(name, args)
                if result is None:
                    return json.dumps({"error": f"Unknown tool: {name}"})
                return result
            return f"Unknown tool: {name}"

        normalized_args = self._normalize_tool_args(name, args)

        handler = self.tool_handlers[name]
        sig = inspect.signature(handler)

        missing = [
            p
            for p, v in sig.parameters.items()
            if p != "self"
            and v.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
            and v.default is inspect.Parameter.empty
            and p not in normalized_args
        ]
        if missing:
            return f"Error: tool '{name}' missing required parameter(s): {', '.join(missing)}"

        accepted = {
            p for p, v in sig.parameters.items() if p != "self" and v.kind not in (inspect.Parameter.VAR_POSITIONAL,)
        }
        has_var_keyword = any(v.kind == inspect.Parameter.VAR_KEYWORD for v in sig.parameters.values())
        if not has_var_keyword:
            normalized_args = {k: v for k, v in normalized_args.items() if k in accepted}

        return await self.tool_handlers[name](**normalized_args)

    def _normalize_tool_args(self, tool_name: str, args: Dict) -> Dict:
        param_mappings = {
            "read_file": {"path": "file_path", "filename": "file_path", "file": "file_path"},
            "write_file": {"path": "file_path", "filename": "file_path", "file": "file_path"},
            "list_files": {"path": "directory", "dir": "directory", "folder": "directory"},
            "execute_command": {
                "cmd": "command",
                "shell_command": "command",
                "shell": "command",
                "code": "command",
                "script": "command",
                "bash": "command",
                "input": "command",
            },
            "execute_long_command": {
                "cmd": "command",
                "shell_command": "command",
                "shell": "command",
                "code": "command",
                "script": "command",
                "bash": "command",
                "input": "command",
            },
        }
        if tool_name not in param_mappings:
            return args

        normalized = {}
        mapping = param_mappings[tool_name]
        for key, value in args.items():
            normalized_key = mapping.get(key, key)
            normalized[normalized_key] = value
        return normalized

    async def cleanup(self):
        try:
            if self._context:
                await self._context.close()
                self._context = None
                self.page = None
        except Exception:
            pass

        if not self._shared_browser:
            try:
                if self.browser:
                    await self.browser.close()
                    self.browser = None
            except Exception:
                pass
            try:
                if self.playwright:
                    await self.playwright.stop()
                    self.playwright = None
            except Exception:
                pass

