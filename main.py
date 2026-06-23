#!/usr/bin/env python3
# ── gRPC fork-safety (must be set BEFORE any grpc/otlp import) ─────────────
# Crash: EXC_BREAKPOINT / BUG IN CLIENT OF LIBDISPATCH: trying to lock
# recursively.  Root cause: subprocess.fork() called after grpc spawned
# background threads that hold libdispatch / XPC dispatch_once locks.
# Fix: tell grpc to support fork and avoid the broken kqueue poll strategy.
import os as _grpc_os
_grpc_os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "1")
_grpc_os.environ.setdefault("GRPC_POLL_STRATEGY", "poll")
_grpc_os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
_grpc_os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
# ────────────────────────────────────────────────────────────────────────────
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
import time
import warnings
from contextlib import nullcontext
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

# Suppress noisy deprecation warnings from third-party libraries
warnings.filterwarnings("ignore", category=DeprecationWarning, module="authlib")
warnings.filterwarnings("ignore", message=".*authlib.jose.*")
_rt_warning_rule = "ignore:resource_tracker:UserWarning"
_py_warnings = os.environ.get("PYTHONWARNINGS", "")
if _rt_warning_rule not in [w.strip() for w in _py_warnings.split(",") if w.strip()]:
    os.environ["PYTHONWARNINGS"] = (
        f"{_py_warnings},{_rt_warning_rule}" if _py_warnings else _rt_warning_rule
    )
warnings.filterwarnings(
    "ignore",
    message=r"resource_tracker: There appear to be \d+ leaked semaphore objects to clean up at shutdown:.*",
    category=UserWarning,
    module=r"multiprocessing\.resource_tracker",
)

from openai import AsyncOpenAI, OpenAI
from core.mcp_client import MCPManager
from core.models import ModelRegistry
from core.skills import SkillManager
from core.vector_memory import VectorMemory
from tools.manager import ToolManager

# ── Telemetry ─────────────────────────────────────────────────────────────────
try:
    from core.telemetry import record_llm_call, setup_telemetry, install_print_bridge
    from core.telemetry.context import get_session_id, get_reporter
    _TELEMETRY_AVAILABLE = True
except ImportError:
    _TELEMETRY_AVAILABLE = False
    def get_session_id(): return None  # type: ignore
    def get_reporter(): return None    # type: ignore
    def setup_telemetry(*a, **kw): pass  # type: ignore
    def install_print_bridge(*a, **kw): pass  # type: ignore


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
DEFAULT_MAX_TOKENS = 64000
DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Tool Execution Limits
# MAX_TOOL_ITERATIONS: Maximum number of tool calls per user message
# - 10_000_000 = effectively unlimited (token is free — run as long as needed)
# - Previous value was 1000 (~8-10 hours). Raised per user request.
# - This is a safety backstop only; agent exits normally via the 'no tool calls' branch.
MAX_TOOL_ITERATIONS = 10_000_000  # effectively unlimited — run as long as needed (token is free)

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


# ── Module-level cached tiktoken encoder (Phase 1 / #7) ──────────────────────
@lru_cache(maxsize=1)
def _get_encoder():
    """Return the cached cl100k_base tiktoken encoder (loaded once per process)."""
    import tiktoken
    return tiktoken.get_encoding("cl100k_base")


# ── LLM concurrency ──────────────────────────────────────────────────────────
# The previous global semaphore (LLM_MAX_CONCURRENCY) has been removed: this app
# serves a single local user, the per-session background-task guard already
# serialises a session's own turns, and the artificial cap added no protection
# that the per-call wall-clock timeout does not already provide. Every LLM call
# MUST still be bounded by a timeout (see get_response) so a stalled request can
# never hang forever.
#
# nullcontext keeps the existing ``async with _get_llm_sem():`` call sites
# working without re-indenting, while imposing no concurrency limit.


