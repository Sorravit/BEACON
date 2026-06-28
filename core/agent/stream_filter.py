#!/usr/bin/env python3
"""
core/agent/stream_filter.py — incremental tool-markup stream filter.

Strips prompt-based tool-call markup from a streamed token sequence so raw tool
XML never reaches the chat UI. Extracted verbatim from the original ``main.py``.
"""

import re


class _ToolMarkupStreamFilter:
    """Incremental filter that strips prompt-based tool-call markup from a token
    stream so it never reaches the chat UI.

    Suppresses the regions:
      * ``<tool_use> … </tool_use>``  (and ``_``/``-`` and mismatched closes)
      * ``<function_calls> … </function_calls>``  (with nested ``<invoke>``)
      * ``<invoke name=…> … </invoke>``

    ``feed(delta)`` returns the safe text to emit for that delta (possibly empty);
    ``flush()`` returns any remaining safe text at end of stream. Ambiguous
    partial-tag tails (a ``<`` that might begin one of the markers) are held back
    until the next delta disambiguates them.
    """

    _OPEN = re.compile(r'<(tool[_-]use|function_calls|invoke)\b', re.IGNORECASE)
    # Per-marker close patterns. function_calls is matched specifically so a
    # nested <invoke>…</invoke> inside it does not end suppression early.
    _CLOSE_TOOL = re.compile(r'</(?:tool[_-](?:use|invoke|call)|use)>', re.IGNORECASE)
    _CLOSE_FUNCTION = re.compile(r'</function_calls>', re.IGNORECASE)
    _CLOSE_INVOKE = re.compile(r'</invoke>', re.IGNORECASE)
    # Longest marker we might need to recognise from a partial tail.
    _MAX_MARKER = len("<function_calls>")

    def __init__(self):
        self._buf = ""
        self._close_re = None  # active close pattern while suppressing

    def _close_for(self, marker: str):
        m = marker.lower()
        if m.startswith("function"):
            return self._CLOSE_FUNCTION
        if m.startswith("invoke"):
            return self._CLOSE_INVOKE
        return self._CLOSE_TOOL

    def feed(self, delta: str) -> str:
        self._buf += delta
        out = []
        while True:
            if self._close_re is None:
                m = self._OPEN.search(self._buf)
                if m is None:
                    safe, hold = self._split_safe_tail(self._buf)
                    if safe:
                        out.append(safe)
                    self._buf = hold
                    break
                out.append(self._buf[:m.start()])
                self._buf = self._buf[m.start():]
                self._close_re = self._close_for(m.group(1))
            else:
                m = self._close_re.search(self._buf)
                if m is None:
                    # Still inside a tool block — keep buffering, emit nothing.
                    break
                self._buf = self._buf[m.end():]
                self._close_re = None
        return "".join(out)

    def flush(self) -> str:
        # An unterminated tool block at EOS is dropped; otherwise emit leftovers
        # (e.g. a held-back '<' that turned out to be ordinary prose).
        if self._close_re is not None:
            self._buf = ""
            return ""
        out, self._buf = self._buf, ""
        return out

    def _split_safe_tail(self, s: str):
        """Split ``s`` into (emit_now, hold_back). Hold back a trailing fragment
        that could still grow into one of the suppressed markers."""
        idx = s.rfind('<')
        if idx == -1:
            return s, ""
        tail = s[idx:]
        # Only hold a short, tag-shaped tail (could be a partial open/close tag).
        if len(tail) <= self._MAX_MARKER and re.fullmatch(r'</?[a-zA-Z_-]*', tail):
            return s[:idx], tail
        return s, ""
