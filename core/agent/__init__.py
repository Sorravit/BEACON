#!/usr/bin/env python3
"""
core/agent — the AIAgent package.

Re-exports the public surface (``AIAgent``, ``Config``) so existing imports such
as ``from core.agent import AIAgent`` work, while the implementation is split
across topic modules (base, config, runtime, *_mixin, stream_filter).
"""

from core.agent.config import Config
from core.agent.base import AIAgent

__all__ = ["AIAgent", "Config"]
