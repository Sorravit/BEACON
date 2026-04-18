#!/usr/bin/env python3
"""
AI Assistant - Agent with Skills
Version: 4.2.0 (MCP Integration)

A production-ready AI assistant with MCP support, browser automation, HTTP requests, and file operations.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from openai import OpenAI
from lib.mcp_client import MCPManager

# ============================================================================
# CONFIGURATION CONSTANTS - Modify these to customize behavior
# ============================================================================

# Version
VERSION = "4.2.0"

# Logging
LOG_FILE = "ai_assistant.log"

# AI Model Configuration
DEFAULT_MODEL = "gpt-3.5-turbo"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 2000
DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Tool Execution Limits
# MAX_TOOL_ITERATIONS: Maximum number of tool calls per user message
# - 1000 = ~8-10 hours of continuous execution (recommended for courses)
# - 2000 = ~16-20 hours (for very long courses)
# - 5000 = ~40-50 hours (for multi-day tasks)
MAX_TOOL_ITERATIONS = 1000

# Conversation Management
# MAX_CONVERSATION_TOKENS: Maximum tokens to keep in conversation history
# - Prevents "prompt too long" errors (200k token limit)
# - Dynamically trims based on actual token count, not message count
# - Keeps as many recent messages as possible within token limit
# - System message is always kept
MAX_CONVERSATION_TOKENS = 150000  # Keep up to 150k tokens (safe buffer from 200k limit)

# MCP Configuration
MCP_CONFIG_FILE = "mcp_config.json"

# ============================================================================

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class ToolManager:
    """Manages all agent tools including file, browser, and HTTP operations."""
    
    def __init__(self):
        """Initialize the tool manager."""
        self.tools: List[Dict[str, Any]] = []
        self.tool_handlers: Dict[str, Callable] = {}
        self.browser = None
        self.page = None
        self.playwright = None
        
    async def initialize(self):
        """Initialize tools"""
        try:
            await self._register_tools()
            logger.info(f"Initialized with {len(self.tools)} tools")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize: {e}")
            return False
    
    async def _register_tools(self):
        """Register all available tools"""
        tools_config = [
            # System tools
            ("get_current_time", "Returns the current date and time. Use this whenever the user asks about time, date, today, now, etc.", {}, self._get_current_time),
            ("execute_command", "Executes a shell command and returns its output. Use this to get system info, run programs, etc.", {"command": "string"}, self._execute_command),
            
            # File tools
            ("read_file", "Returns the contents of a file at the specified path", {"file_path": "string"}, self._read_file),
            ("write_file", "Writes content to a file at the specified path", {"file_path": "string", "content": "string"}, self._write_file),
            ("list_files", "Returns a list of files and directories in the specified directory path", {"directory": "string"}, self._list_files),
            
            # Web search tool
            ("web_search", "Searches Google and returns the search results. Use this for any question requiring current/recent information, news, facts, etc.", {"query": "string"}, self._web_search),
            
            # Browser tools
            ("browser_navigate", "Opens a web browser and navigates to the specified URL", {"url": "string"}, self._browser_navigate),
            ("browser_click", "Clicks on an element in the browser using a CSS selector", {"selector": "string"}, self._browser_click),
            ("browser_type", "Types text into an input field in the browser using a CSS selector", {"selector": "string", "text": "string"}, self._browser_type),
            ("browser_screenshot", "Takes a screenshot of the current browser window and saves it to a file", {"filename": "string"}, self._browser_screenshot),
            ("browser_get_text", "Gets the text content from an element in the browser using a CSS selector", {"selector": "string"}, self._browser_get_text),
            ("browser_close", "Closes the browser window", {}, self._browser_close),
            
            # HTTP tools
            ("http_get", "Makes an HTTP GET request to the specified URL and returns the response", {"url": "string"}, self._http_get),
            ("http_post", "Makes an HTTP POST request to the specified URL with data and returns the response", {"url": "string", "data": "string"}, self._http_post),
        ]
        
        for name, desc, params, handler in tools_config:
            self.tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": {k: {"type": v, "description": k.replace("_", " ")} for k, v in params.items()},
                        "required": list(params.keys())
                    }
                }
            })
            self.tool_handlers[name] = handler
    
    # System tools
    async def _get_current_time(self):
        """Get current date and time"""
        try:
            from datetime import datetime
            now = datetime.now()
            return f"Current time: {now.strftime('%A, %B %d, %Y at %I:%M:%S %p')}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _web_search(self, query: str):
        """Perform a Google search and return results"""
        try:
            import httpx
            from urllib.parse import quote
            
            # Use Google search with httpx
            encoded_query = quote(query)
            url = f"https://www.google.com/search?q={encoded_query}"
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
            }
            
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                response = await client.get(url, headers=headers)
                html = response.text
                
                # Use BeautifulSoup for better parsing
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    results = []
                    
                    # Find all search result divs
                    # Google uses various div structures, try multiple selectors
                    search_divs = soup.find_all('div', class_='g') or soup.find_all('div', {'data-sokoban-container': True})
                    
                    for i, div in enumerate(search_divs[:10]):
                        # Extract title
                        title_elem = div.find('h3')
                        if title_elem:
                            title = title_elem.get_text(strip=True)
                            
                            # Extract snippet/description
                            snippet_elem = div.find('div', class_=['VwiC3b', 'yXK7lf', 'lVm3ye'])
                            if not snippet_elem:
                                # Try alternative selectors
                                snippet_elem = div.find('span', class_='aCOpRe')
                            
                            snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""
                            
                            # Extract URL
                            link_elem = div.find('a')
                            url = link_elem.get('href', '') if link_elem else ""
                            
                            if title:
                                results.append(f"\n{i+1}. {title}")
                                if snippet:
                                    results.append(f"   {snippet[:200]}...")
                                if url and url.startswith('http'):
                                    results.append(f"   URL: {url}")
                    
                    if results:
                        return f"Google search results for '{query}':\n" + "\n".join(results)
                    else:
                        # Fallback: return raw text content
                        text_content = soup.get_text()
                        # Extract first few meaningful lines
                        lines = [line.strip() for line in text_content.split('\n') if line.strip() and len(line.strip()) > 20]
                        if lines:
                            return f"Search results for '{query}' (extracted text):\n\n" + "\n".join(lines[:15])
                        else:
                            return f"Searched Google for '{query}' but couldn't extract results. Try using browser_navigate for interactive search."
                
                except ImportError:
                    # BeautifulSoup not available, use simple regex
                    import re
                    results = []
                    
                    # Try to extract any text that looks like search results
                    text_blocks = re.findall(r'<div[^>]*>(.*?)</div>', html, re.DOTALL)
                    meaningful_blocks = [re.sub('<[^<]+?>', '', block).strip() for block in text_blocks if len(block) > 50]
                    
                    if meaningful_blocks:
                        return f"Search results for '{query}':\n\n" + "\n\n".join(meaningful_blocks[:10])
                    else:
                        return f"Searched Google for '{query}' but couldn't extract results. Try using browser_navigate for interactive search."
                    
        except Exception as e:
            return f"Error searching: {e}. Try using browser_navigate to search interactively."
    
    # File tools
    async def _read_file(self, file_path: str):
        try:
            with open(file_path, 'r') as f:
                return f"Content of {file_path}:\n{f.read()}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _write_file(self, file_path: str, content: str):
        try:
            with open(file_path, 'w') as f:
                f.write(content)
            return f"Wrote to {file_path}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _list_files(self, directory: str):
        try:
            files = [f.name for f in Path(directory).iterdir()]
            return f"Files in {directory}:\n" + "\n".join(files)
        except Exception as e:
            return f"Error: {e}"
    
    async def _execute_command(self, command: str):
        try:
            import subprocess
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            return f"Output:\n{result.stdout or result.stderr}"
        except Exception as e:
            return f"Error: {e}"
    
    # Browser tools
    async def _ensure_browser(self):
        """Ensure browser is running"""
        if not self.browser:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=False)
            self.page = await self.browser.new_page()
            logger.info("Browser launched")
        return self.page
    
    async def _browser_navigate(self, url: str):
        try:
            page = await self._ensure_browser()
            await page.goto(url, wait_until="domcontentloaded")
            return f"Navigated to {url}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _browser_click(self, selector: str):
        try:
            page = await self._ensure_browser()
            await page.click(selector)
            return f"Clicked {selector}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _browser_type(self, selector: str, text: str):
        try:
            page = await self._ensure_browser()
            await page.fill(selector, text)
            return f"Typed '{text}' into {selector}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _browser_screenshot(self, filename: str):
        try:
            page = await self._ensure_browser()
            await page.screenshot(path=filename)
            return f"Screenshot saved to {filename}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _browser_get_text(self, selector: str):
        try:
            page = await self._ensure_browser()
            text = await page.text_content(selector)
            return f"Text: {text}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _browser_close(self):
        try:
            if self.browser:
                await self.browser.close()
                await self.playwright.stop()
                self.browser = None
                self.page = None
                return "Browser closed"
            return "Browser was not open"
        except Exception as e:
            return f"Error: {e}"
    
    # HTTP tools
    async def _http_get(self, url: str):
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                return f"Status: {response.status_code}\nBody: {response.text[:500]}"
        except Exception as e:
            return f"Error: {e}"
    
    async def _http_post(self, url: str, data: str):
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.post(url, content=data)
                return f"Status: {response.status_code}\nBody: {response.text[:500]}"
        except Exception as e:
            return f"Error: {e}"
    
    async def execute_tool(self, name: str, args: Dict):
        if name not in self.tool_handlers:
            return f"Unknown tool: {name}"
        
        # Normalize parameter names to handle AI model variations
        normalized_args = self._normalize_tool_args(name, args)
        return await self.tool_handlers[name](**normalized_args)
    
    def _normalize_tool_args(self, tool_name: str, args: Dict) -> Dict:
        """
        Normalize parameter names to match tool function signatures.
        Handles cases where AI uses generic names like 'path', 'filename' instead of specific parameter names.
        """
        # Parameter mapping for each tool - maps AI's parameter names to actual function parameter names
        param_mappings = {
            'read_file': {
                'path': 'file_path',
                'filename': 'file_path',
                'file': 'file_path'
            },
            'write_file': {
                'path': 'file_path',
                'filename': 'file_path',
                'file': 'file_path'
            },
            'list_files': {
                'path': 'directory',
                'dir': 'directory',
                'folder': 'directory'
            },
        }
        
        if tool_name not in param_mappings:
            return args
        
        normalized = {}
        mapping = param_mappings[tool_name]
        
        for key, value in args.items():
            # If the key needs to be mapped, use the mapped name
            normalized_key = mapping.get(key, key)
            normalized[normalized_key] = value
        
        return normalized
    
    async def cleanup(self):
        """Cleanup resources"""
        try:
            if self.browser:
                await self._browser_close()
        except:
            pass


class Config:
    """Configuration manager for the AI assistant."""
    
    def __init__(self):
        """Initialize configuration from environment variables and .env file."""
        self._load_env_file()
        self._load_config()
    
    def _load_env_file(self):
        """Load environment variables from .env file if it exists."""
        env_file = Path(".env")
        if not env_file.exists():
            return
        
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip("\"'")
    
    def _load_config(self):
        """Load configuration values from environment."""
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL)
        self.model = os.getenv("AI_MODEL", DEFAULT_MODEL)
        self.temperature = float(os.getenv("AI_TEMPERATURE", str(DEFAULT_TEMPERATURE)))
        self.max_tokens = int(os.getenv("AI_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
        self.enable_tools = os.getenv("ENABLE_TOOLS", "true").lower() == "true"
    
    def validate(self) -> bool:
        """Validate that required configuration is present."""
        return bool(self.api_key)
    
    def display(self):
        """Display current configuration."""
        print(f"Model: {self.model}")
        print(f"Endpoint: {self.base_url}")
        print(f"Tools: {self.enable_tools}")


class AIAgent:
    """Main AI agent that handles conversations and tool execution."""
    
    def __init__(self, config: Config):
        """Initialize the AI agent with configuration."""
        self.config = config
        self.client: Optional[OpenAI] = None
        self.conversation: List[Dict[str, Any]] = []
        self.tools: Optional[ToolManager] = None
        self.mcp_manager: Optional[MCPManager] = None
        self.tools_available = False
    
    async def initialize(self) -> bool:
        """
        Initialize the AI agent and its tools.
        
        Returns:
            bool: True if initialization successful, False otherwise.
        """
        try:
            self.client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)
            
            # Initialize MCP manager
            self.mcp_manager = MCPManager()
            
            # Load MCP servers from config
            await self._load_mcp_servers()
            
            if self.config.enable_tools:
                self.tools = ToolManager()
                self.tools_available = await self.tools.initialize()
                
                # Initialize MCP manager and load servers
                self.mcp_manager = MCPManager()
                await self._load_mcp_servers()
                
                # Add system message establishing tool usage context
                if self.tools_available:
                    system_message = {
                        "role": "system",
                        "content": (
                            "You are an AUTONOMOUS AI AGENT. Your job is to TAKE ACTION, not explain.\n\n"
                            "CRITICAL RULES:\n"
                            "1. When user asks a question → USE TOOLS IMMEDIATELY to get the answer\n"
                            "2. NEVER say 'I cannot' or 'I don't have access' - you have tools, USE THEM\n"
                            "3. NEVER explain how to do something - JUST DO IT\n"
                            "4. For time/date questions → use get_current_time\n"
                            "5. For recent events/news/current info → use web_search\n"
                            "6. For system info → use execute_command\n"
                            "7. Chain multiple tools when needed\n"
                            "8. Only respond in text AFTER you've used tools to get the answer\n\n"
                            "Example: User asks 'what time is it?' → Call get_current_time() → Return the time\n"
                            "Example: User asks 'latest aviation accident' → Call web_search('latest aviation accident') → Summarize results\n\n"
                            "BE PROACTIVE. ACT FIRST. EXPLAIN AFTER."
                        )
                    }
                    self.conversation.append(system_message)
                    
            logger.info("Agent initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Agent initialization failed: {e}")
            return False
    
    async def _load_mcp_servers(self):
        """Load MCP servers from mcp_config.json"""
        try:
            config_file = Path("mcp_config.json")
            if not config_file.exists():
                logger.info("No mcp_config.json found, skipping MCP servers")
                return
            
            with open(config_file) as f:
                mcp_config = json.load(f)
            
            servers = mcp_config.get("servers", {})
            for server_name, server_config in servers.items():
                command = server_config.get("command")
                args = server_config.get("args", [])
                
                if command and self.mcp_manager:
                    logger.info(f"Loading MCP server: {server_name}")
                    success = await self.mcp_manager.add_server(server_name, command, args)
                    if success:
                        logger.info(f"✅ MCP server {server_name} loaded")
                    else:
                        logger.warning(f"⚠️  Failed to load MCP server {server_name}")
        
        except Exception as e:
            logger.error(f"Error loading MCP servers: {e}")
    
    async def _detect_and_execute_tool(self, user_input: str) -> Optional[tuple]:
        """
        Detect if user input requires a tool and execute it.
        Returns (tool_name, result) if tool was executed, None otherwise.
        """
        user_lower = user_input.lower()
        
        # File operations
        if "list files" in user_lower or "list directory" in user_lower or "show files" in user_lower:
            directory = "."
            if " in " in user_lower:
                parts = user_lower.split(" in ")
                if len(parts) > 1:
                    directory = parts[1].strip()
            if self.tools:
                result = await self.tools.execute_tool("list_files", {"directory": directory})
                return ("list_files", result)
        
        if "read file" in user_lower or "show file" in user_lower or "cat " in user_lower:
            # Extract filename
            for word in user_input.split():
                if "." in word and not word.startswith(".") and self.tools:
                    result = await self.tools.execute_tool("read_file", {"file_path": word})
                    return ("read_file", result)
        
        # Web navigation and automation
        if "open " in user_lower and ("website" in user_lower or "url" in user_lower or "http" in user_input):
            # Extract URL
            words = user_input.split()
            url = None
            for word in words:
                if "http" in word or ".com" in word or ".org" in word or ".net" in word:
                    url = word
                    break
            if url and self.tools:
                result = await self.tools.execute_tool("browser_navigate", {"url": url})
                # Also take a screenshot to see what's there
                await self.tools.execute_tool("browser_screenshot", {"filename": "current_page.png"})
                result += "\n\nScreenshot saved as current_page.png"
                return ("browser_navigate", result)
        
        if "search google" in user_lower or "google search" in user_lower or "search for" in user_lower:
            if self.tools:
                query = user_input.lower()
                for phrase in ["search google for", "google search for", "search google", "google search", "search for"]:
                    query = query.replace(phrase, "")
                query = query.strip()
                url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
                result = await self.tools.execute_tool("browser_navigate", {"url": url})
                # Take screenshot of search results
                await self.tools.execute_tool("browser_screenshot", {"filename": "google_search.png"})
                result += "\n\nOpened Google search. Screenshot saved as google_search.png"
                result += "\nYou can now ask me to click on specific search results or take other actions."
                return ("browser_navigate", result)
        
        if "take screenshot" in user_lower or "screenshot" in user_lower:
            if self.tools:
                result = await self.tools.execute_tool("browser_screenshot", {"filename": "screenshot.png"})
                return ("browser_screenshot", result)
        
        if "close browser" in user_lower or "close the browser" in user_lower:
            if self.tools:
                result = await self.tools.execute_tool("browser_close", {})
                return ("browser_close", result)
        
        if "click" in user_lower and ("button" in user_lower or "link" in user_lower or "on" in user_lower):
            # This is more complex - would need CSS selector or text to click
            # For now, provide guidance
            return ("info", "To click on an element, I need you to specify a CSS selector or describe the element more specifically. "
                           "For example: 'click on the button with text Submit' or 'click on .submit-button'")
        
        return None
    
    def _build_tools_prompt(self) -> str:
        """Build system prompt with tool descriptions"""
        prompt = "\n\nYou have access to the following tools:\n\n"
        
        # Add built-in tools
        if self.tools_available and self.tools:
            for tool in self.tools.tools:
                func = tool['function']
                prompt += f"<tool name=\"{func['name']}\">\n"
                prompt += f"Description: {func['description']}\n"
                if func.get('parameters', {}).get('properties'):
                    prompt += "Parameters:\n"
                    for param_name, param_info in func['parameters']['properties'].items():
                        param_type = param_info.get('type', 'string')
                        param_desc = param_info.get('description', '')
                        required = param_name in func['parameters'].get('required', [])
                        req_str = " (required)" if required else " (optional)"
                        prompt += f"  - {param_name} ({param_type}){req_str}: {param_desc}\n"
                else:
                    prompt += "Parameters: none\n"
                prompt += "</tool>\n\n"
        
        # Add MCP tools
        if self.mcp_manager:
            for tool in self.mcp_manager.get_all_tools_for_openai():
                func = tool['function']
                prompt += f"<tool name=\"{func['name']}\">\n"
                prompt += f"Description: {func['description']}\n"
                if func.get('parameters', {}).get('properties'):
                    prompt += "Parameters:\n"
                    for param_name, param_info in func['parameters']['properties'].items():
                        param_type = param_info.get('type', 'string')
                        param_desc = param_info.get('description', '')
                        required = param_name in func['parameters'].get('required', [])
                        req_str = " (required)" if required else " (optional)"
                        prompt += f"  - {param_name} ({param_type}){req_str}: {param_desc}\n"
                else:
                    prompt += "Parameters: none\n"
                prompt += "</tool>\n\n"
        
        prompt += """To use a tool, respond with:
