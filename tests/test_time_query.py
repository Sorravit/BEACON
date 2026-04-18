#!/usr/bin/env python3
"""
Test agent mode with simple time query
"""
import asyncio
from agent_executor import AgentExecutor
from main import AIAgent, Config

async def test_time_query():
    """Test agent with time query"""
    print("="*60)
    print("Testing Agent Mode - Time Query")
    print("="*60)
    
    # Initialize
    config = Config()
    config.enable_tools = True
    agent = AIAgent(config)
    
    if not await agent.initialize():
        print("❌ Failed to initialize")
        return
    
    print("\n✅ Agent initialized")
    
    # Create executor
    executor = AgentExecutor(agent, max_steps=5, max_retries=2)
    
    # Execute task
    task_description = "What time is it?"
    print(f"\n📋 Task: {task_description}")
    print("\n" + "="*60)
    
    task = await executor.execute_task(task_description)
    
    print("\n" + "="*60)
    print("Results")
    print("="*60)
    print(f"\nStatus: {task.status.value}")
    print(f"Steps: {len(task.steps)}")
    
    if task.result:
        print(f"\n📝 Result:")
        print("-"*60)
        print(task.result)
        print("-"*60)
    
    # Show steps
    print(f"\n📋 Steps:")
    for i, step in enumerate(task.steps, 1):
        print(f"\n  {i}. {step.description}")
        print(f"     Tool: {step.tool_name}")
        print(f"     Status: {step.status.value}")
        if step.result:
            print(f"     Result: {step.result[:200]}")
    
    # Cleanup
    if agent.mcp_manager:
        await agent.mcp_manager.stop_all()
    
    return task

if __name__ == "__main__":
    asyncio.run(test_time_query())