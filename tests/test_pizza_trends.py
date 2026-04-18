#!/usr/bin/env python3
"""
Test agent with pizza trends query
"""
import asyncio
from agent_executor import AgentExecutor
from main import AIAgent, Config

async def test_pizza_trends():
    """Test agent with pizza trends query"""
    print("="*60)
    print("Testing Agent Mode - Pizza Trends Query")
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
    executor = AgentExecutor(agent, max_steps=15, max_retries=2)
    
    # Execute task
    task_description = "Latest pizza trend in the world for each country"
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
            result_preview = step.result[:300] + "..." if len(step.result) > 300 else step.result
            print(f"     Result: {result_preview}")
    
    # Cleanup
    if agent.mcp_manager:
        await agent.mcp_manager.stop_all()
    
    return task

if __name__ == "__main__":
    asyncio.run(test_pizza_trends())