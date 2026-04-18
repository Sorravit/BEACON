#!/bin/bash
# Start persistent MCP Playwright server
# This keeps the browser session alive across multiple Python runs

echo "🚀 Starting persistent MCP Playwright server..."
echo "This will keep the browser session alive."
echo "Press Ctrl+C to stop the server and close all browsers."
echo ""

# Start the MCP server
npx @playwright/mcp@latest