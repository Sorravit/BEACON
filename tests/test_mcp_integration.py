#!/usr/bin/env python3
"""
Test MCP Integration with Main Application
"""
import asyncio
import sys
from main import AIAgent, Config

async def test_mcp_integration():
    """Test that MCP tools are properly integrated"""
    print("="*60)
    print("Testing MCP Integration")
    print("="*60)
    
    # Create config
    config = Config()
    config.enable_tools = True
    
    # Initialize agent
    agent = AIAgent(config)
    success = await agent.initialize()
    
    if not success:
        print("❌ Failed to initialize agent")
        return False
    
    print(f"\n✅ Agent initialized successfully")
    
    # Check built-in tools
    if agent.tools:
        builtin_count = len(agent.tools.tools)
        print(f"✅ Built-in tools: {builtin_count}")
    else:
        print("⚠️  No built-in tools")
        builtin_count = 0
    
    # Check MCP tools
    if agent.mcp_manager:
        mcp_tools = agent.mcp_manager.get_all_tools_for_openai()
        mcp_count = len(mcp_tools)
        print(f"✅ MCP tools: {mcp_count}")
        
        # List first 5 MCP tools
        if mcp_tools:
            print("\nFirst 5 MCP tools:")
            for i, tool in enumerate(mcp_tools[:5]):
                print(f"  {i+1}. {tool['function']['name']}")
    else:
        print("⚠️  No MCP manager")
        mcp_count = 0
    
    total_tools = builtin_count + mcp_count
    print(f"\n📊 Total tools available: {total_tools}")
    
    # Test that tools are properly combined in get_response
    print("\n" + "="*60)
    print("Testing Tool Combination in API Call")
    print("="*60)
    
    # Simulate what get_response does
    all_tools = []
    if agent.tools_available and agent.tools:
        all_tools.extend(agent.tools.tools)
    if agent.mcp_manager:
        all_tools.extend(agent.mcp_manager.get_all_tools_for_openai())
    
    print(f"✅ Combined tools for API: {len(all_tools)}")
    
    # Verify tool names don't conflict
    tool_names = [t['function']['name'] for t in all_tools]
    unique_names = set(tool_names)
    
    if len(tool_names) == len(unique_names):
        print(f"✅ All tool names are unique")
    else:
        print(f"⚠️  Warning: {len(tool_names) - len(unique_names)} duplicate tool names")
        duplicates = [name for name in tool_names if tool_names.count(name) > 1]
        print(f"   Duplicates: {set(duplicates)}")
    
    # Cleanup
    if agent.mcp_manager:
        await agent.mcp_manager.stop_all()
    
    print("\n" + "="*60)
    print("✅ MCP Integration Test Complete")
    print("="*60)
    
    return True

if __name__ == "__main__":
    try:
        result = asyncio.run(test_mcp_integration())
        sys.exit(0 if result else 1)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)