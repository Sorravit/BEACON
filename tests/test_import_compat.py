def test_root_wrapper_imports() -> None:
    import api.agent_api as agent_api
    import api.agent_executor as agent_executor

    assert hasattr(agent_api, "AgentAPI")
    assert hasattr(agent_executor, "AgentExecutor")


def test_core_wrapper_imports() -> None:
    import core.mcp_client as legacy_mcp
    import core.vector_memory as legacy_vector
    import core.agent_memory as legacy_memory

    assert hasattr(legacy_mcp, "MCPManager")
    assert hasattr(legacy_vector, "VectorMemory")
    assert hasattr(legacy_memory, "AgentMemory")
