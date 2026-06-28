#!/usr/bin/env python3
"""
core/agent/skills_mixin.py — keyword skill dispatch.
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


class SkillsMixin:
    """Mixin methods for AIAgent (see module docstring)."""

    _SKILL_TRIGGERS = {
        "business_analyst": ["act as ba", "act as business analyst", "write user stories", "write brd"],
        "lead_qa": ["act as lead qa", "qa strategy", "test strategy", "test plan"],
        "senior_qa": ["act as senior qa", "write test cases", "test case design"],
        "automated_qa_cypress": ["cypress test", "write cypress", "cypress spec"],
        "automated_qa_robot": ["robot framework", "write robot", "robot test"],
        "senior_java_engineer": ["act as java engineer", "write spring boot", "java service"],
        "senior_python_engineer": ["act as python engineer", "write fastapi", "fastapi service"],
        "senior_javascript_engineer": ["act as javascript engineer", "typescript service"],
        "frontend_engineer": ["act as frontend engineer", "write react component"],
        "backend_engineer": ["act as backend engineer", "write api spec", "openapi spec"],
        "devops_engineer": ["act as devops", "write gitlab ci", "kubernetes yaml", "write dockerfile"],
        "security_engineer": ["act as security engineer", "threat model", "security review"],
        "solution_architect": ["act as architect", "solution architect", "write adr"],
        "researcher": ["act as researcher", "do research on", "research report on"],
        "reviewer": ["act as reviewer", "review this code", "code review"],
        "financial_analyst": ["act as financial analyst", "npv analysis", "roi analysis"],
        "stock_market_analyst": ["act as stock analyst", "stock analysis", "analyse stock"],
    }

    async def _maybe_dispatch_skill(self, user_input: str):
        lower = user_input.lower()
        matched = None
        for skill_id, triggers in self._SKILL_TRIGGERS.items():
            if any(t in lower for t in triggers):
                matched = skill_id
                break
        if not matched:
            return None
        try:
            import importlib.util as ilu
            base = os.path.dirname(os.path.abspath(__file__))
            agent_path = os.path.join(base, "skills", matched, "agent.py")
            if not os.path.exists(agent_path):
                return None
            spec = ilu.spec_from_file_location("skills." + matched, agent_path)
            mod = ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cls = None
            for name in dir(mod):
                obj = getattr(mod, name)
                if isinstance(obj, type) and hasattr(obj, "execute") and name not in ("BaseSkillAgent", "object"):
                    cls = obj
                    break
            if not cls:
                return None
            inst = cls()
            result = inst.execute({"user_request": user_input, "context": ""})
            header = "\n\U0001f916 **[" + matched.replace("_", " ").title() + " Agent]**\n\n"
            # ── SkillResult unwrap ladder ────────────────────────────────────────
            # BEACON skill agents return SkillResult where .data is a dict with
            # keys: skill_id, display_name, persona, reasoning_steps,
            # inputs_received, outputs (nested dict of actual results),
            # ready_for_llm.  The old code checked .data["output"|"result"|
            # "content"] which do not exist, so every branch fell through to
            # str(result) → "<SkillResult object at 0x…>" in the chat.
            # Fix: try .output/.content direct attrs first, then .data["outputs"]
            # (the real nested outputs dict used by all BEACON skill agents),
            # then a structured summary, then str() as absolute last resort.
            # logger.debug records which branch resolved for easy diagnosis.
            _result_type = type(result).__name__
            if isinstance(result, dict):
                out = (result.get("output") or result.get("result")
                       or result.get("content") or str(result))
                logger.debug("[SkillDispatch] result type=dict keys=%s", list(result.keys()))
            elif hasattr(result, "output") and result.output:
                out = str(result.output)
                logger.debug("[SkillDispatch] result type=%s resolved via .output attr", _result_type)
            elif hasattr(result, "content") and result.content:
                out = str(result.content)
                logger.debug("[SkillDispatch] result type=%s resolved via .content attr", _result_type)
            elif hasattr(result, "data") and isinstance(result.data, dict):
                _d = result.data
                # 1) direct key check (for any agent that uses output/result/content)
                _direct = (_d.get("output") or _d.get("result") or _d.get("content"))
                if _direct:
                    out = str(_direct)
                    logger.debug("[SkillDispatch] result type=%s resolved via .data direct key", _result_type)
                else:
                    # 2) BEACON agents store real work product in data["outputs"] (nested dict)
                    #    e.g. {"review_report": "…", "score": "…", "verdict": "…", "improvements": "…"}
                    _nested = _d.get("outputs")
                    if isinstance(_nested, dict) and _nested:
                        _parts = []
                        for _k, _v in _nested.items():
                            _vs = str(_v).strip()
                            _parts.append(f"**{_k.replace('_', ' ').title()}**\n{_vs}")
                        out = "\n\n".join(_parts) if _parts else str(_d)
                        logger.debug(
                            "[SkillDispatch] result type=%s resolved via .data['outputs'] keys=%s",
                            _result_type, list(_nested.keys()))
                    elif _d.get("persona") or _d.get("reasoning_steps"):
                        # 3) Render a structured summary from known SkillResult data fields
                        _parts = []
                        _persona = _d.get("persona", "")
                        _steps = _d.get("reasoning_steps", [])
                        if _persona:
                            _parts.append(f"**Role:** {_persona}")
                        if _steps:
                            _parts.append("**Execution Steps:**\n" + "\n".join(_steps))
                        _nested2 = _d.get("outputs", {})
                        if isinstance(_nested2, dict):
                            for _k, _v in _nested2.items():
                                _parts.append(f"**{_k.replace('_', ' ').title()}:** {_v}")
                        out = "\n\n".join(_parts) if _parts else str(_d)
                        logger.debug("[SkillDispatch] result type=%s resolved via .data structured summary", _result_type)
                    else:
                        out = str(_d)
                        logger.debug("[SkillDispatch] result type=%s fell back to str(.data)", _result_type)
            elif hasattr(result, "to_dict"):
                _td = result.to_dict()
                _direct2 = (_td.get("output") or _td.get("result") or _td.get("content"))
                if _direct2:
                    out = str(_direct2)
                else:
                    _inner = _td.get("data", {})
                    if isinstance(_inner, dict):
                        _nested3 = _inner.get("outputs")
                        if isinstance(_nested3, dict) and _nested3:
                            _parts3 = [f"**{_k.replace('_', ' ').title()}**\n{_v}"
                                       for _k, _v in _nested3.items()]
                            out = "\n\n".join(_parts3) if _parts3 else str(_inner)
                        else:
                            out = str(_inner) if _inner else str(_td)
                    else:
                        out = str(_td)
                logger.debug("[SkillDispatch] result type=%s resolved via .to_dict()", _result_type)
            else:
                out = str(result)
                logger.debug("[SkillDispatch] result type=%s final str() fallback", _result_type)
            logger.info("[SkillDispatch] skill=%s resolved_type=%s content_len=%d",
                        matched, _result_type, len(out))
            self._last_dispatched_skill = matched  # signal for web_app skill indicator
            return header + out
        except Exception as e:
            logger.warning("Skill dispatch failed: " + str(e))
            return None

