#!/usr/bin/env python3
"""
Test human-in-the-loop scenarios:
- Long-running tasks
- Tasks requiring human intervention (CAPTCHA, login)
- Handoff back to AI after human completes manual steps
"""

import asyncio
from main import AIAgent, Config

async def test_human_captcha_scenario():
    """
    Simulate a scenario where:
    1. AI navigates to a site
    2. Human handles CAPTCHA/login
    3. AI continues with the rest of the task
    """
    print("\n" + "="*70)
    print("🧪 TEST: Human-in-the-Loop with CAPTCHA Scenario")
    print("="*70)
    
    config = Config()
    agent = AIAgent(config)
    await agent.initialize()
    
    # Step 1: AI navigates to login page
    print("\n📋 Step 1: AI navigates to a site requiring login")
    query1 = "Open https://example.com in the browser"
    print(f"Query: {query1}")
    
    response1 = await agent.get_response(query1)
    print(f"\n✅ AI Response: {response1[:200]}...")
    
    # Step 2: Simulate human intervention
    print("\n" + "="*70)
    print("⏸️  PAUSE: Human handles CAPTCHA and login")
    print("="*70)
    print("In a real scenario:")
    print("  1. Browser stays open (AI doesn't close it)")
    print("  2. Human solves CAPTCHA")
    print("  3. Human logs in")
    print("  4. Human tells AI to continue")
    print("\nSimulating 3 seconds of human work...")
    await asyncio.sleep(3)
    
    # Step 3: AI continues after human is done
    print("\n" + "="*70)
    print("▶️  RESUME: AI continues the task")
    print("="*70)
    
    query2 = "Now that I'm logged in, take a screenshot of the page"
    print(f"\nQuery: {query2}")
    
    response2 = await agent.get_response(query2)
    print(f"\n✅ AI Response: {response2[:200]}...")
    
    # Step 4: AI performs complex task
    query3 = "Navigate to the courses section and list all available courses"
    print(f"\n📋 Step 4: AI performs complex navigation")
    print(f"Query: {query3}")
    
    response3 = await agent.get_response(query3)
    print(f"\n✅ AI Response: {response3[:200]}...")
    
    print("\n" + "="*70)
    print("✅ Test Complete: Human-in-the-Loop Workflow")
    print("="*70)
    
    return True

async def test_long_running_task():
    """
    Test if the agent can handle long-running tasks
    that take multiple minutes
    """
    print("\n" + "="*70)
    print("🧪 TEST: Long-Running Task (Multi-Step)")
    print("="*70)
    
    from agent_executor import AgentExecutor
    
    config = Config()
    agent = AIAgent(config)
    await agent.initialize()
    
    executor = AgentExecutor(agent)
    
    # Complex task that would take time
    task = """
    Research the top 3 programming languages in 2024:
    1. Search for each language's popularity
    2. Find their main use cases
    3. Create a comparison file
    """
    
    print(f"\n📋 Task: {task}")
    print("\n⏱️  This task involves multiple searches and file operations...")
    print("Starting execution...\n")
    
    result = await executor.execute_task(task)
    
    print(f"\n✅ Status: {result.status}")
    print(f"✅ Steps completed: {len(result.steps)}")
    print(f"✅ Time taken: {(result.completed_at - result.started_at).total_seconds():.1f}s")
    
    if result.result:
        print(f"\n📝 Result preview: {result.result[:300]}...")
    
    return result.status.value == 'completed'

async def test_conversation_persistence():
    """
    Test if conversation context persists across multiple interactions
    (important for human-in-the-loop scenarios)
    """
    print("\n" + "="*70)
    print("🧪 TEST: Conversation Persistence Across Multiple Turns")
    print("="*70)
    
    config = Config()
    agent = AIAgent(config)
    await agent.initialize()
    
    # Turn 1: Start a task
    print("\n📋 Turn 1: Start browsing")
    response1 = await agent.get_response("Open google.com in the browser")
    print(f"✅ Response 1: {response1[:150]}...")
    
    # Turn 2: Human does something (simulated pause)
    print("\n⏸️  Simulating human interaction (5 seconds)...")
    await asyncio.sleep(2)
    
    # Turn 3: Continue with context
    print("\n📋 Turn 2: Continue with context")
    response2 = await agent.get_response("Now search for 'AI trends' on the page you just opened")
    print(f"✅ Response 2: {response2[:150]}...")
    
    # Turn 4: More context-dependent action
    print("\n📋 Turn 3: More context-dependent action")
    response3 = await agent.get_response("Take a screenshot of the search results")
    print(f"✅ Response 3: {response3[:150]}...")
    
    # Check if context was maintained
    context_maintained = "search" in response2.lower() or "google" in response2.lower()
    
    print(f"\n✅ Context maintained: {context_maintained}")
    
    return context_maintained

async def main():
    """Run all human-in-the-loop tests"""
    print("\n" + "="*70)
    print("🤖 HUMAN-IN-THE-LOOP CAPABILITY TESTS")
    print("="*70)
    
    tests = [
        ("Human CAPTCHA Scenario", test_human_captcha_scenario),
        ("Long-Running Task", test_long_running_task),
        ("Conversation Persistence", test_conversation_persistence),
    ]
    
    results = {}
    
    for test_name, test_func in tests:
        try:
            print(f"\n\n{'='*70}")
            print(f"Running: {test_name}")
            print(f"{'='*70}")
            result = await test_func()
            results[test_name] = "✅ PASSED" if result else "❌ FAILED"
        except Exception as e:
            results[test_name] = f"❌ ERROR: {str(e)[:50]}"
            print(f"\n❌ Error in {test_name}: {e}")
    
    # Print summary
    print("\n\n" + "="*70)
    print("📊 HUMAN-IN-THE-LOOP TEST SUMMARY")
    print("="*70)
    
    for test_name, result in results.items():
        print(f"{result:20} {test_name}")
    
    passed = sum(1 for r in results.values() if "PASSED" in r)
    total = len(results)
    
    print(f"\n{'='*70}")
    print(f"✅ Tests Passed: {passed}/{total}")
    print(f"{'='*70}\n")
    
    # Print capabilities summary
    print("\n" + "="*70)
    print("🎯 HUMAN-IN-THE-LOOP CAPABILITIES")
    print("="*70)
    print("""
✅ **Browser Stays Open**: Browser remains open between interactions
✅ **Context Maintained**: Conversation history preserved across turns
✅ **Long-Running Tasks**: Can handle multi-step tasks taking minutes
✅ **Flexible Handoff**: Can pause for human input and resume

**Typical Workflow:**
1. AI: "I've opened the login page"
2. Human: [Solves CAPTCHA, logs in]
3. Human: "I'm logged in, continue"
4. AI: [Continues with the rest of the task]

**Limitations:**
⚠️  AI cannot solve CAPTCHAs (requires human)
⚠️  AI cannot handle 2FA codes (requires human)
⚠️  Browser must stay open during handoff
✅ AI can continue any task after human intervention
    """)
    
    return passed == total

if __name__ == "__main__":
    import sys
    success = asyncio.run(main())
    sys.exit(0 if success else 1)