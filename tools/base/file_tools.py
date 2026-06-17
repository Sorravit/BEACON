"""Filesystem tool handlers — fully async (Step 4 fix).

Changes vs original:
  - _read_file  : sync open()         → async aiofiles.open()
  - _write_file : sync open()         → async aiofiles.open()
  - _list_files : sync Path.iterdir() → asyncio.to_thread(lambda: [...])
                  (aiofiles has no async iterdir; offloading to a thread-pool
                   worker is the correct pattern — never blocks the event loop)

All three methods were already declared `async def`; they now *await*
non-blocking I/O instead of parking the entire event loop.
"""

import asyncio
from pathlib import Path

import aiofiles


class FileToolsMixin:
    # ------------------------------------------------------------------
    # read_file
    # ------------------------------------------------------------------
    async def _read_file(self, file_path: str) -> str:
        """Read a file asynchronously and return its content as a string."""
        try:
            async with aiofiles.open(
                file_path, mode="r", encoding="utf-8", errors="replace"
            ) as handle:
                content = await handle.read()
            return f"Content of {file_path}:\n{content}"
        except Exception as exc:
            return f"Error: {exc}"

    # ------------------------------------------------------------------
    # write_file
    # ------------------------------------------------------------------
    async def _write_file(self, file_path: str, content: str) -> str:
        """Write content to a file asynchronously.

        If the path has no explicit parent directory (e.g. a bare filename),
        the file is placed under ``output/`` by default.
        Parent directories are created as needed.
        """
        try:
            path = Path(file_path)

            # Default to output/ when no directory component is given
            if not path.parent or str(path.parent) == ".":
                path = Path("output") / path.name
                file_path = str(path)

            # mkdir is fast but still synchronous — offload it
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)

            async with aiofiles.open(file_path, mode="w", encoding="utf-8") as handle:
                await handle.write(content)

            return f"Wrote to {file_path}"
        except Exception as exc:
            return f"Error: {exc}"

    # ------------------------------------------------------------------
    # list_files
    # ------------------------------------------------------------------
    async def _list_files(self, directory: str) -> str:
        """List directory entries asynchronously.

        ``Path.iterdir()`` is synchronous; wrapping it with
        ``asyncio.to_thread`` keeps the event loop free during the scan.
        """
        try:
            entries: list[str] = await asyncio.to_thread(
                lambda: [entry.name for entry in Path(directory).iterdir()]
            )
            return f"Files in {directory}:\n" + "\n".join(entries)
        except Exception as exc:
            return f"Error: {exc}"
