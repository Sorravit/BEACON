# AI Assistant

**Version:** 4.2.0 | **Status:** Production Ready

AI Assistant for automating tasks, web automation, and long-running operations.

## Quick Start

```bash
# Setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.env .env
# Edit .env with your API credentials

# Run
python main.py
```

## Usage

```bash
# Chat mode - Interactive conversation
python main.py

# Task mode - Single task
python run.py "your task description"

# Agent mode - Autonomous execution  
python scripts/run_agent.py "your task description"
```

## Features

- 35+ Tools (browser automation, web search, file operations, system commands)
- MCP Protocol integration
- Playwright browser automation
- Conversation memory
- Long-running task support

## Documentation

- **[Getting Started](docs/GETTING_STARTED.md)** - Installation and basics
- **[Configuration](docs/CONFIGURATION.md)** - Settings
- **[Troubleshooting](docs/TROUBLESHOOTING.md)** - Common issues
- **[Full Documentation](docs/README.md)** - All docs

## Configuration

Edit `main.py` lines 22-50:

```python
MAX_TOOL_ITERATIONS = 1000        # ~8-10 hours
MAX_CONVERSATION_TOKENS = 150000  # Memory limit
DEFAULT_MODEL = "gpt-3.5-turbo"   # AI model
```

## Requirements

- Python 3.8+
- Node.js (for MCP servers)
- Virtual environment (recommended)

## Project Structure

```
ClineSandbox/
├── main.py                    # Main application
├── agent_api.py               # Agent API
├── agent_executor.py          # Autonomous executor
├── run.py                     # Task launcher
├── docs/                      # Documentation
├── scripts/                   # Helper scripts
└── tests/                     # Tests
```

---

**Version:** 4.2.0 | **Last Updated:** April 18, 2026