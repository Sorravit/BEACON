#!/usr/bin/env python3
"""
Test Autonomous Agent Mode with Real Task
"""
import asyncio
import sys
from agent_executor import AgentExecutor
from main import AIAgent, Config

async def test_autonomous_agent():
    """Test autonomous agent with volcanic activity search task"""
    print("="*60)
    print("Testing Autonomous Agent Mode")
    print("="*60)
    
    # Create config
    config = Config()
    config.enable_tools = True
    
    # Create and initialize AI agent
    agent = AIAgent(config)
    if not await agent.initialize():
        print("❌ Failed to initialize AI agent")
        return False
    
    print("\n✅ AI Agent initialized")
    print(f"✅ Tools available: {agent.tools_available}")
    
    # Create executor
    executor = AgentExecutor(agent, max_steps=10, max_retries=2)
    
    # Create task
    task_description = "Search for latest update regarding volcanic activity in Indonesia for this year"
    
    print(f"\n📋 Task: {task_description}")
    print("\n" + "="*60)
    print("Executing Task...")
    print("="*60 + "\n")
    
    # Execute task
    task = await executor.execute_task(task_description)
    
    print("\n" + "="*60)
    print("Task Execution Complete")
    print("="*60)
    
    # Display results
    print(f"\n✅ Task Status: {task.status.value}")
    print(f"📊 Steps Completed: {len(task.steps)}")
    
    if task.result:
        print(f"\n📝 Task Result:")
        print("-"*60)
        print(task.result)
        print("-"*60)
    
    # Show steps
    print(f"\n📋 Execution Steps:")
    for i, step in enumerate(task.steps, 1):
        print(f"\n  Step {i}: {step.description}")
        print(f"  Status: {step.status.value}")
        if step.tool_name:
            print(f"  Tool: {step.tool_name}")
        if step.result:
            result_preview = step.result[:200] + "..." if len(step.result) > 200 else step.result
            print(f"  Result: {result_preview}")
    
    # Cleanup
    if agent.mcp_manager:
        await agent.mcp_manager.stop_all()
    
    success = task.status.value == "completed"
    return success

if __name__ == "__main__":
    try:
        result = asyncio.run(test_autonomous_agent())
        sys.exit(0 if result else 1)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)