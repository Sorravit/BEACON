# Agent API Reference

For programmatic usage of the agent.

## Basic Usage

```python
import asyncio
from agent_api import run_agent_task

async def main():
    result = await run_agent_task("Your task description")
    print(result)

asyncio.run(main())
```

## Agent Builder

```python
from agent_api import AgentBuilder

api = await AgentBuilder.quick_start()
result = await api.run_task("Your task")
```

## API Methods

### `run_agent_task(description)`
Run a single task and return results.

### `AgentBuilder.quick_start()`
Create an agent with default configuration.

### `api.run_task(description)`
Execute a task with the agent.

### `api.run_task_async(description)`
Start a task asynchronously, returns task_id.

### `api.get_task_status(task_id)`
Get status of a running task.

## Examples

See `examples/example_agent_usage.py` for complete examples.

## Modes

**Chat Mode** (`python main.py`):
- Interactive conversation
- Manual control

**Agent Mode** (`python scripts/run_agent.py "task"`):
- Autonomous execution
- Multi-step planning

See [Getting Started](GETTING_STARTED.md) for details.