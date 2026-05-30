"""HTTP tool handlers."""


class HttpToolsMixin:
    async def _http_get(self, url: str):
        try:
            import httpx

            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                return f"Status: {response.status_code}\nBody: {response.text[:500]}"
        except Exception as exc:
            return f"Error: {exc}"

    async def _http_post(self, url: str, data: str):
        try:
            import httpx

            async with httpx.AsyncClient() as client:
                response = await client.post(url, content=data)
                return f"Status: {response.status_code}\nBody: {response.text[:500]}"
        except Exception as exc:
            return f"Error: {exc}"

