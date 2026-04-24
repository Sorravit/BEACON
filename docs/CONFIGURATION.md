# ⚙️ Configuration Guide

All configuration constants are now at the **top of [`main.py`](main.py)** for easy access.

---

## 📍 Location

**File:** [`main.py`](main.py)  
**Lines:** 22-50 (Configuration Constants section)

---

## 🎛️ Available Settings

### **1. MAX_TOOL_ITERATIONS**

**What it does:** Controls how many tool calls the AI can make per user message.

**Default:** `1000`

**Recommended values:**
```python
MAX_TOOL_ITERATIONS = 1000   # ~8-10 hours (default, good for most courses)
MAX_TOOL_ITERATIONS = 2000   # ~16-20 hours (very long courses)
MAX_TOOL_ITERATIONS = 5000   # ~40-50 hours (multi-day tasks)
MAX_TOOL_ITERATIONS = 10000  # ~80-100 hours (extreme cases)
```

**When to change:**
- Your course has 200+ videos → Use `2000`
- Multi-day automation tasks → Use `5000`
- You keep hitting the limit → Increase by 1000

**How to change:**
```python
# In main.py, line ~44:
MAX_TOOL_ITERATIONS = 2000  # Change this number
```

---

### **2. AI Model Configuration**

**DEFAULT_MODEL**
```python
DEFAULT_MODEL = "gpt-3.5-turbo"  # Fast, cost-effective
# DEFAULT_MODEL = "gpt-4"        # More capable, slower, more expensive
```

**DEFAULT_TEMPERATURE**
```python
DEFAULT_TEMPERATURE = 0.7  # Balanced creativity (0.0 = deterministic, 1.0 = creative)
```

**DEFAULT_MAX_TOKENS**
```python
DEFAULT_MAX_TOKENS = 2000  # Maximum response length
```

---

### **3. Logging**

**LOG_FILE**
```python
LOG_FILE = "logs/ai_assistant.log"  # Where logs are saved
```

**To change log level:**
```python
# In main.py, line ~56:
logging.basicConfig(
    level=logging.INFO,  # Change to DEBUG for more details
    ...
)
```

---

### **4. MCP Configuration**

**MCP_CONFIG_FILE**
```python
MCP_CONFIG_FILE = "mcp_config.json"  # MCP servers configuration
```

---

## 🎯 Common Configuration Scenarios

### **Scenario 1: Very Long Course (200+ videos)**

```python
# In main.py:
MAX_TOOL_ITERATIONS = 2000  # Increase from 1000
```

### **Scenario 2: Multi-Day Automation**

```python
# In main.py:
MAX_TOOL_ITERATIONS = 5000  # Allow up to ~40-50 hours
```

### **Scenario 3: More Detailed Logging**

```python
# In main.py:
logging.basicConfig(
    level=logging.DEBUG,  # Change from INFO to DEBUG
    ...
)
```

### **Scenario 4: Use GPT-4 for Better Results**

```python
# In main.py:
DEFAULT_MODEL = "gpt-4"  # Change from gpt-3.5-turbo
```

---

## 📊 Tool Iterations Calculator

| Videos | Avg Length | Estimated Tool Calls | Recommended Setting |
|--------|-----------|---------------------|---------------------|
| 10 | 5 min | ~50 | 1000 (default) |
| 50 | 10 min | ~250 | 1000 (default) |
| 100 | 5 min | ~500 | 1000 (default) |
| 200 | 10 min | ~1000 | 2000 |
| 500 | 5 min | ~2500 | 5000 |

**Formula:** `Tool Calls ≈ Number of Videos × 5`

---

## 🔧 How to Apply Changes

### **Step 1: Edit main.py**

```bash
# Open in your editor
code main.py

# Or use vim/nano
vim main.py
```

### **Step 2: Find the Configuration Section**

Look for this at the top of the file:
```python
# ============================================================================
# CONFIGURATION CONSTANTS - Modify these to customize behavior
# ============================================================================
```

### **Step 3: Change the Value**

```python
# Change this:
MAX_TOOL_ITERATIONS = 1000

# To this:
MAX_TOOL_ITERATIONS = 2000
```

### **Step 4: Save and Restart**

```bash
# Save the file (Ctrl+S or :wq in vim)

# Restart the application
python main.py
```

**That's it!** Your changes are now active.

---

## ⚠️ Important Notes

### **About MAX_TOOL_ITERATIONS:**

- **Higher is not always better** - Each tool call takes time
- **1000 is good for most use cases** - Covers ~8-10 hours
- **Only increase if you hit the limit** - You'll see a message if you do
- **You can always continue** - If you hit the limit, just say "continue"

### **About Model Selection:**

- **gpt-3.5-turbo** - Fast, cheap, good for most tasks
- **gpt-4** - Better reasoning, slower, more expensive
- **For courses** - gpt-3.5-turbo is usually sufficient

### **About Logging:**

- **INFO** - Normal operation (recommended)
- **DEBUG** - Detailed information (for troubleshooting)
- **WARNING** - Only warnings and errors (minimal)

---

## 🎯 Quick Reference

**To change max iterations:**
```python
# main.py, line ~44
MAX_TOOL_ITERATIONS = 2000  # Your value here
```

**To change AI model:**
```python
# main.py, line ~38
DEFAULT_MODEL = "gpt-4"  # Your model here
```

**To enable debug logging:**
```python
# main.py, line ~56
level=logging.DEBUG,  # Change from INFO
```

---

## ✅ Summary

All important settings are now at the **top of [`main.py`](main.py)** in the **Configuration Constants** section.

**Most common change:** Increase `MAX_TOOL_ITERATIONS` for longer tasks.

**Default settings work for:** Most courses up to 50-100 videos (~8-10 hours).

**Need help?** Check [`LONG_RUNNING_TASKS_GUIDE.md`](LONG_RUNNING_TASKS_GUIDE.md) for more details.