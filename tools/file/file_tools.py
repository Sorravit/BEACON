"""Filesystem tool handlers."""

from pathlib import Path


class FileToolsMixin:
    async def _read_file(self, file_path: str):
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
                return f"Content of {file_path}:\n{handle.read()}"
        except Exception as exc:
            return f"Error: {exc}"

    async def _write_file(self, file_path: str, content: str):
        try:
            path = Path(file_path)
            # If no directory is specified, default to 'output/'
            if not path.parent or str(path.parent) == ".":
                path = Path("output") / path.name
                file_path = str(path)
            
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as handle:
                handle.write(content)
            return f"Wrote to {file_path}"
        except Exception as exc:
            return f"Error: {exc}"

    async def _list_files(self, directory: str):
        try:
            files = [entry.name for entry in Path(directory).iterdir()]
            return f"Files in {directory}:\n" + "\n".join(files)
        except Exception as exc:
            return f"Error: {exc}"

