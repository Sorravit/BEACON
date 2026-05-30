"""Encoding helpers used across the BEACON runtime."""

import logging

logger = logging.getLogger(__name__)


def safe_encode_string(text: str, errors: str = "replace") -> str:
    """Normalize invalid UTF-8 sequences so logs/JSON serialization never crash."""
    if not text:
        return text
    try:
        return text.encode("utf-8", errors=errors).decode("utf-8")
    except Exception as exc:
        logger.warning("Failed to encode string safely: %s", exc)
        try:
            return str(text).encode("utf-8", errors="ignore").decode("utf-8")
        except Exception:
            return "[Invalid UTF-8 content]"

