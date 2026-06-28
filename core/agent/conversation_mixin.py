#!/usr/bin/env python3
"""
core/agent/conversation_mixin.py — token estimation and conversation trimming.
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
    Mem0Memory,
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


class ConversationMixin:
    """Mixin methods for AIAgent (see module docstring)."""

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

