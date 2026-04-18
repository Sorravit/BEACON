#!/usr/bin/env python3
"""
Test improved web_search tool
"""
import asyncio
from main import AIAgent, Config

async def test_search():
    """Test web search"""
    print("="*60)
    print("Testing Improved Web Search")
    print("="*60)
    
    # Initialize
    config = Config()
    config.enable_tools = True
    agent = AIAgent(config)
    
    if not await agent.initialize():
        print("❌ Failed to initialize")
        return
    
    print("\n✅ Agent initialized")
    
    # Test search
    query = "latest pizza trends 2024"
    print(f"\n📋 Query: {query}")
    print("\n" + "="*60)
    
    response = await agent.get_response(f"Search for: {query}")
    
    print("\n📝 Response:")
    print("-"*60)
    print(response)
    print("-"*60)
    
    # Cleanup
    if agent.mcp_manager:
        await agent.mcp_manager.stop_all()
    
    return response

if __name__ == "__main__":
    asyncio.run(test_search())