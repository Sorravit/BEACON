#!/usr/bin/env python3
"""
core/agent/config.py — configuration manager for the AI assistant.

Extracted verbatim from the original ``main.py``.
"""

import os
from pathlib import Path
from typing import Optional

from core.agent.runtime import (
    ModelRegistry,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
)


class Config:
    """Configuration manager for the AI assistant."""

    def __init__(self):
        """Initialize configuration from environment variables and .env file."""
        self._load_env_file()
        self._load_config()

    def _load_env_file(self):
        """Load environment variables from .env file if it exists."""
        env_file = Path(".env")
        if not env_file.exists():
            return

        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip("\"'")

    def _load_config(self):
        """Load configuration values from environment."""
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL)
        self.model = os.getenv("AI_MODEL", DEFAULT_MODEL)
        self.temperature = float(os.getenv("AI_TEMPERATURE", str(DEFAULT_TEMPERATURE)))
        self.max_tokens = int(os.getenv("AI_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
        self.enable_tools = os.getenv("ENABLE_TOOLS", "true").lower() == "true"

        # Curated registry of selectable models + per-role defaults (models.yaml).
        # Loaded once here so chat and orchestration share a single source of truth.
        self.models = ModelRegistry.load(env_default=self.model)
        # Honour the registry default unless AI_MODEL was explicitly set.
        if "AI_MODEL" not in os.environ:
            self.model = self.models.default_model

    def resolve_model(self, requested: Optional[str] = None, role: Optional[str] = None) -> str:
        """Resolve a requested/role model id to a valid registered model.

        Resolution order: an explicit valid ``requested`` id → the role default
        from models.yaml → the global default. The global ``self.model`` is only
        used as the request when no role is supplied, so role-scoped sub-agents
        correctly pick up their per-role defaults instead of the chat model.
        """
        if requested is None and role is None:
            requested = self.model
        return self.models.resolve(requested, role=role)

    def validate(self) -> bool:
        """Validate that required configuration is present."""
        return bool(self.api_key)

    def display(self):
        """Display current configuration."""
        print(f"Model: {self.model}")
        print(f"Endpoint: {self.base_url}")
        print(f"Tools: {self.enable_tools}")
        print(f"Models available: {len(self.models.ids())}")
