
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
import socket
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, TypeVar
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

import os as _os
WEAVIATE_URL = f"http://localhost:{_os.getenv('WEAVIATE_PORT', '8090')}"
# Local embedding model — no API key needed, works with any LLM backend
LOCAL_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
T = TypeVar("T")


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

            # Load local embedding model once (downloads ~90MB on first run only)
            if self._encoder is None:
                logger.info(f"Loading local embedding model: {self.embedding_model}")
                self._encoder = SentenceTransformer(
                    self.embedding_model,
                    local_files_only=os.getenv("TRANSFORMERS_OFFLINE", "0") == "1"
                )

            # Weaviate client (anonymous access, no vectorizer module)
            if self.client:
                try:
                    self.client.close()
                except Exception:
                    pass

            parsed = urlparse(self.weaviate_url)
            host = parsed.hostname or "localhost"
            port = parsed.port or int(_os.getenv("WEAVIATE_PORT", "8090"))
            self.client = weaviate.connect_to_local(host=host, port=port)

            await self._ensure_collections()
            self._ready = True
            logger.info("VectorMemory initialized (local sentence-transformers embeddings)")
            return True

        except ImportError as e:
            logger.error(f"Missing dependency: {e}. Run: pip install weaviate-client sentence-transformers")
            self._ready = False
            return False
        except Exception as e:
            logger.warning(f"VectorMemory not available: {e}")
            self._ready = False
            self.client = None
            return False

    async def ensure_ready(self) -> bool:
        """Ensure memory backend is ready; try to reconnect lazily when needed."""
        if self._ready and self.client:
            return True

        logger.info("VectorMemory is not ready, attempting reconnect")
        return await self.initialize()

    def _mark_unavailable(self):
        """Mark current DB connection as unavailable so next call reconnects cleanly."""
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None
        self._ready = False

    def _is_grpc_reachable(self, timeout_seconds: float = 0.4) -> bool:
        """Fast TCP check for Weaviate gRPC port to avoid long internal backoff retries."""
        parsed = urlparse(self.weaviate_url)
        host = parsed.hostname or "localhost"
        grpc_port = int(_os.getenv("WEAVIATE_GRPC_PORT", "50051"))
        try:
            with socket.create_connection((host, grpc_port), timeout=timeout_seconds):
                return True
        except OSError:
            return False

    async def _run_with_reconnect(
        self,
        operation_name: str,
        operation: Callable[[], T],
        *,
        on_failure: Optional[T],
        retries: int = 3,
    ) -> Optional[T]:
        """Run a DB operation with reconnect retries; return on_failure if unavailable."""
        if not await self.ensure_ready():
            return on_failure

        if not self._is_grpc_reachable():
            logger.info(f"{operation_name} skipped: Weaviate gRPC is unreachable")
            self._mark_unavailable()
            return on_failure

        attempts = max(retries, 1)
        for attempt in range(1, attempts + 1):
            try:
                return operation()
            except Exception as exc:
                if attempt >= attempts:
                    logger.error(f"{operation_name} failed after {attempts} attempt(s): {exc}")
                    self._mark_unavailable()
                    return on_failure

                logger.warning(
                    f"{operation_name} failed (attempt {attempt}/{attempts}), reconnecting: {exc}"
                )
                self._mark_unavailable()
                if not await self.ensure_ready():
                    return on_failure
                if not self._is_grpc_reachable():
                    logger.info(f"{operation_name} retry aborted: Weaviate gRPC is unreachable")
                    self._mark_unavailable()
                    return on_failure

        return on_failure

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

        if "AutoLearned" not in existing:
            self.client.collections.create(
                name="AutoLearned",
                vectorizer_config=wc.Configure.Vectorizer.none(),
                properties=[
                    wc.Property(name="topic",       data_type=wc.DataType.TEXT),
                    wc.Property(name="fact",        data_type=wc.DataType.TEXT),
                    wc.Property(name="confidence",  data_type=wc.DataType.TEXT),
                    wc.Property(name="stored_at",   data_type=wc.DataType.TEXT),
                    wc.Property(name="updated_at",  data_type=wc.DataType.TEXT),
                ]
            )
            logger.info("Created AutoLearned collection")

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

    async def store_research(self, tool_name: str, query: str, result: str) -> Optional[bool]:
        """Store a tool result in ResearchMemory."""
        def _op() -> bool:
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

        return await self._run_with_reconnect(
            "store_research",
            _op,
            on_failure=None,
        )

    async def search_research(self, query: str, limit: int = 5) -> Optional[List[Dict[str, Any]]]:
        """Semantic search over ResearchMemory using near_vector."""
        def _op() -> List[Dict[str, Any]]:
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

        return await self._run_with_reconnect(
            "search_research",
            _op,
            on_failure=None,
        )

    # -------------------------------------------------------------------------
    # Personal facts memory
    # -------------------------------------------------------------------------

    async def store_personal_fact(self, topic: str, fact: str) -> Optional[bool]:
        """Store a personal fact in PersonalFacts."""
        def _op() -> bool:
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

        return await self._run_with_reconnect(
            "store_personal_fact",
            _op,
            on_failure=None,
        )

    async def search_personal_facts(self, query: str, limit: int = 10) -> Optional[List[Dict[str, Any]]]:
        """Semantic search over PersonalFacts."""
        def _op() -> List[Dict[str, Any]]:
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

        return await self._run_with_reconnect(
            "search_personal_facts",
            _op,
            on_failure=None,
        )

    async def get_all_personal_facts(self) -> Optional[List[Dict[str, Any]]]:
        """Return all stored personal facts, or None when backend access fails."""
        def _op() -> List[Dict[str, Any]]:
            collection = self.client.collections.get("PersonalFacts")
            response = collection.query.fetch_objects(
                limit=200,
                return_properties=["topic", "fact", "stored_at"]
            )
            return [obj.properties for obj in response.objects]

        return await self._run_with_reconnect(
            "get_all_personal_facts",
            _op,
            on_failure=None,
        )

    async def delete_personal_facts(self, keyword: str) -> Optional[int]:
        """
        Delete personal facts whose topic or fact text contains the keyword.
        Returns the number of entries deleted.
        """
        def _op() -> int:
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

        return await self._run_with_reconnect(
            "delete_personal_facts",
            _op,
            on_failure=None,
        )

    async def delete_research(self, keyword: str) -> Optional[int]:
        """
        Delete research entries whose query or result contains the keyword.
        Returns the number of entries deleted.
        """
        def _op() -> int:
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

        return await self._run_with_reconnect(
            "delete_research",
            _op,
            on_failure=None,
        )

    async def clear_all_research(self) -> Optional[int]:
        """Delete all research memory entries. Returns count deleted."""
        def _op() -> int:
            collection = self.client.collections.get("ResearchMemory")
            response = collection.query.fetch_objects(limit=5000)
            count = len(response.objects)
            for obj in response.objects:
                collection.data.delete_by_id(obj.uuid)
            logger.info(f"Cleared all {count} research memory entries")
            return count

        return await self._run_with_reconnect(
            "clear_all_research",
            _op,
            on_failure=None,
        )


    # -------------------------------------------------------------------------
    # Auto-learned memory (extracted by hook after each conversation turn)
    # -------------------------------------------------------------------------

    async def store_auto_fact(self, topic: str, fact: str, confidence: str = "medium") -> Optional[bool]:
        """Store an auto-extracted fact. Updates existing entry if topic matches."""
        def _op() -> bool:
            # Check if topic already exists — update instead of duplicate
            collection = self.client.collections.get("AutoLearned")
            existing = collection.query.fetch_objects(
                limit=200,
                return_properties=["topic", "fact"]
            )
            lower_topic = topic.lower()
            for obj in existing.objects:
                if (obj.properties.get("topic") or "").lower() == lower_topic:
                    # Update in place
                    collection.data.update(
                        uuid=obj.uuid,
                        properties={
                            "fact": fact,
                            "confidence": confidence,
                            "updated_at": datetime.now().isoformat(),
                        }
                    )
                    logger.info(f"Updated auto-fact [{topic}]")
                    return True

            # New fact — insert
            vector = self._embed(f"{topic}: {fact}", label="Auto-learning")
            if vector is None:
                return False
            collection.data.insert(
                properties={
                    "topic":      topic,
                    "fact":       fact,
                    "confidence": confidence,
                    "stored_at":  datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat(),
                },
                vector=vector
            )
            logger.info(f"Stored auto-fact [{topic}]: {fact[:80]}")
            return True

        return await self._run_with_reconnect(
            "store_auto_fact",
            _op,
            on_failure=None,
        )

    async def get_all_auto_facts(self) -> Optional[List[Dict[str, Any]]]:
        """Return all auto-learned facts."""
        def _op() -> List[Dict[str, Any]]:
            collection = self.client.collections.get("AutoLearned")
            response = collection.query.fetch_objects(
                limit=200,
                return_properties=["topic", "fact", "confidence", "stored_at", "updated_at"]
            )
            return [obj.properties for obj in response.objects]

        return await self._run_with_reconnect(
            "get_all_auto_facts",
            _op,
            on_failure=None,
        )

    async def search_auto_facts(self, query: str, limit: int = 10) -> Optional[List[Dict[str, Any]]]:
        """Semantic search over AutoLearned collection."""
        def _op() -> List[Dict[str, Any]]:
            vector = self._embed(query)
            if vector is None:
                return []
            collection = self.client.collections.get("AutoLearned")
            response = collection.query.near_vector(
                near_vector=vector,
                limit=limit,
                return_properties=["topic", "fact", "confidence", "stored_at"]
            )
            return [obj.properties for obj in response.objects]

        return await self._run_with_reconnect(
            "search_auto_facts",
            _op,
            on_failure=None,
        )

    async def delete_auto_facts(self, keyword: str) -> Optional[int]:
        """Delete auto-learned facts matching keyword."""
        def _op() -> int:
            collection = self.client.collections.get("AutoLearned")
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
            return len(to_delete)

        return await self._run_with_reconnect(
            "delete_auto_facts",
            _op,
            on_failure=None,
        )

    async def clear_auto_facts(self) -> Optional[int]:
        """Clear all auto-learned facts."""
        def _op() -> int:
            collection = self.client.collections.get("AutoLearned")
            response = collection.query.fetch_objects(limit=5000)
            count = len(response.objects)
            for obj in response.objects:
                collection.data.delete_by_id(obj.uuid)
            logger.info(f"Cleared {count} auto-learned facts")
            return count

        return await self._run_with_reconnect(
            "clear_auto_facts",
            _op,
            on_failure=None,
        )

    # -------------------------------------------------------------------------
    # Context builder
    # -------------------------------------------------------------------------
    # Auto-learned memory (extracted by hook after each conversation turn)
    # -------------------------------------------------------------------------

    async def build_context_prompt(self, user_query: str) -> str:
        """
        Build a compact memory context string to inject into the prompt.
        Searches all three collections for the most relevant entries.
        """
        sections = []

        facts = await self.search_personal_facts(user_query, limit=10) or []
        if facts:
            lines = [f"  - [{f['topic']}] {f['fact']}" for f in facts]
            sections.append("📋 What I know about you:\n" + "\n".join(lines))

        auto_facts = await self.search_auto_facts(user_query, limit=8) or []
        if auto_facts:
            lines = [f"  - [{f['topic']}] {f['fact']}" for f in auto_facts]
            sections.append("🧠 What I've learned about you:\n" + "\n".join(lines))

        research = await self.search_research(user_query, limit=3) or []
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
        """Close the Weaviate connection and shut down the loky process pool.

        sentence-transformers → joblib → loky creates a reusable worker pool
        backed by a POSIX semaphore. Without an explicit shutdown() call the
        semaphore is left alive at process exit and Python's resource_tracker
        reports: "leaked semaphore objects to clean up at shutdown".
        """
        # Fix #2: shut down the loky reusable executor so its POSIX semaphore
        # is released cleanly instead of being reported as a leak.
        try:
            from joblib.externals.loky import get_reusable_executor
            executor = get_reusable_executor()
            try:
                # Supported on newer loky; forces worker teardown and semaphore cleanup.
                executor.shutdown(wait=True, kill_workers=True)
            except TypeError:
                executor.shutdown(wait=True)
        except Exception:
            pass

        try:
            from joblib.externals.loky.backend import resource_tracker as _loky_rt
            tracker = getattr(_loky_rt, "_resource_tracker", None)
            if tracker is not None:
                tracker._stop()
        except Exception:
            pass

        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
            self._ready = False
