"""Curated model registry for BEACON.

BEACON supports many models exposed by the configured API endpoint. Rather than
discovering them over the network, the set of *selectable* models and the default
model for each agent role are declared in a static, user-editable ``models.yaml``.
This keeps model selection predictable, reviewable and fully offline-friendly.

The registry powers three things:

* the chat model picker (``GET /models``);
* per-role defaults used by the multi-agent orchestrator;
* per-request model overrides (the UI/API may pass any registered model id).

Unknown or missing model ids are resolved safely back to a sensible default so a
stale UI selection can never crash a request.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Roles understood by the orchestrator + chat. Extra roles may be added freely in
# models.yaml; these are the ones BEACON references by name.
KNOWN_ROLES = (
    "chat",
    "orchestrator",
    "researcher",
    "planner",
    "engineer",
    "devops",
    "verifier",
    "titler",
)

# Used only if models.yaml is missing/empty so the app still boots.
_FALLBACK_DEFAULT = "global/anthropic.claude-sonnet-4-6"


@dataclass(frozen=True)
class ModelInfo:
    """A single selectable model."""

    id: str
    label: str
    description: str = ""
    context: Optional[int] = None
    tags: tuple = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "context": self.context,
            "tags": list(self.tags),
        }


class ModelRegistry:
    """In-memory view of ``models.yaml`` with safe lookups and role defaults."""

    def __init__(
        self,
        models: List[ModelInfo],
        role_defaults: Dict[str, str],
        default_model: str,
        source: Optional[Path] = None,
    ) -> None:
        self._models: Dict[str, ModelInfo] = {m.id: m for m in models}
        self._role_defaults = dict(role_defaults)
        self._default_model = default_model
        self.source = source

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def load(
        cls,
        path: Optional[os.PathLike | str] = None,
        env_default: Optional[str] = None,
    ) -> "ModelRegistry":
        """Load the registry from ``models.yaml``.

        Args:
            path: Optional explicit path. Defaults to ``$MODELS_CONFIG_FILE`` or
                ``models.yaml`` in the working directory.
            env_default: Model id from the environment (e.g. ``AI_MODEL``) used as
                the default when the YAML omits one.
        """
        registry_path = Path(
            path or os.getenv("MODELS_CONFIG_FILE", "models.yaml")
        )

        data: Dict[str, object] = {}
        if registry_path.exists():
            try:
                data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Failed to parse %s: %s", registry_path, exc)
                data = {}
        else:
            logger.warning(
                "Model registry %s not found — using built-in fallback", registry_path
            )

        models = cls._parse_models(data.get("models"))
        default_model = (
            data.get("default")
            or env_default
            or (models[0].id if models else _FALLBACK_DEFAULT)
        )

        # Ensure the default and any env model are always selectable.
        known_ids = {m.id for m in models}
        for extra in (default_model, env_default):
            if extra and extra not in known_ids:
                models.append(ModelInfo(id=extra, label=extra, description="(from environment)"))
                known_ids.add(extra)

        role_defaults = cls._parse_roles(data.get("roles"), default_model, known_ids)

        registry = cls(models, role_defaults, default_model, source=registry_path)
        logger.info(
            "Model registry loaded: %d models, default=%s (%s)",
            len(models),
            default_model,
            registry_path,
        )
        return registry

    @staticmethod
    def _parse_models(raw: object) -> List[ModelInfo]:
        models: List[ModelInfo] = []
        if not isinstance(raw, list):
            return models
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            model_id = str(entry["id"]).strip()
            tags = entry.get("tags") or []
            models.append(
                ModelInfo(
                    id=model_id,
                    label=str(entry.get("label") or model_id),
                    description=str(entry.get("description") or ""),
                    context=entry.get("context"),
                    tags=tuple(str(t) for t in tags),
                )
            )
        return models

    @staticmethod
    def _parse_roles(
        raw: object, default_model: str, known_ids: set
    ) -> Dict[str, str]:
        roles: Dict[str, str] = {role: default_model for role in KNOWN_ROLES}
        if isinstance(raw, dict):
            for role, model_id in raw.items():
                model_id = str(model_id).strip()
                if model_id not in known_ids:
                    logger.warning(
                        "Role '%s' points at unregistered model '%s' — using default",
                        role,
                        model_id,
                    )
                    model_id = default_model
                roles[str(role)] = model_id
        return roles

    # ── lookups ──────────────────────────────────────────────────────────────

    @property
    def default_model(self) -> str:
        return self._default_model

    def ids(self) -> List[str]:
        return list(self._models.keys())

    def has(self, model_id: Optional[str]) -> bool:
        return bool(model_id) and model_id in self._models

    def get(self, model_id: str) -> Optional[ModelInfo]:
        return self._models.get(model_id)

    def model_for_role(self, role: Optional[str]) -> str:
        """Return the configured default model for a role (or the global default)."""
        if role and role in self._role_defaults:
            return self._role_defaults[role]
        return self._default_model

    def resolve(self, requested: Optional[str], role: Optional[str] = None) -> str:
        """Resolve a requested model id to a valid, registered model.

        Resolution order: explicit valid request → role default → global default.
        This guarantees the returned id is always selectable, so a stale or invalid
        client selection degrades gracefully instead of erroring.
        """
        if self.has(requested):
            return requested  # type: ignore[return-value]
        if requested:
            logger.debug("Requested model '%s' not registered — falling back", requested)
        return self.model_for_role(role)

    # ── serialisation ────────────────────────────────────────────────────────

    def to_public_list(self) -> List[Dict[str, object]]:
        return [m.to_dict() for m in self._models.values()]

    def role_defaults(self) -> Dict[str, str]:
        return dict(self._role_defaults)

