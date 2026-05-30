from core.agent_memory import AgentMemory


def test_agent_memory_remember_and_recall(tmp_path) -> None:
    memory = AgentMemory(memory_dir=str(tmp_path / "memory"))
    memory.remember("stack", "python", persistent=True)

    assert memory.recall("stack") == "python"


def test_agent_memory_context_stack(tmp_path) -> None:
    memory = AgentMemory(memory_dir=str(tmp_path / "memory"))
    memory.push_context("task")
    memory.push_context("step1")

    assert memory.get_current_context() == "task > step1"
    assert memory.pop_context() == "step1"
    assert memory.get_current_context() == "task"

