#!/bin/bash
# Auto-continue course completion script
# Runs every hour, but only if previous run has finished
# Usage: ./scripts/auto_continue_course.sh "course_url"

# Configuration
COURSE_URL="${1:-https://ibmdt.udemy.com/course/working-with-design-patterns-in-go-golang/}"
VENV_PATH="venv/bin/activate"
LOCKFILE="/tmp/course_completion.lock"
LOGFILE="course_completion.log"
CHECK_INTERVAL=3600  # 1 hour in seconds

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Function to log with timestamp
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOGFILE"
}

# Function to check if process is running
is_running() {
    if [ -f "$LOCKFILE" ]; then
        PID=$(cat "$LOCKFILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            return 0  # Process is running
        else
            # Stale lockfile, remove it
            rm -f "$LOCKFILE"
            return 1  # Process not running
        fi
    fi
    return 1  # No lockfile, not running
}

# Function to start course completion
start_course() {
    log "${GREEN}Starting course completion...${NC}"
    
    # Activate virtual environment and run
    source "$VENV_PATH"
    
    # Run in background and save PID
    python scripts/run_agent.py "Complete each video in this course. Watch the whole video, don't skip. Continue from where you left off. Course: $COURSE_URL" >> "$LOGFILE" 2>&1 &
    
    PID=$!
    echo $PID > "$LOCKFILE"
    
    log "Started with PID: $PID"
}

# Main loop
log "${GREEN}=== Auto-Continue Course Script Started ===${NC}"
log "Course URL: $COURSE_URL"
log "Check interval: $CHECK_INTERVAL seconds ($(($CHECK_INTERVAL / 60)) minutes)"
log "Lockfile: $LOCKFILE"
log "Logfile: $LOGFILE"

while true; do
    if is_running; then
        PID=$(cat "$LOCKFILE")
        log "${YELLOW}Previous run still active (PID: $PID). Waiting...${NC}"
    else
        log "${GREEN}No active run detected. Starting new session...${NC}"
        start_course
    fi
    
    # Wait for next check
    log "Next check in $(($CHECK_INTERVAL / 60)) minutes..."
    sleep $CHECK_INTERVAL
done