#!/usr/bin/env python3
"""
core/agent/response_mixin.py — the streaming get_response engine.
"""

import asyncio
import json
import os
import re
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.agent.runtime import (
    logger,
    get_session_id,
    get_reporter,
    record_llm_call,
    setup_telemetry,
    install_print_bridge,
    _TELEMETRY_AVAILABLE,
    _get_encoder,
    _get_llm_sem,
    AsyncOpenAI,
    OpenAI,
    MCPManager,
    ModelRegistry,
    SkillManager,
    VectorMemory,
    ToolManager,
    VERSION,
    LOG_FILE,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_BASE_URL,
    MAX_TOOL_ITERATIONS,
    MAX_CONVERSATION_TOKENS,
    MAX_MEMORY_CONTEXT_CHARS,
    MCP_CONFIG_FILE,
)
from core.agent.stream_filter import _ToolMarkupStreamFilter


class ResponseMixin:
    """Mixin methods for AIAgent (see module docstring)."""

    async def get_response(self, user_input: str, conversation: Optional[List[Dict]] = None, tools: Optional["ToolManager"] = None, token_callback=None, model: Optional[str] = None) -> Optional[str]:
        """
        Get AI response with prompt-based tool execution loop (Cline/Roo style).
        
        Args:
            user_input: User's message
            conversation: Optional external conversation list (stateless/per-request mode).
                          If None, falls back to self.conversation (CLI/backward-compat mode).
            tools: Optional per-request ToolManager. If provided, used instead of self.tools so
                   concurrent requests never share or mutate the agent's tool reference.
            model: Optional model id override for this call. Resolved against the
                   curated registry; invalid ids fall back to the chat default.
            
        Returns:
            Optional[str]: AI response or None if error occurred
        """
        if not self.client:
            return None

        # Resolve the model for this call from the curated registry (never trust
        # a raw client value — an unknown id degrades to the chat default).
        effective_model = self.config.resolve_model(model, role="chat")

        # Resolve which ToolManager to use for this call — never mutate self.tools
        effective_tools = tools if tools is not None else self.tools

        # Helper: push a full string to the UI via token_callback in 24-char
        # batches. Used by every return path that must not produce a blank bubble
        # (warning fallback, iteration backstop, empty-after-strip recovery).
        def _stream_out(text: str) -> None:
            if not token_callback or not text:
                return
            _BATCH = 24
            for _i in range(0, len(text), _BATCH):
                try:
                    token_callback(text[_i:_i + _BATCH])
                except Exception:
                    pass

        # Use passed-in conversation or fall back to self.conversation (backward compat)
        if conversation is not None:
            conv = conversation  # stateless mode: caller owns this list
        else:
            conv = self.conversation  # CLI mode: mutate self.conversation as before

        # Build tools prompt once and store it for use in the first iteration only.
        # We do NOT permanently inject it into conv[0] because:
        #   1. conv[0] is a shared reference to agent.conversation[0] — mutating it
        #      would corrupt the shared system message across sessions.
        #   2. Including the full 80k-char tools prompt in EVERY iteration (including
        #      the tool-results feedback round-trip) bloats the payload and causes
        #      IBM ICA (and other APIs with tight context limits) to return 400.
        # Instead, we pass a _first-iteration-only_ system message with the tools
        # description, and strip it back to the base system content for subsequent
        # iterations.
        _base_system_content = conv[0]["content"] if conv and conv[0]["role"] == "system" else ""
        # MEMORY-FIRST: prepend local path rules so BEACON checks local files before GitLab
        _local_rules = (
            "\nCRITICAL - MEMORY-FIRST LOCAL PATH RULES (before every tool call):\n"
            "1. For KRS/project code READ LOCAL FILES FIRST. Never open GitLab unless asked.\n"
            "   krs-service    : /Users/sorravit/IdeaProjects/KRS/krs-service\n"
            "   krs-sftp-batch : /Users/sorravit/IdeaProjects/KRS/krs-sftp-batch\n"
            "   BEACON         : /Users/sorravit/sandbox/beacon\n"
            "2. Use execute_command or read_file with the local path above.\n"
            "3. Only open URL/browser when explicitly asked OR local path missing.\n"
        )
        _base_system_content = _base_system_content + _local_rules

        _tools_prompt = self._build_tools_prompt() if self.tools_available else ""

        try:
            # (tools prompt is applied per-iteration below, not permanently here)
            pass

            # SKILL DISPATCH: route to specialist agent before LLM loop
            self._last_dispatched_skill = ""  # reset before each request
            _skill_out = await self._maybe_dispatch_skill(user_input)
            if _skill_out is not None:
                if token_callback:
                    # Phase 1 / #4: batch emit (24-char chunks)
                    _BATCH = 24
                    for _i in range(0, len(_skill_out), _BATCH):
                        try:
                            token_callback(_skill_out[_i:_i + _BATCH])
                        except Exception:
                            pass
                conv.append({"role": "assistant", "content": _skill_out})
                # Phase 2 / auto-memory: also learn from skill dispatches
                if self.memory_worker is None:
                    logger.warning(
                        "[AutoMemory] memory_worker is None — skipping"
                        " _auto_memory_extract for this turn (skill-dispatch path)"
                    )
                elif self.memory_available:
                    self.memory_worker.submit(
                        user_input, _skill_out, get_session_id() or ""
                    )
                return _skill_out
            # END SKILL DISPATCH

            # --- Vector memory: detect and store personal facts from user input ---
            if self.memory_available and self.vector_memory:
                await self._extract_and_store_facts(user_input)

            # --- Vector memory: inject relevant context as prefix (capped size) ---
            memory_prefix = ""
            if self.memory_available and self.vector_memory:
                raw_prefix = await self.vector_memory.build_context_prompt(user_input)
                # Cap memory context to avoid token overflow
                if raw_prefix and len(raw_prefix) > MAX_MEMORY_CONTEXT_CHARS:
                    raw_prefix = raw_prefix[:MAX_MEMORY_CONTEXT_CHARS] + "\n...[memory truncated]\n\n"
                memory_prefix = raw_prefix

            user_message = (memory_prefix + user_input) if memory_prefix else user_input
            conv.append({"role": "user", "content": user_message})

            # Trim AFTER adding the new message so trimming sees the full picture.
            # tiktoken encoding is CPU-bound; run it in a worker thread so the
            # single asyncio event loop stays responsive and concurrent streams
            # are not starved (root cause of intermittent "returns nothing").
            await asyncio.to_thread(self._trim_conversation, conv)

            # Agent loop - allow multiple tool calls
            # For long-running tasks (courses, etc.), use a very high limit
            # Configured via MAX_TOOL_ITERATIONS constant at top of file
            for iteration in range(MAX_TOOL_ITERATIONS):
                # On the first iteration: inject tools prompt into system message so
                # the model knows the tool format.  On subsequent iterations (tool
                # results feedback): use the base system content only to keep the
                # payload small and avoid 400 errors from context-size limits.
                if conv and conv[0]["role"] == "system":
                    if iteration == 0 and _tools_prompt:
                        conv[0] = {"role": "system", "content": _base_system_content + _tools_prompt}
                    else:
                        conv[0] = {"role": "system", "content": _base_system_content}

                # Reset per-iteration live-streaming flag (each LLM call is independent).
                _live_streamed = False

                params = {
                    "model": effective_model,
                    "messages": conv,
                    "temperature": self.config.temperature,
                    "max_tokens": self.config.max_tokens
                }

                # NO tools parameter - use prompt-based approach instead

                # One OTel span per LLM request
                llm_span_cm = (
                    record_llm_call(
                        effective_model,
                        session_id=get_session_id(),
                        temperature=self.config.temperature,
                        max_tokens=self.config.max_tokens,
                        message_count=len(conv),
                        streaming=bool(token_callback),
                    )
                    if _TELEMETRY_AVAILABLE
                    else nullcontext()
                )

                # Streaming uses an INACTIVITY (idle) timeout rather than a total
                # wall-clock cap: a response may take arbitrarily long as long as
                # the server keeps sending chunks. The timer resets on every chunk
                # received (including empty/keepalive/reasoning chunks), so only a
                # genuine stall (no data for LLM_STREAM_IDLE_TIMEOUT seconds) aborts.
                # Set LLM_STREAM_IDLE_TIMEOUT=0 to disable the idle guard entirely
                # (default). A positive value (e.g. 180) caps the inter-chunk silence.
                # Non-streaming calls can't measure inactivity (single blocking
                # call) so they use a generous total wall-clock guard instead;
                # set LLM_REQUEST_TIMEOUT=0 to disable that guard entirely.
                _idle_raw = float(os.getenv("LLM_STREAM_IDLE_TIMEOUT", "0"))
                _idle_timeout = _idle_raw if _idle_raw > 0 else None  # None = no limit
                _total_timeout = float(os.getenv("LLM_REQUEST_TIMEOUT", "0"))
                _nonstream_timeout = _total_timeout if _total_timeout > 0 else None

                # One non-streaming completion. Returns (text, reason) where
                # reason is "" on success, else "timeout" / "empty" / "error: …".
                async def _nonstream_once(span=None):
                    try:
                        _coro = self.client.chat.completions.create(**params)
                        if _nonstream_timeout is not None:
                            _resp = await asyncio.wait_for(_coro, timeout=_nonstream_timeout)
                        else:
                            _resp = await _coro
                    except asyncio.TimeoutError:
                        return "", "timeout"
                    except Exception as _exc:
                        logger.warning(
                            "LLM non-streaming call failed: %s: %s",
                            type(_exc).__name__, _exc,
                        )
                        return "", f"error: {type(_exc).__name__}: {_exc}"
                    _txt = _resp.choices[0].message.content or ""
                    _usage = getattr(_resp, "usage", None)
                    if span is not None and _usage:
                        try:
                            span.set_attribute("llm.input_tokens", _usage.prompt_tokens or 0)
                            span.set_attribute("llm.output_tokens", _usage.completion_tokens or 0)
                        except Exception:
                            pass
                    return _txt, ("" if _txt.strip() else "empty")

                response_text = ""
                fail_reason = ""
                _live_streamed = False  # set True when delta tokens reach token_callback live
                _t_start = time.monotonic()

                async with _get_llm_sem():
                    if token_callback:
                        # STREAMING MODE — native async streaming, zero worker threads.
                        # Tokens are emitted to the UI live inside the chunk loop so
                        # the user sees incremental output instead of a frozen spinner.
                        # A tool-markup filter suppresses <tool_use>/<function_calls>/
                        # <invoke> blocks so raw tool XML never reaches the chat.
                        stream_params = {**params, "stream": True}
                        with llm_span_cm as llm_span:
                            async def _consume_stream():
                                nonlocal _live_streamed
                                _text = ""
                                _filter = _ToolMarkupStreamFilter()

                                def _emit(piece: str):
                                    nonlocal _live_streamed
                                    if not piece:
                                        return
                                    try:
                                        token_callback(piece)
                                        _live_streamed = True
                                    except Exception:
                                        pass

                                stream = await asyncio.wait_for(
                                    self.client.chat.completions.create(**stream_params),
                                    timeout=_idle_timeout,  # time-to-first-token guard
                                )
                                # Manual iteration so each chunk fetch has its own
                                # inactivity timeout. The timer resets on every chunk
                                # received (regardless of whether it carries visible
                                # content), so long-but-active streams never time out
                                # while a true stall is still bounded.
                                _aiter = stream.__aiter__()
                                while True:
                                    try:
                                        chunk = await asyncio.wait_for(
                                            _aiter.__anext__(), timeout=_idle_timeout
                                        )
                                    except StopAsyncIteration:
                                        break
                                    delta = (
                                        chunk.choices[0].delta.content
                                        if chunk.choices else None
                                    )
                                    if delta:
                                        _text += delta
                                        # Live-stream the filtered (tool-markup-free) text.
                                        _emit(_filter.feed(delta))
                                # Flush any safe text held back by the filter.
                                _emit(_filter.flush())
                                return _text

                            try:
                                response_text = await _consume_stream()
                                if not response_text.strip():
                                    fail_reason = "empty"
                            except asyncio.TimeoutError:
                                logger.warning(
                                    "LLM streaming call stalled — no data for %.0fs",
                                    _idle_timeout or 0,
                                )
                                response_text, fail_reason = "", "timeout"
                            except Exception as _sexc:
                                logger.warning(
                                    "LLM streaming call failed: %s: %s",
                                    type(_sexc).__name__, _sexc,
                                )
                                response_text = ""
                                fail_reason = f"error: {type(_sexc).__name__}: {_sexc}"

                            # Resilience: a streaming stall/empty/error is the #1
                            # cause of "returns nothing". Retry ONCE via the more
                            # robust non-streaming call before giving up.
                            if fail_reason:
                                logger.warning(
                                    "Streaming response failed (%s) after %.1fs — "
                                    "retrying once non-streaming (model=%s)",
                                    fail_reason, time.monotonic() - _t_start, effective_model,
                                )
                                _retry_text, _retry_reason = await _nonstream_once(llm_span)
                                if _retry_text.strip():
                                    # Recovered. The final-response branch below
                                    # emits tokens to the UI, so don't stream here.
                                    response_text, fail_reason = _retry_text, ""
                                else:
                                    fail_reason = _retry_reason or fail_reason

                            if llm_span is not None:
                                llm_span.set_attribute("llm.response_chars", len(response_text))
                                llm_span.set_attribute(
                                    "llm.output_tokens", self._estimate_tokens(response_text)
                                )
                    else:
                        # NON-STREAMING MODE: CLI / planning / background tasks.
                        with llm_span_cm as llm_span:
                            response_text, fail_reason = await _nonstream_once(llm_span)

                if not response_text or fail_reason:
                    _elapsed = time.monotonic() - _t_start
                    if fail_reason == "timeout":
                        if token_callback:
                            _detail = (
                                f"the response stalled — no output for {_idle_timeout:.0f}s"
                                if _idle_timeout
                                else "the response stalled with no output"
                            )
                        else:
                            _detail = f"the request timed out after {_nonstream_timeout:.0f}s"
                    elif fail_reason == "empty" or not fail_reason:
                        _detail = "the model returned an empty response"
                    elif fail_reason.startswith("error:"):
                        _detail = f"an upstream error occurred ({fail_reason[7:].strip()})"
                    else:
                        _detail = "no content was returned"
                    logger.warning(
                        "[get_response] No usable response: reason=%s model=%s elapsed=%.1fs",
                        fail_reason or "empty", effective_model, _elapsed,
                    )
                    _warn = (
                        f"⚠️ No response from AI — {_detail} "
                        f"(model: {effective_model}, {_elapsed:.0f}s). Please try again."
                    )
                    _stream_out(_warn)
                    return _warn

                # Parse tool calls from response
                tool_calls = self._parse_tool_calls(response_text)

                if tool_calls:
                    # Add assistant's response with tool calls
                    conv.append({
                        "role": "assistant",
                        "content": response_text
                    })

                    # Execute all tool calls
                    tool_results = []
                    for tool_call in tool_calls:
                        tool_name = tool_call["tool_name"]
                        tool_args = tool_call["parameters"]

                        print(f"\033[92m  🔧 Executing: {tool_name}({', '.join(f'{k}={str(v)}' for k, v in tool_args.items()) if tool_args else ''})\033[0m")

                        # Route ALL tool calls (built-in and MCP) through effective_tools.execute_tool()
                        # so web_app.py's instrumented wrapper emits SSE events for every call,
                        # including MCP tools. execute_tool() now falls through to mcp_manager for
                        # tools not in tool_handlers.
                        if effective_tools:
                            tool_result = await effective_tools.execute_tool(tool_name, tool_args)
                        else:
                            tool_result = json.dumps({"error": "No tool manager available"})

                        # Print tool result in cyan
                        result_preview = str(tool_result)
                        if len(result_preview) > 500:
                            result_preview = result_preview[:500] + "... [truncated]"
                        print(f"\033[96m  📤 Result: {result_preview}\033[0m")

                        # --- Store tool result in vector memory ---
                        if self.memory_available and self.vector_memory:
                            query_hint = str(list(tool_args.values())[0]) if tool_args else tool_name
                            await self.vector_memory.store_research(
                                tool_name=tool_name,
                                query=query_hint[:200],
                                result=str(tool_result)
                            )

                        tool_results.append(f"Tool: {tool_name}\nResult: {tool_result}")

                    # Notify instead of silently truncate — tell the AI the result is large
                    MAX_RESULT_CHARS = 80000  # ~26k tokens — warn if larger but don't silently cut off
                    notified_results = []
                    for tr in tool_results:
                        if len(tr) > MAX_RESULT_CHARS:
                            # Don't silently truncate — tell the AI the result is large so it can navigate it
                            preview = tr[:3000]
                            size_kb = len(tr) // 1024
                            notified_results.append(
                                f"[RESULT TOO LARGE: {size_kb}KB — only first 3KB shown below. "
                                f"Use execute_command with grep/head/tail/sed to search/navigate this content "
                                f"rather than reading the whole thing at once.]\n\n{preview}\n\n[...{size_kb - 3}KB more not shown]"
                            )
                        else:
                            notified_results.append(tr)
                    results_text = "\n\n".join(notified_results)
                    conv.append({
                        "role": "user",
                        "content": f"Tool execution results:\n\n{results_text}\n\nPlease provide your final response based on these results."
                    })

                    # Continue loop to let AI process tool results
                    continue
                else:
                    # No tool calls — final response.
                    # Compute the clean (tool-markup-free) text once; it is the
                    # canonical value we stream, save, learn from, and return so
                    # raw tool XML never reaches the chat, history, or memory.
                    clean = re.sub(
                        r'<tool[_-]use>.*?</(?:tool[_-](?:use|invoke|call)|use)>',
                        '', response_text, flags=re.DOTALL,
                    ).strip()
                    # Empty-after-strip: the model emitted only a tool block that
                    # _parse_tool_calls could not parse. Fall back to raw text so
                    # the user sees something instead of "no response".
                    if not clean:
                        clean = response_text.strip() or (
                            "⚠️ The model returned only a malformed tool call "
                            "and no readable text. Please try again."
                        )

                    # If we were in streaming mode and tokens were already emitted
                    # live (delta-by-delta, already filtered), do NOT re-emit them.
                    # Otherwise (non-streaming retry recovery) batch-emit here.
                    if token_callback and not _live_streamed:
                        _BATCH = 24
                        for _i in range(0, len(clean), _BATCH):
                            try:
                                token_callback(clean[_i:_i + _BATCH])
                            except Exception:
                                pass

                    final_text = clean
                    conv.append({"role": "assistant", "content": final_text})
                    # Phase 2 / auto-memory: submit to durable worker queue
                    if self.memory_worker is None:
                        logger.warning(
                            "[AutoMemory] memory_worker is None — skipping"
                            " _auto_memory_extract for this turn (response path)"
                        )
                    elif self.memory_available:
                        self.memory_worker.submit(
                            user_input, final_text, get_session_id() or ""
                        )
                    return final_text

            # If we hit max iterations, inform user but don't fail
            logger.warning(f"Reached safety backstop of {MAX_TOOL_ITERATIONS} iterations — this should never happen in normal use.")
            _backstop = f"I've reached the iteration safety backstop ({MAX_TOOL_ITERATIONS} calls). This should never happen in normal use — please report this."
            _stream_out(_backstop)
            return _backstop

        except Exception as e:
            logger.error(f"Error: {e}")
            import traceback
            traceback.print_exc()
            if conv and conv[-1]["role"] == "user":
                conv.pop()
            raise  # Re-raise so web_app.py can surface it to the frontend

