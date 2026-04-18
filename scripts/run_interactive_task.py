#!/usr/bin/env python3
"""
Interactive task runner - keeps browser open for human intervention
Usage: python run_interactive_task.py "Your task description"

This is designed for tasks that require human intervention (like CAPTCHA/login)
The browser stays open and you can interact with the AI to continue the task.
"""

import sys
import asyncio
from pathlib import Path

# Add parent directory to path so we can import from root
sys.path.insert(0, str(Path(__file__).parent.parent))

from main import AIAgent, Config


async def main():
    if len(sys.argv) < 2:
        print("Usage: python run_interactive_task.py \"Your task description\"")
        print("\nExample:")
        print('  python run_interactive_task.py "Open udemy.com and complete my course"')
        print("\nThe browser will stay open. You can:")
        print("  - Handle CAPTCHA/login manually")
        print("  - Type 'continue' when ready for AI to proceed")
        print("  - Type 'quit' to exit")
        sys.exit(1)
    
    task = " ".join(sys.argv[1:])
    
    # Initialize agent
    print("🤖 Initializing AI Agent...")
    config = Config()
    agent = AIAgent(config)
    
    if not await agent.initialize():
        print("❌ Failed to initialize agent")
        sys.exit(1)
    
    print(f"\n{'='*70}")
    print(f"📋 Initial Task: {task}")
    print(f"{'='*70}\n")
    
    # Start the task
    print("🔄 AI working on initial task...\n")
    response = await agent.get_response(task)
    
    if response:
        print(f"✅ AI: {response}\n")
    else:
        print("❌ Failed to get response\n")
    
    # Interactive loop - keep browser open
    print(f"\n{'='*70}")
    print("🎮 INTERACTIVE MODE")
    print(f"{'='*70}")
    print("The browser is still open. You can now:")
    print("  - Handle any CAPTCHA or login manually")
    print("  - Type your next instruction for the AI")
    print("  - Type 'quit' or 'exit' to close")
    print(f"{'='*70}\n")
    
    while True:
        try:
            user_input = input("👤 You: ").strip()
            
            if not user_input:
                continue
            
            if user_input.lower() in ['quit', 'exit', 'q']:
                print("\n👋 Closing browser and exiting...")
                # Close browser if open
                if agent.tools and agent.tools.browser:
                    await agent.tools.browser.close()
                break
            
            if user_input.lower() in ['help', 'h']:
                print("\n📖 Available commands:")
                print("  continue / c - Tell AI to continue the task")
                print("  screenshot - Take a screenshot")
                print("  status - Check current page status")
                print("  quit / exit - Close and exit")
                print("  Or just type any instruction for the AI\n")
                continue
            
            # Send to AI
            print("\n🔄 AI working...\n")
            response = await agent.get_response(user_input)
            
            if response:
                print(f"✅ AI: {response}\n")
            else:
                print("❌ Failed to get response\n")
                
        except (KeyboardInterrupt, EOFError):
            print("\n\n⚠️  Interrupted!")
            print("\nWhat would you like to do?")
            print("  1. Keep browser open and exit (you can resume later)")
            print("  2. Close browser and exit")
            print("  3. Continue session")
            
            try:
                choice = input("\nChoice (1/2/3): ").strip()
                
                if choice == "1":
                    print("\n✅ Browser will stay open. You can resume by running this script again.")
                    print("⚠️  Note: The MCP server will stop, but browser tabs remain open.")
                    break
                elif choice == "2":
                    print("\n👋 Closing browser and exiting...")
                    if agent.tools and agent.tools.browser:
                        await agent.tools.browser.close()
                    break
                elif choice == "3":
                    print("\n▶️  Continuing session...")
                    continue
                else:
                    print("\n✅ Keeping browser open by default.")
                    break
            except (KeyboardInterrupt, EOFError):
                print("\n✅ Keeping browser open. Exiting...")
                break
        except Exception as e:
            print(f"❌ Error: {e}\n")
    
    print("\n✅ Session ended")


if __name__ == "__main__":
    asyncio.run(main())