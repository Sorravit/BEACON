# Getting Started

## Installation

```bash
cd /Users/sorravit/sandbox/ClineSandbox
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.env .env
# Edit .env with your API key
```

## Basic Usage

### Chat Mode
```bash
python main.py
```
Interactive conversation with the AI.

### Task Mode (with Human Intervention Support)
```bash
python scripts/run_interactive_task.py "your task description"
```
Single task execution. The browser stays open so you can handle CAPTCHA, login, or other manual steps as needed. Perfect for tasks requiring human intervention.

### Agent Mode
```bash
python scripts/run_agent.py "your task description"
```
Autonomous multi-step execution.

## Configuration

Edit `main.py` lines 22-50:

```python
MAX_TOOL_ITERATIONS = 1000        # Max tool calls per message
MAX_CONVERSATION_TOKENS = 150000  # Conversation memory limit  
DEFAULT_MODEL = "gpt-3.5-turbo"   # AI model
```

## Common Issues

**"Maximum iterations reached"**
- Increase `MAX_TOOL_ITERATIONS` in main.py
- Or just continue: say "continue" to the AI

**"Prompt too long"**
- Automatically handled by token trimming
- Adjust `MAX_CONVERSATION_TOKENS` if needed

**Session timeout**
- Refresh page and continue manually

## Next Steps

- Read [Configuration](CONFIGURATION.md) for detailed settings
- Check [Troubleshooting](TROUBLESHOOTING.md) for issues