#!/usr/bin/env python3
"""
core/agent/tooling_mixin.py — tools prompt construction and tool-call parsing.
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


class ToolingMixin:
    """Mixin methods for AIAgent (see module docstring)."""

    def _build_tools_prompt(self) -> str:
        """Build system prompt with tool descriptions.

        The result (~80k chars) is cached and only rebuilt when the available
        tool/MCP set changes. Rebuilding this string on every request added
        avoidable CPU on the single event loop and contributed to concurrent
        requests stalling.
        """
        # Cheap signature of the current toolset — rebuild only when it changes.
        builtin_names = (
            tuple(t['function']['name'] for t in self.tools.tools)
            if (self.tools_available and self.tools) else ()
        )
        mcp_names = (
            tuple(t['function']['name'] for t in self.mcp_manager.get_all_tools_for_openai())
            if self.mcp_manager else ()
        )
        cache_key = (builtin_names, mcp_names)
        if self._tools_prompt_cache is not None and self._tools_prompt_key == cache_key:
            return self._tools_prompt_cache

        prompt = "\n\nYou have access to the following tools:\n\n"

        # Add built-in tools
        if self.tools_available and self.tools:
            for tool in self.tools.tools:
                func = tool['function']
                prompt += f"<tool name=\"{func['name']}\">\n"
                prompt += f"Description: {func['description']}\n"
                if func.get('parameters', {}).get('properties'):
                    prompt += "Parameters:\n"
                    for param_name, param_info in func['parameters']['properties'].items():
                        param_type = param_info.get('type', 'string')
                        param_desc = param_info.get('description', '')
                        required = param_name in func['parameters'].get('required', [])
                        req_str = " (required)" if required else " (optional)"
                        prompt += f"  - {param_name} ({param_type}){req_str}: {param_desc}\n"
                else:
                    prompt += "Parameters: none\n"
                prompt += "</tool>\n\n"

        # Add MCP tools
        if self.mcp_manager:
            for tool in self.mcp_manager.get_all_tools_for_openai():
                func = tool['function']
                prompt += f"<tool name=\"{func['name']}\">\n"
                prompt += f"Description: {func['description']}\n"
                if func.get('parameters', {}).get('properties'):
                    prompt += "Parameters:\n"
                    for param_name, param_info in func['parameters']['properties'].items():
                        param_type = param_info.get('type', 'string')
                        param_desc = param_info.get('description', '')
                        required = param_name in func['parameters'].get('required', [])
                        req_str = " (required)" if required else " (optional)"
                        prompt += f"  - {param_name} ({param_type}){req_str}: {param_desc}\n"
                else:
                    prompt += "Parameters: none\n"
                prompt += "</tool>\n\n"

        prompt += """To use a tool, respond with:
<tool_use>
<tool_name>tool_name_here</tool_name>
<parameters>
{
  "param1": "value1",
  "param2": "value2"
}
</parameters>
</tool_use>

You can use multiple tools in sequence. After I execute each tool, I'll provide the result and you can continue or use another tool.
IMPORTANT: When you use a tool, ONLY output the <tool_use> block, nothing else. After I give you the result, then provide your final answer."""

        self._tools_prompt_cache = prompt
        self._tools_prompt_key = cache_key
        return prompt


    def _parse_tool_calls(self, response_text: str) -> List[Dict[str, Any]]:
        """Parse tool calls from Claude's XML response"""
        import re

        tool_calls = []

        # ── Format 1: <tool_use>...<tool_name>NAME</tool_name>...</tool_use> ──
        # Also tolerates mismatched closing tags: </tool_invoke>, </tool_call>.
        # Separator-tolerant: open/close may use '_' or '-' for the same word
        # (e.g. opens <tool_use> but closes </tool-use>).
        pattern = r'<tool[_-]use>(.*?)</(?:tool[_-](?:use|invoke|call)|use)>'
        matches = re.findall(pattern, response_text, re.DOTALL)

        # ── Format 2: <function_calls><invoke name="NAME">...</invoke></function_calls> ──
        # Claude sometimes uses this anthropic-native format for MCP tools.
        invoke_pattern = r'<invoke\s+name=["\']([^"\']+)["\']>(.*?)</invoke>'
        for inv_name, inv_body in re.findall(invoke_pattern, response_text, re.DOTALL):
            # Convert <parameter name="key">value</parameter> → JSON dict
            params = {}
            for p_name, p_val in re.findall(r'<parameter\s+name=["\']([^"\']+)["\']>(.*?)</parameter>', inv_body, re.DOTALL):
                params[p_name] = p_val.strip()
            tool_calls.append({"tool_name": inv_name.strip(), "parameters": params})
            logger.warning(f"Parsed function_calls/invoke format for tool: {inv_name.strip()}")

        for match in matches:
            try:
                # Parse tool name
                tool_name_match = re.search(r'<tool[_-]name>(.*?)</tool[_-]name>', match)
                if not tool_name_match:
                    continue
                tool_name = tool_name_match.group(1).strip()

                # Parse parameters
                params_match = re.search(r'<parameters>(.*?)</parameters>', match, re.DOTALL)
                if params_match:
                    params_text = params_match.group(1).strip()
                    if params_text:
                        try:
                            parameters = json.loads(params_text)
                        except json.JSONDecodeError:
                            # Try 1: repair truncated JSON (max_tokens cut off closing brace)
                            open_count = params_text.count('{')
                            close_count = params_text.count('}')
                            if open_count > close_count:
                                repaired = params_text.rstrip() + '}' * (open_count - close_count)
                                try:
                                    parameters = json.loads(repaired)
                                    logger.warning(f"Repaired truncated parameters JSON")
                                    # Successfully repaired — skip XML fallback
                                except json.JSONDecodeError:
                                    parameters = None
                            else:
                                parameters = None

                            # Try 2: model used XML-style params <key>value</key> instead of JSON
                            if parameters is None:
                                xml_params = re.findall(r'<(\w+)>(.*?)</\1>', params_text, re.DOTALL)
                                if xml_params:
                                    parameters = {k: v.strip() for k, v in xml_params}
                                    logger.warning(f"Parsed XML-style parameters: {list(parameters.keys())}")
                                else:
                                    logger.error(f"Failed to parse parameters: {params_text[:200]}")
                                    parameters = {}
                    else:
                        parameters = {}
                else:
                    parameters = {}

                tool_calls.append({
                    "tool_name": tool_name,
                    "parameters": parameters
                })
            except Exception as e:
                logger.error(f"Error parsing tool call: {e}")
                continue

        return tool_calls

