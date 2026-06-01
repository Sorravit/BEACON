#!/usr/bin/env python3
"""
AI Assistant - Agent with Skills
Version: 4.2.0 (MCP Integration)

A production-ready AI assistant with MCP support, browser automation, HTTP requests, and file operations.
"""

import asyncio
import json
import logging
import logging.handlers
import os
import sys
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

# Suppress noisy deprecation warnings from third-party libraries
warnings.filterwarnings("ignore", category=DeprecationWarning, module="authlib")
warnings.filterwarnings("ignore", message=".*authlib.jose.*")

from openai import OpenAI
from core.mcp_client import MCPManager
from core.models import ModelRegistry
from core.skills import SkillManager
from core.vector_memory import VectorMemory
from tools.manager import ToolManager

# ============================================================================
# CONFIGURATION CONSTANTS - Modify these to customize behavior
# ============================================================================

# Version
VERSION = "4.2.0"

# Logging
os.makedirs("logs", exist_ok=True)
LOG_FILE = "logs/ai_assistant.log"

# AI Model Configuration
DEFAULT_MODEL = "gpt-3.5-turbo"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 4096
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
# tiktoken gives accurate counts now — raised from 80k. Actual Claude limit is 200k.
# and the system message (with tools XML) + memory context add thousands of untracked tokens.
MAX_CONVERSATION_TOKENS = 80000
# Max characters for the memory context block injected per user message
MAX_MEMORY_CONTEXT_CHARS = 3000

# MCP Configuration
MCP_CONFIG_FILE = "mcp_config.json"

