
#!/usr/bin/env python3
"""
Vector Memory - Weaviate-backed semantic memory for the AI agent.

Uses client-side embeddings (OpenAI embeddings API via custom base URL)
so it works with any OpenAI-compatible endpoint (IBM, Azure, etc.).

Two collections:
  - ResearchMemory : stores tool results (web search, browser, HTTP, commands)
  - PersonalFacts  : stores facts the user tells the agent

Usage:
    memory = VectorMemory(openai_api_key="...", openai_base_url="https://...")
    await memory.initialize()

    await memory.store_research("web_search", "Latest AI news", "OpenAI released GPT-5...")
    await memory.store_personal_fact("oven", "I have a Samsung NV75K5571RS oven")

    results = await memory.search_research("AI news", limit=3)
    facts   = await memory.search_personal_facts("oven", limit=5)
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

import os as _os
WEAVIATE_URL = f"http://localhost:{_os.getenv('WEAVIATE_PORT', '8090')}"
# Local embedding model — no API key needed, works with any LLM backend
LOCAL_EMBEDDING_MODEL = "all-MiniLM-L6-v2"


class VectorMemory:
    """
    Weaviate-backed vector memory with client-side embeddings.

    Two collections:
      - ResearchMemory  : tool results, researched content
      - PersonalFacts   : user-provided personal details

    Embeddings are generated client-side using the OpenAI embeddings API
    (supports custom base URLs for IBM/Azure/etc.) and stored as raw vectors
    in Weaviate (no built-in vectorizer module required).
    """

    def __init__(
        self,
        openai_api_key: str = "",
        weaviate_url: str = WEAVIATE_URL,
        openai_base_url: str = "https://api.openai.com/v1",
        embedding_model: str = LOCAL_EMBEDDING_MODEL,
    ):
        self.weaviate_url = weaviate_url
        self.embedding_model = embedding_model
        self.client = None      # Weaviate client
        self._encoder = None    # sentence-transformers model
        self._ready = False

    async def initialize(self) -> bool:
        """Connect to Weaviate, load local embedding model, ensure collections exist."""
        try:
            import os
            import weaviate
            from sentence_transformers import SentenceTransformer

            # Load local embedding model (downloads ~90MB on first run only)
            # After first run: set TRANSFORMERS_OFFLINE=1 to skip HuggingFace checks
            logger.info(f"Loading local embedding model: {self.embedding_model}")
            self._encoder = SentenceTransformer(
                self.embedding_model,
                local_files_only=os.getenv("TRANSFORMERS_OFFLINE", "0") == "1"
            )

            # Weaviate client (anonymous access, no vectorizer module)
            weaviate_port = int(_os.getenv("WEAVIATE_PORT", "8090"))
            self.client = weaviate.connect_to_local(host="localhost", port=weaviate_port)

            await self._ensure_collections()
            self._ready = True
            logger.info("VectorMemory initialized (local sentence-transformers embeddings)")
            return True

        except ImportError as e:
            logger.error(f"Missing dependency: {e}. Run: pip install weaviate-client sentence-transformers")
            return False
        except Exception as e:
            logger.warning(f"VectorMemory not available: {e}")
            return False

    async def _ensure_collections(self):
        """Create (or recreate) collections with no vectorizer (we supply vectors directly)."""
        import weaviate.classes.config as wc

        existing = [c.name for c in self.client.collections.list_all().values()]

        # If collections exist but were created with old vectorizer, delete and recreate
        for name in ["ResearchMemory", "PersonalFacts"]:
            if name in existing:
                try:
                    col = self.client.collections.get(name)
                    # Check if the vectorizer is 'none' by trying a zero-vector insert check
                    cfg = col.config.get()
                    vectorizer = str(cfg.vectorizer_config)
                    if "none" not in vectorizer.lower() and "None" not in vectorizer:
                        logger.info(f"Recreating {name} collection with none vectorizer")
                        self.client.collections.delete(name)
                        existing.remove(name)
                except Exception:
                    # Can't check, delete and recreate to be safe
                    self.client.collections.delete(name)
                    existing.remove(name)

        if "ResearchMemory" not in existing:
            self.client.collections.create(
                name="ResearchMemory",
                vectorizer_config=wc.Configure.Vectorizer.none(),
                properties=[
                    wc.Property(name="tool_name",  data_type=wc.DataType.TEXT),
                    wc.Property(name="query",       data_type=wc.DataType.TEXT),
                    wc.Property(name="result",      data_type=wc.DataType.TEXT),
                    wc.Property(name="stored_at",   data_type=wc.DataType.TEXT),
                ]
            )
            logger.info("Created ResearchMemory collection")

        if "PersonalFacts" not in existing:
            self.client.collections.create(
                name="PersonalFacts",
                vectorizer_config=wc.Configure.Vectorizer.none(),
                properties=[
                    wc.Property(name="topic",       data_type=wc.DataType.TEXT),
                    wc.Property(name="fact",        data_type=wc.DataType.TEXT),
                    wc.Property(name="stored_at",   data_type=wc.DataType.TEXT),
                ]
            )
            logger.info("Created PersonalFacts collection")

    # -------------------------------------------------------------------------
    # Embedding helper
    # -------------------------------------------------------------------------

    def _embed(self, text: str, label: str = "🧠 Encoding memory") -> Optional[List[float]]:
        """Generate an embedding vector locally using sentence-transformers."""
        try:
            from tqdm import tqdm
            with tqdm(total=1, desc=label, bar_format="{desc}: {elapsed}s @ {rate_fmt}", leave=True) as pbar:
                vector = self._encoder.encode(
                    text[:2000],
                    normalize_embeddings=True,
                    show_progress_bar=False
                )
                pbar.update(1)
            return vector.tolist()
        except Exception as e:
            logger.error(f"Embedding failed: {e}")
            return None

    # -------------------------------------------------------------------------
    # Research memory
    # -------------------------------------------------------------------------

    async def store_research(self, tool_name: str, query: str, result: str) -> bool:
        """Store a tool result in ResearchMemory."""
        if not self._ready:
            return False
        try:
            truncated = result[:4000] if len(result) > 4000 else result
            text_to_embed = f"{tool_name}: {query}\n{truncated}"
            vector = self._embed(text_to_embed, label="Storing research")
            if vector is None:
                return False

            collection = self.client.collections.get("ResearchMemory")
            collection.data.insert(
                properties={
                    "tool_name": tool_name,
                    "query":     query,
                    "result":    truncated,
                    "stored_at": datetime.now().isoformat(),
                },
                vector=vector
            )
            logger.debug(f"Stored research: [{tool_name}] {query[:60]}")
            return True
        except Exception as e:
            logger.error(f"Failed to store research: {e}")
            return False

    async def search_research(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Semantic search over ResearchMemory using near_vector."""
        if not self._ready:
            return []
        try:
            vector = self._embed(query)
            if vector is None:
                return []
            collection = self.client.collections.get("ResearchMemory")
            response = collection.query.near_vector(
                near_vector=vector,
                limit=limit,
                return_properties=["tool_name", "query", "result", "stored_at"]
            )
            return [obj.properties for obj in response.objects]
        except Exception as e:
            logger.error(f"Research search failed: {e}")
            return []

    # -------------------------------------------------------------------------
    # Personal facts memory
    # -------------------------------------------------------------------------

    async def store_personal_fact(self, topic: str, fact: str) -> bool:
        """Store a personal fact in PersonalFacts."""
        if not self._ready:
            return False
        try:
            vector = self._embed(f"{topic}: {fact}")
            if vector is None:
                return False
            collection = self.client.collections.get("PersonalFacts")
            collection.data.insert(
                properties={
                    "topic":     topic,
                    "fact":      fact,
                    "stored_at": datetime.now().isoformat(),
                },
                vector=vector
            )
            logger.info(f"Stored personal fact [{topic}]: {fact[:80]}")
            return True
        except Exception as e:
            logger.error(f"Failed to store personal fact: {e}")
            return False

    async def search_personal_facts(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Semantic search over PersonalFacts."""
        if not self._ready:
            return []
        try:
            vector = self._embed(query)
            if vector is None:
                return []
            collection = self.client.collections.get("PersonalFacts")
            response = collection.query.near_vector(
                near_vector=vector,
                limit=limit,
                return_properties=["topic", "fact", "stored_at"]
            )
            return [obj.properties for obj in response.objects]
        except Exception as e:
            logger.error(f"Personal facts search failed: {e}")
            return []

    async def get_all_personal_facts(self) -> List[Dict[str, Any]]:
        """Return all stored personal facts."""
        if not self._ready:
            return []
        try:
            collection = self.client.collections.get("PersonalFacts")
            response = collection.query.fetch_objects(
                limit=200,
                return_properties=["topic", "fact", "stored_at"]
            )
            return [obj.properties for obj in response.objects]
        except Exception as e:
            logger.error(f"Failed to fetch personal facts: {e}")
            return []

    async def delete_personal_facts(self, keyword: str) -> int:
        """
        Delete personal facts whose topic or fact text contains the keyword.
        Returns the number of entries deleted.
        """
        if not self._ready:
            return 0
        try:
            import weaviate.classes.query as wq
            collection = self.client.collections.get("PersonalFacts")
            # Fetch all, filter client-side by keyword
            response = collection.query.fetch_objects(
                limit=500,
                return_properties=["topic", "fact"]
            )
            lower_kw = keyword.lower()
            to_delete = [
                obj.uuid for obj in response.objects
                if lower_kw in (obj.properties.get("topic") or "").lower()
                or lower_kw in (obj.properties.get("fact") or "").lower()
            ]
            for uuid in to_delete:
                collection.data.delete_by_id(uuid)
            logger.info(f"Deleted {len(to_delete)} personal fact(s) matching '{keyword}'")
            return len(to_delete)
        except Exception as e:
            logger.error(f"Failed to delete personal facts: {e}")
            return 0

    async def delete_research(self, keyword: str) -> int:
        """
        Delete research entries whose query or result contains the keyword.
        Returns the number of entries deleted.
        """
        if not self._ready:
            return 0
        try:
            collection = self.client.collections.get("ResearchMemory")
            response = collection.query.fetch_objects(
                limit=500,
                return_properties=["tool_name", "query", "result"]
            )
            lower_kw = keyword.lower()
            to_delete = [
                obj.uuid for obj in response.objects
                if lower_kw in (obj.properties.get("query") or "").lower()
                or lower_kw in (obj.properties.get("result") or "").lower()
            ]
            for uuid in to_delete:
                collection.data.delete_by_id(uuid)
            logger.info(f"Deleted {len(to_delete)} research entry/entries matching '{keyword}'")
            return len(to_delete)
        except Exception as e:
            logger.error(f"Failed to delete research: {e}")
            return 0

    async def clear_all_research(self) -> int:
        """Delete all research memory entries. Returns count deleted."""
        if not self._ready:
            return 0
        try:
            collection = self.client.collections.get("ResearchMemory")
            response = collection.query.fetch_objects(limit=5000)
            count = len(response.objects)
            for obj in response.objects:
                collection.data.delete_by_id(obj.uuid)
            logger.info(f"Cleared all {count} research memory entries")
            return count
        except Exception as e:
            logger.error(f"Failed to clear research memory: {e}")
            return 0

    # -------------------------------------------------------------------------
    # Context builder
    # -------------------------------------------------------------------------

    async def build_context_prompt(self, user_query: str) -> str:
        """
        Build a compact memory context string to inject into the prompt.
        Searches both collections for the most relevant entries.
        """
        sections = []

        facts = await self.search_personal_facts(user_query, limit=10)
        if facts:
            lines = [f"  - [{f['topic']}] {f['fact']}" for f in facts]
            sections.append("📋 What I know about you:\n" + "\n".join(lines))

        research = await self.search_research(user_query, limit=3)
        if research:
            lines = []
            for r in research:
                snippet = r['result'][:300].replace('\n', ' ')
                lines.append(f"  - [{r['tool_name']}] {r['query'][:60]}\n    {snippet}...")
            sections.append("🔍 Relevant past research:\n" + "\n".join(lines))

        if not sections:
            return ""

        return (
            "=== MEMORY CONTEXT ===\n"
            + "\n\n".join(sections)
            + "\n=== END MEMORY CONTEXT ===\n\n"
        )

    def close(self):
        """Close the Weaviate connection."""
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
            self._ready = False