"""Browser automation tool handlers."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class BrowserToolsMixin:
    async def _ensure_browser(self):
        """Get or create a per-request browser context and page."""
        if self._shared_browser:
            self.browser = self._shared_browser
        elif not self.browser:
            from playwright.async_api import async_playwright

            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=False)
            logger.info("Browser launched")

        if not self._context:
            self._context = await self.browser.new_context()
            self.page = await self._context.new_page()
            logger.info("Browser context created")

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
        try:
            if self.browser:
                await self.browser.close()
                await self.playwright.stop()
                self.browser = None
                self.page = None
                return "Browser closed"
            return "Browser was not open"
        except Exception as exc:
            return f"Error: {exc}"

