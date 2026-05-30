"""System-level tool handlers."""

import json
import logging
import urllib.request as _ureq
from urllib.parse import quote

logger = logging.getLogger(__name__)


class SystemToolsMixin:
    async def _get_current_time(self):
        """Get current date and time."""
        try:
            from datetime import datetime

            now = datetime.now()
            return f"Current time: {now.strftime('%A, %B %d, %Y at %I:%M:%S %p')}"
        except Exception as exc:
            return f"Error: {exc}"

    async def _web_search(self, query: str):
        """Search with API first, then browser fallbacks."""
        _NEWS_KW = [
            "latest",
            "recent",
            "news",
            "today",
            "tonight",
            "this week",
            "this month",
            "right now",
            "currently",
            "breaking",
            "just ",
            "happened",
            "trending",
            "update",
            "2024",
            "2025",
            "2026",
        ]
        skip_api = any(kw in query.lower() for kw in _NEWS_KW)

        if not skip_api:
            try:
                req = _ureq.Request(
                    f"https://api.duckduckgo.com/?q={quote(query)}&format=json&no_redirect=1&no_html=1",
                    headers={"User-Agent": "Mozilla/5.0 (compatible; BigAI/1.0)"},
                )
                with _ureq.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())

                rows = []
                if data.get("AbstractText"):
                    rows.append(f"**{data.get('Heading', 'Answer')}**: {data['AbstractText']}")
                    if data.get("AbstractURL"):
                        rows.append(f"Source: {data['AbstractURL']}")
                    rows.append("")

                for i, topic in enumerate(data.get("RelatedTopics", [])[:8]):
                    if "Topics" in topic:
                        for subtopic in topic.get("Topics", [])[:3]:
                            if subtopic.get("Text"):
                                rows.append(f"{len(rows)+1}. {subtopic['Text']}")
                                if subtopic.get("FirstURL"):
                                    rows.append(f"   {subtopic['FirstURL']}")
                    else:
                        if topic.get("Text"):
                            rows.append(f"{i+1}. {topic['Text']}")
                            if topic.get("FirstURL"):
                                rows.append(f"   {topic['FirstURL']}")

                for item in data.get("Results", [])[:5]:
                    if item.get("Text"):
                        rows.append(f"- {item['Text']}")
                        if item.get("FirstURL"):
                            rows.append(f"  {item['FirstURL']}")

                if rows:
                    return "DuckDuckGo results for '" + query + "':\n\n" + "\n".join(rows)
            except Exception:
                pass

        try:
            page = await self._ensure_browser()
            await page.goto("https://duckduckgo.com", wait_until="domcontentloaded")
            await page.wait_for_selector('input[name="q"]', timeout=6000)
            await page.click('input[name="q"]')
            await page.fill('input[name="q"]', query)
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
            current_url = page.url
            text_result = await self._browser_get_text("body")
            safe = query[:20].replace(" ", "_")
            await self._browser_screenshot(f"output/search_{safe}.png")
            return (
                "DuckDuckGo browser search for '"
                + query
                + "':\nURL: "
                + str(current_url)
                + "\n\nPage text (first 2000 chars):\n"
                + str(text_result)[:2000]
            )
        except Exception:
            pass

        try:
            page = await self._ensure_browser()
            await page.goto("https://www.google.com", wait_until="domcontentloaded")
            await page.wait_for_selector('textarea[name="q"], input[name="q"]', timeout=6000)
            await page.click('textarea[name="q"], input[name="q"]')
            await page.fill('textarea[name="q"], input[name="q"]', query)
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
            current_url = page.url
            text_result = await self._browser_get_text("body")
            safe = query[:20].replace(" ", "_")
            await self._browser_screenshot(f"output/search_{safe}.png")
            return (
                "Google search for '"
                + query
                + "':\nURL: "
                + str(current_url)
                + "\n\nPage text (first 2000 chars):\n"
                + str(text_result)[:2000]
            )
        except Exception as google_err:
            return f"All search methods failed for '{query}'. Last error: {google_err}"