<tool_use>
<tool_name>tool_name_here</tool_name>
<parameters>
{
  "param1": "value1",
  "param2": "value2"
}
</parameters>
</tool_use>

You can use multiple tools in sequence. After I execute each tool, I'll provide the result and you can continue or use another tool.
IMPORTANT: When you use a tool, ONLY output the <tool_use> block, nothing else. After I give you the result, then provide your final answer."""
        
        return prompt
    
    def _parse_tool_calls(self, response_text: str) -> List[Dict[str, Any]]:
        """Parse tool calls from Claude's XML response"""
        import re
        
        tool_calls = []
        
        # Find all <tool_use> blocks
        pattern = r'<tool_use>(.*?)</tool_use>'
        matches = re.findall(pattern, response_text, re.DOTALL)
        
        for match in matches:
            try:
                # Parse tool name
                tool_name_match = re.search(r'<tool_name>(.*?)</tool_name>', match)
                if not tool_name_match:
                    continue
                tool_name = tool_name_match.group(1).strip()
                
                # Parse parameters
                params_match = re.search(r'<parameters>(.*?)</parameters>', match, re.DOTALL)
                if params_match:
                    params_text = params_match.group(1).strip()
                    if params_text:
                        try:
                            parameters = json.loads(params_text)
                        except json.JSONDecodeError:
                            logger.error(f"Failed to parse parameters JSON: {params_text}")
                            parameters = {}
                    else:
                        parameters = {}
                else:
                    parameters = {}
                
                tool_calls.append({
                    "tool_name": tool_name,
                    "parameters": parameters
                })
            except Exception as e:
                logger.error(f"Error parsing tool call: {e}")
                continue
        
        return tool_calls
    
    async def get_response(self, user_input: str) -> Optional[str]:
        """
        Get AI response with prompt-based tool execution loop (Cline/Roo style).
        
        Args:
            user_input: User's message
            
        Returns:
            Optional[str]: AI response or None if error occurred
        """
        if not self.client:
            return None
        
        try:
            # Add tools description to system message if not already present
            if self.tools_available and len(self.conversation) > 0:
                # Check if we need to update system message with tools
                if self.conversation[0]["role"] == "system":
                    # Update system message to include tools
                    tools_prompt = self._build_tools_prompt()
                    if tools_prompt not in self.conversation[0]["content"]:
                        self.conversation[0]["content"] += tools_prompt
            
            self.conversation.append({"role": "user", "content": user_input})
            
            # Trim conversation if it's getting too long
            self._trim_conversation()
            
            # Agent loop - allow multiple tool calls
            # For long-running tasks (courses, etc.), use a very high limit
            # Configured via MAX_TOOL_ITERATIONS constant at top of file
            for iteration in range(MAX_TOOL_ITERATIONS):
                params = {
                    "model": self.config.model,
                    "messages": self.conversation,
                    "temperature": self.config.temperature,
                    "max_tokens": self.config.max_tokens
                }
                
                # NO tools parameter - use prompt-based approach instead
                
                response = self.client.chat.completions.create(**params)
                response_text = response.choices[0].message.content
                
                if not response_text:
                    return "No response from AI"
                
                # Parse tool calls from response
                tool_calls = self._parse_tool_calls(response_text)
                
                if tool_calls:
                    # Add assistant's response with tool calls
                    self.conversation.append({
                        "role": "assistant",
                        "content": response_text
                    })
                    
                    # Execute all tool calls
                    tool_results = []
                    for tool_call in tool_calls:
                        tool_name = tool_call["tool_name"]
                        tool_args = tool_call["parameters"]
                        
                        print(f"  🔧 Executing: {tool_name}({', '.join(f'{k}={str(v)[:30]}' for k, v in tool_args.items()) if tool_args else ''})")
                        
                        # Check if it's an MCP tool or built-in tool
                        if tool_name.startswith("mcp_") and self.mcp_manager:
                            tool_result = await self.mcp_manager.call_tool(tool_name, tool_args)
                            if tool_result is None:
                                tool_result = json.dumps({"error": f"Unknown tool: {tool_name}"})
                        elif self.tools:
                            tool_result = await self.tools.execute_tool(tool_name, tool_args)
                        else:
                            tool_result = json.dumps({"error": "No tool manager available"})
                        
                        tool_results.append(f"Tool: {tool_name}\nResult: {tool_result}")
                    
                    # Add tool results as user message
                    results_text = "\n\n".join(tool_results)
                    self.conversation.append({
                        "role": "user",
                        "content": f"Tool execution results:\n\n{results_text}\n\nPlease provide your final response based on these results."
                    })
                    
                    # Continue loop to let AI process tool results
                    continue
                else:
                    # No tool calls found, this is the final response
                    self.conversation.append({"role": "assistant", "content": response_text})
                    return response_text
            
            # If we hit max iterations, inform user but don't fail
            logger.warning(f"Reached maximum iterations ({MAX_TOOL_ITERATIONS}). Task may not be complete.")
            return f"I've executed {MAX_TOOL_ITERATIONS} tool calls. The task may need to be continued. Please check the current state and let me know if you want me to continue."
                
        except Exception as e:
            logger.error(f"Error: {e}")
            import traceback
            traceback.print_exc()
            if self.conversation and self.conversation[-1]["role"] == "user":
                self.conversation.pop()
            return None
    
    def _estimate_tokens(self, text: str) -> int:
        """
        Estimate token count for text.
        Rough estimate: 1 token ≈ 4 characters
        """
        return len(text) // 4
    
    def _get_conversation_tokens(self) -> int:
        """Calculate total tokens in current conversation"""
        total = 0
        for msg in self.conversation:
            content = msg.get("content", "")
            total += self._estimate_tokens(content)
        return total
    
    def _trim_conversation(self):
        """
        Dynamically trim conversation history based on token count.
        Keeps system message and as many recent messages as possible within token limit.
        """
        total_tokens = self._get_conversation_tokens()
        
        # If under limit, no trimming needed
        if total_tokens <= MAX_CONVERSATION_TOKENS:
            return
        
        # Keep system message (first message with tools description)
        system_msg = None
        start_idx = 0
        if self.conversation and self.conversation[0]["role"] == "system":
            system_msg = self.conversation[0]
            start_idx = 1
        
        # Calculate tokens for system message
        system_tokens = self._estimate_tokens(system_msg["content"]) if system_msg else 0
        available_tokens = MAX_CONVERSATION_TOKENS - system_tokens
        
        # Keep as many recent messages as possible within token limit
        kept_messages = []
        current_tokens = 0
        
        # Iterate from most recent to oldest
        for msg in reversed(self.conversation[start_idx:]):
            msg_tokens = self._estimate_tokens(msg.get("content", ""))
            if current_tokens + msg_tokens <= available_tokens:
                kept_messages.insert(0, msg)  # Insert at beginning to maintain order
                current_tokens += msg_tokens
            else:
                break  # Stop when we would exceed limit
        
        # Rebuild conversation
        self.conversation.clear()
        if system_msg:
            self.conversation.append(system_msg)
        self.conversation.extend(kept_messages)
        
        new_total = self._get_conversation_tokens()
        logger.info(f"Trimmed conversation: {total_tokens} → {new_total} tokens ({len(kept_messages)} messages kept)")
    
    def clear(self):
        """Clear all conversation history"""
        self.conversation.clear()
    
    async def run(self, mode: str = "chat"):
        """
        Run the agent in specified mode.
        
        Args:
            mode: "chat" for interactive chat, "agent" for programmatic agent mode
        """
        if mode == "agent":
            # Agent mode - programmatic interface
            from agent_api import AgentAPI
            api = AgentAPI(self)
            print("="*60)
            print("🤖 AGENT MODE - Programmatic Interface")
            print("="*60)
            print("Agent is ready for programmatic task execution.")
            print("Use the AgentAPI to run tasks programmatically.")
            print("See example_agent_usage.py for examples.")
            print("-"*60)
            return api
        
        # Chat mode - interactive conversation
        print("="*60)
        print("🤖 AUTONOMOUS AI AGENT (v4.1.0) - CHAT MODE")
        print("="*60)
        self.config.display()
        if self.tools_available:
            tool_count = 0
            if self.tools:
                tool_count += len(self.tools.tools)
            if self.mcp_manager:
                tool_count += len(self.mcp_manager.get_all_tools_for_openai())
            print(f"🔧 Tools: {tool_count} available")
            print("⚡ I take action immediately - just ask!")
            print("🕐 Time/Date | 🔍 Web Search | 📁 Files | 🌐 Browser | 💻 Commands")
        print("\nCommands: help, clear, quit, agent")
        print("-"*60)
        
        while True:
            try:
                user_input = input("\n👤 You: ").strip()
                if not user_input:
                    continue
                
                if user_input.lower() in ['quit', 'exit', 'q']:
                    print("\n👋 Goodbye!")
                    break
                
                if user_input.lower() in ['clear', 'reset']:
                    self.clear()
                    print("🔄 Cleared!")
                    continue
                
                if user_input.lower() == 'agent':
                    print("\n🤖 Switching to Agent Mode...")
                    print("To use agent mode programmatically, see example_agent_usage.py")
                    print("Agent mode allows you to:")
                    print("  - Run tasks programmatically")
                    print("  - Execute multi-step workflows")
                    print("  - Monitor task progress")
                    print("  - Get detailed reports")
                    continue
                
                if user_input.lower() == 'help':
                    print("\n📖 I'm an autonomous agent - I act immediately!")
                    print("\nExamples:")
                    print("  'what time is it?' → I'll get the time")
                    print("  'latest aviation accidents' → I'll search Google")
                    print("  'list files here' → I'll list directory")
                    print("  'open google.com' → I'll launch browser")
                    print("\nCommands:")
                    print("  quit/exit - Exit")
                    print("  clear - Clear history")
                    print("  agent - Info about agent mode")
                    continue
                
                print("\n🔄 Agent working...")
                response = await self.get_response(user_input)
                if response:
                    print(f"\n✅ {response}")
                else:
                    print("❌ Failed to get response")
                    
            except (KeyboardInterrupt, EOFError):
                print("\n\n👋 Goodbye!")
                break
            except Exception as e:
                logger.error(f"Error: {e}")
                print("❌ An error occurred")
        
        if self.tools:
            await self.tools.cleanup()


async def main():
    try:
        config = Config()
        if not config.validate():
            print("❌ API key not configured\nSet OPENAI_API_KEY in .env or environment")
            return 1
        
        agent = AIAgent(config)
        if not await agent.initialize():
            print("❌ Failed to initialize")
            return 1
        
        await agent.run()
        return 0
    except Exception as e:
        logger.error(f"Fatal: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))