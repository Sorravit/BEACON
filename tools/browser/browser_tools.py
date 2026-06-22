"""Browser automation tool handlers."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class BrowserToolsMixin:
    async def _ensure_browser(self):
        """Get or create a browser page for this request.

        Phase 6 / #2: When self._context is already set (from BrowserPool),
        reuse it — never launch a new browser or create a new context.
        """
        # If we already have a page that is still open, reuse it
        if self.page and not self.page.is_closed():
            return self.page

        if self._context:
            # Pool-provided context — create a new page inside it
            pages = self._context.pages
            if pages:
                self.page = pages[-1]
            else:
                self.page = await self._context.new_page()
            return self.page

        # Legacy standalone path (no pool context)
        if self._shared_browser:
            self.browser = self._shared_browser
        elif not self.browser:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=False)
            logger.info("Browser launched (standalone)")

        if not self._context:
            self._context = await self.browser.new_context()
            self.page = await self._context.new_page()
            logger.info("Browser context created (standalone)")

        return self.page

    async def _browser_navigate(self, url: str):
        try:
            page = await self._ensure_browser()
            await page.goto(url, wait_until="domcontentloaded")
            return f"Navigated to {url}"
        except Exception as exc:
            return f"Error: {exc}"

    async def _browser_click(self, selector: str):
        try:
            page = await self._ensure_browser()
            await page.click(selector)
            return f"Clicked {selector}"
        except Exception as exc:
            return f"Error: {exc}"

    async def _browser_type(self, selector: str, text: str):
        try:
            page = await self._ensure_browser()
            await page.fill(selector, text)
            return f"Typed '{text}' into {selector}"
        except Exception as exc:
            return f"Error: {exc}"

    async def _browser_screenshot(self, filename: str):
        try:
            page = await self._ensure_browser()
            path = Path(filename)
            if not path.parent or str(path.parent) == ".":
                Path("output").mkdir(exist_ok=True)
                filename = str(Path("output") / path.name)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=filename)
            return f"Screenshot saved to {filename}"
        except Exception as exc:
            return f"Error: {exc}"

    async def _browser_get_text(self, selector: str):
        try:
            page = await self._ensure_browser()
            text = await page.text_content(selector)
            return f"Text: {text}"
        except Exception as exc:
            return f"Error: {exc}"

    async def _browser_close(self):
        """Close the browser / tab.

        Phase 6 / #2 fix: when using a pool-provided context, only close the
        current tab/page — never close the shared context or Chromium process.
        """
        try:
            if self._shared_context or (self._context and self._context is getattr(self, '_shared_context', None)):
                # Pool-owned context: close only this page (tab)
                if self.page and not self.page.is_closed():
                    await self.page.close()
                self.page = None
                return "Closed this browser tab (shared context preserved)"

            # Standalone path: close fully
            if self.browser:
                await self.browser.close()
                if self.playwright:
                    await self.playwright.stop()
                self.browser = None
                self.page = None
                self._context = None
                self.playwright = None
                return "Browser closed"
            return "Browser was not open"
        except Exception as exc:
            return f"Error: {exc}"
