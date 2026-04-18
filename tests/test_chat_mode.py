#!/usr/bin/env python3
"""
Test chat mode with prompt-based tools
"""
import asyncio
from main import AIAgent, Config

async def test_chat():
    """Test chat mode with various queries"""
    print("="*60)
    print("Testing Chat Mode with Prompt-Based Tools")
    print("="*60)
    
    # Initialize
    config = Config()
    config.enable_tools = True
    agent = AIAgent(config)
    
    if not await agent.initialize():
        print("❌ Failed to initialize")
        return False
    
    print("\n✅ Agent initialized")
    print(f"✅ Tools available: {agent.tools_available}")
    
    # Test queries
    test_queries = [
        "What time is it?",
        "What's 2+2?",
        "List files in current directory"
    ]
    
    for i, query in enumerate(test_queries, 1):
        print(f"\n{'='*60}")
        print(f"Test {i}: {query}")
        print('='*60)
        
        response = await agent.get_response(query)
        
        print(f"\n📝 Response:")
        print("-"*60)
        print(response)
        print("-"*60)
        
        # Small delay between tests
        await asyncio.sleep(1)
    
    # Cleanup
    if agent.mcp_manager:
        await agent.mcp_manager.stop_all()
    
    print("\n" + "="*60)
    print("✅ All Chat Mode Tests Complete")
    print("="*60)
    
    return True

if __name__ == "__main__":
    try:
        result = asyncio.run(test_chat())
    except KeyboardInterrupt:
        print("\n\nTest interrupted")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()