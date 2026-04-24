# Documentation

## Quick Links

- **[Getting Started](GETTING_STARTED.md)** - Installation and basic usage
- **[Multi-Agent System](MULTI_AGENT_SYSTEM.md)** - 🆕 Collaborative AI agents
- **[Configuration](CONFIGURATION.md)** - Settings and customization  
- **[Troubleshooting](TROUBLESHOOTING.md)** - Common issues
- **[API Reference](API_REFERENCE.md)** - Agent API documentation
- **[Auto-Continue Guide](AUTO_CONTINUE_GUIDE.md)** - Automated long-running tasks

## Quick Start

```bash
# Install
source venv/bin/activate

# Single agent modes
python main.py                           # Chat mode
python scripts/run_interactive_task.py "task"   # Task mode (with human intervention)
python scripts/run_agent.py "your task" # Agent mode

# Multi-agent collaboration 🆕
python collaborate.py "your task"        # Quick collaboration
python multi_agent_system.py             # Interactive mode
```

## What's New

### Multi-Agent Collaboration System 🆕
Have multiple AI agents with different roles collaborate on tasks:
- Solution Architect designs architecture
- Tech Lead coordinates implementation
- Developer handles coding details
- QA Engineer ensures quality

See [Multi-Agent System](MULTI_AGENT_SYSTEM.md) for details.

## Configuration

Edit `main.py` lines 22-50 for settings.

## Need Help?

1. Check [Troubleshooting](TROUBLESHOOTING.md)
2. Review [Getting Started](GETTING_STARTED.md)
3. Check logs: `tail -f logs/ai_assistant.log`
