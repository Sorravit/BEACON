"""MCP management tool handlers."""


class MCPToolsMixin:
    async def _mcp_list_servers(self):
        if not self.mcp_manager:
            return "No MCP manager available."
        servers = await self.mcp_manager.list_servers()
        if not servers:
            return "No MCP servers registered."
        lines = [f"  {item['name']}: {item['status']} ({item['tools']} tools)" for item in servers]
        return "MCP Servers:\n" + "\n".join(lines)

    async def _mcp_restart_server(self, server_name: str):
        if not self.mcp_manager:
            return "No MCP manager available."
        ok = await self.mcp_manager.restart_server(server_name)
        if ok:
            servers = await self.mcp_manager.list_servers()
            info = next((item for item in servers if item["name"] == server_name), None)
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
        for server in servers:
            ok = await self.mcp_manager.restart_server(server["name"])
            results.append(f"  {server['name']}: {'✅ restarted' if ok else '❌ failed'}")
        return "MCP server restart results:\n" + "\n".join(results)

