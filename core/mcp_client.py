
#!/usr/bin/env python3
"""
MCP (Model Context Protocol) Client Implementation
Handles communication with MCP servers via stdio using async I/O with timeouts.
"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_START_TIMEOUT = 30
_REQUEST_TIMEOUT = 60
_TOOL_CALL_TIMEOUT = 120


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: Dict[str, Any]


class MCPClient:
    """Async stdio MCP client — never blocks the event loop."""

    def __init__(self, server_name: str, command: str, args: List[str]):
        self.server_name = server_name
        self.command = command
        self.args = args
        self.process: Optional[asyncio.subprocess.Process] = None
        self.tools: List[MCPTool] = []
        self.request_id = 0
        self._stderr_task: Optional[asyncio.Task] = None
        self._lock: asyncio.Lock = asyncio.Lock()  # serialise send→read pairs per server

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> bool:
        try:
            logger.info(f"Starting MCP server: {self.server_name}")
            self.process = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._stderr_task = asyncio.ensure_future(self._drain_stderr())

            if not await self._wait_for_ready():
                logger.error(f"MCP server {self.server_name} did not become ready within {_START_TIMEOUT}s")
                await self.stop()
                return False

            await self._load_tools()
            logger.info(f"MCP server {self.server_name} started with {len(self.tools)} tools")
            return True
        except Exception as e:
            logger.error(f"Failed to start MCP server {self.server_name}: {e}")
            return False

    async def _drain_stderr(self):
        """Read stderr continuously in background to prevent pipe blocking."""
        if not self.process or not self.process.stderr:
            return
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if text:
                    logger.debug(f"[{self.server_name} stderr] {text}")
        except Exception:
            pass

    async def _wait_for_ready(self) -> bool:
        """Retry the MCP initialize handshake until server responds or timeout."""
        deadline = asyncio.get_event_loop().time() + _START_TIMEOUT
        attempt = 0
        while asyncio.get_event_loop().time() < deadline:
            attempt += 1
            try:
                resp = await asyncio.wait_for(
                    self._send_raw("initialize", {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "ai-agent", "version": "1.0"},
                    }),
                    timeout=5,
                )
                if resp is not None and "error" not in resp:
                    await self._send_notification("notifications/initialized", {})
                    logger.info(f"MCP server {self.server_name} ready (attempt {attempt})")
                    return True
            except Exception as e:
                remaining = deadline - asyncio.get_event_loop().time()
                logger.debug(f"MCP {self.server_name} not ready (attempt {attempt}, {remaining:.0f}s left): {e}")
                await asyncio.sleep(min(2, max(0, remaining)))
        return False

    async def _load_tools(self):
        try:
            resp = await asyncio.wait_for(self._send_raw("tools/list", {}), timeout=_REQUEST_TIMEOUT)
            if resp and "result" in resp:
                self.tools = [
                    MCPTool(
                        name=t["name"],
                        description=t.get("description", ""),
                        input_schema=t.get("inputSchema", {}),
                    )
                    for t in resp["result"].get("tools", [])
                ]
                logger.info(f"Loaded {len(self.tools)} tools from {self.server_name}")
        except asyncio.TimeoutError:
            logger.error(f"Timeout loading tools from {self.server_name}")
        except Exception as e:
            logger.error(f"Failed to load tools from {self.server_name}: {e}")

    # ── communication ────────────────────────────────────────────────────

    async def _send_notification(self, method: str, params: Dict[str, Any]):
        if not self.process or not self.process.stdin:
            return
        try:
            msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
            self.process.stdin.write((msg + "\n").encode())
            await self.process.stdin.drain()
        except Exception as e:
            logger.debug(f"Error sending notification to {self.server_name}: {e}")

    async def _send_raw(self, method: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Send a JSON-RPC request and return the matching response.
        Non-JSON lines from stdout (e.g. mcp-remote OAuth messages) are skipped.
        Uses asyncio.Lock to serialise send→read pairs — safe for concurrent requests,
        does NOT block the event loop (asyncio lock yields while waiting).
        """
        if not self.process or not self.process.stdin or not self.process.stdout:
            logger.error(f"MCP server {self.server_name} not running")
            return None

        async with self._lock:  # only one send→read in flight per server at a time
            self.request_id += 1
            req_id = self.request_id
            payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})

            try:
                self.process.stdin.write((payload + "\n").encode())
                await self.process.stdin.drain()

                # Read lines, skipping non-JSON, until we find our response id
                while True:
                    raw = await self.process.stdout.readline()
                    if not raw:
                        logger.error(f"MCP server {self.server_name} closed stdout unexpectedly")
                        return None
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    try:
                        resp = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug(f"[{self.server_name}] skipping non-JSON stdout: {line[:120]}")
                        continue
                    # Accept response only if id matches (ignore notifications/other messages)
                    if resp.get("id") == req_id:
                        if "error" in resp:
                            logger.error(f"MCP error from {self.server_name}: {resp['error']}")
                            return None
                        return resp
            except Exception as e:
                logger.error(f"Error communicating with {self.server_name}: {e}")
                return None

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        try:
            resp = await asyncio.wait_for(
                self._send_raw("tools/call", {"name": tool_name, "arguments": arguments}),
                timeout=_TOOL_CALL_TIMEOUT,
            )
            if resp and "result" in resp:
                result = resp["result"]
                if isinstance(result, dict) and "content" in result:
                    content = result["content"]
                    if isinstance(content, list) and content:
                        first = content[0]
                        return first.get("text", str(first)) if isinstance(first, dict) else str(first)
                return str(result)
            return None
        except asyncio.TimeoutError:
            logger.error(f"Timeout calling tool {tool_name} on {self.server_name}")
            return json.dumps({"error": f"Timeout calling {tool_name}"})
        except Exception as e:
            logger.error(f"Error calling MCP tool {tool_name}: {e}")
            return None

    def get_tools_for_openai(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": f"mcp_{self.server_name}_{tool.name}",
                    "description": f"[MCP:{self.server_name}] {tool.description}",
                    "parameters": tool.input_schema,
                },
            }
            for tool in self.tools
        ]

    async def stop(self):
        if self._stderr_task:
            self._stderr_task.cancel()
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5)
                logger.info(f"MCP server {self.server_name} stopped")
            except Exception as e:
                logger.error(f"Error stopping MCP server {self.server_name}: {e}")
                try:
                    self.process.kill()
                except Exception:
                    pass


