#!/usr/bin/env python3
"""
Convenience wrapper for running the AI Assistant
Usage: python run.py [task description]
"""
import sys
import os

# Add scripts directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts'))

if len(sys.argv) > 1:
    # Run interactive task mode
    from run_interactive_task import main
    sys.exit(main())
else:
    # Run regular chat mode
    import asyncio
    from main import main as chat_main
    sys.exit(asyncio.run(chat_main()))