def _get_llm_sem():
    """No-op async context manager (LLM concurrency is unbounded)."""
    return nullcontext()



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
        self.client: Optional[AsyncOpenAI] = None
        self.conversation: List[Dict[str, Any]] = []
        self.tools: Optional[ToolManager] = None
        self.mcp_manager: Optional[MCPManager] = None
        self.tools_available = False
        self.vector_memory: Optional[VectorMemory] = None
        self.memory_available = False
        self.skill_manager: Optional[SkillManager] = None
        self.memory_worker = None          # MemoryWorker (Phase 2)
        self.browser_pool = None           # BrowserPool  (Phase 6)
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
            # IMPORTANT: an explicit timeout + retry budget is required. Without it
            # a half-open/stalled streaming connection hangs forever and keeps its
            # _llm_sem permit (see _get_llm_sem), which can exhaust the shared
            # concurrency pool and make every task/chat appear to "stop".
            _llm_timeout = float(os.getenv("LLM_REQUEST_TIMEOUT", "120"))
            _llm_retries = int(os.getenv("LLM_MAX_RETRIES", "2"))
            self.client = AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                timeout=_llm_timeout,
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
            _extract_params = dict(
                model=self.config.model,
                messages=[{"role": "user", "content": extraction_prompt}],
                temperature=0,
                max_tokens=100
            )
            import contextlib as _cl_ef
            _ef_ctx = record_llm_call(self.config.model, session_id=get_session_id()) if _TELEMETRY_AVAILABLE else _cl_ef.nullcontext()
            with _ef_ctx:
                # Phase 5: use native async client — no run_in_executor
                extraction_response = await self.client.chat.completions.create(**_extract_params)
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


    async def _auto_memory_extract(
        self, user_input: str, ai_response: str, session_id: str = ""
    ) -> int:
        """
        Extract facts from a conversation exchange and store them in AutoLearned.

        Renamed from _auto_memory_hook. Returns the number of facts stored (>= 0)
        so the MemoryWorker can accumulate the total.  Raises on hard failures
        so the worker can log at WARNING.

        Logging checkpoints (Step-5 requirements):
          (a) Entry   — session id, per-session turn counter, input sizes
          (b) Pre-LLM — model name, prompt char length
          (c) Post-LLM — raw LLM response char length
          (d) Per item found   — topic, confidence, fact preview
          (e) Per item skipped — topic/fact and reason
          (f) Exit    — total candidates parsed, total stored
        """
        # ── (a) ENTRY: increment per-session turn counter ────────────────────
        _turn_attr = "_am_turn_" + (session_id or "default").replace("-", "_").replace(".", "_")
        _turn = getattr(self, _turn_attr, 0) + 1
        setattr(self, _turn_attr, _turn)
        logger.info(
            "[AutoMemory] ENTER _auto_memory_extract "
            "turn=%d session=%s user_chars=%d ai_chars=%d",
            _turn, session_id or "?", len(user_input), len(ai_response),
        )

        # ── Memory guard ─────────────────────────────────────────────────────
        if not self.memory_available or not self.vector_memory:
            logger.info(
                "[AutoMemory] SKIP (memory unavailable) "
                "turn=%d session=%s memory_available=%s vector_memory=%s",
                _turn, session_id or "?",
                self.memory_available, bool(self.vector_memory),
            )
            return 0

        # Get existing topics to avoid duplication
        existing = await self.vector_memory.get_all_auto_facts() or []
        existing_topics = [f.get("topic", "").lower() for f in existing]
        existing_summary = ", ".join(existing_topics[:50]) if existing_topics else "none yet"

        # Get existing personal facts topics too
        personal = await self.vector_memory.get_all_personal_facts() or []
        personal_topics = [f.get("topic", "").lower() for f in personal]
        personal_summary = ", ".join(personal_topics[:50]) if personal_topics else "none"

        # Use cheaper model if configured via AUTO_LEARN_MODEL
        extract_model = os.getenv("AUTO_LEARN_MODEL", self.config.model)

        extraction_prompt = (
            "You are a memory extraction assistant. Analyze this conversation exchange"
            " and extract meaningful facts about the USER"
            " (not about AI, not tool outputs).\n\n"
            f"Conversation:\nUser: {user_input[:1000]}\nAssistant: {ai_response[:800]}\n\n"
            f"Already known personal facts (do NOT re-extract these topics): {personal_summary}\n"
            f"Already auto-learned topics (update if changed, skip if same): {existing_summary}\n\n"
            "Extract facts about:\n"
            "- User preferences, dislikes, opinions\n"
            "- Projects they work on, tools they use\n"
            "- People they mention (names, roles, relationships)\n"
            "- Technical environment (OS, languages, stack)\n"
            "- Decisions they made\n"
            "- Problems they solved or encountered\n"
            "- Things they corrected the AI about\n"
            "- Work context and habits\n\n"
            "Do NOT extract:\n"
            "- Questions the user asked\n"
            "- AI responses or tool results\n"
            "- One-time lookups (time, weather)\n"
            "- Greetings or small talk\n"
            "- Anything already in personal facts\n\n"
            'Respond ONLY with a JSON array. Each item: {"topic": "short_snake_case_key",'
            ' "fact": "concise fact sentence", "confidence": "high|medium|low"}\n'
            "If nothing meaningful to extract, respond with: []\n\n"
            "JSON:"
        )

        # ── (b) PRE-LLM call log ─────────────────────────────────────────────
        logger.info(
            "[AutoMemory] PRE-LLM turn=%d session=%s model=%s "
            "prompt_chars=%d existing_auto=%d existing_personal=%d",
            _turn, session_id or "?", extract_model, len(extraction_prompt),
            len(existing_topics), len(personal_topics),
        )

        import contextlib as _cl_am
        _am_ctx = (
            record_llm_call(extract_model, session_id=session_id)
            if _TELEMETRY_AVAILABLE
            else _cl_am.nullcontext()
        )
        with _am_ctx:
            # Phase 5: native async call — no run_in_executor
            result = await self.client.chat.completions.create(
                model=extract_model,
                messages=[{"role": "user", "content": extraction_prompt}],
                temperature=0.2,
                max_tokens=800,
            )

        raw = (result.choices[0].message.content or "").strip()

        # ── (c) POST-LLM response log ────────────────────────────────────────
        logger.info(
            "[AutoMemory] POST-LLM turn=%d session=%s "
            "raw_response_chars=%d raw_preview=%r",
            _turn, session_id or "?", len(raw), raw[:120],
        )

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        import json as _json
        try:
            facts = _json.loads(raw)
        except _json.JSONDecodeError as _jde:
            logger.warning(
                "[AutoMemory] JSON parse FAILED turn=%d session=%s "
                "error=%s raw_preview=%r",
                _turn, session_id or "?", _jde, raw[:200],
            )
            raise  # propagate so MemoryWorker logs at WARNING level

        if not isinstance(facts, list):
            logger.warning(
                "[AutoMemory] LLM returned non-list type=%s "
                "turn=%d session=%s — skipping",
                type(facts).__name__, _turn, session_id or "?",
            )
            return 0

        logger.info(
            "[AutoMemory] PARSE OK turn=%d session=%s candidates=%d",
            _turn, session_id or "?", len(facts),
        )

        stored_count = 0
        skipped_count = 0
        for _idx, item in enumerate(facts):
            topic      = item.get("topic", "").strip()
            fact       = item.get("fact",  "").strip()
            confidence = item.get("confidence", "medium")

            # ── (e) SKIP logs ────────────────────────────────────────────────
            if not topic:
                logger.info(
                    "[AutoMemory] SKIP item[%d] turn=%d session=%s "
                    "reason=blank_topic raw_item=%r",
                    _idx, _turn, session_id or "?", item,
                )
                skipped_count += 1
                continue
            if not fact:
                logger.info(
                    "[AutoMemory] SKIP item[%d] turn=%d session=%s "
                    "reason=blank_fact topic=%r",
                    _idx, _turn, session_id or "?", topic,
                )
                skipped_count += 1
                continue
            if len(fact) <= 5:
                logger.info(
                    "[AutoMemory] SKIP item[%d] turn=%d session=%s "
                    "reason=fact_too_short topic=%r fact_len=%d fact=%r",
                    _idx, _turn, session_id or "?", topic, len(fact), fact,
                )
                skipped_count += 1
                continue

            # ── (d) FOUND log — valid item about to be stored ────────────────
            logger.info(
                "[AutoMemory] STORE item[%d] turn=%d session=%s "
                "topic=%r confidence=%s fact_chars=%d fact_preview=%r",
                _idx, _turn, session_id or "?",
                topic, confidence, len(fact), fact[:120],
            )

            ok = await self.vector_memory.store_auto_fact(topic, fact, confidence)
            if ok:
                stored_count += 1
            else:
                logger.info(
                    "[AutoMemory] STORE FAILED item[%d] turn=%d session=%s "
                    "topic=%r (store_auto_fact returned falsy)",
                    _idx, _turn, session_id or "?", topic,
                )

        # ── (f) EXIT log ─────────────────────────────────────────────────────
        logger.info(
            "[AutoMemory] EXIT turn=%d session=%s "
            "candidates=%d skipped=%d stored=%d",
            _turn, session_id or "?", len(facts), skipped_count, stored_count,
        )
        return stored_count


    _SKILL_TRIGGERS = {
        "business_analyst":           ["act as ba", "act as business analyst", "write user stories", "write brd"],
        "lead_qa":                    ["act as lead qa", "qa strategy", "test strategy", "test plan"],
        "senior_qa":                  ["act as senior qa", "write test cases", "test case design"],
        "automated_qa_cypress":       ["cypress test", "write cypress", "cypress spec"],
        "automated_qa_robot":         ["robot framework", "write robot", "robot test"],
        "senior_java_engineer":       ["act as java engineer", "write spring boot", "java service"],
        "senior_python_engineer":     ["act as python engineer", "write fastapi", "fastapi service"],
        "senior_javascript_engineer": ["act as javascript engineer", "typescript service"],
        "frontend_engineer":          ["act as frontend engineer", "write react component"],
        "backend_engineer":           ["act as backend engineer", "write api spec", "openapi spec"],
        "devops_engineer":            ["act as devops", "write gitlab ci", "kubernetes yaml", "write dockerfile"],
        "security_engineer":          ["act as security engineer", "threat model", "security review"],
        "solution_architect":         ["act as architect", "solution architect", "write adr"],
        "researcher":                 ["act as researcher", "do research on", "research report on"],
        "reviewer":                   ["act as reviewer", "review this code", "code review"],
        "financial_analyst":          ["act as financial analyst", "npv analysis", "roi analysis"],
        "stock_market_analyst":       ["act as stock analyst", "stock analysis", "analyse stock"],
    }

    async def _maybe_dispatch_skill(self, user_input: str):
        lower = user_input.lower()
        matched = None
        for skill_id, triggers in self._SKILL_TRIGGERS.items():
            if any(t in lower for t in triggers):
                matched = skill_id
                break
        if not matched:
            return None
        try:
            import importlib.util as ilu
            base = os.path.dirname(os.path.abspath(__file__))
            agent_path = os.path.join(base, "skills", matched, "agent.py")
            if not os.path.exists(agent_path):
                return None
            spec = ilu.spec_from_file_location("skills." + matched, agent_path)
            mod = ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cls = None
            for name in dir(mod):
                obj = getattr(mod, name)
                if isinstance(obj, type) and hasattr(obj, "execute") and name not in ("BaseSkillAgent", "object"):
                    cls = obj
                    break
            if not cls:
                return None
            inst = cls()
            result = inst.execute({"user_request": user_input, "context": ""})
            header = "\n\U0001f916 **[" + matched.replace("_", " ").title() + " Agent]**\n\n"
            # ── SkillResult unwrap ladder ────────────────────────────────────────
            # BEACON skill agents return SkillResult where .data is a dict with
            # keys: skill_id, display_name, persona, reasoning_steps,
            # inputs_received, outputs (nested dict of actual results),
            # ready_for_llm.  The old code checked .data["output"|"result"|
            # "content"] which do not exist, so every branch fell through to
            # str(result) → "<SkillResult object at 0x…>" in the chat.
            # Fix: try .output/.content direct attrs first, then .data["outputs"]
            # (the real nested outputs dict used by all BEACON skill agents),
            # then a structured summary, then str() as absolute last resort.
            # logger.debug records which branch resolved for easy diagnosis.
            _result_type = type(result).__name__
            if isinstance(result, dict):
                out = (result.get("output") or result.get("result")
                       or result.get("content") or str(result))
                logger.debug("[SkillDispatch] result type=dict keys=%s", list(result.keys()))
            elif hasattr(result, "output") and result.output:
                out = str(result.output)
                logger.debug("[SkillDispatch] result type=%s resolved via .output attr", _result_type)
            elif hasattr(result, "content") and result.content:
                out = str(result.content)
                logger.debug("[SkillDispatch] result type=%s resolved via .content attr", _result_type)
            elif hasattr(result, "data") and isinstance(result.data, dict):
                _d = result.data
                # 1) direct key check (for any agent that uses output/result/content)
                _direct = (_d.get("output") or _d.get("result") or _d.get("content"))
                if _direct:
                    out = str(_direct)
                    logger.debug("[SkillDispatch] result type=%s resolved via .data direct key", _result_type)
                else:
                    # 2) BEACON agents store real work product in data["outputs"] (nested dict)
                    #    e.g. {"review_report": "…", "score": "…", "verdict": "…", "improvements": "…"}
                    _nested = _d.get("outputs")
                    if isinstance(_nested, dict) and _nested:
                        _parts = []
                        for _k, _v in _nested.items():
                            _vs = str(_v).strip()
                            _parts.append(f"**{_k.replace('_', ' ').title()}**\n{_vs}")
                        out = "\n\n".join(_parts) if _parts else str(_d)
                        logger.debug(
                            "[SkillDispatch] result type=%s resolved via .data['outputs'] keys=%s",
                            _result_type, list(_nested.keys()))
                    elif _d.get("persona") or _d.get("reasoning_steps"):
                        # 3) Render a structured summary from known SkillResult data fields
                        _parts = []
                        _persona = _d.get("persona", "")
                        _steps   = _d.get("reasoning_steps", [])
                        if _persona:
                            _parts.append(f"**Role:** {_persona}")
                        if _steps:
                            _parts.append("**Execution Steps:**\n" + "\n".join(_steps))
                        _nested2 = _d.get("outputs", {})
                        if isinstance(_nested2, dict):
                            for _k, _v in _nested2.items():
                                _parts.append(f"**{_k.replace('_', ' ').title()}:** {_v}")
                        out = "\n\n".join(_parts) if _parts else str(_d)
                        logger.debug("[SkillDispatch] result type=%s resolved via .data structured summary", _result_type)
                    else:
                        out = str(_d)
                        logger.debug("[SkillDispatch] result type=%s fell back to str(.data)", _result_type)
            elif hasattr(result, "to_dict"):
                _td = result.to_dict()
                _direct2 = (_td.get("output") or _td.get("result") or _td.get("content"))
                if _direct2:
                    out = str(_direct2)
                else:
                    _inner = _td.get("data", {})
                    if isinstance(_inner, dict):
                        _nested3 = _inner.get("outputs")
                        if isinstance(_nested3, dict) and _nested3:
                            _parts3 = [f"**{_k.replace('_', ' ').title()}**\n{_v}"
                                       for _k, _v in _nested3.items()]
                            out = "\n\n".join(_parts3) if _parts3 else str(_inner)
                        else:
                            out = str(_inner) if _inner else str(_td)
                    else:
                        out = str(_td)
                logger.debug("[SkillDispatch] result type=%s resolved via .to_dict()", _result_type)
            else:
                out = str(result)
                logger.debug("[SkillDispatch] result type=%s final str() fallback", _result_type)
            logger.info("[SkillDispatch] skill=%s resolved_type=%s content_len=%d",
                        matched, _result_type, len(out))
            self._last_dispatched_skill = matched  # signal for web_app skill indicator
            return header + out
        except Exception as e:
            logger.warning("Skill dispatch failed: " + str(e))
            return None


    def _build_tools_prompt(self) -> str:
        """Build system prompt with tool descriptions.

        The result (~80k chars) is cached and only rebuilt when the available
        tool/MCP set changes. Rebuilding this string on every request added
        avoidable CPU on the single event loop and contributed to concurrent
        requests stalling.
        """
        # Cheap signature of the current toolset — rebuild only when it changes.
        builtin_names = (
            tuple(t['function']['name'] for t in self.tools.tools)
            if (self.tools_available and self.tools) else ()
        )
        mcp_names = (
            tuple(t['function']['name'] for t in self.mcp_manager.get_all_tools_for_openai())
            if self.mcp_manager else ()
        )
        cache_key = (builtin_names, mcp_names)
        if self._tools_prompt_cache is not None and self._tools_prompt_key == cache_key:
            return self._tools_prompt_cache

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

        self._tools_prompt_cache = prompt
        self._tools_prompt_key = cache_key
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
        # MEMORY-FIRST: prepend local path rules so BEACON checks local files before GitLab
        _local_rules = (
            "\nCRITICAL - MEMORY-FIRST LOCAL PATH RULES (before every tool call):\n"
            "1. For KRS/project code READ LOCAL FILES FIRST. Never open GitLab unless asked.\n"
            "   krs-service    : /Users/sorravit/IdeaProjects/KRS/krs-service\n"
            "   krs-sftp-batch : /Users/sorravit/IdeaProjects/KRS/krs-sftp-batch\n"
            "   BEACON         : /Users/sorravit/sandbox/beacon\n"
            "2. Use execute_command or read_file with the local path above.\n"
            "3. Only open URL/browser when explicitly asked OR local path missing.\n"
        )
        _base_system_content = _base_system_content + _local_rules

        _tools_prompt = self._build_tools_prompt() if self.tools_available else ""

        try:
            # (tools prompt is applied per-iteration below, not permanently here)
            pass

            # SKILL DISPATCH: route to specialist agent before LLM loop
            self._last_dispatched_skill = ""  # reset before each request
            _skill_out = await self._maybe_dispatch_skill(user_input)
            if _skill_out is not None:
                if token_callback:
                    # Phase 1 / #4: batch emit (24-char chunks)
                    _BATCH = 24
                    for _i in range(0, len(_skill_out), _BATCH):
                        try: token_callback(_skill_out[_i:_i+_BATCH])
                        except Exception: pass
                conv.append({"role": "assistant", "content": _skill_out})
                # Phase 2 / auto-memory: also learn from skill dispatches
                if self.memory_worker is None:
                    logger.warning(
                        "[AutoMemory] memory_worker is None — skipping"
                        " _auto_memory_extract for this turn (skill-dispatch path)"
                    )
                elif self.memory_available:
                    self.memory_worker.submit(
                        user_input, _skill_out, get_session_id() or ""
                    )
                return _skill_out
            # END SKILL DISPATCH

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

            # Trim AFTER adding the new message so trimming sees the full picture.
            # tiktoken encoding is CPU-bound; run it in a worker thread so the
            # single asyncio event loop stays responsive and concurrent streams
            # are not starved (root cause of intermittent "returns nothing").
            await asyncio.to_thread(self._trim_conversation, conv)
            
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
                
                # One OTel span per LLM request
                llm_span_cm = (
                    record_llm_call(
                        effective_model,
                        session_id=get_session_id(),
                        temperature=self.config.temperature,
                        max_tokens=self.config.max_tokens,
                        message_count=len(conv),
                        streaming=bool(token_callback),
                    )
                    if _TELEMETRY_AVAILABLE
                    else nullcontext()
                )

                # Both paths use AsyncOpenAI (no worker threads). LLM concurrency
                # is unbounded (the global semaphore was removed); a hard
                # wall-clock timeout guards every call so a stalled stream can
                # never hang forever.
                _call_timeout = float(os.getenv("LLM_REQUEST_TIMEOUT", "120")) + 30.0

                # One non-streaming completion. Returns (text, reason) where
                # reason is "" on success, else "timeout" / "empty" / "error: …".
                async def _nonstream_once(span=None):
                    try:
                        _resp = await asyncio.wait_for(
                            self.client.chat.completions.create(**params),
                            timeout=_call_timeout,
                        )
                    except asyncio.TimeoutError:
                        return "", "timeout"
                    except Exception as _exc:
                        logger.warning(
                            "LLM non-streaming call failed: %s: %s",
                            type(_exc).__name__, _exc,
                        )
                        return "", f"error: {type(_exc).__name__}: {_exc}"
                    _txt = _resp.choices[0].message.content or ""
                    _usage = getattr(_resp, "usage", None)
                    if span is not None and _usage:
                        try:
                            span.set_attribute("llm.input_tokens", _usage.prompt_tokens or 0)
                            span.set_attribute("llm.output_tokens", _usage.completion_tokens or 0)
                        except Exception:
                            pass
                    return _txt, ("" if _txt.strip() else "empty")

                response_text = ""
                fail_reason = ""
                _t_start = time.monotonic()

                async with _get_llm_sem():
                    if token_callback:
                        # STREAMING MODE — native async streaming, zero worker threads.
                        stream_params = {**params, "stream": True}
                        with llm_span_cm as llm_span:
                            async def _consume_stream():
                                _text = ""
                                stream = await self.client.chat.completions.create(**stream_params)
                                async for chunk in stream:
                                    delta = (
                                        chunk.choices[0].delta.content
                                        if chunk.choices else None
                                    )
                                    if delta:
                                        _text += delta
                                return _text
                            try:
                                response_text = await asyncio.wait_for(
                                    _consume_stream(), timeout=_call_timeout
                                )
                                if not response_text.strip():
                                    fail_reason = "empty"
                            except asyncio.TimeoutError:
                                logger.warning(
                                    "LLM streaming call timed out after %.0fs", _call_timeout
                                )
                                response_text, fail_reason = "", "timeout"
                            except Exception as _sexc:
                                logger.warning(
                                    "LLM streaming call failed: %s: %s",
                                    type(_sexc).__name__, _sexc,
                                )
                                response_text = ""
                                fail_reason = f"error: {type(_sexc).__name__}: {_sexc}"

                            # Resilience: a streaming stall/empty/error is the #1
                            # cause of "returns nothing". Retry ONCE via the more
                            # robust non-streaming call before giving up.
                            if fail_reason:
                                logger.warning(
                                    "Streaming response failed (%s) after %.1fs — "
                                    "retrying once non-streaming (model=%s)",
                                    fail_reason, time.monotonic() - _t_start, effective_model,
                                )
                                _retry_text, _retry_reason = await _nonstream_once(llm_span)
                                if _retry_text.strip():
                                    # Recovered. The final-response branch below
                                    # emits tokens to the UI, so don't stream here.
                                    response_text, fail_reason = _retry_text, ""
                                else:
                                    fail_reason = _retry_reason or fail_reason

                            if llm_span is not None:
                                llm_span.set_attribute("llm.response_chars", len(response_text))
                                llm_span.set_attribute(
                                    "llm.output_tokens", self._estimate_tokens(response_text)
                                )
                    else:
                        # NON-STREAMING MODE: CLI / planning / background tasks.
                        with llm_span_cm as llm_span:
                            response_text, fail_reason = await _nonstream_once(llm_span)

                if not response_text or fail_reason:
                    _elapsed = time.monotonic() - _t_start
                    if fail_reason == "timeout":
                        _detail = f"the request timed out after {_call_timeout:.0f}s"
                    elif fail_reason == "empty" or not fail_reason:
                        _detail = "the model returned an empty response"
                    elif fail_reason.startswith("error:"):
                        _detail = f"an upstream error occurred ({fail_reason[7:].strip()})"
                    else:
                        _detail = "no content was returned"
                    logger.warning(
                        "[get_response] No usable response: reason=%s model=%s elapsed=%.1fs",
                        fail_reason or "empty", effective_model, _elapsed,
                    )
                    return (
                        f"⚠️ No response from AI — {_detail} "
                        f"(model: {effective_model}, {_elapsed:.0f}s). Please try again."
                    )

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
                    # No tool calls — final response. NOW emit tokens to UI.
                    # Phase 1 / #4: batch-emit in 24-char chunks (not per-char).
                    if token_callback:
                        import re as _re
                        clean = _re.sub(r'<tool_use>.*?</tool_use>', '', response_text, flags=_re.DOTALL).strip()
                        _BATCH = 24
                        for _i in range(0, len(clean), _BATCH):
                            try:
                                token_callback(clean[_i:_i+_BATCH])
                            except Exception:
                                pass
                    conv.append({"role": "assistant", "content": response_text})
                    # Phase 2 / auto-memory: submit to durable worker queue
                    if self.memory_worker is None:
                        logger.warning(
                            "[AutoMemory] memory_worker is None — skipping"
                            " _auto_memory_extract for this turn (response path)"
                        )
                    elif self.memory_available:
                        self.memory_worker.submit(
                            user_input, response_text, get_session_id() or ""
                        )
                    return response_text
            
            # If we hit max iterations, inform user but don't fail
            logger.warning(f"Reached safety backstop of {MAX_TOOL_ITERATIONS} iterations — this should never happen in normal use.")
            return f"I've reached the iteration safety backstop ({MAX_TOOL_ITERATIONS} calls). This should never happen in normal use — please report this."
                
        except Exception as e:
            logger.error(f"Error: {e}")
            import traceback
            traceback.print_exc()
            if conv and conv[-1]["role"] == "user":
                conv.pop()
            raise  # Re-raise so web_app.py can surface it to the frontend
    
    def _estimate_tokens(self, text: str) -> int:
        """
        Accurate token count using the module-level cached tiktoken encoder.
        Falls back to conservative char-based estimate if tiktoken unavailable.
        Phase 1 / #7: encoder cached via @lru_cache — no repeated construction.
        """
        try:
            return len(_get_encoder().encode(text))
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
        """Shut down agent resources: worker, browser pool, tools, memory."""
        # Stop the auto-memory background worker first
        try:
            if self.memory_worker:
                await self.memory_worker.stop()
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

        # ── Boot telemetry BEFORE agent starts so every log/span is captured ──
        if _TELEMETRY_AVAILABLE:
            try:
                setup_telemetry(service_name="beacon")
                install_print_bridge()
                logger.info("🔭 Telemetry initialised (OTLP → localhost:14318)")
            except Exception as _tel_err:
                logger.warning("Telemetry init failed (non-fatal): %s", _tel_err)

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