class MCPManager:
    """Manages multiple MCP clients."""

    def __init__(self):
        self.clients: Dict[str, MCPClient] = {}

    async def add_server(self, server_name: str, command: str, args: List[str]) -> bool:
        if server_name in self.clients:
            logger.warning(f"MCP server {server_name} already exists")
            return True
        client = MCPClient(server_name, command, args)
        if await client.start():
            self.clients[server_name] = client
            return True
        return False

    def get_all_tools_for_openai(self) -> List[Dict[str, Any]]:
        all_tools: List[Dict[str, Any]] = []
        for client in self.clients.values():
            all_tools.extend(client.get_tools_for_openai())
        return all_tools

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        """Call an MCP tool. Tool name format: mcp_<server>_<tool>"""
        if not tool_name.startswith("mcp_"):
            logger.error(f"Invalid MCP tool name: {tool_name}")
            return json.dumps({"error": f"Invalid MCP tool name: {tool_name}"})

        parts = tool_name[4:].split("_", 1)
        if len(parts) != 2:
            logger.error(f"Invalid MCP tool name format: {tool_name}")
            return json.dumps({"error": f"Invalid format: {tool_name}. Expected: mcp_<server>_<tool>"})

        server_name, actual_tool_name = parts
        if server_name not in self.clients:
            available = list(self.clients.keys())
            logger.error(f"MCP server '{server_name}' not found. Available: {available}")
            return json.dumps({"error": f"MCP server '{server_name}' not found. Available: {available}"})

        logger.info(f"Calling MCP tool: server={server_name}, tool={actual_tool_name}, args={arguments}")
        result = await self.clients[server_name].call_tool(actual_tool_name, arguments)
        if result is None:
            return json.dumps({"error": f"Tool {actual_tool_name} on {server_name} returned no result"})
        return result

    async def restart_server(self, server_name: str) -> bool:
        if server_name not in self.clients:
            logger.error(f"Cannot restart '{server_name}': not found. Available: {list(self.clients.keys())}")
            return False
        client = self.clients[server_name]
        command, args = client.command, client.args
        logger.info(f"Restarting MCP server: {server_name}")
        await client.stop()
        del self.clients[server_name]
        new_client = MCPClient(server_name, command, args)
        if await new_client.start():
            self.clients[server_name] = new_client
            logger.info(f"MCP server {server_name} restarted with {len(new_client.tools)} tools")
            return True
        logger.error(f"Failed to restart MCP server {server_name}")
        return False

    async def list_servers(self) -> List[Dict[str, Any]]:
        result = []
        for name, client in self.clients.items():
            is_running = (
                client.process is not None
                and client.process.returncode is None
            )
            result.append({
                "name": name,
                "status": "running" if is_running else "stopped",
                "tools": len(client.tools),
            })
        return result

    async def stop_all(self):
        for client in self.clients.values():
            await client.stop()
        self.clients.clear()