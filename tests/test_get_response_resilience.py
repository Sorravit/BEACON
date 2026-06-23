"""Resilience tests for AIAgent.get_response streaming path.

Guards the fix for the intermittent "chat returns nothing" bug: when the
streaming LLM call yields an empty/failed response, get_response must retry
once via the non-streaming call and, if that also fails, return a detailed
user-visible error instead of a silent blank.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from main import AIAgent


def _fake_response(text: str):
    choice = MagicMock()
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    return resp


class _FakeStream:
    """Awaitable-returned async iterator over the given content chunks."""

    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                choice = MagicMock()
                choice.delta.content = c
                chunk = MagicMock()
                chunk.choices = [choice]
                yield chunk
        return _gen()


def _make_agent(create_fn):
    """Build a minimal AIAgent wired to a fake OpenAI client."""
    agent = object.__new__(AIAgent)
    completions = MagicMock()
    completions.create = create_fn
    chat = MagicMock()
    chat.completions = completions
    client = MagicMock()
    client.chat = chat

    agent.client = client
    agent.config = SimpleNamespace(
        temperature=0.7,
        max_tokens=256,
        resolve_model=lambda requested=None, role=None: "test-model",
        models=SimpleNamespace(get=lambda m: None),
    )
    agent.conversation = [{"role": "system", "content": "You are a test assistant."}]
    agent.tools = None
    agent.tools_available = False
    agent.memory_available = False
    agent.vector_memory = None
    agent.skill_manager = None
    agent.memory_worker = None
    agent._last_dispatched_skill = ""
    agent._tools_prompt_cache = None
    agent._tools_prompt_key = None
    return agent


class GetResponseResilienceTest(unittest.TestCase):
    def test_empty_stream_recovers_via_nonstreaming_retry(self):
        """Empty stream → retry non-streaming → recovered text is returned."""

        async def create(**kwargs):
            if kwargs.get("stream"):
                return _FakeStream([])  # empty stream → triggers retry
            return _fake_response("recovered answer")

        agent = _make_agent(create)
        tokens = []
        result = asyncio.run(
            agent.get_response("hello there", token_callback=tokens.append)
        )
        self.assertEqual(result, "recovered answer")
        self.assertNotIn("No response from AI", result)
        # recovered text should still be streamed to the UI exactly once
        self.assertEqual("".join(tokens), "recovered answer")

    def test_both_empty_returns_detailed_error(self):
        """Empty stream AND empty retry → detailed (non-blank) error message."""

        async def create(**kwargs):
            if kwargs.get("stream"):
                return _FakeStream([])
            return _fake_response("")  # retry also empty

        agent = _make_agent(create)
        result = asyncio.run(
            agent.get_response("hello there", token_callback=lambda t: None)
        )
        self.assertIn("No response from AI", result)
        self.assertIn("empty", result)
        self.assertIn("test-model", result)

    def test_happy_path_streams_normally(self):
        """Non-empty stream is returned as-is with no retry."""
        calls = {"n": 0}

        async def create(**kwargs):
            calls["n"] += 1
            if kwargs.get("stream"):
                return _FakeStream(["Hel", "lo!"])
            raise AssertionError("non-streaming retry should not happen")

        agent = _make_agent(create)
        tokens = []
        result = asyncio.run(
            agent.get_response("hello there", token_callback=tokens.append)
        )
        self.assertEqual(result, "Hello!")
        self.assertEqual("".join(tokens), "Hello!")


if __name__ == "__main__":
    unittest.main()
