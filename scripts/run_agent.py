#!/usr/bin/env python3
"""
Quick launcher for agent mode
Usage: python run_agent.py "Your task description here"
"""

import sys
import asyncio
from pathlib import Path

# Add parent directory to path so we can import from root
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_api import run_agent_task


async def main():
    if len(sys.argv) < 2:
        print("Usage: python run_agent.py \"Your task description\"")
        print("\nExamples:")
        print("  python run_agent.py \"What time is it?\"")
        print("  python run_agent.py \"Search for Python tutorials and summarize\"")
        print("  python run_agent.py \"List files in current directory\"")
        sys.exit(1)
    
    task = " ".join(sys.argv[1:])
    
    print(f"🤖 Running task: {task}\n")
    
    result = await run_agent_task(task)
    
    print("\n" + "="*60)
    print("RESULT")
    print("="*60)
    print(f"Status: {result['status']}")
    print(f"Success: {result['success']}")
    print(f"Steps: {result['steps_completed']}/{result['steps_total']}")
    if result.get('duration_seconds'):
        print(f"Duration: {result['duration_seconds']:.2f}s")
    print(f"\n{result.get('result', result.get('error', 'No result'))}")


if __name__ == "__main__":
    asyncio.run(main())