# ============================================================================

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,
            encoding="utf-8",
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)



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

        # Curated registry of selectable models + per-role defaults (models.yaml).
        # Loaded once here so chat and orchestration share a single source of truth.
        self.models = ModelRegistry.load(env_default=self.model)
        # Honour the registry default unless AI_MODEL was explicitly set.
        if "AI_MODEL" not in os.environ:
            self.model = self.models.default_model

    def resolve_model(self, requested: Optional[str] = None, role: Optional[str] = None) -> str:
        """Resolve a requested/role model id to a valid registered model.

        Resolution order: an explicit valid ``requested`` id → the role default
        from models.yaml → the global default. The global ``self.model`` is only
        used as the request when no role is supplied, so role-scoped sub-agents
        correctly pick up their per-role defaults instead of the chat model.
        """
        if requested is None and role is None:
            requested = self.model
        return self.models.resolve(requested, role=role)

    def validate(self) -> bool:
        """Validate that required configuration is present."""
        return bool(self.api_key)

    def display(self):
        """Display current configuration."""
        print(f"Model: {self.model}")
        print(f"Endpoint: {self.base_url}")
        print(f"Tools: {self.enable_tools}")
        print(f"Models available: {len(self.models.ids())}")


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
        self.vector_memory: Optional[VectorMemory] = None
        self.memory_available = False
        self.skill_manager: Optional[SkillManager] = None
        self._shared_browser = None  # shared Playwright browser process
        self._playwright = None

    async def _get_shared_browser(self):
        """Lazily create ONE shared Chromium process for all per-request ToolManagers.
        Each ToolManager creates its own isolated BrowserContext from this browser.
        """
        if self._shared_browser is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._shared_browser = await self._playwright.chromium.launch(headless=False)
            logger.info("Shared browser process started (lazy init)")
        return self._shared_browser
    
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

            # Discover Agent Skills (SKILL.md playbooks) — progressive disclosure
            self.skill_manager = SkillManager.load()
            if self.skill_manager.has_skills():
                logger.info("✅ %d skill(s) available: %s",
                            len(self.skill_manager), ", ".join(self.skill_manager.names()))
            
            # Initialize vector memory (Weaviate) — optional, graceful if unavailable
            weaviate_port = int(os.getenv("WEAVIATE_PORT", "8090"))
            self.vector_memory = VectorMemory(
                openai_api_key=self.config.api_key,
                weaviate_url=f"http://localhost:{weaviate_port}",
                openai_base_url=self.config.base_url,
            )
            self.memory_available = await self.vector_memory.initialize()
            if self.memory_available:
                logger.info("✅ Vector memory (Weaviate) available")
            else:
                logger.info("ℹ️  Vector memory unavailable — running without persistent memory")

            if self.config.enable_tools:
                self.tools = ToolManager(vector_memory=self.vector_memory, mcp_manager=self.mcp_manager, skill_manager=self.skill_manager)
                self.tools_available = await self.tools.initialize()

                # mcp_manager already loaded above — update reference in ToolManager
                if self.tools:
                    self.tools.mcp_manager = self.mcp_manager
                
                # Add system message establishing tool usage context
                if self.tools_available:
                    # Ensure output/temp directories exist for AI-generated files
                    Path("output").mkdir(exist_ok=True)
                    Path("temp").mkdir(exist_ok=True)
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
                            "FILE CREATION RULES (ALWAYS FOLLOW):\n"
                            "9. ALWAYS save screenshots, images, and temp files to the 'output/' folder (e.g. output/screenshot.png)\n"
                            "10. ALWAYS save intermediate/scratch files to the 'temp/' folder (e.g. temp/work.txt)\n"
                            "11. NEVER create temp/image files in the project root directory\n"
                            "12. If a filename has no directory prefix, prepend 'output/' automatically\n\n"
                            "BACKGROUND TASK CAPABILITIES — CRITICAL:\n"
                            "You CAN proactively send messages into this chat using background tasks.\n"
                            "'Send me', 'tell me', 'notify me', 'alert me', 'say X to me' = use delegate_background_task.\n\n"
                            "HOW TO SEND MESSAGES TO CHAT FROM A BACKGROUND TASK:\n"
                            "Any line printed by the background script that starts with a special prefix\n"
                            "is AUTOMATICALLY injected as a message into THIS chat within 3 seconds.\n\n"
                            "The prefixes are:\n"
                            "  print('NOTIFY: <text>')   → appears as info message in chat\n"
                            "  print('SUCCESS: <text>')  → appears as green success message in chat\n"
                            "  print('WARNING: <text>')  → appears as yellow warning in chat\n"
                            "  print('ALERT: <text>')    → appears as red urgent alert in chat\n\n"
                            "IMPORTANT: Plain print() WITHOUT the prefix only goes to the log file, NOT the chat.\n"
                            "You MUST use print('NOTIFY: ...') or similar prefix to send to chat.\n\n"
                            "EXAMPLE — 'say hi to me every 10 seconds':\n"
                            "Step 1: Write this to temp/say_hi.py:\n"
                            "  import time\n"
                            "  for i in range(6):\n"
                            "      print('NOTIFY: Hi! This is message number', i+1)\n"
                            "      time.sleep(10)\n"
                            "Step 2: Call delegate_background_task(name='say_hi', command='python temp/say_hi.py', interval_seconds='0')\n"
                            "Result: 'NOTIFY: Hi! This is message number 1' appears in THIS chat after 3 seconds.\n\n"
                            "EXAMPLE — 'alert me every minute with CPU usage':\n"
                            "  import time, subprocess\n"
                            "  while True:\n"
                            "      cpu = subprocess.check_output(['top', '-l1', '-n0']).decode()\n"
                            "      print('NOTIFY: CPU status -', cpu[:100])\n"
                            "      time.sleep(60)\n\n"
                            "NEVER use plain print() when you want to send something to the chat.\n"
                            "ALWAYS use print('NOTIFY: ...') to send messages to the chat.\n"
                            "NEVER say 'I cannot send messages to this chat' — you CAN via delegate_background_task.\n\n"
                            "Example: User asks 'what time is it?' → Call get_current_time() → Return the time\n"
                            "Example: User asks 'latest aviation accident' → Call web_search('latest aviation accident') → Summarize results\n"
                            "Example: Taking a screenshot → use filename 'output/screenshot.png' not 'screenshot.png'\n\n"
                            "LARGE FILE HANDLING RULES:\n"
                            "17. If read_file returns a very large result, DO NOT try to read it again — it will be the same size.\n"
                            "18. For large files, use execute_command with grep/head/tail/sed to navigate:\n"
                            "    - Search: grep -n 'keyword' filename\n"
                            "    - First N lines: head -100 filename\n"
                            "    - Last N lines: tail -100 filename\n"
                            "    - Line range: sed -n '100,200p' filename\n"
                            "    - Count lines: wc -l filename\n"
                            "19. Never include raw binary or huge JSON blobs in your response — summarise them.\n\n"
                            "BE PROACTIVE. ACT FIRST. EXPLAIN AFTER."
                        )
                    }
                    # Progressive disclosure: append the skills catalog (names +
                    # descriptions only) so the model knows what playbooks exist
                    # and can load the full body on demand via load_skill.
                    if self.skill_manager and self.skill_manager.has_skills():
                        system_message["content"] += self.skill_manager.system_prompt_block()
                    self.conversation.append(system_message)
                    
            logger.info("Agent initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Agent initialization failed: {e}")
            return False
    
    async def _load_mcp_servers(self):
        """Load MCP servers from config file"""
        try:
            config_file = Path(MCP_CONFIG_FILE)
            if not config_file.exists():
                logger.info(f"No {MCP_CONFIG_FILE} found, skipping MCP servers")
                return
            
            with open(config_file) as f:
                mcp_config = json.load(f)
            
            servers = mcp_config.get("servers", {})
            for server_name, server_config in servers.items():
                command = server_config.get("command")
                args = server_config.get("args", [])
                env = server_config.get("env", None)

                if command and self.mcp_manager:
                    logger.info(f"Loading MCP server: {server_name}")
                    success = await self.mcp_manager.add_server(server_name, command, args, env=env)
                    if success:
                        logger.info(f"✅ MCP server {server_name} loaded")
                    else:
                        logger.warning(f"⚠️  Failed to load MCP server {server_name}")
        
        except Exception as e:
            logger.error(f"Error loading MCP servers: {e}")
    
    async def _extract_and_store_facts(self, user_input: str):
        """
        Detect personal facts in user input and store them in vector memory.
        Examples: "I have a Samsung oven", "my name is John", "I live in Bangkok"
        """
        # Simple heuristic patterns that suggest personal facts
        fact_patterns = [
            "i have", "i own", "i use", "i am", "i'm", "my name is",
            "i live", "i work", "my ", "i prefer", "i like", "i hate",
            "i drive", "i eat", "i drink", "i take", "i need",
        ]
        lower = user_input.lower()
        if not any(p in lower for p in fact_patterns):
            return

        # Ask the AI to extract the fact cleanly
        try:
            extraction_prompt = (
                f'Extract personal facts from this message as JSON.\n'
                f'Message: "{user_input}"\n'
                f'Respond ONLY with JSON like: {{"topic": "oven", "fact": "I have a Samsung NV75K5571RS oven"}}\n'
                f'If no personal fact found, respond: {{"topic": null, "fact": null}}'
            )
            _loop = asyncio.get_running_loop()
            _extract_params = dict(
                model=self.config.model,
                messages=[{"role": "user", "content": extraction_prompt}],
                temperature=0,
                max_tokens=100
            )
            extraction_response = await _loop.run_in_executor(
                None, lambda: self.client.chat.completions.create(**_extract_params)
            )
            text = extraction_response.choices[0].message.content or ""
            # Parse JSON
            json_start = text.find('{')
            json_end = text.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                data = json.loads(text[json_start:json_end])
                topic = data.get("topic")
                fact = data.get("fact")
                if topic and fact and self.vector_memory:
                    await self.vector_memory.store_personal_fact(topic, fact)
                    print(f"  🧠 Remembered: [{topic}] {fact}")
        except Exception as e:
            logger.debug(f"Fact extraction skipped: {e}")


    async def _auto_memory_hook(self, user_input: str, ai_response: str):
        """
        Auto-learning memory hook — fires after every final AI response.
        Sends the exchange to Claude Sonnet to extract meaningful facts about
        the user, then stores them in the AutoLearned Weaviate collection.
        Completely separate from PersonalFacts (explicit memory).
        Non-blocking: always called via asyncio.create_task().
        """
        if not self.memory_available or not self.vector_memory:
            return
        # Skip trivial exchanges (one-liners, time queries, greetings)
        if len(user_input.strip()) < 20 or len(ai_response.strip()) < 20:
            return
        try:
            # Get existing auto-facts topics to avoid duplication
            existing = await self.vector_memory.get_all_auto_facts() or []
            existing_topics = [f.get('topic', '').lower() for f in existing]
            existing_summary = ', '.join(existing_topics[:50]) if existing_topics else 'none yet'

            # Get existing personal facts topics too
            personal = await self.vector_memory.get_all_personal_facts() or []
            personal_topics = [f.get('topic', '').lower() for f in personal]
            personal_summary = ', '.join(personal_topics[:50]) if personal_topics else 'none'

            extraction_prompt = f"""You are a memory extraction assistant. Analyze this conversation exchange and extract meaningful facts about the USER (not about AI, not tool outputs).

Conversation:
User: {user_input[:1000]}
Assistant: {ai_response[:800]}

Already known personal facts (do NOT re-extract these topics): {personal_summary}
Already auto-learned topics (update if changed, skip if same): {existing_summary}

Extract facts about:
- User preferences, dislikes, opinions
- Projects they work on, tools they use
- People they mention (names, roles, relationships)
- Technical environment (OS, languages, stack)
- Decisions they made
- Problems they solved or encountered
- Things they corrected the AI about
- Work context and habits

Do NOT extract:
- Questions the user asked
- AI responses or tool results
- One-time lookups (time, weather)
- Greetings or small talk
- Anything already in personal facts

Respond ONLY with a JSON array. Each item: {{"topic": "short_snake_case_key", "fact": "concise fact sentence", "confidence": "high|medium|low"}}
If nothing meaningful to extract, respond with: []

JSON:"""

            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.client.chat.completions.create(
                    model=self.config.model,
                    messages=[{"role": "user", "content": extraction_prompt}],
                    temperature=0.2,
                    max_tokens=800
                )
            )

            raw = result.choices[0].message.content.strip()
            # Strip markdown code fences if present
            if raw.startswith('```'):
                raw = raw.split('```')[1]
                if raw.startswith('json'):
                    raw = raw[4:]
            raw = raw.strip()

            import json as _json
            facts = _json.loads(raw)
            if not isinstance(facts, list):
                return

            stored_count = 0
            for item in facts:
                topic = item.get('topic', '').strip()
                fact = item.get('fact', '').strip()
                confidence = item.get('confidence', 'medium')
                if topic and fact and len(fact) > 5:
                    ok = await self.vector_memory.store_auto_fact(topic, fact, confidence)
                    if ok:
                        stored_count += 1

            if stored_count > 0:
                logger.info(f"Auto-memory hook stored {stored_count} new/updated facts")

        except Exception as e:
            logger.debug(f"Auto-memory hook failed (non-critical): {e}")

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
        
        # ── Format 1: <tool_use>...<tool_name>NAME</tool_name>...</tool_use> ──
        # Also tolerates mismatched closing tags: </tool_invoke>, </tool_call>
        pattern = r'<tool_use>(.*?)</(?:tool_use|tool_invoke|tool_call|use)>'
        matches = re.findall(pattern, response_text, re.DOTALL)

        # ── Format 2: <function_calls><invoke name="NAME">...</invoke></function_calls> ──
        # Claude sometimes uses this anthropic-native format for MCP tools.
        invoke_pattern = r'<invoke\s+name=["\']([^"\']+)["\']>(.*?)</invoke>'
        for inv_name, inv_body in re.findall(invoke_pattern, response_text, re.DOTALL):
            # Convert <parameter name="key">value</parameter> → JSON dict
            params = {}
            for p_name, p_val in re.findall(r'<parameter\s+name=["\']([^"\']+)["\']>(.*?)</parameter>', inv_body, re.DOTALL):
                params[p_name] = p_val.strip()
            tool_calls.append({"tool_name": inv_name.strip(), "parameters": params})
            logger.warning(f"Parsed function_calls/invoke format for tool: {inv_name.strip()}")

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
                            # Try 1: repair truncated JSON (max_tokens cut off closing brace)
                            open_count = params_text.count('{')
                            close_count = params_text.count('}')
                            if open_count > close_count:
                                repaired = params_text.rstrip() + '}' * (open_count - close_count)
                                try:
                                    parameters = json.loads(repaired)
                                    logger.warning(f"Repaired truncated parameters JSON")
                                    # Successfully repaired — skip XML fallback
                                except json.JSONDecodeError:
                                    parameters = None
                            else:
                                parameters = None

                            # Try 2: model used XML-style params <key>value</key> instead of JSON
                            if parameters is None:
                                xml_params = re.findall(r'<(\w+)>(.*?)</\1>', params_text, re.DOTALL)
                                if xml_params:
                                    parameters = {k: v.strip() for k, v in xml_params}
                                    logger.warning(f"Parsed XML-style parameters: {list(parameters.keys())}")
                                else:
                                    logger.error(f"Failed to parse parameters: {params_text[:200]}")
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
    
    async def get_response(self, user_input: str, conversation: Optional[List[Dict]] = None, tools: Optional["ToolManager"] = None, token_callback=None, model: Optional[str] = None) -> Optional[str]:
        """
        Get AI response with prompt-based tool execution loop (Cline/Roo style).
        
        Args:
            user_input: User's message
            conversation: Optional external conversation list (stateless/per-request mode).
                          If None, falls back to self.conversation (CLI/backward-compat mode).
            tools: Optional per-request ToolManager. If provided, used instead of self.tools so
                   concurrent requests never share or mutate the agent's tool reference.
            model: Optional model id override for this call. Resolved against the
                   curated registry; invalid ids fall back to the chat default.
            
        Returns:
            Optional[str]: AI response or None if error occurred
        """
        if not self.client:
            return None

        # Resolve the model for this call from the curated registry (never trust
        # a raw client value — an unknown id degrades to the chat default).
        effective_model = self.config.resolve_model(model, role="chat")

        # Resolve which ToolManager to use for this call — never mutate self.tools
        effective_tools = tools if tools is not None else self.tools

        # Use passed-in conversation or fall back to self.conversation (backward compat)
        if conversation is not None:
            conv = conversation  # stateless mode: caller owns this list
        else:
            conv = self.conversation  # CLI mode: mutate self.conversation as before
        
        # Build tools prompt once and store it for use in the first iteration only.
        # We do NOT permanently inject it into conv[0] because:
        #   1. conv[0] is a shared reference to agent.conversation[0] — mutating it
        #      would corrupt the shared system message across sessions.
        #   2. Including the full 80k-char tools prompt in EVERY iteration (including
        #      the tool-results feedback round-trip) bloats the payload and causes
        #      IBM ICA (and other APIs with tight context limits) to return 400.
        # Instead, we pass a _first-iteration-only_ system message with the tools
        # description, and strip it back to the base system content for subsequent
        # iterations.
        _base_system_content = conv[0]["content"] if conv and conv[0]["role"] == "system" else ""
        _tools_prompt = self._build_tools_prompt() if self.tools_available else ""

        try:
            # (tools prompt is applied per-iteration below, not permanently here)
            pass

            # --- Vector memory: detect and store personal facts from user input ---
            if self.memory_available and self.vector_memory:
                await self._extract_and_store_facts(user_input)

            # --- Vector memory: inject relevant context as prefix (capped size) ---
            memory_prefix = ""
            if self.memory_available and self.vector_memory:
                raw_prefix = await self.vector_memory.build_context_prompt(user_input)
                # Cap memory context to avoid token overflow
                if raw_prefix and len(raw_prefix) > MAX_MEMORY_CONTEXT_CHARS:
                    raw_prefix = raw_prefix[:MAX_MEMORY_CONTEXT_CHARS] + "\n...[memory truncated]\n\n"
                memory_prefix = raw_prefix

            user_message = (memory_prefix + user_input) if memory_prefix else user_input
            conv.append({"role": "user", "content": user_message})

            # Trim AFTER adding the new message so trimming sees the full picture
            self._trim_conversation(conv)
            
            # Agent loop - allow multiple tool calls
            # For long-running tasks (courses, etc.), use a very high limit
            # Configured via MAX_TOOL_ITERATIONS constant at top of file
            for iteration in range(MAX_TOOL_ITERATIONS):
                # On the first iteration: inject tools prompt into system message so
                # the model knows the tool format.  On subsequent iterations (tool
                # results feedback): use the base system content only to keep the
                # payload small and avoid 400 errors from context-size limits.
                if conv and conv[0]["role"] == "system":
                    if iteration == 0 and _tools_prompt:
                        conv[0] = {"role": "system", "content": _base_system_content + _tools_prompt}
                    else:
                        conv[0] = {"role": "system", "content": _base_system_content}

                params = {
                    "model": effective_model,
                    "messages": conv,
                    "temperature": self.config.temperature,
                    "max_tokens": self.config.max_tokens
                }
                
                # NO tools parameter - use prompt-based approach instead
                
                _loop = asyncio.get_running_loop()
                if token_callback:
                    # STREAMING MODE — entire chunk iteration runs in executor thread
                    # so the async event loop stays free to flush SSE tokens to the
                    # browser in real time. This fixes the UI freeze / step-112 lockup
                    # where tokens were generated but the browser never received them
                    # because the sync for-loop was blocking the event loop.
                    import queue as _queue
                    _token_q: _queue.Queue = _queue.Queue()
                    stream_params = {**params, 'stream': True}

                    def _stream_in_thread():
                        try:
                            stream = self.client.chat.completions.create(**stream_params)
                            for chunk in stream:
                                delta = chunk.choices[0].delta.content if chunk.choices else None
                                if delta:
                                    _token_q.put(delta)
                        except Exception as _e:
                            _token_q.put(_e)
                        finally:
                            _token_q.put(None)  # sentinel — stream finished

                    # Launch blocking stream iteration in thread pool
                    _stream_future = _loop.run_in_executor(None, _stream_in_thread)

                    response_text = ''
                    while True:
                        # Poll queue; yield to event loop between polls so SSE can flush
                        try:
                            item = _token_q.get_nowait()
                        except _queue.Empty:
                            await asyncio.sleep(0)  # yield → event loop flushes SSE
                            continue
                        if item is None:
                            break  # sentinel: stream done
                        if isinstance(item, Exception):
                            raise item
                        response_text += item
                        # Do NOT emit tokens yet — buffer until we know this is
                        # the final response (no tool calls). Replayed below.

                    await _stream_future  # ensure thread is fully joined
                else:
                    # NON-STREAMING MODE: CLI / planning / background tasks
                    response = await _loop.run_in_executor(
                        None, lambda: self.client.chat.completions.create(**params)
                    )
                    response_text = response.choices[0].message.content
                
                if not response_text:
                    return "No response from AI"
                
                # Parse tool calls from response
                tool_calls = self._parse_tool_calls(response_text)
                
                if tool_calls:
                    # Add assistant's response with tool calls
                    conv.append({
                        "role": "assistant",
                        "content": response_text
                    })
                    
                    # Execute all tool calls
                    tool_results = []
                    for tool_call in tool_calls:
                        tool_name = tool_call["tool_name"]
                        tool_args = tool_call["parameters"]
                        
                        print(f"\033[92m  🔧 Executing: {tool_name}({', '.join(f'{k}={str(v)}' for k, v in tool_args.items()) if tool_args else ''})\033[0m")
                        
                        # Route ALL tool calls (built-in and MCP) through effective_tools.execute_tool()
                        # so web_app.py's instrumented wrapper emits SSE events for every call,
                        # including MCP tools. execute_tool() now falls through to mcp_manager for
                        # tools not in tool_handlers.
                        if effective_tools:
                            tool_result = await effective_tools.execute_tool(tool_name, tool_args)
                        else:
                            tool_result = json.dumps({"error": "No tool manager available"})
                        
                        # Print tool result in cyan
                        result_preview = str(tool_result)
                        if len(result_preview) > 500:
                            result_preview = result_preview[:500] + "... [truncated]"
                        print(f"\033[96m  📤 Result: {result_preview}\033[0m")
                        
                        # --- Store tool result in vector memory ---
                        if self.memory_available and self.vector_memory:
                            query_hint = str(list(tool_args.values())[0]) if tool_args else tool_name
                            await self.vector_memory.store_research(
                                tool_name=tool_name,
                                query=query_hint[:200],
                                result=str(tool_result)
                            )

                        tool_results.append(f"Tool: {tool_name}\nResult: {tool_result}")
                    
                    # Notify instead of silently truncate — tell the AI the result is large
                    MAX_RESULT_CHARS = 80000  # ~26k tokens — warn if larger but don't silently cut off
                    notified_results = []
                    for tr in tool_results:
                        if len(tr) > MAX_RESULT_CHARS:
                            # Don't silently truncate — tell the AI the result is large so it can navigate it
                            preview = tr[:3000]
                            size_kb = len(tr) // 1024
                            notified_results.append(
                                f"[RESULT TOO LARGE: {size_kb}KB — only first 3KB shown below. "
                                f"Use execute_command with grep/head/tail/sed to search/navigate this content "
                                f"rather than reading the whole thing at once.]\n\n{preview}\n\n[...{size_kb-3}KB more not shown]"
                            )
                        else:
                            notified_results.append(tr)
                    results_text = "\n\n".join(notified_results)
                    conv.append({
                        "role": "user",
                        "content": f"Tool execution results:\n\n{results_text}\n\nPlease provide your final response based on these results."
                    })
                    
                    # Continue loop to let AI process tool results
                    continue
                else:
                    # No tool calls — final response. NOW stream tokens to UI.
                    # This means the browser never sees raw <tool_use> XML blocks
                    # from intermediate iterations — only the clean final answer.
                    if token_callback:
                        import re as _re
                        clean = _re.sub(r'<tool_use>.*?</tool_use>', '', response_text, flags=_re.DOTALL).strip()
                        for ch in clean:
                            try:
                                token_callback(ch)
                            except Exception:
                                pass
                    conv.append({"role": "assistant", "content": response_text})
                    # Fire auto-learning hook (non-blocking)
                    try:
                        asyncio.create_task(self._auto_memory_hook(user_input, response_text))
                    except Exception:
                        pass
                    return response_text
            
            # If we hit max iterations, inform user but don't fail
            logger.warning(f"Reached maximum iterations ({MAX_TOOL_ITERATIONS}). Task may not be complete.")
            return f"I've executed {MAX_TOOL_ITERATIONS} tool calls. The task may need to be continued. Please check the current state and let me know if you want me to continue."
                
        except Exception as e:
            logger.error(f"Error: {e}")
            import traceback
            traceback.print_exc()
            if conv and conv[-1]["role"] == "user":
                conv.pop()
            raise  # Re-raise so web_app.py can surface it to the frontend
    
    def _estimate_tokens(self, text: str) -> int:
        """
        Accurate token count using tiktoken (cl100k_base covers gpt-4, gpt-4o, claude via proxy).
        Falls back to conservative char-based estimate if tiktoken unavailable.
        """
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            # Fallback: len//2 is safer than //3 for Thai/CJK text
            return max(1, len(text) // 2)
    
    def _get_conversation_tokens(self, conv: List[Dict]) -> int:
        """Calculate total tokens in a conversation list"""
        total = 0
        for msg in conv:
            content = msg.get("content", "")
            total += self._estimate_tokens(content)
        return total
    
    def _trim_conversation(self, conv: List[Dict]):
        """
        Dynamically trim conversation history based on token count.
        Keeps system message and as many recent messages as possible within token limit.
        Operates on the passed-in conversation list (works for both self.conversation and
        per-request conversation lists).
        """
        total_tokens = self._get_conversation_tokens(conv)
        
        # If under limit, no trimming needed
        if total_tokens <= MAX_CONVERSATION_TOKENS:
            return
        
        # Keep system message (first message with tools description)
        system_msg = None
        start_idx = 0
        if conv and conv[0]["role"] == "system":
            system_msg = conv[0]
            start_idx = 1
        
        # Calculate tokens for system message
        system_tokens = self._estimate_tokens(system_msg["content"]) if system_msg else 0
        available_tokens = MAX_CONVERSATION_TOKENS - system_tokens
        
        # Keep as many recent messages as possible within token limit
        kept_messages = []
        current_tokens = 0
        
        # Iterate from most recent to oldest
        for msg in reversed(conv[start_idx:]):
            msg_tokens = self._estimate_tokens(msg.get("content", ""))
            if current_tokens + msg_tokens <= available_tokens:
                kept_messages.insert(0, msg)  # Insert at beginning to maintain order
                current_tokens += msg_tokens
            else:
                break  # Stop when we would exceed limit
        
        # Rebuild conversation in-place
        conv.clear()
        if system_msg:
            conv.append(system_msg)

        # Guard: OpenAI requires first non-system message to be 'user'
        # Trimming can leave an orphaned 'assistant' message at the front
        while kept_messages and kept_messages[0]["role"] != "user":
            logger.debug(f"Trim: dropping orphaned leading '{kept_messages[0]['role']}' message to satisfy OpenAI role ordering")
            kept_messages.pop(0)

        conv.extend(kept_messages)
        
        new_total = self._get_conversation_tokens(conv)
        logger.info(f"Trimmed conversation: {total_tokens} → {new_total} tokens ({len(kept_messages)} messages kept)")
    
    def clear(self):
        """Clear all conversation history"""
        self.conversation.clear()

    async def shutdown(self):
        """Shut down agent resources including the shared browser process."""
        if self.tools:
            await self.tools.cleanup()
        # Close shared browser process owned by this agent
        try:
            if self._shared_browser:
                await self._shared_browser.close()
                self._shared_browser = None
        except:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
        except:
            pass
        if self.vector_memory:
            self.vector_memory.close()

    async def run(self, mode: str = "chat", task: Optional[str] = None):
        """
        Run the agent in specified mode.
        
        Args:
            mode: "chat" for interactive chat, "agent" for programmatic/task mode
            task: Optional task to run immediately in agent mode
        """
        if mode == "agent":
            # Agent mode - programmatic/task interface
            from api.agent_api import AgentAPI
            api = AgentAPI(self)
            
            if task:
                from datetime import datetime
                start_time = datetime.now()
                timestamp = start_time.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
                print(f"\n{timestamp} - 🚀 Running task: {task}")
                print("="*60)
                result = await api.run_task(task)
                
                end_time = datetime.now()
                timestamp = end_time.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
                
                # Calculate total elapsed time in human-readable format
                duration_secs = result.get('duration_seconds') or (end_time - start_time).total_seconds()
                hours, remainder = divmod(int(duration_secs), 3600)
                minutes, seconds = divmod(remainder, 60)
                if hours > 0:
                    duration_str = f"{hours}h {minutes}m {seconds}s"
                elif minutes > 0:
                    duration_str = f"{minutes}m {seconds}s"
                else:
                    duration_str = f"{duration_secs:.2f}s"
                
                print("\n" + "="*60)
                print(f"{timestamp} - 🏁 TASK COMPLETED")
                print(f"⏱️  Total time: {duration_str}")
                print("="*60)
                print(f"Status: {result.get('status', 'unknown')}")
                print(f"Success: {result.get('success', False)}")
                
                if result.get('result'):
                    print("\nRESULT:")
                    print("-" * 20)
                    print(result['result'])
                    print("-" * 20)
                elif result.get('error'):
                    print(f"\nERROR: {result['error']}")
                
                print("="*60 + "\n")
                
                await self.shutdown()
                return result

            print("="*60)
            print("🤖 AGENT MODE - Programmatic Interface")
            print("="*60)
            print("Agent is ready for programmatic task execution.")
            print("Use the AgentAPI to run tasks programmatically.")
            print("See example_agent_usage.py for examples.")
            print("-" * 60)
            print("\nTo run a task directly from CLI:")
            print(f"  python {sys.argv[0]} --mode agent \"your task here\"")
            print("-" * 60)
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

        # Use prompt_toolkit for rich input: arrow keys, history recall (↑), no length limit
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory
            session = PromptSession(history=FileHistory(".chat_history"))
        except ImportError:
            session = None

        while True:
            try:
                from datetime import datetime
                prompt_label = f"\n👤 You [{datetime.now().strftime('%H:%M:%S')}]: "
                if session:
                    user_input = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: session.prompt(prompt_label)
                    )
                else:
                    user_input = input(prompt_label)
                user_input = user_input.strip()
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
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    print(f"\n✅ [{timestamp}] {response}")
                else:
                    print("❌ Failed to get response")
                    
            except (KeyboardInterrupt, EOFError):
                print("\n\n👋 Goodbye!")
                break
            except Exception as e:
                logger.error(f"Error: {e}")
                print("❌ An error occurred")
        
        await self.shutdown()


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Autonomous AI Agent")
    parser.add_argument("--mode", choices=["chat", "agent"], default="chat", help="Execution mode")
    parser.add_argument("task", nargs="*", help="Task to run (only for agent mode)")
    
    args = parser.parse_args()
    task_str = " ".join(args.task) if args.task else None

    try:
        config = Config()
        if not config.validate():
            print("❌ API key not configured\nSet OPENAI_API_KEY in .env or environment")
            return 1
        
        agent = AIAgent(config)
        if not await agent.initialize():
            print("❌ Failed to initialize")
            return 1
        
        await agent.run(mode=args.mode, task=task_str)
        return 0
    except Exception as e:
        logger.error(f"Fatal: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))