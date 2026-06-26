"""A single role-scoped sub-agent spawned by the orchestrator.

A :class:`SubAgent` binds an :class:`~core.orchestration.roles.AgentRole` to a
concrete model (resolved from the curated registry) and exposes two execution
modes:

* :meth:`act` -- full tool-using execution via the shared agent loop. Used by
  tool-enabled roles (researcher, engineer/specialist, verifier).
* :meth:`reason` / :meth:`reason_json` -- a direct, tool-free LLM call. Used by
  reasoning-only roles (planner) and for extracting structured verdicts.

The sub-agent never owns long-lived state; it is cheap to create per phase so
each phase can use a different model.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Dict, List, Optional

from core.orchestration.roles import AgentRole
from utils.encoding import safe_encode_string

# ---------------------------------------------------------------------------
# OpenTelemetry -- tracer initialised at module level so every SubAgent
# instance shares the same tracer and spans are automatically parented into
# the active trace.  The try/except guard keeps this module importable in
# environments where opentelemetry-sdk is not installed (e.g. unit tests).
# ---------------------------------------------------------------------------
try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import StatusCode as _StatusCode

    _tracer = _otel_trace.get_tracer(__name__)
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OTEL_AVAILABLE = False
    _tracer = None       # type: ignore[assignment]
    _StatusCode = None   # type: ignore[assignment]

logger = logging.getLogger(__name__)


def extract_json(text: str) -> Optional[Any]:
    """Best-effort extraction of a JSON object/array from a model response."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if "```" in cleaned[3:] else cleaned[3:]
        if cleaned.lstrip().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    for open_ch, close_ch in (("{" , "}"), ("[", "]")):
        start = cleaned.find(open_ch)
        end = cleaned.rfind(close_ch) + 1
        if 0 <= start < end:
            try:
                return json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                continue
    return None


class SubAgent:
    """Binds a role to a model and runs a single phase of work."""

    def __init__(
        self,
        ai_agent,
        role: AgentRole,
        model: Optional[str] = None,
        tools=None,
    ) -> None:
        self.ai_agent = ai_agent
        self.role = role
        self.tools = tools
        self.model = ai_agent.config.resolve_model(model, role=role.model_role)

    # -- tool-using execution -------------------------------------------------

    async def act(self, instruction: str, context: str = "") -> str:
        """Run a tool-enabled phase through the shared agent loop."""
        conv: List[Dict[str, str]] = [{"role": "system", "content": self.role.system_prompt}]
        user = instruction if not context else f"{instruction}\n\nContext:\n{context}"
        try:
            resp = await self.ai_agent.get_response(
                user,
                conversation=conv,
                tools=self.tools if self.role.uses_tools else None,
                model=self.model,
            )
            return safe_encode_string(resp or "")
        except Exception as exc:  # pragma: no cover
            logger.error("[%s] act failed: %s", self.role.key, exc)
            return f"[{self.role.title} error: {exc}]"

    # -- reasoning-only execution ---------------------------------------------

    async def reason(self, instruction: str, context: str = "", temperature: float = 0.3) -> str:
        """Direct, tool-free LLM call for pure reasoning phases."""
        messages = [
            {"role": "system", "content": self.role.system_prompt},
            {"role": "user", "content": instruction if not context else f"{instruction}\n\nContext:\n{context}"},
        ]
        return await self._llm(messages, temperature=temperature)

    async def reason_json(
        self, instruction: str, context: str = "", temperature: float = 0.2
    ) -> Optional[Any]:
        """Reason and parse a JSON result from the response."""
        raw = await self.reason(instruction, context=context, temperature=temperature)
        return extract_json(raw)

    async def _llm(self, messages: List[Dict[str, str]], temperature: float) -> str:
        """Call the LLM API and record an OpenTelemetry span for the call.

        Span name : ``llm_call``
        Attributes set **before** the API call:
          * ``llm.model``             -- model identifier string
        Attributes set **after** the API call (from the response object):
          * ``llm.prompt_tokens``     -- tokens consumed by the prompt
          * ``llm.completion_tokens`` -- tokens produced by the model
        On exception the span status is set to ERROR and the exception is
        recorded on the span before the error string is returned.
        """
        loop = asyncio.get_running_loop()  # retained for compatibility
        params = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self.ai_agent.config.max_tokens,
        }
        # Hard wall-clock timeout so a stalled response can never hang a phase
        # forever (these direct calls bypass get_response's own guard).
        import os as _os
        _t_raw = float(_os.getenv("LLM_REQUEST_TIMEOUT", "0"))
        _timeout = (_t_raw + 30.0) if _t_raw > 0 else None  # None = no limit

        async def _create():
            # AsyncOpenAI returns a coroutine; sync clients/mocks return the
            # response directly. Support both, and bound the awaitable path.
            _call = self.ai_agent.client.chat.completions.create(**params)
            if inspect.isawaitable(_call):
                return await (asyncio.wait_for(_call, timeout=_timeout) if _timeout else _call)
            return _call

        if _OTEL_AVAILABLE:
            with _tracer.start_as_current_span("llm_call") as span:
                # --- attributes BEFORE the call ---
                span.set_attribute("llm.model", self.model)
                try:
                    resp = await _create()
                    # --- attributes AFTER the call ---
                    if hasattr(resp, "usage") and resp.usage:
                        span.set_attribute("llm.prompt_tokens", resp.usage.prompt_tokens)
                        span.set_attribute("llm.completion_tokens", resp.usage.completion_tokens)
                    span.set_status(_StatusCode.OK)
                    return safe_encode_string((resp.choices[0].message.content or "").strip())
                except Exception as exc:  # pragma: no cover
                    span.set_status(_StatusCode.ERROR, str(exc))
                    span.record_exception(exc)
                    logger.error("[%s] llm call failed: %s", self.role.key, exc)
                    return f"[{self.role.title} error: {exc}]"
        else:
            # opentelemetry not installed -- pass through without instrumentation
            try:
                resp = await _create()
                return safe_encode_string((resp.choices[0].message.content or "").strip())
            except Exception as exc:  # pragma: no cover
                logger.error("[%s] llm call failed: %s", self.role.key, exc)
                return f"[{self.role.title} error: {exc}]"

    def describe(self) -> Dict[str, str]:
        return {"role": self.role.key, "title": self.role.title, "model": self.model}
