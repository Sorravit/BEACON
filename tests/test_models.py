"""Tests for the curated model registry (core.models.ModelRegistry)."""

import textwrap

from core.models import ModelRegistry


def _write_registry(tmp_path, body: str):
    path = tmp_path / "models.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_load_parses_models_and_roles(tmp_path) -> None:
    path = _write_registry(
        tmp_path,
        """
        default: model-a
        roles:
          chat: model-a
          researcher: model-b
        models:
          - id: model-a
            label: Model A
          - id: model-b
            label: Model B
            tags: [fast]
        """,
    )
    registry = ModelRegistry.load(path=path)

    assert registry.default_model == "model-a"
    assert set(registry.ids()) == {"model-a", "model-b"}
    assert registry.model_for_role("researcher") == "model-b"
    assert registry.model_for_role("chat") == "model-a"


def test_resolve_falls_back_for_unknown_model(tmp_path) -> None:
    path = _write_registry(
        tmp_path,
        """
        default: model-a
        roles:
          verifier: model-b
        models:
          - id: model-a
            label: Model A
          - id: model-b
            label: Model B
        """,
    )
    registry = ModelRegistry.load(path=path)

    # Unknown id resolves to the role default, then to global default.
    assert registry.resolve("nope", role="verifier") == "model-b"
    assert registry.resolve("nope") == "model-a"
    assert registry.resolve("model-b") == "model-b"


def test_role_pointing_at_unregistered_model_falls_back(tmp_path) -> None:
    path = _write_registry(
        tmp_path,
        """
        default: model-a
        roles:
          planner: ghost-model
        models:
          - id: model-a
            label: Model A
        """,
    )
    registry = ModelRegistry.load(path=path)

    assert registry.model_for_role("planner") == "model-a"


def test_missing_file_uses_env_default(tmp_path) -> None:
    missing = tmp_path / "does_not_exist.yaml"
    registry = ModelRegistry.load(path=missing, env_default="env-model")

    assert registry.default_model == "env-model"
    assert registry.has("env-model")


def test_env_default_is_always_selectable(tmp_path) -> None:
    path = _write_registry(
        tmp_path,
        """
        default: model-a
        models:
          - id: model-a
            label: Model A
        """,
    )
    registry = ModelRegistry.load(path=path, env_default="custom-env-model")

    # The env model is injected so a configured AI_MODEL is never unselectable.
    assert registry.has("custom-env-model")
    assert registry.has("model-a")

