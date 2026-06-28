#!/usr/bin/env python3
"""
core/agent/memory_mixin.py — fact extraction, auto-memory and mem0 learning.

Methods extracted verbatim from the original ``main.py`` AIAgent class.
"""

import asyncio
import json
import os
import re
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.agent.runtime import (
    logger,
    get_session_id,
    get_reporter,
    record_llm_call,
    setup_telemetry,
    install_print_bridge,
    _TELEMETRY_AVAILABLE,
    _get_encoder,
    _get_llm_sem,
    AsyncOpenAI,
    OpenAI,
    MCPManager,
    ModelRegistry,
    SkillManager,
    VectorMemory,
    ToolManager,
    VERSION,
    LOG_FILE,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_BASE_URL,
    MAX_TOOL_ITERATIONS,
    MAX_CONVERSATION_TOKENS,
    MAX_MEMORY_CONTEXT_CHARS,
    MCP_CONFIG_FILE,
)
from core.agent.stream_filter import _ToolMarkupStreamFilter


class MemoryMixin:
    """Mixin methods for AIAgent (see module docstring)."""

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
                # Extended-thinking models on the gateway require
                # max_tokens > thinking.budget_tokens, so keep a safe floor.
                max_tokens=int(os.getenv("AUTO_MEMORY_MAX_TOKENS", "4096")),
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
            " and extract ONLY HIGHLY RELEVANT and MEANINGFUL facts about the USER.\n\n"
            "CRITICAL: If the information is trivial, common sense, or not worth remembering, "
            "do NOT extract it. Only remember things that will be useful for personalizing "
            "future interactions or provide deep context about the user's work/life.\n\n"
            f"Conversation:\nUser: {user_input[:1000]}\nAssistant: {ai_response[:800]}\n\n"
            f"Already known personal facts: {personal_summary}\n"
            f"Already auto-learned topics: {existing_summary}\n\n"
            "Focus on:\n"
            "- Core user preferences and persistent context\n"
            "- Specific projects, unique tools, or technical stacks used\n"
            "- Key relationships or roles mentioned\n"
            "- Direct corrections made to the AI\n"
            "- Decisions and specific problem-solving context\n\n"
            "STRICTLY IGNORE:\n"
            "- Trivial observations (e.g., 'User likes to ask questions')\n"
            "- One-off tasks or temporary lookups\n"
            "- Greetings, small talk, or polite fillers\n"
            "- Anything the user is just asking ABOUT (rather than sharing info)\n\n"
            'Respond ONLY with a JSON array. Each item: {"topic": "short_snake_case_key",'
            ' "fact": "concise fact sentence", "confidence": "high|medium|low"}\n'
            "If nothing worthwhile to extract, respond with: []\n\n"
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
                # Extended-thinking models require max_tokens > budget_tokens.
                max_tokens=int(os.getenv("AUTO_MEMORY_MAX_TOKENS", "4096")),
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
            topic = item.get("topic", "").strip()
            fact = item.get("fact", "").strip()
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


