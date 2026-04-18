# Documentation

## Quick Links

- **[Getting Started](GETTING_STARTED.md)** - Installation and basic usage
- **[Configuration](CONFIGURATION.md)** - Settings and customization  
- **[Troubleshooting](TROUBLESHOOTING.md)** - Common issues
- **[API Reference](API_REFERENCE.md)** - Agent API documentation
- **[Auto-Continue Guide](AUTO_CONTINUE_GUIDE.md)** - Automated long-running tasks

## Quick Start

```bash
# Install
source venv/bin/activate

# Run chat mode
python main.py

# Run task
python run.py "your task"

# Run agent mode
python scripts/run_agent.py "your task"
```

## Configuration

Edit `main.py` lines 22-50 for settings.

## Need Help?

1. Check [Troubleshooting](TROUBLESHOOTING.md)
2. Review [Getting Started](GETTING_STARTED.md)
3. Check logs: `tail -f ai_assistant.log`