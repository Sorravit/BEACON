"""Tests for Step 6 — multi-model inference and dynamic model listing.

Covers:
* core.models.ModelRegistry.resolve_many()
* core.models.fetch_live_models_sync() (mocked HTTP)
* core.models._merge_live_into_static()
* core.models.ModelRegistry.load() with DYNAMIC_MODEL_LISTING_ENABLED
* core.multi_inference.run_multi_model_inference() (mocked OpenAI client)
* core.multi_inference._call_single_model() timeout and error paths
"""

from __future__ import annotations

import asyncio
import json
import textwrap
import time
import unittest
from io import BytesIO
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(yaml_body: str, tmp_path):
    """Write a models.yaml to tmp_path and load a ModelRegistry from it."""
    from core.models import ModelRegistry

    p = tmp_path / "models.yaml"
    p.write_text(textwrap.dedent(yaml_body), encoding="utf-8")
    return ModelRegistry.load(path=p)


def _fake_openai_response(text: str, prompt_tokens: int = 10, completion_tokens: int = 20):
    """Build a minimal fake openai ChatCompletion response object."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens

    choice = MagicMock()
    choice.message.content = text

    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


def _make_agent(responses: Dict[str, str]):
    """Build a mock AIAgent whose client returns model-specific responses."""

    def fake_create(**kwargs):
        model = kwargs.get("model", "")
        text = responses.get(model, f"[no mock for {model}]")
        return _fake_openai_response(text)

    completions = MagicMock()
    completions.create = fake_create

    chat = MagicMock()
    chat.completions = completions

    client = MagicMock()
    client.chat = chat

    config = MagicMock()
    config.temperature = 0.7
    config.max_tokens = 512

    agent = MagicMock()
    agent.client = client
    agent.config = config
    agent.conversation = [
        {"role": "system", "content": "You are a test assistant."}
    ]
    return agent


# ===========================================================================
# Tests: ModelRegistry.resolve_many
# ===========================================================================

class TestResolveManyMethod:
    """ModelRegistry.resolve_many() validates a list of model ids."""

    def test_all_valid_returns_same_list(self, tmp_path):
        registry = _make_registry(
            """
            default: model-a
            models:
              - id: model-a
                label: A
              - id: model-b
                label: B
            """,
            tmp_path,
        )
        assert registry.resolve_many(["model-a", "model-b"]) == ["model-a", "model-b"]

    def test_unknown_ids_are_dropped(self, tmp_path):
        registry = _make_registry(
            """
            default: model-a
            models:
              - id: model-a
                label: A
            """,
            tmp_path,
        )
        result = registry.resolve_many(["model-a", "does-not-exist"])
        assert result == ["model-a"]

    def test_all_unknown_falls_back_to_default(self, tmp_path):
        registry = _make_registry(
            """
            default: model-a
            models:
              - id: model-a
                label: A
            """,
            tmp_path,
        )
        result = registry.resolve_many(["nope", "also-nope"])
        assert result == ["model-a"]

    def test_deduplicates_preserving_order(self, tmp_path):
        registry = _make_registry(
            """
            default: model-a
            models:
              - id: model-a
                label: A
              - id: model-b
                label: B
              - id: model-c
                label: C
            """,
            tmp_path,
        )
        result = registry.resolve_many(["model-c", "model-a", "model-c", "model-b"])
        assert result == ["model-c", "model-a", "model-b"]

    def test_empty_input_falls_back_to_default(self, tmp_path):
        registry = _make_registry(
            """
            default: model-a
            models:
              - id: model-a
                label: A
            """,
            tmp_path,
        )
        assert registry.resolve_many([]) == ["model-a"]


# ===========================================================================
# Tests: ModelInfo.dynamic flag in to_dict
# ===========================================================================

class TestModelInfoDynamicFlag:
    def test_static_model_dynamic_false(self, tmp_path):
        registry = _make_registry(
            """
            default: model-a
            models:
              - id: model-a
                label: A
            """,
            tmp_path,
        )
        d = registry.get("model-a").to_dict()
        assert d["dynamic"] is False

    def test_to_public_list_includes_dynamic_key(self, tmp_path):
        registry = _make_registry(
            """
            default: model-a
            models:
              - id: model-a
                label: A
            """,
            tmp_path,
        )
        items = registry.to_public_list()
        assert all("dynamic" in m for m in items)


# ===========================================================================
# Tests: fetch_live_models_sync (mocked HTTP)
# ===========================================================================

class TestFetchLiveModelsSync:
    """Unit tests for the synchronous live listing function."""

    def _mock_urlopen(self, payload: Any):
        """Return a context manager that yields a fake HTTP response."""
        raw = json.dumps(payload).encode()
        cm = MagicMock()
        cm.__enter__ = lambda s: MagicMock(read=lambda: raw)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    def test_parses_openai_style_response(self):
        from core.models import fetch_live_models_sync
        import urllib.request

        payload = {
            "object": "list",
            "data": [
                {"id": "global/model-x", "object": "model"},
                {"id": "global/model-y", "object": "model"},
            ],
        }
        with patch.object(urllib.request, "urlopen", return_value=self._mock_urlopen(payload)):
            results = fetch_live_models_sync("https://fake.api", "key-abc")

        ids = [r.id for r in results]
        assert "global/model-x" in ids
        assert "global/model-y" in ids
        assert all(r.dynamic for r in results)

    def test_prefix_filter_applied(self):
        from core.models import fetch_live_models_sync
        import urllib.request

        payload = {
            "data": [
                {"id": "global/anthropic.claude-x"},
                {"id": "global/ibm/granite-y"},
                {"id": "local/private-z"},
            ]
        }
        with patch.object(urllib.request, "urlopen", return_value=self._mock_urlopen(payload)):
            results = fetch_live_models_sync(
                "https://fake.api", "key", prefix_filters=["global/"]
            )

        ids = [r.id for r in results]
        assert "global/anthropic.claude-x" in ids
        assert "global/ibm/granite-y" in ids
        assert "local/private-z" not in ids

    def test_http_error_returns_empty_list(self):
        from core.models import fetch_live_models_sync
        import urllib.error
        import urllib.request

        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.HTTPError(
                url="https://fake.api/models", code=401,
                msg="Unauthorized", hdrs=None, fp=None
            ),
        ):
            results = fetch_live_models_sync("https://fake.api", "bad-key")
        assert results == []

    def test_connection_error_returns_empty_list(self):
        from core.models import fetch_live_models_sync
        import urllib.error
        import urllib.request

        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            results = fetch_live_models_sync("https://fake.api", "key")
        assert results == []

    def test_label_generated_from_id_when_absent(self):
        from core.models import fetch_live_models_sync
        import urllib.request

        payload = {"data": [{"id": "global/anthropic.claude-sonnet-4-6"}]}
        with patch.object(urllib.request, "urlopen", return_value=self._mock_urlopen(payload)):
            results = fetch_live_models_sync("https://fake.api", "key")

        assert results
        # Should strip the "global/" prefix and vendor dot prefix
        assert "claude-sonnet-4-6" in results[0].label


# ===========================================================================
# Tests: _merge_live_into_static
# ===========================================================================

class TestMergeLiveIntoStatic:
    def test_static_entries_win(self):
        from core.models import ModelInfo, _merge_live_into_static

        static = [ModelInfo(id="m-a", label="Static Label", description="from yaml")]
        live = [ModelInfo(id="m-a", label="Live Label", description="from api", dynamic=True)]
        merged = _merge_live_into_static(static, live)

        assert len(merged) == 1
        assert merged[0].label == "Static Label"
        assert merged[0].dynamic is False

    def test_new_live_models_appended(self):
        from core.models import ModelInfo, _merge_live_into_static

        static = [ModelInfo(id="m-a", label="A")]
        live = [
            ModelInfo(id="m-a", label="A (live)", dynamic=True),
            ModelInfo(id="m-b", label="B (live)", dynamic=True),
        ]
        merged = _merge_live_into_static(static, live)

        assert len(merged) == 2
        ids = [m.id for m in merged]
        assert "m-a" in ids
        assert "m-b" in ids

    def test_empty_live_returns_static_unchanged(self):
        from core.models import ModelInfo, _merge_live_into_static

        static = [ModelInfo(id="m-a", label="A"), ModelInfo(id="m-b", label="B")]
        merged = _merge_live_into_static(static, [])
        assert [m.id for m in merged] == ["m-a", "m-b"]


# ===========================================================================
# Tests: ModelRegistry.load with DYNAMIC_MODEL_LISTING_ENABLED
# ===========================================================================

class TestRegistryLoadWithDynamicListing:
    def test_dynamic_disabled_by_default(self, tmp_path):
        """Without setting the env var, static list is used."""
        from core.models import ModelRegistry

        p = tmp_path / "models.yaml"
        p.write_text("default: m-a\nmodels:\n  - id: m-a\n    label: A\n")

        with patch.dict("os.environ", {}, clear=False):
            # Ensure disabled
            import os
            os.environ.pop("DYNAMIC_MODEL_LISTING_ENABLED", None)
            registry = ModelRegistry.load(path=p)

        assert registry.ids() == ["m-a"]

    def test_dynamic_enabled_merges_live_models(self, tmp_path):
        from core.models import ModelRegistry, fetch_live_models_sync

        p = tmp_path / "models.yaml"
        p.write_text("default: m-a\nmodels:\n  - id: m-a\n    label: A\n")

        live_result = [
            __import__("core.models", fromlist=["ModelInfo"]).ModelInfo(
                id="m-live", label="Live Model", dynamic=True
            )
        ]

        with patch.dict(
            "os.environ",
            {
                "DYNAMIC_MODEL_LISTING_ENABLED": "true",
                "OPENAI_BASE_URL": "https://fake.ica",
                "OPENAI_API_KEY": "test-key",
            },
        ):
            with patch(
                "core.models.fetch_live_models_sync", return_value=live_result
            ):
                registry = ModelRegistry.load(path=p)

        assert "m-a" in registry.ids()
        assert "m-live" in registry.ids()
        assert registry.get("m-live").dynamic is True

    def test_dynamic_enabled_but_no_credentials_skips_live(self, tmp_path):
        from core.models import ModelRegistry

        p = tmp_path / "models.yaml"
        p.write_text("default: m-a\nmodels:\n  - id: m-a\n    label: A\n")

        with patch.dict(
            "os.environ",
            {"DYNAMIC_MODEL_LISTING_ENABLED": "true"},
        ):
            import os
            os.environ.pop("OPENAI_BASE_URL", None)
            os.environ.pop("OPENAI_API_KEY", None)
            registry = ModelRegistry.load(path=p)

        # Falls back gracefully — only static model present
        assert registry.ids() == ["m-a"]


# ===========================================================================
# Tests: run_multi_model_inference
# ===========================================================================

class TestRunMultiModelInference:
    """Tests for the concurrent multi-model inference function."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_single_model_returns_one_result(self):
        from core.multi_inference import run_multi_model_inference

        agent = _make_agent({"model-a": "Hello from A"})
        results = self._run(
            run_multi_model_inference(
                ai_agent=agent,
                user_input="Hi",
                model_ids=["model-a"],
            )
        )
        assert len(results) == 1
        assert results[0].model_id == "model-a"
        assert results[0].text == "Hello from A"
        assert results[0].succeeded() is True

    def test_multiple_models_all_called(self):
        from core.multi_inference import run_multi_model_inference

        agent = _make_agent({"model-a": "Resp A", "model-b": "Resp B"})
        results = self._run(
            run_multi_model_inference(
                ai_agent=agent,
                user_input="Test",
                model_ids=["model-a", "model-b"],
            )
        )
        assert len(results) == 2
        model_ids = [r.model_id for r in results]
        assert "model-a" in model_ids
        assert "model-b" in model_ids
        assert all(r.succeeded() for r in results)

    def test_order_preserved(self):
        from core.multi_inference import run_multi_model_inference

        agent = _make_agent({"m1": "r1", "m2": "r2", "m3": "r3"})
        results = self._run(
            run_multi_model_inference(
                ai_agent=agent,
                user_input="order test",
                model_ids=["m1", "m2", "m3"],
            )
        )
        assert [r.model_id for r in results] == ["m1", "m2", "m3"]

    def test_failed_model_captured_not_raised(self):
        from core.multi_inference import run_multi_model_inference

        def raise_for_b(**kwargs):
            if kwargs.get("model") == "model-b":
                raise RuntimeError("model-b is down")
            return _fake_openai_response("ok from a")

        client = MagicMock()
        client.chat.completions.create = raise_for_b
        config = MagicMock()
        config.temperature = 0.7
        config.max_tokens = 512
        agent = MagicMock()
        agent.client = client
        agent.config = config
        agent.conversation = []

        results = self._run(
            run_multi_model_inference(
                ai_agent=agent,
                user_input="test",
                model_ids=["model-a", "model-b"],
            )
        )
        assert len(results) == 2
        a = next(r for r in results if r.model_id == "model-a")
        b = next(r for r in results if r.model_id == "model-b")
        assert a.succeeded() is True
        assert b.succeeded() is False
        assert "model-b is down" in b.error

    def test_empty_model_ids_returns_empty(self):
        from core.multi_inference import run_multi_model_inference

        agent = _make_agent({})
        results = self._run(
            run_multi_model_inference(
                ai_agent=agent,
                user_input="test",
                model_ids=[],
            )
        )
        assert results == []

    def test_deduplicates_model_ids(self):
        from core.multi_inference import run_multi_model_inference

        agent = _make_agent({"model-a": "resp A"})
        results = self._run(
            run_multi_model_inference(
                ai_agent=agent,
                user_input="test",
                model_ids=["model-a", "model-a", "model-a"],
            )
        )
        assert len(results) == 1
        assert results[0].model_id == "model-a"

    def test_system_prompt_override_applied(self):
        from core.multi_inference import run_multi_model_inference

        captured = {}

        def capture_create(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _fake_openai_response("ok")

        client = MagicMock()
        client.chat.completions.create = capture_create
        config = MagicMock()
        config.temperature = 0.7
        config.max_tokens = 512
        agent = MagicMock()
        agent.client = client
        agent.config = config
        agent.conversation = [{"role": "system", "content": "original system"}]

        self._run(
            run_multi_model_inference(
                ai_agent=agent,
                user_input="hello",
                model_ids=["model-x"],
                system_prompt="override system",
            )
        )
        assert captured["messages"][0]["role"] == "system"
        assert captured["messages"][0]["content"] == "override system"

    def test_conversation_not_mutated(self):
        from core.multi_inference import run_multi_model_inference

        agent = _make_agent({"m": "r"})
        original_conv = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "prev question"},
            {"role": "assistant", "content": "prev answer"},
        ]
        conv_copy = [dict(m) for m in original_conv]

        self._run(
            run_multi_model_inference(
                ai_agent=agent,
                user_input="new question",
                model_ids=["m"],
                conversation=original_conv,
            )
        )
        # Original conversation must be unchanged
        assert original_conv == conv_copy

    def test_elapsed_ms_populated(self):
        from core.multi_inference import run_multi_model_inference

        agent = _make_agent({"fast-model": "fast response"})
        results = self._run(
            run_multi_model_inference(
                ai_agent=agent,
                user_input="speed test",
                model_ids=["fast-model"],
            )
        )
        assert results[0].elapsed_ms >= 0

    def test_token_counts_populated(self):
        from core.multi_inference import run_multi_model_inference

        agent = _make_agent({"m": "response"})
        results = self._run(
            run_multi_model_inference(
                ai_agent=agent,
                user_input="tokens test",
                model_ids=["m"],
            )
        )
        assert results[0].prompt_tokens == 10
        assert results[0].completion_tokens == 20

    def test_to_dict_shape(self):
        from core.multi_inference import ModelResult

        r = ModelResult(
            model_id="global/test",
            text="hello",
            elapsed_ms=500,
            prompt_tokens=5,
            completion_tokens=10,
        )
        d = r.to_dict()
        assert d["model_id"] == "global/test"
        assert d["text"] == "hello"
        assert d["error"] is None
        assert d["succeeded"] is True
        assert d["elapsed_ms"] == 500


# ===========================================================================
# Run as script (fallback for environments without pytest)
# ===========================================================================

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__, "-v"]))