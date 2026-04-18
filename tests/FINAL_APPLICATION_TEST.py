#!/usr/bin/env python3
"""
Final comprehensive test of the AI Assistant application
Tests all major functionality with realistic scenarios
"""

import asyncio
import sys
from main import AIAgent, Config

async def test_chat_mode():
    """Test interactive chat mode"""
    print("\n" + "="*60)
    print("TEST 1: Chat Mode - Simple Query")
    print("="*60)
    
    config = Config()
    agent = AIAgent(config)
    await agent.initialize()
    
    query = "What time is it now?"
    print(f"\n📋 Query: {query}\n")
    
    response = await agent.get_response(query)
    print(f"✅ Response received: {response[:200]}...")
    
    await agent.cleanup()
    return True

async def test_tool_execution():
    """Test tool execution in chat mode"""
    print("\n" + "="*60)
    print("TEST 2: Tool Execution - Web Search")
    print("="*60)
    
    config = Config()
    agent = AIAgent(config)
    await agent.initialize()
    
    query = "Search for the latest AI trends in 2024"
    print(f"\n📋 Query: {query}\n")
    
    response = await agent.get_response(query)
    print(f"✅ Response received ({len(response)} chars)")
    print(f"Preview: {response[:300]}...")
    
    await agent.cleanup()
    return True

async def test_file_operations():
    """Test file read/write operations"""
    print("\n" + "="*60)
    print("TEST 3: File Operations")
    print("="*60)
    
    config = Config()
    agent = AIAgent(config)
    await agent.initialize()
    
    # Test write
    query = "Create a file called test_output.txt with the text 'Hello from AI Assistant'"
    print(f"\n📋 Query: {query}\n")
    
    response = await agent.get_response(query)
    print(f"✅ Write response: {response[:200]}...")
    
    # Test read
    query = "Read the file test_output.txt"
    print(f"\n📋 Query: {query}\n")
    
    response = await agent.get_response(query)
    print(f"✅ Read response: {response[:200]}...")
    
    await agent.cleanup()
    return True

async def test_agent_mode_simple():
    """Test agent mode with simple task"""
    print("\n" + "="*60)
    print("TEST 4: Agent Mode - Simple Task")
    print("="*60)
    
    from agent_executor import AgentExecutor
    
    config = Config()
    agent = AIAgent(config)
    await agent.initialize()
    
    executor = AgentExecutor(agent)
    
    task = "Create a file called agent_test.txt with today's date and a greeting"
    print(f"\n📋 Task: {task}\n")
    
    result = await executor.execute_task(task)
    
    print(f"\n✅ Status: {result.status}")
    print(f"✅ Steps completed: {len(result.steps)}")
    if result.result:
        print(f"✅ Result: {result.result[:200]}...")
    
    await agent.cleanup()
    return result.status.value == 'completed'

async def test_memory():
    """Test conversation memory"""
    print("\n" + "="*60)
    print("TEST 5: Conversation Memory")
    print("="*60)
    
    config = Config()
    agent = AIAgent(config)
    await agent.initialize()
    
    # First message
    query1 = "My favorite color is blue"
    print(f"\n📋 Query 1: {query1}")
    response1 = await agent.get_response(query1)
    print(f"✅ Response 1: {response1[:150]}...")
    
    # Second message referencing first
    query2 = "What did I just tell you about my favorite color?"
    print(f"\n📋 Query 2: {query2}")
    response2 = await agent.get_response(query2)
    print(f"✅ Response 2: {response2[:150]}...")
    
    # Check if memory works
    memory_works = "blue" in response2.lower()
    print(f"\n✅ Memory test: {'PASSED' if memory_works else 'FAILED'}")
    
    await agent.cleanup()
    return memory_works

async def test_mcp_tools():
    """Test MCP tool availability"""
    print("\n" + "="*60)
    print("TEST 6: MCP Tools")
    print("="*60)
    
    config = Config()
    agent = AIAgent(config)
    await agent.initialize()
    
    # Check MCP tools loaded
    mcp_tools = agent.mcp_manager.list_tools()
    print(f"\n✅ MCP tools loaded: {len(mcp_tools)}")
    print(f"✅ Sample tools: {', '.join(list(mcp_tools.keys())[:5])}...")
    
    await agent.cleanup()
    return len(mcp_tools) > 0

async def run_all_tests():
    """Run all tests and generate report"""
    print("\n" + "="*70)
    print("🧪 AI ASSISTANT - COMPREHENSIVE TEST SUITE")
    print("="*70)
    
    tests = [
        ("Chat Mode", test_chat_mode),
        ("Tool Execution", test_tool_execution),
        ("File Operations", test_file_operations),
        ("Agent Mode", test_agent_mode_simple),
        ("Memory", test_memory),
        ("MCP Tools", test_mcp_tools),
    ]
    
    results = {}
    
    for test_name, test_func in tests:
        try:
            result = await test_func()
            results[test_name] = "✅ PASSED" if result else "❌ FAILED"
        except Exception as e:
            results[test_name] = f"❌ ERROR: {str(e)[:50]}"
            print(f"\n❌ Error in {test_name}: {e}")
    
    # Print summary
    print("\n" + "="*70)
    print("📊 TEST SUMMARY")
    print("="*70)
    
    for test_name, result in results.items():
        print(f"{result:20} {test_name}")
    
    passed = sum(1 for r in results.values() if "PASSED" in r)
    total = len(results)
    
    print(f"\n{'='*70}")
    print(f"✅ Tests Passed: {passed}/{total}")
    print(f"{'='*70}\n")
    
    return passed == total

if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)