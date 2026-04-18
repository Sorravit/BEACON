# Troubleshooting

## Common Issues

### "Maximum iterations reached"
**Cause:** Hit MAX_TOOL_ITERATIONS limit

**Fix:**
```python
# In main.py line ~43:
MAX_TOOL_ITERATIONS = 2000  # Increase from 1000
```

Or just say "continue" to the AI.

### "Prompt too long"
**Cause:** Too many tokens in conversation

**Fix:** Automatically handled by token trimming. If still occurs:
```python
# In main.py line ~51:
MAX_CONVERSATION_TOKENS = 180000  # Increase from 150000
```

### Browser closes when pressing Ctrl+C
**Cause:** Using wrong script

**Fix:** Use interactive scripts, not agent scripts for manual control.

### Session timeout
**Fix:** Refresh the page and continue manually.

### ModuleNotFoundError
**Fix:** 
```bash
source venv/bin/activate
pip install -r requirements.txt
```

### MCP server not starting
**Fix:**
```bash
# Check if Node.js is installed
node --version

# Restart the application
```

## Debug Tips

**Check logs:**
```bash
tail -f ai_assistant.log
```

**Test configuration:**
```bash
python main.py
# Try a simple command like "what time is it?"
```

**Verify environment:**
```bash
cat .env  # Should have API key
```

## Still Having Issues?

1. Check configuration in `main.py` lines 22-50
2. Review [Getting Started](GETTING_STARTED.md)
3. Check logs for specific errors