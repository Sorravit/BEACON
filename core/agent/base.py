#!/usr/bin/env python3
"""
core/agent/base.py — the AIAgent class.

Composes the topic mixins and defines the agent lifecycle methods (init,
MCP loading, clear, shutdown, interactive/CLI run). Behaviour is identical to
the original monolithic ``main.py`` AIAgent class.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.agent.runtime import (
    logger,
    get_session_id,
    setup_telemetry,
    install_print_bridge,
    _TELEMETRY_AVAILABLE,
    AsyncOpenAI,
    OpenAI,
    MCPManager,
    ModelRegistry,
    SkillManager,
    VectorMemory,
    Mem0Memory,
    ToolManager,
    MCP_CONFIG_FILE,
)
from core.agent.config import Config
from core.agent.memory_mixin import MemoryMixin
from core.agent.skills_mixin import SkillsMixin
from core.agent.tooling_mixin import ToolingMixin
from core.agent.response_mixin import ResponseMixin
from core.agent.conversation_mixin import ConversationMixin


class AIAgent(MemoryMixin, SkillsMixin, ToolingMixin, ResponseMixin, ConversationMixin):
    """Main AI agent that handles conversations and tool execution."""

    def __init__(self, config: Config):
        """Initialize the AI agent with configuration."""
        self.config = config
        self.client: Optional[AsyncOpenAI] = None
        self.conversation: List[Dict[str, Any]] = []
        self.tools: Optional[ToolManager] = None
        self.mcp_manager: Optional[MCPManager] = None
        self.tools_available = False
        self.vector_memory: Optional[VectorMemory] = None
        self.memory_available = False
        self.skill_manager: Optional[SkillManager] = None
        self.memory_worker = None  # MemoryWorker (Phase 2)
        self.mem0_memory: Optional[Mem0Memory] = None  # mem0 auto-learning
        self._mem0_tasks: set = set()  # strong refs to fire-and-forget adds
        self.browser_pool = None  # BrowserPool  (Phase 6)
        self._last_dispatched_skill: str = ""
        # Cache for the (expensive, ~80k-char) tools prompt. Invalidated when the
        # available tool/MCP set changes (keyed on tool-name signature).
        self._tools_prompt_cache: Optional[str] = None
        self._tools_prompt_key: Optional[tuple] = None


    async def _get_browser_context(self, session_id: str = ""):
        """Return the BrowserContext for session_id from the shared pool."""
        if self.browser_pool is None:
            from core.browser_pool import BrowserPool
            self.browser_pool = BrowserPool()
        return await self.browser_pool.get_context(session_id or "default")

    async def initialize(self) -> bool:
        """
        Initialize the AI agent and its tools.
        
        Returns:
            bool: True if initialization successful, False otherwise.
        """
        try:
            # Phase 5 / #3: AsyncOpenAI client — no worker threads for LLM calls.
            # The SDK's own (httpx) timeout must be generous enough not to pre-empt
            # long, legitimately-streaming responses. Per-call inactivity limits are
            # enforced in get_response (LLM_STREAM_IDLE_TIMEOUT for streaming, a total
            # wall-clock guard for non-streaming), so here we set a large ceiling.
            _llm_timeout = float(os.getenv("LLM_REQUEST_TIMEOUT", "0"))
            _llm_retries = int(os.getenv("LLM_MAX_RETRIES", "2"))
            self.client = AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                timeout=_llm_timeout if _llm_timeout > 0 else None,
                max_retries=_llm_retries,
            )

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
                # Phase 2 / auto-memory: start the background extraction worker
                from core.memory_worker import MemoryWorker
                self.memory_worker = MemoryWorker(self)
                self.memory_worker.start()
                # ── mem0 auto-learning (lib mode, backed by same Weaviate) ──
                self.mem0_memory = Mem0Memory()
                _mem0_ok = await self.mem0_memory.initialize()
                if _mem0_ok:
                    logger.info("✅ mem0 auto-learning memory ready")
                else:
                    logger.warning("⚠️  mem0 unavailable — will rely on AutoLearned only")
                    self.mem0_memory = None
            else:
                logger.info("ℹ️  Vector memory unavailable — running without persistent memory")
            # Phase 6 / #2: create browser pool (lazy — Chromium launched on first use)
            from core.browser_pool import BrowserPool
            self.browser_pool = BrowserPool()

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
                            "SERVER LAUNCH RULE:\n"
                            "- To start a web server, ASGI/WSGI server, or any long-lived/never-returning process\n"
                            "  (uvicorn, gunicorn, flask run, manage.py runserver, python -m http.server,\n"
                            "   npm start, npm run dev, yarn dev, pnpm dev, next dev, serve, nohup ...)\n"
                            "  ALWAYS use delegate_background_task — NEVER execute_command or execute_long_command.\n"
                            "  execute_command blocks for 30s then kills the server; execute_long_command blocks for 2h.\n\n"
                            "- One-shot commands (pip install, pytest, build, grep, etc.) use execute_command as normal.\n\n"
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


    def clear(self):
        """Clear all conversation history"""
        self.conversation.clear()

    async def shutdown(self):
        """Shut down agent resources: worker, browser pool, tools, memory."""
        # Stop the auto-memory background worker first
        try:
            if self.memory_worker:
                await self.memory_worker.stop()
            if self.mem0_memory:
                self.mem0_memory = None
                logger.info("mem0 memory released")
        except Exception as e:
            logger.debug(f"MemoryWorker shutdown failed: {e}")

        try:
            if self.tools:
                await self.tools.cleanup()
        except Exception as e:
            logger.debug(f"Tool cleanup during shutdown failed: {e}")

        # Phase 6 / #2: shut down the browser pool
        try:
            if self.browser_pool:
                await self.browser_pool.shutdown()
                self.browser_pool = None
        except Exception as e:
            logger.debug(f"BrowserPool shutdown failed: {e}")

        # Always attempt vector memory shutdown last so loky resources are released.
        try:
            if self.vector_memory:
                self.vector_memory.close()
        except Exception as e:
            logger.debug(f"Vector memory shutdown failed: {e}")

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
                print("=" * 60)
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

                print("\n" + "=" * 60)
                print(f"{timestamp} - 🏁 TASK COMPLETED")
                print(f"⏱️  Total time: {duration_str}")
                print("=" * 60)
                print(f"Status: {result.get('status', 'unknown')}")
                print(f"Success: {result.get('success', False)}")

                if result.get('result'):
                    print("\nRESULT:")
                    print("-" * 20)
                    print(result['result'])
                    print("-" * 20)
                elif result.get('error'):
                    print(f"\nERROR: {result['error']}")

                print("=" * 60 + "\n")

                await self.shutdown()
                return result

            print("=" * 60)
            print("🤖 AGENT MODE - Programmatic Interface")
            print("=" * 60)
            print("Agent is ready for programmatic task execution.")
            print("Use the AgentAPI to run tasks programmatically.")
            print("See example_agent_usage.py for examples.")
            print("-" * 60)
            print("\nTo run a task directly from CLI:")
            print(f"  python {sys.argv[0]} --mode agent \"your task here\"")
            print("-" * 60)
            return api

        # Chat mode - interactive conversation
        print("=" * 60)
        print("🤖 AUTONOMOUS AI AGENT (v4.1.0) - CHAT MODE")
        print("=" * 60)
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
        print("-" * 60)

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
