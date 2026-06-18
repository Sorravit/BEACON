"""Curated model registry for BEACON — with dynamic live-listing support.

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

Dynamic listing
---------------
When ``DYNAMIC_MODEL_LISTING_ENABLED=true`` (env var), the registry will call
``GET {OPENAI_BASE_URL}/models`` on the ICA/OpenAI-compatible endpoint at startup
(and on ``POST /models/reload``) and **merge** the live results with the static
``models.yaml`` entries.

Merge rules:
* Static ``models.yaml`` entries always win (richer metadata preserved).
* Live-API models **not** already in ``models.yaml`` are auto-added with a
  generated label derived from the model id.
* Models prefixed with values in ``DYNAMIC_MODEL_LISTING_PREFIX_FILTER``
  (comma-separated) are included; blank = include all.
* If the live call fails or times out (``DYNAMIC_MODEL_LISTING_TIMEOUT_SECS``),
  the system logs a warning and falls back silently to the static list.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Roles understood by the orchestrator + chat.  Extra roles may be added freely
# in models.yaml; these are the ones BEACON references by name.
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


# ---------------------------------------------------------------------------
# ModelInfo
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelInfo:
    """A single selectable model."""

    id: str
    label: str
    description: str = ""
    context: Optional[int] = None
    tags: tuple = field(default_factory=tuple)
    # True when this entry was discovered from the live API (not in models.yaml)
    dynamic: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "context": self.context,
            "tags": list(self.tags),
            "dynamic": self.dynamic,
        }


# ---------------------------------------------------------------------------
# Dynamic listing helpers
# ---------------------------------------------------------------------------

def _dynamic_listing_enabled() -> bool:
    """Return True when dynamic listing is opted-in via env var."""
    return os.getenv("DYNAMIC_MODEL_LISTING_ENABLED", "false").lower() in ("1", "true", "yes")


def _listing_timeout() -> float:
    try:
        return float(os.getenv("DYNAMIC_MODEL_LISTING_TIMEOUT_SECS", "10"))
    except ValueError:
        return 10.0


def _prefix_filters() -> List[str]:
    raw = os.getenv("DYNAMIC_MODEL_LISTING_PREFIX_FILTER", "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()] if raw else []


def _model_id_to_label(model_id: str) -> str:
    """Generate a human-readable label from a raw model id.

    Examples::

        "global/anthropic.claude-sonnet-4-6"  -> "claude-sonnet-4-6 (anthropic)"
        "global/ibm/granite-4-h-small"         -> "granite-4-h-small (ibm)"
        "gpt-4o"                               -> "gpt-4o"
    """
    parts = [p for p in model_id.split("/") if p and p.lower() != "global"]
    if not parts:
        return model_id
    name = parts[-1]
    # Normalise "vendor.modelname" prefixes (e.g. "anthropic.claude-sonnet-4-6")
    if "." in name and len(name.split(".")) == 2:
        _vendor, name = name.split(".", 1)
    provider = parts[-2] if len(parts) >= 2 else ""
    return f"{name} ({provider})" if provider else name


def fetch_live_models_sync(
    base_url: str,
    api_key: str,
    timeout: float = 10.0,
    prefix_filters: Optional[List[str]] = None,
) -> List[ModelInfo]:
    """Call ``GET {base_url}/models`` synchronously and return ModelInfo objects.

    This is intentionally *synchronous* so it can be called both at startup
    (plain call) and from the event loop (via ``run_in_executor``).

    Args:
        base_url: ICA / OpenAI-compatible endpoint base URL.
        api_key:  API key, sent as ``Authorization: Bearer …``.
        timeout:  Request timeout in seconds.
        prefix_filters: When non-empty, only model ids starting with one of
            these prefixes are included.  Empty list = include all.

    Returns:
        List of :class:`ModelInfo` (all marked ``dynamic=True``).
        Returns ``[]`` on any error so callers always fall back gracefully.
    """
    import json as _json
    import urllib.error
    import urllib.request

    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        data = _json.loads(raw)
    except urllib.error.HTTPError as exc:
        logger.warning(
            "Dynamic model listing HTTP %s from %s — falling back to static registry",
            exc.code, url,
        )
        return []
    except urllib.error.URLError as exc:
        logger.warning(
            "Dynamic model listing connection error (%s) for %s — falling back to static registry",
            exc.reason, url,
        )
        return []
    except Exception as exc:
        logger.warning(
            "Dynamic model listing failed (%s) — falling back to static registry", exc
        )
        return []

    # OpenAI /models shape: {"object": "list", "data": [{...}, ...]}
    # Some providers wrap differently; handle both.
    entries: List[object] = []
    if isinstance(data, dict):
        entries = data.get("data") or data.get("models") or []
    elif isinstance(data, list):
        entries = data

    filters = prefix_filters or []
    results: List[ModelInfo] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or "").strip()
        if not model_id:
            continue
        if filters and not any(model_id.startswith(pf) for pf in filters):
            continue
        label = str(
            entry.get("label") or entry.get("name") or _model_id_to_label(model_id)
        )
        description = str(entry.get("description") or "(auto-discovered from live API)")
        context_raw = entry.get("context_length") or entry.get("context")
        results.append(
            ModelInfo(
                id=model_id,
                label=label,
                description=description,
                context=int(context_raw) if context_raw else None,
                tags=("dynamic",),
                dynamic=True,
            )
        )

    logger.info(
        "Dynamic model listing: %d model(s) fetched from %s", len(results), url
    )
    return results


async def fetch_live_models_async(
    base_url: str,
    api_key: str,
    timeout: float = 10.0,
    prefix_filters: Optional[List[str]] = None,
) -> List[ModelInfo]:
    """Async wrapper around :func:`fetch_live_models_sync`.

    Runs the blocking network call in the default thread-pool executor so the
    event loop is never blocked.
    """
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: fetch_live_models_sync(
                    base_url, api_key, timeout, prefix_filters
                ),
            ),
            timeout=timeout + 2,  # outer guard: executor + network latency
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Dynamic model listing timed out after %.0fs — using static registry",
            timeout,
        )
        return []


def _merge_live_into_static(
    static_models: List[ModelInfo],
    live_models: List[ModelInfo],
) -> List[ModelInfo]:
    """Merge live-discovered models into the static list.

    Static entries always win (richer metadata).  Live models absent from the
    static list are appended in the order returned by the API.
    """
    static_ids = {m.id for m in static_models}
    merged = list(static_models)
    added = 0
    for live in live_models:
        if live.id not in static_ids:
            merged.append(live)
            static_ids.add(live.id)
            added += 1
    if added:
        logger.info("Dynamic listing added %d new model(s) to registry", added)
    return merged


# ---------------------------------------------------------------------------
# ModelRegistry
# ---------------------------------------------------------------------------


class ModelRegistry:
    """In-memory view of ``models.yaml`` with safe lookups and role defaults.

    Optionally enriched with live model data from the ICA / OpenAI-compatible
    ``GET /models`` endpoint when ``DYNAMIC_MODEL_LISTING_ENABLED=true``.
    """

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
        """Load the registry from ``models.yaml``, optionally merging live models.

        When ``DYNAMIC_MODEL_LISTING_ENABLED=true`` this makes a *synchronous*
        HTTP call to ``GET {OPENAI_BASE_URL}/models``, guarded by
        ``DYNAMIC_MODEL_LISTING_TIMEOUT_SECS`` (default 10 s).  On any failure
        the static registry is used as-is (silent fallback).

        Args:
            path: Optional explicit path.  Defaults to ``$MODELS_CONFIG_FILE``
                or ``models.yaml`` in the working directory.
            env_default: Model id from the environment (e.g. ``AI_MODEL``) used
                as the default when the YAML omits one.
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

        static_models = cls._parse_models(data.get("models"))
        default_model = (
            data.get("default")
            or env_default
            or (static_models[0].id if static_models else _FALLBACK_DEFAULT)
        )

        # Ensure the default and any env model are always selectable.
        known_ids = {m.id for m in static_models}
        for extra in (default_model, env_default):
            if extra and extra not in known_ids:
                static_models.append(
                    ModelInfo(id=extra, label=extra, description="(from environment)")
                )
                known_ids.add(extra)

        # ── Dynamic listing (sync path, used at startup) ──────────────────
        all_models = static_models
        if _dynamic_listing_enabled():
            base_url = os.getenv("OPENAI_BASE_URL", "").rstrip("/")
            api_key = os.getenv("OPENAI_API_KEY", "")
            if base_url and api_key:
                live = fetch_live_models_sync(
                    base_url,
                    api_key,
                    timeout=_listing_timeout(),
                    prefix_filters=_prefix_filters(),
                )
                all_models = _merge_live_into_static(static_models, live)
            else:
                logger.warning(
                    "DYNAMIC_MODEL_LISTING_ENABLED=true but OPENAI_BASE_URL/"
                    "OPENAI_API_KEY are not set — skipping live listing"
                )

        known_ids = {m.id for m in all_models}
        role_defaults = cls._parse_roles(data.get("roles"), default_model, known_ids)

        registry = cls(all_models, role_defaults, default_model, source=registry_path)
        logger.info(
            "Model registry loaded: %d models (%d static, %d dynamic), default=%s (%s)",
            len(all_models),
            sum(1 for m in all_models if not m.dynamic),
            sum(1 for m in all_models if m.dynamic),
            default_model,
            registry_path,
        )
        return registry

    @classmethod
    async def load_async(
        cls,
        path: Optional[os.PathLike | str] = None,
        env_default: Optional[str] = None,
    ) -> "ModelRegistry":
        """Async variant of :meth:`load` — preferred when called from the event loop.

        Uses :func:`fetch_live_models_async` so the event loop is never blocked
        during the live API call.
        """
        registry_path = Path(
            path or os.getenv("MODELS_CONFIG_FILE", "models.yaml")
        )

        data: Dict[str, object] = {}
        if registry_path.exists():
            try:
                data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                logger.error("Failed to parse %s: %s", registry_path, exc)
                data = {}
        else:
            logger.warning(
                "Model registry %s not found — using built-in fallback", registry_path
            )

        static_models = cls._parse_models(data.get("models"))
        default_model = (
            data.get("default")
            or env_default
            or (static_models[0].id if static_models else _FALLBACK_DEFAULT)
        )

        known_ids = {m.id for m in static_models}
        for extra in (default_model, env_default):
            if extra and extra not in known_ids:
                static_models.append(
                    ModelInfo(id=extra, label=extra, description="(from environment)")
                )
                known_ids.add(extra)

        all_models = static_models
        if _dynamic_listing_enabled():
            base_url = os.getenv("OPENAI_BASE_URL", "").rstrip("/")
            api_key = os.getenv("OPENAI_API_KEY", "")
            if base_url and api_key:
                live = await fetch_live_models_async(
                    base_url,
                    api_key,
                    timeout=_listing_timeout(),
                    prefix_filters=_prefix_filters(),
                )
                all_models = _merge_live_into_static(static_models, live)
            else:
                logger.warning(
                    "DYNAMIC_MODEL_LISTING_ENABLED=true but OPENAI_BASE_URL/"
                    "OPENAI_API_KEY are not set — skipping live listing"
                )

        known_ids = {m.id for m in all_models}
        role_defaults = cls._parse_roles(data.get("roles"), default_model, known_ids)

        registry = cls(all_models, role_defaults, default_model, source=registry_path)
        logger.info(
            "Model registry loaded (async): %d models (%d static, %d dynamic), default=%s",
            len(all_models),
            sum(1 for m in all_models if not m.dynamic),
            sum(1 for m in all_models if m.dynamic),
            default_model,
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
                    dynamic=False,
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
        This guarantees the returned id is always selectable, so a stale or
        invalid client selection degrades gracefully instead of erroring.
        """
        if self.has(requested):
            return requested  # type: ignore[return-value]
        if requested:
            logger.debug("Requested model '%s' not registered — falling back", requested)
        return self.model_for_role(role)

    def resolve_many(self, requested: List[str]) -> List[str]:
        """Resolve a list of model ids, discarding any that are not registered.

        If **none** of the ids resolve, returns ``[self.default_model]`` so
        callers always get at least one valid model.

        De-duplicates while preserving the original order.

        Args:
            requested: Raw list of model ids from the client / API.

        Returns:
            De-duplicated list of valid, registered model ids.
        """
        seen: set = set()
        valid: List[str] = []
        for mid in requested:
            if mid and self.has(mid) and mid not in seen:
                valid.append(mid)
                seen.add(mid)
            elif mid:
                logger.debug("Requested model '%s' not in registry — skipping", mid)
        if not valid:
            logger.warning(
                "resolve_many: none of %r are registered — using default %s",
                requested,
                self._default_model,
            )
            return [self._default_model]
        return valid

    # ── serialisation ────────────────────────────────────────────────────────

    def to_public_list(self) -> List[Dict[str, object]]:
        return [m.to_dict() for m in self._models.values()]

    def role_defaults(self) -> Dict[str, str]:
        return dict(self._role_defaults)