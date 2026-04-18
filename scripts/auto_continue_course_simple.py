#!/usr/bin/env python3
"""
Auto-continue course completion script (Simple version - no extra dependencies)
Runs every hour, but only if previous run has finished
"""

import os
import sys
import time
import signal
import subprocess
from pathlib import Path
from datetime import datetime

# Configuration
COURSE_URL = "https://ibmdt.udemy.com/course/react-testing-library-and-jest/?kw=React+Testing+Library+and+Jest%3A+The+Complete+Guide&src=sac"
CHECK_INTERVAL = 600  # 1 hour in seconds
LOCKFILE = "/tmp/course_completion.lock"
LOGFILE = "course_completion.log"

def log(message: str):
    """Log with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {message}"
    print(log_msg)
    with open(LOGFILE, "a") as f:
        f.write(log_msg + "\n")

def is_process_running() -> bool:
    """Check if previous process is still running"""
    if not os.path.exists(LOCKFILE):
        return False
    
    try:
        with open(LOCKFILE, "r") as f:
            pid = int(f.read().strip())
        
        # Check if process exists using ps command
        result = subprocess.run(
            ["ps", "-p", str(pid)],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            return True
        else:
            # Stale lockfile
            os.remove(LOCKFILE)
            return False
    except Exception as e:
        log(f"Error checking process: {e}")
        return False

def create_lockfile(pid: int):
    """Create lockfile with PID"""
    with open(LOCKFILE, "w") as f:
        f.write(str(pid))

def remove_lockfile():
    """Remove lockfile"""
    if os.path.exists(LOCKFILE):
        os.remove(LOCKFILE)

def run_course_completion(course_url: str):
    """Run course completion task"""
    log("🚀 Starting course completion...")
    
    try:
        # Get the directory where this script is located
        script_dir = Path(__file__).parent
        project_dir = script_dir.parent
        
        # Build command
        cmd = [
            sys.executable,  # Use same Python interpreter
            str(script_dir / "run_agent.py"),
            f"Complete each video in this course. Watch the whole video, don't skip. Continue from where you left off. Course: {course_url}"
        ]
        
        log(f"Running command: {' '.join(cmd)}")
        
        # Run in background
        process = subprocess.Popen(
            cmd,
            stdout=open(LOGFILE, "a"),
            stderr=subprocess.STDOUT,
            cwd=str(project_dir)
        )
        
        # Save PID
        create_lockfile(process.pid)
        log(f"Started with PID: {process.pid}")
        
        # Wait for process to complete
        process.wait()
        
        log(f"✅ Process completed with exit code: {process.returncode}")
        
    except Exception as e:
        log(f"❌ Exception: {e}")
        import traceback
        log(traceback.format_exc())
    finally:
        remove_lockfile()

def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully"""
    print("\n🛑 Stopping automation...")
    remove_lockfile()
    sys.exit(0)

def main():
    # Handle command line arguments
    course_url = sys.argv[1] if len(sys.argv) > 1 else COURSE_URL
    
    # Setup signal handler
    signal.signal(signal.SIGINT, signal_handler)
    
    log("=" * 60)
    log("🤖 Auto-Continue Course Script Started")
    log("=" * 60)
    log(f"Course URL: {course_url}")
    log(f"Check interval: {CHECK_INTERVAL} seconds ({CHECK_INTERVAL // 60} minutes)")
    log(f"Lockfile: {LOCKFILE}")
    log(f"Logfile: {LOGFILE}")
    log("=" * 60)
    
    try:
        while True:
            if is_process_running():
                log("⏳ Previous run still active. Waiting...")
            else:
                log("✨ No active run detected. Starting new session...")
                run_course_completion(course_url)
            
            # Wait for next check
            log(f"💤 Next check in {CHECK_INTERVAL // 60} minutes...")
            time.sleep(CHECK_INTERVAL)
            
    except KeyboardInterrupt:
        remove_lockfile()
        print("\n✅ Automation stopped")

if __name__ == "__main__":
    main()