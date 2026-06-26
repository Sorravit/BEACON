#!/usr/bin/env python3
"""
core/mem0_memory.py — mem0 library integration backed by existing Weaviate.

Uses mem0 in *library* mode (no separate server needed).
All heavy LLM/vector work is offloaded via asyncio.to_thread() so the
async event loop is never blocked.

Configuration
-------------
  WEAVIATE_PORT     (default 8090)  — existing Weaviate instance
  OPENAI_API_KEY                    — used by mem0's LLM extractor
  OPENAI_BASE_URL                   — IBM ICA proxy endpoint
  AI_MODEL                          — model for mem0 extraction
  MEM0_COLLECTION   (default Mem0Conversations) — Weaviate collection name

Usage
-----
  from core.mem0_memory import Mem0Memory

  m = Mem0Memory()
  await m.initialize()                          # call once at startup
  await m.add(user_msg, ai_msg, user_id="u1")  # after every turn
  results = await m.search(query, user_id="u1") # before response
  all_mem = await m.get_all(user_id="u1")
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── embedder constants ───────────────────────────────────────────────────────
# Reuse the same local model as VectorMemory — avoids double-loading.
# all-MiniLM-L6-v2 produces 384-dim vectors; the Weaviate collection MUST be
# created at this width or every store/search raises a dimension error.
_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_EMBED_DIMS  = 384

# mem0 ships anonymous PostHog telemetry that fires blocking HTTPS calls (with a
# 0.5s timeout) on every operation. Disable it to avoid network noise/latency.
os.environ.setdefault("MEM0_TELEMETRY", "False")


def _build_mem0_config() -> Dict[str, Any]:
    """
    Return mem0 Memory.from_config() configuration dict.

    IMPORTANT: env vars are read here (at call time), NOT at module import time.
    `core.mem0_memory` is imported before `load_dotenv()` runs, so reading them
    at import time captured empty/placeholder values (api_key="", default model)
    which silently broke mem0's LLM fact-extraction (0 facts ever stored).
    """
    weaviate_host = os.getenv("WEAVIATE_HOST", "127.0.0.1")
    weaviate_port = int(os.getenv("WEAVIATE_PORT", "8090"))
    collection    = os.getenv("MEM0_COLLECTION", "Mem0Conversations")
    api_key       = os.getenv("OPENAI_API_KEY", "")
    base_url      = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    # Allow a dedicated extraction model; fall back to the chat model.
    llm_model     = os.getenv("MEM0_LLM_MODEL") or os.getenv("AI_MODEL", "gpt-4o-mini")
    # Extended-thinking models on the ICA gateway require
    # max_tokens > thinking.budget_tokens, so keep this comfortably high.
    max_tokens    = int(os.getenv("MEM0_MAX_TOKENS", "4096"))

    # mem0 Weaviate config ONLY accepts: cluster_url, collection_name,
    # auth_client_secret, embedding_model_dims, additional_headers
    # DO NOT pass host/port/url — they are not valid fields.
    cluster_url = f"http://{weaviate_host}:{weaviate_port}"
    return {
        "vector_store": {
            "provider": "weaviate",
            "config": {
                "cluster_url":          cluster_url,
                "collection_name":      collection,
                "embedding_model_dims": _EMBED_DIMS,
            },
        },
        "llm": {
            "provider": "openai",
            "config": {
                "api_key":         api_key,
                "openai_base_url": base_url,
                "model":           llm_model,
                "temperature":     0.1,
                "max_tokens":      max_tokens,
            },
        },
        "embedder": {
            "provider": "huggingface",
            "config": {
                "model":                _EMBED_MODEL,
                "embedding_dims":       _EMBED_DIMS,
            },
        },
        "version": "v1.1",
    }


class Mem0Memory:
    """
    Async-safe wrapper around mem0's synchronous Memory class.

    All mem0 calls are blocking (network + LLM) so we run them in a
    thread-pool via asyncio.to_thread() — same pattern used in VectorMemory.
    """

    def __init__(self) -> None:
        self._mem:   Optional[Any] = None   # mem0.Memory instance
        self._ready: bool          = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        """Import mem0, build config, connect to Weaviate. Returns True on success."""
        try:
            from mem0 import Memory  # type: ignore

            config = _build_mem0_config()
            _vs   = config["vector_store"]["config"]
            _llm  = config["llm"]["config"]
            logger.info(
                "Mem0Memory: initialising — Weaviate %s  collection=%s  model=%s  dims=%d",
                _vs["cluster_url"], _vs["collection_name"], _llm["model"], _EMBED_DIMS,
            )
            # Memory() constructor does I/O (Weaviate connect) — offload it.
            self._mem   = await asyncio.to_thread(Memory.from_config, config)
            self._ready = True
            logger.info("Mem0Memory: ready ✅")
            return True

        except ImportError:
            logger.error(
                "Mem0Memory: mem0ai not installed — run: pip install mem0ai"
            )
            return False
        except Exception as exc:
            logger.warning("Mem0Memory: initialisation failed — %s", exc)
            self._ready = False
            return False

    @property
    def ready(self) -> bool:
        return self._ready and self._mem is not None

    # ── core operations ──────────────────────────────────────────────────────

    async def add(
        self,
        user_message: str,
        ai_message:   str,
        user_id:      str = "default",
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Feed a conversation turn to mem0 for automatic fact extraction + storage.
        Returns the list of extracted memory objects, or None on failure.
        """
        if not self.ready:
            return None

        messages = [
            {"role": "user",      "content": user_message},
            {"role": "assistant", "content": ai_message},
        ]

        try:
            result = await asyncio.to_thread(
                self._mem.add,
                messages,
                user_id=user_id,
            )
            # mem0 v1.1 returns {"results": [...]}; older returns a list
            items: List[Dict] = (
                result.get("results", []) if isinstance(result, dict) else result
            )
            logger.info(
                "Mem0Memory.add: extracted %d fact(s) [user=%s]",
                len(items), user_id,
            )
            return items
        except Exception as exc:
            logger.warning("Mem0Memory.add failed: %s", exc)
            return None

    async def search(
        self,
        query:   str,
        user_id: str = "default",
        limit:   int = 8,
    ) -> List[Dict[str, Any]]:
        """
        Semantic search over stored memories for a given user.
        Returns list of memory dicts: {memory, score, id, ...}
        """
        if not self.ready:
            return []

        try:
            result = await asyncio.to_thread(
                self._mem.search,
                query,
                filters={"user_id": user_id},
                top_k=limit,
            )
            items: List[Dict] = (
                result.get("results", []) if isinstance(result, dict) else result
            )
            return items
        except Exception as exc:
            logger.warning("Mem0Memory.search failed: %s", exc)
            return []

    async def get_all(
        self,
        user_id: str = "default",
    ) -> List[Dict[str, Any]]:
        """Return all memories stored for a user."""
        if not self.ready:
            return []

        try:
            result = await asyncio.to_thread(
                self._mem.get_all,
                filters={"user_id": user_id},
            )
            items: List[Dict] = (
                result.get("results", []) if isinstance(result, dict) else result
            )
            return items
        except Exception as exc:
            logger.warning("Mem0Memory.get_all failed: %s", exc)
            return []

    async def delete(self, memory_id: str) -> bool:
        """Delete a single memory by ID."""
        if not self.ready:
            return False
        try:
            await asyncio.to_thread(self._mem.delete, memory_id)
            return True
        except Exception as exc:
            logger.warning("Mem0Memory.delete failed: %s", exc)
            return False

    async def delete_all(self, user_id: str = "default") -> bool:
        """Delete ALL memories for a user."""
        if not self.ready:
            return False
        try:
            await asyncio.to_thread(self._mem.delete_all, user_id=user_id)
            return True
        except Exception as exc:
            logger.warning("Mem0Memory.delete_all failed: %s", exc)
            return False

    # ── context builder ──────────────────────────────────────────────────────

    async def build_context_snippet(
        self,
        query:   str,
        user_id: str = "default",
        limit:   int = 8,
    ) -> str:
        """
        Return a formatted memory context block ready to inject into system prompt.
        Returns empty string when no relevant memories exist.
        """
        memories = await self.search(query, user_id=user_id, limit=limit)
        if not memories:
            return ""

        lines = []
        for m in memories:
            text  = m.get("memory", m.get("text", ""))
            score = m.get("score", 0.0)
            if text:
                lines.append(f"  - {text}  [relevance: {score:.2f}]")

        if not lines:
            return ""

        return (
            "\U0001f9e0 Learned from our conversations:\n"
            + "\n".join(lines)
        )
