"""
core/browser_pool.py — Per-session BrowserContext pool over one shared Chromium.

Design
------
- ONE non-headless Chromium process (so Big can watch / manually log in).
- Each session gets its own BrowserContext (isolated cookies, login state, tabs).
- Up to BROWSER_MAX_CONTEXTS (default 6) contexts live at once; LRU eviction
  closes the oldest context when the cap is reached.
- Within a session, turns are already serialised by the bg["task"] guard in
  web_app.py, so a single context per session has no intra-session races.
- Cross-session isolation is guaranteed because Playwright BrowserContexts
  never share cookies or local storage.
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_MAX_CONTEXTS = int(os.getenv("BROWSER_MAX_CONTEXTS", "6"))


class BrowserPool:
    """One non-headless Chromium; one BrowserContext per session (LRU-pooled)."""

    def __init__(self, max_contexts: int = _MAX_CONTEXTS):
        self._pw = None
        self._browser = None
        self._contexts: dict = {}   # session_id → BrowserContext
        self._lru: list = []         # session_ids ordered oldest→newest
        self._max = max_contexts
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_context(self, session_id: str):
        """Return (or lazily create) the BrowserContext for session_id."""
        async with self._lock:
            await self._ensure_browser()
            ctx = self._contexts.get(session_id)
            if ctx is None:
                # Evict LRU context if at capacity
                if len(self._contexts) >= self._max:
                    old_sid = self._lru.pop(0)
                    old_ctx = self._contexts.pop(old_sid, None)
                    if old_ctx:
                        try:
                            await old_ctx.close()
                        except Exception:
                            pass
                    logger.info(
                        "BrowserPool evicted context for session %s", old_sid
                    )
                ctx = await self._browser.new_context()
                self._contexts[session_id] = ctx
                logger.info(
                    "BrowserPool created context for session %s (%d/%d)",
                    session_id, len(self._contexts), self._max,
                )
            # Refresh LRU position
            if session_id in self._lru:
                self._lru.remove(session_id)
            self._lru.append(session_id)
            return ctx

    async def close_session(self, session_id: str) -> None:
        """Close and discard the context for a session (e.g. on session delete)."""
        async with self._lock:
            ctx = self._contexts.pop(session_id, None)
            if session_id in self._lru:
                self._lru.remove(session_id)
        if ctx:
            try:
                await ctx.close()
                logger.info("BrowserPool closed context for session %s", session_id)
            except Exception:
                pass

    async def shutdown(self) -> None:
        """Close all contexts, the shared browser, and Playwright."""
        async with self._lock:
            for ctx in list(self._contexts.values()):
                try:
                    await ctx.close()
                except Exception:
                    pass
            self._contexts.clear()
            self._lru.clear()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        logger.info("BrowserPool shut down")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _ensure_browser(self) -> None:
        """Lazily launch the shared Chromium process (non-headless)."""
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            # Keep headless=False so Big can see the browser and log in manually
            self._browser = await self._pw.chromium.launch(headless=False)
            logger.info("BrowserPool: shared non-headless Chromium started")

