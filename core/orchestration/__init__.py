"""Multi-agent orchestration for BEACON.

Exposes the :class:`Orchestrator`, which coordinates role-scoped sub-agents
(research → plan → engineer/specialist → verify) with a verification feedback
loop and per-agent model selection.
"""

from core.orchestration.orchestrator import Orchestrator, OrchestrationResult
from core.orchestration.roles import (
    CORE_PIPELINE,
    AgentRole,
    known_specialties,
    specialist_role,
)
from core.orchestration.sub_agent import SubAgent

__all__ = [
    "Orchestrator",
    "OrchestrationResult",
    "SubAgent",
    "AgentRole",
    "CORE_PIPELINE",
    "specialist_role",
    "known_specialties",
]

