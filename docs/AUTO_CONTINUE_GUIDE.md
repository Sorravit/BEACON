# 🔄 Auto-Continue Course Completion Guide

## Overview

This guide now uses one script to automatically continue your course completion every hour, but only when the previous run has finished.

---

## 📋 Script Available

### Shell Script
**File:** [`scripts/auto_continue_course.sh`](scripts/auto_continue_course.sh)
- Lightweight loop-based automation
- Checks every hour (configurable)
- Uses lockfile detection to avoid duplicate runs
- Logs activity to `course_completion.log`

---

## 🚀 Quick Start

```bash
# Run with default course URL
./scripts/auto_continue_course.sh

# Or specify custom course URL
./scripts/auto_continue_course.sh "https://ibmdt.udemy.com/course/your-course/"
```

---

## 🎯 How It Works

### Workflow

```
Start Script
    ↓
Check if previous run is active
    ↓
┌─────────────────┐
│ Is Running?     │
└─────────────────┘
    ↓           ↓
   YES         NO
    ↓           ↓
  Wait      Start New Run
    ↓           ↓
    └───────────┘
         ↓
    Wait 1 Hour
         ↓
    (Repeat)
```

### Key Features

1. **Smart Detection**
   - Checks if previous process is still running
   - Uses lockfile (`/tmp/course_completion.lock`)
   - Removes stale lockfile automatically

2. **Automatic Retry**
   - Checks every hour by default (`CHECK_INTERVAL=3600`)
   - Starts new run only when previous finished
   - Continues indefinitely until stopped

3. **Logging**
   - All activity logged to `course_completion.log`
   - Timestamps for every action
   - Easy to monitor progress

4. **Graceful Shutdown**
   - Press `Ctrl+C` to stop
   - Safe to restart anytime

---

## ⚙️ Configuration

### Change Check Interval

```bash
# Edit scripts/auto_continue_course.sh
CHECK_INTERVAL=3600  # Change to desired seconds
# 1800 = 30 minutes
# 3600 = 1 hour (default)
# 7200 = 2 hours
```

### Change Course URL

**Option 1: Command Line**
```bash
./scripts/auto_continue_course.sh "your_course_url"
```

**Option 2: Edit Script**
```bash
# Edit scripts/auto_continue_course.sh and change COURSE_URL
COURSE_URL="https://ibmdt.udemy.com/course/your-course/"
```

---

## 📊 Monitoring

### View Logs in Real-Time

```bash
# Watch logs as they happen
tail -f course_completion.log
```

### Check Current Status

```bash
# Check if automation is running
ps aux | grep auto_continue_course.sh

# Check if course completion is active
cat /tmp/course_completion.lock  # Shows PID if running
```

### View Full Log History

```bash
# View all logs
cat course_completion.log

# View last 50 lines
tail -50 course_completion.log

# Search for errors
grep -i error course_completion.log
```

---

## 🛑 Stopping the Automation

### Method 1: Ctrl+C (Graceful)
```bash
# In the terminal where script is running
# Press Ctrl+C
```

### Method 2: Kill Process
```bash
# Find the process
ps aux | grep auto_continue_course.sh

# Kill it (replace PID with actual process ID)
kill <PID>
```

### Method 3: Kill All Related Processes
```bash
# Kill automation script
pkill -f auto_continue_course.sh

# Kill any running course completion
pkill -f run_agent.py
```

---

## 🔧 Advanced Usage

### Run in Background (Detached)

```bash
nohup ./scripts/auto_continue_course.sh > /dev/null 2>&1 &
```

### Run on System Startup (macOS)

Create a LaunchAgent:

```bash
# Create plist file
cat > ~/Library/LaunchAgents/com.course.automation.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.course.automation</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/sorravit/sandbox/ClineSandbox/scripts/auto_continue_course.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>/Users/sorravit/sandbox/ClineSandbox</string>
</dict>
</plist>
EOF

# Load the agent
launchctl load ~/Library/LaunchAgents/com.course.automation.plist

# Unload (to stop)
launchctl unload ~/Library/LaunchAgents/com.course.automation.plist
```

---

## 🐛 Troubleshooting

### Script Won't Start

**Problem:** Permission denied
```bash
# Solution: Make executable
chmod +x scripts/auto_continue_course.sh
```

**Problem:** `venv/bin/activate` not found
```bash
# Solution: create or restore your virtual environment
python3 -m venv venv
```

### Multiple Runs Starting

**Problem:** Lockfile not working
```bash
# Solution: Remove stale lockfile
rm /tmp/course_completion.lock
```

### Script Stops Unexpectedly

**Problem:** Terminal closed
```bash
# Solution: Run in background with nohup
nohup ./scripts/auto_continue_course.sh > /dev/null 2>&1 &
```

**Problem:** Computer went to sleep
```bash
# Solution: Prevent sleep or use LaunchAgent
```

---

## 📝 Example Session

```bash
# 1. Start automation
$ ./scripts/auto_continue_course.sh

[2026-04-18 11:00:00] === Auto-Continue Course Script Started ===
[2026-04-18 11:00:00] Course URL: https://ibmdt.udemy.com/course/...
[2026-04-18 11:00:00] Check interval: 3600 seconds (60 minutes)
[2026-04-18 11:00:00] No active run detected. Starting new session...
[2026-04-18 11:00:00] Starting course completion...
[2026-04-18 11:00:00] Started with PID: 12345
[2026-04-18 11:00:00] Next check in 60 minutes...

# ... script keeps checking and relaunching only when needed ...
```

---

## ✅ Best Practices

1. **Monitor Regularly**
   - Check logs every few hours
   - Verify progress is being made
   - Watch for errors

2. **Keep Computer Awake**
   - Disable sleep mode
   - Or use LaunchAgent for auto-restart

3. **Have a Backup Plan**
   - Save progress periodically
   - Know how to restart manually
   - Keep logs for debugging

4. **Test First**
   - Run manually once to verify it works
   - Check logs are being created
   - Verify lockfile mechanism works

---

## 🎓 Your Exact Commands

**For 10-hour course completion:**

```bash
# 1. Start automation
./scripts/auto_continue_course.sh "https://ibmdt.udemy.com/course/working-with-design-patterns-in-go-golang/"

# 2. Monitor in another terminal
tail -f course_completion.log

# 3. Stop when done (Ctrl+C in automation terminal)
```

**Or run in background:**

```bash
nohup ./scripts/auto_continue_course.sh > /dev/null 2>&1 &

# Check it's running
ps aux | grep auto_continue_course.sh

# Monitor logs
tail -f course_completion.log

# Stop when done
pkill -f auto_continue_course.sh
```

---

## 🎉 Summary

**You now have two ways to run course completion:**

1. **Manual:** Run `python scripts/run_agent.py "task"` once
2. **Auto-Continue:** Run `./scripts/auto_continue_course.sh` and let it run for days

**The auto-continue script:**
- ✅ Checks every hour
- ✅ Only starts if previous finished
- ✅ Logs everything
- ✅ Runs indefinitely
- ✅ Easy to stop (`Ctrl+C`)

**This guide now matches your shell-only setup.** 🎓
