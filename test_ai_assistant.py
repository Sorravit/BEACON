#!/usr/bin/env python3
"""
Test script for AI Assistant
"""
import subprocess
import sys

def test_assistant():
    """Test the AI assistant with a simple query"""
    print("Testing AI Assistant with IBM ICA OpenAI-compatible endpoint...")
    print("="*60)
    
    # Create test input
    test_input = "Hello, can you tell me what 2+2 equals?\nquit\n"
    
    try:
        # Run the assistant with test input using virtual environment
        result = subprocess.run(
            ['venv/bin/python3', 'ai_assistant.py'],
            input=test_input,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        print("\n--- OUTPUT ---")
        print(result.stdout)
        
        if result.stderr:
            print("\n--- ERRORS ---")
            print(result.stderr)
        
        print("\n--- TEST RESULT ---")
        if result.returncode == 0:
            print("✅ Test completed successfully!")
            print("The AI assistant is working with the IBM ICA endpoint.")
        else:
            print(f"❌ Test failed with return code: {result.returncode}")
            return 1
            
        return 0
        
    except subprocess.TimeoutExpired:
        print("❌ Test timed out after 30 seconds")
        return 1
    except Exception as e:
        print(f"❌ Test failed with error: {str(e)}")
        return 1

if __name__ == "__main__":
    sys.exit(test_assistant())