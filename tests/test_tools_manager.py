import asyncio

from tools.manager import ToolManager


def test_tool_manager_registers_tools() -> None:
    manager = ToolManager()
    ok = asyncio.run(manager.initialize())

    assert ok is True
    assert len(manager.tools) > 10
    assert "get_current_time" in manager.tool_handlers
    assert "read_file" in manager.tool_handlers

