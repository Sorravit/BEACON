"""Pydantic request models for the web API."""

from typing import Dict, Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    session_id: str
    model: Optional[str] = None


class RenameRequest(BaseModel):
    title: str


class ReorderRequest(BaseModel):
    order: list


class AgentTaskRequest(BaseModel):
    description: str
    session_id: str = None


class OrchestrateRequest(BaseModel):
    description: str
    session_id: Optional[str] = None
    max_rounds: int = 2
    # Optional per-role model overrides, e.g. {"researcher": "global/o3-mini"}.
    # The special key "all" overrides every role.
    model_overrides: Optional[Dict[str, str]] = None


class TaskAnswerRequest(BaseModel):
    answer: str
