#!/usr/bin/env python3
"""
Example Agent Usage - Demonstrates how to use the agent programmatically
"""

import asyncio
from agent_api import AgentBuilder, run_agent_task, run_agent_workflow


async def example_simple_task():
    """Example 1: Run a simple task"""
    print("=" * 60)
    print("Example 1: Simple Task Execution")
    print("=" * 60)
    
    result = await run_agent_task("What time is it right now?")
    
    print(f"\nTask Status: {result['status']}")
    print(f"Success: {result['success']}")
    print(f"Result: {result['result']}")
    print(f"Duration: {result['duration_seconds']:.2f}s")


async def example_complex_task():
    """Example 2: Run a complex multi-step task"""
    print("\n" + "=" * 60)
    print("Example 2: Complex Multi-Step Task")
    print("=" * 60)
    
    # Build agent
    api = await AgentBuilder.quick_start()
    
    # Run complex task
    result = await api.run_task(
        "Search for the latest news about AI agents and summarize the top 3 results"
    )
    
    print(f"\nTask ID: {result['task_id']}")
    print(f"Status: {result['status']}")
    print(f"Steps Completed: {result['steps_completed']}/{result['steps_total']}")
    print(f"Result: {result['result']}")
    
    # Get detailed report
    report = api.get_task_report(result['task_id'])
    print("\n" + "=" * 60)
    print("Detailed Report:")
    print("=" * 60)
    print(report)


async def example_workflow():
    """Example 3: Run a workflow of multiple tasks"""
    print("\n" + "=" * 60)
    print("Example 3: Sequential Workflow")
    print("=" * 60)
    
    tasks = [
        "Get the current time",
        "List files in the current directory",
        "Read the README.md file"
    ]
    
    results = await run_agent_workflow(tasks, sequential=True)
    
    for i, result in enumerate(results, 1):
        print(f"\nTask {i}: {result['description']}")
        print(f"  Status: {result['status']}")
        print(f"  Success: {result['success']}")
        if result.get('result'):
            print(f"  Result: {result['result'][:100]}...")


async def example_async_task():
    """Example 4: Run task asynchronously"""
    print("\n" + "=" * 60)
    print("Example 4: Asynchronous Task Execution")
    print("=" * 60)
    
    api = await AgentBuilder.quick_start()
    
    # Start task asynchronously
    task_id = await api.run_task_async(
        "Search for information about Python async programming"
    )
    
    print(f"Task started with ID: {task_id}")
    print("Checking status...")
    
    # Poll for completion
    import time
    for i in range(10):
        await asyncio.sleep(2)
        status = api.get_task_status(task_id)
        
        if status:
            print(f"  [{i+1}] Status: {status['status']} - "
                  f"Steps: {status['steps_completed']}/{status['steps_total']}")
            
            if status['status'] in ['completed', 'failed']:
                break
    
    # Get final result
    final_status = api.get_task_status(task_id)
    if final_status:
        print(f"\nFinal Status: {final_status['status']}")
        print(f"Result: {final_status.get('result', 'N/A')}")


async def example_file_operations():
    """Example 5: File operations"""
    print("\n" + "=" * 60)
    print("Example 5: File Operations")
    print("=" * 60)
    
    api = await AgentBuilder.quick_start()
    
    # Create a test file
    result = await api.run_task(
        "Create a file called 'agent_test.txt' with the content 'Hello from the agent!'"
    )
    
    print(f"Create file: {result['status']}")
    
    # Read the file
    result = await api.run_task("Read the file 'agent_test.txt'")
    print(f"Read file: {result['status']}")
    print(f"Content: {result['result']}")


async def example_web_automation():
    """Example 6: Web automation"""
    print("\n" + "=" * 60)
    print("Example 6: Web Automation")
    print("=" * 60)
    
    api = await AgentBuilder.quick_start()
    
    # Open a website and take a screenshot
    result = await api.run_task(
        "Open google.com in the browser and take a screenshot"
    )
    
    print(f"Status: {result['status']}")
    print(f"Result: {result['result']}")
    
    # Close browser
    await api.run_task("Close the browser")


async def example_monitoring():
    """Example 7: Task monitoring and reporting"""
    print("\n" + "=" * 60)
    print("Example 7: Task Monitoring")
    print("=" * 60)
    
    api = await AgentBuilder.quick_start()
    
    # Run several tasks
    tasks = [
        "Get current time",
        "List files in current directory",
        "Search for Python tutorials"
    ]
    
    for task_desc in tasks:
        await api.run_task(task_desc)
    
    # List all tasks
    all_tasks = api.list_tasks()
    
    print(f"\nTotal tasks executed: {len(all_tasks)}")
    print("\nTask Summary:")
    for task in all_tasks:
        print(f"  - {task['task_id']}: {task['description'][:50]}... [{task['status']}]")


async def main():
    """Run all examples"""
    print("\n" + "=" * 60)
    print("AGENT USAGE EXAMPLES")
    print("=" * 60)
    print("\nThese examples demonstrate how to use the agent programmatically")
    print("instead of just chatting with it.\n")
    
    try:
        # Run examples
        await example_simple_task()
        await example_complex_task()
        await example_workflow()
        # await example_async_task()  # Uncomment to test async
        # await example_file_operations()  # Uncomment to test file ops
        # await example_web_automation()  # Uncomment to test web automation
        # await example_monitoring()  # Uncomment to test monitoring
        
        print("\n" + "=" * 60)
        print("Examples completed!")
        print("=" * 60)
        
    except Exception as e:
        print(f"\nError running examples: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())