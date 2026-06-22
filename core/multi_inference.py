"""Multi-model inference for BEACON.

Provides :func:`run_multi_model_inference` which accepts a list of model IDs
and runs the same prompt through each model **concurrently** (via
``asyncio.gather``), returning all results so the caller can display or compare
them side-by-side.

Usage example
-------------
::

    from core.multi_inference import run_multi_model_inference

    results = await run_multi_model_inference(
        ai_agent=agent,
        user_input="Explain async/await in Python",
        model_ids=["global/anthropic.claude-sonnet-4-6", "global/gpt-5.1-chat"],
        conversation=conv,   # optional base conversation (each model gets a copy)
    )
    for r in results:
        print(r.model_id, r.elapsed_ms, r.text or r.error)

Design notes
------------
* Each model call gets its **own copy** of the conversation list so calls are
  completely independent and never mutate the caller's state.
* The pattern used throughout this codebase for LLM calls is
  ``loop.run_in_executor(None, lambda: client.chat.completions.create(...))``
  (synchronous OpenAI SDK offloaded to a thread-pool).  We keep exactly that
  pattern here so multi-inference is consistent with ``get_response`` in
  ``main.py`` and ``SubAgent._llm``.
* If a model call raises an exception the error is captured in
  :attr:`ModelResult.error`; other models are unaffected.
* A per-model ``timeout_secs`` guard (default 120 s) prevents a hung model
  from blocking the entire multi-model round.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECS = 120.0


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ModelResult:
    """The result of a single model inference call."""

    model_id: str
    """The model id that was invoked."""

    text: Optional[str] = None
    """The response text on success (``None`` on error)."""

    error: Optional[str] = None
    """Error message if the call failed (``None`` on success)."""

    elapsed_ms: int = 0
    """Wall-clock milliseconds for this model's call."""

    prompt_tokens: Optional[int] = None
    """Input token count (when reported by the API)."""

    completion_tokens: Optional[int] = None
    """Output token count (when reported by the API)."""

    def succeeded(self) -> bool:
        """Return True when the call produced a text response."""
        return self.error is None and self.text is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "text": self.text,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "succeeded": self.succeeded(),
        }


# ---------------------------------------------------------------------------
# Internal: single-model call
# ---------------------------------------------------------------------------

async def _call_single_model(
    *,
    client,                          # openai.OpenAI instance
    model_id: str,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout_secs: float,
) -> ModelResult:
    """Run one non-streaming LLM call and return a :class:`ModelResult`."""
    loop = asyncio.get_running_loop()
    start = time.monotonic()

    params = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        # Support both AsyncOpenAI (returns a coroutine) and a synchronous
        # client/mock (returns the response directly). This keeps multi-inference
        # working after the AsyncOpenAI migration without breaking sync test mocks.
        _call = client.chat.completions.create(**params)
        if inspect.isawaitable(_call):
            resp = await asyncio.wait_for(_call, timeout=timeout_secs)
        else:
            resp = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _call),
                timeout=timeout_secs,
            )
        elapsed = int((time.monotonic() - start) * 1000)
        text = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        return ModelResult(
            model_id=model_id,
            text=text,
            elapsed_ms=elapsed,
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
        )

    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - start) * 1000)
        logger.warning(
            "Multi-inference: model %s timed out after %dms", model_id, elapsed
        )
        return ModelResult(
            model_id=model_id,
            error=f"Timed out after {timeout_secs:.0f}s",
            elapsed_ms=elapsed,
        )

    except Exception as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        logger.error(
            "Multi-inference: model %s raised %s: %s", model_id, type(exc).__name__, exc
        )
        return ModelResult(
            model_id=model_id,
            error=str(exc),
            elapsed_ms=elapsed,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_multi_model_inference(
    *,
    ai_agent,
    user_input: str,
    model_ids: List[str],
    conversation: Optional[List[Dict[str, str]]] = None,
    system_prompt: Optional[str] = None,
    timeout_secs: float = _DEFAULT_TIMEOUT_SECS,
) -> List[ModelResult]:
    """Run ``user_input`` through every model in ``model_ids`` concurrently.

    All models share the **same** base conversation context — each gets an
    independent copy so they never interfere with each other.

    The results list preserves the original order of ``model_ids``.

    Note on tools
    ~~~~~~~~~~~~~
    Multi-model inference is intentionally **tool-free**.  Allowing each model
    to execute tool calls would cause different side-effects per model, making
    side-by-side comparison meaningless and potentially dangerous.  If you need
    tool-using multi-agent work, use the ``Orchestrator`` with
    ``model_overrides``.

    Args:
        ai_agent:      Initialised ``AIAgent`` (provides ``client`` and ``config``).
        user_input:    The user's message / prompt.
        model_ids:     List of model ids to call.  Should be pre-validated via
                       ``ModelRegistry.resolve_many()`` but is safe even if an
                       id is unknown (the call will error gracefully).
        conversation:  Optional base conversation list.  When ``None`` the
                       agent's own conversation is used as a starting point (a
                       copy is taken — the original is never mutated).
        system_prompt: Optional system message override.  When ``None`` the
                       existing system message in ``conversation`` is preserved.
        timeout_secs:  Per-model timeout in seconds (default 120 s).

    Returns:
        List of :class:`ModelResult` objects, one per model, in the same order
        as the (de-duplicated) ``model_ids`` list.
    """
    if not model_ids:
        logger.warning(
            "run_multi_model_inference called with empty model_ids — returning []"
        )
        return []

    client = ai_agent.client
    config = ai_agent.config

    # ── Build the base messages list ──────────────────────────────────────
    if conversation is not None:
        base_messages: List[Dict[str, str]] = [
            {"role": m["role"], "content": m.get("content") or ""}
            for m in conversation
            if m.get("role") in ("system", "user", "assistant")
        ]
    else:
        # Fall back to the agent's own shared conversation (read-only copy)
        agent_conv = getattr(ai_agent, "conversation", [])
        base_messages = [
            {"role": m["role"], "content": m.get("content") or ""}
            for m in agent_conv
            if m.get("role") in ("system", "user", "assistant")
        ]

    # Override / inject system prompt when requested
    if system_prompt is not None:
        if base_messages and base_messages[0]["role"] == "system":
            base_messages = [{"role": "system", "content": system_prompt}] + base_messages[1:]
        else:
            base_messages = [{"role": "system", "content": system_prompt}] + base_messages

    # Append the new user turn
    base_messages = base_messages + [{"role": "user", "content": user_input}]

    temperature = config.temperature
    max_tokens = config.max_tokens

    # Deduplicate while preserving order
    seen: set = set()
    unique_ids: List[str] = []
    for mid in model_ids:
        if mid and mid not in seen:
            unique_ids.append(mid)
            seen.add(mid)

    logger.info(
        "Multi-model inference: %d model(s) | prompt=%r",
        len(unique_ids),
        user_input[:80],
    )

    # Launch all model calls concurrently — each gets its own message list copy
    tasks = [
        _call_single_model(
            client=client,
            model_id=mid,
            messages=list(base_messages),  # shallow copy is fine (strings are immutable)
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_secs=timeout_secs,
        )
        for mid in unique_ids
    ]
    results: List[ModelResult] = list(await asyncio.gather(*tasks))

    # Log summary
    for r in results:
        status = "OK " if r.succeeded() else "ERR"
        logger.info(
            "  [%s] %-50s  %dms  in=%s out=%s  err=%s",
            status,
            r.model_id,
            r.elapsed_ms,
            r.prompt_tokens,
            r.completion_tokens,
            r.error or "-",
        )

    return results