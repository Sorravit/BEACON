"""Role definitions for BEACON's multi-agent orchestration.

Each :class:`AgentRole` describes a specialised sub-agent the orchestrator can
spawn: its model role key (resolved against ``models.yaml``), the system prompt
that establishes its expertise, and whether it is allowed to use tools (act on
the world) or is reasoning-only.

The core pipeline roles are fixed (researcher → planner → engineer → verifier),
but the orchestrator may also spawn a *dynamic specialist* — e.g. devops, k8s,
data engineer — when the plan calls for expertise beyond the defaults. Dynamic
specialists are built from :func:`specialist_role` using a templated prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class AgentRole:
    """A spawnable sub-agent role."""

    key: str           # stable identifier, also the models.yaml role key
    title: str         # human-friendly label for the UI
    model_role: str    # which models.yaml role default to use
    uses_tools: bool   # True → may execute tools; False → reasoning only
    system_prompt: str

    def with_model_role(self, model_role: str) -> "AgentRole":
        return AgentRole(
            key=self.key,
            title=self.title,
            model_role=model_role,
            uses_tools=self.uses_tools,
            system_prompt=self.system_prompt,
        )


ORCHESTRATOR = AgentRole(
    key="orchestrator",
    title="Orchestrator",
    model_role="orchestrator",
    uses_tools=False,
    system_prompt=(
        "You are the ORCHESTRATOR — the lead agent coordinating a team of "
        "specialist sub-agents to complete a complex task. You decompose the "
        "goal, decide which specialists to spawn, and judge when the work is "
        "truly done. You are decisive, structured, and never do the specialists' "
        "work yourself — you direct it. Always respond in the exact JSON schema "
        "you are asked for."
    ),
)

RESEARCHER = AgentRole(
    key="researcher",
    title="Research Agent",
    model_role="researcher",
    uses_tools=True,
    system_prompt=(
        "You are a meticulous RESEARCH AGENT. Your job is to gather and verify "
        "all the context needed to complete a task — WITHOUT performing the task "
        "itself. Use read-only tools: web_search, read_file, list_files, "
        "execute_command (read-only), and MCP tools to fetch specs (Jira issues, "
        "Confluence pages, related tickets in the project). Cross-check facts, "
        "note constraints and acceptance criteria, and surface anything "
        "ambiguous. Never write files or change state in this phase."
    ),
)

PLANNER = AgentRole(
    key="planner",
    title="Project Planner",
    model_role="planner",
    uses_tools=False,
    system_prompt=(
        "You are a senior PROJECT PLANNER. Given a goal and research findings, "
        "you produce a concrete, ordered, independently-executable plan. You "
        "identify the single best specialist to execute it (e.g. lead software "
        "engineer, devops engineer, kubernetes engineer, data engineer) and you "
        "define explicit, testable ACCEPTANCE CRITERIA. You output ONLY valid "
        "JSON — never prose, never tool calls."
    ),
)

ENGINEER = AgentRole(
    key="engineer",
    title="Lead Software Engineer",
    model_role="engineer",
    uses_tools=True,
    system_prompt=(
        "You are a LEAD SOFTWARE ENGINEER executing an approved plan. You write "
        "correct, production-quality code and use tools to make real changes: "
        "write_file, execute_command, execute_long_command, read_file, and "
        "browser tools when needed. You follow the plan step by step, validate "
        "your own work as you go (run tests, re-read files), and fix errors you "
        "introduce. Save artifacts under output/ or the project as appropriate."
    ),
)

VERIFIER = AgentRole(
    key="verifier",
    title="Verification Agent",
    model_role="verifier",
    uses_tools=True,
    system_prompt=(
        "You are a rigorous SENIOR QA / VERIFICATION AGENT. You independently "
        "validate that the work satisfies EVERY acceptance criterion and the "
        "original specification. Use tools to verify for real: run tests with "
        "execute_command, read_file/list_files to confirm artifacts, browser "
        "tools for web results, and MCP tools to re-read the spec source (the "
        "Jira issue, its acceptance criteria, and related Confluence pages and "
        "tickets in that project). Be skeptical: if something is unproven, it "
        "fails. Output ONLY the JSON verdict you are asked for."
    ),
)

# Fixed core pipeline, in execution order.
CORE_PIPELINE = (RESEARCHER, PLANNER, ENGINEER, VERIFIER)

# Catalogue of recognised dynamic specialists. The orchestrator/planner may pick
# any of these for the ACT phase; unknown specialties fall back to a generic
# engineer prompt via :func:`specialist_role`.
_SPECIALIST_PROFILES: Dict[str, str] = {
    "lead-software-engineer": (
        "a LEAD SOFTWARE ENGINEER who writes production-quality application code, "
        "designs clean modules, and adds tests"
    ),
    "devops": (
        "a DEVOPS ENGINEER who automates builds, CI/CD pipelines, infrastructure "
        "as code, shell tooling, and deployment"
    ),
    "kubernetes": (
        "a KUBERNETES / PLATFORM ENGINEER who writes manifests, Helm charts, and "
        "debugs cluster workloads"
    ),
    "data-engineer": (
        "a DATA ENGINEER who builds data pipelines, transformations, and queries"
    ),
    "sre": (
        "a SITE RELIABILITY ENGINEER focused on reliability, monitoring, and "
        "incident response"
    ),
    "security": (
        "a SECURITY ENGINEER who audits code and infrastructure for "
        "vulnerabilities and hardens them"
    ),
}

# Specialties that have their own model-role default in models.yaml. Anything
# not listed here resolves to the generic ``engineer`` model role.
_MODEL_ROLE_SPECIALTIES = {"devops", "engineer"}


def specialist_role(specialty: str) -> AgentRole:
    """Build a dynamic specialist ACT-phase role from a specialty name.

    Known specialties get a tailored persona; unknown ones get a generic but
    still domain-named engineer persona. The model role is the specialty itself
    when models.yaml defines a default for it (e.g. ``devops``), otherwise it
    falls back to the ``engineer`` model role.
    """
    normalized = (specialty or "").strip().lower().replace(" ", "-")
    profile = _SPECIALIST_PROFILES.get(normalized)
    if profile:
        title = normalized.replace("-", " ").title()
    else:
        # Generic fallback so the orchestrator can invent roles on the fly.
        human = (specialty or "engineer").strip() or "engineer"
        profile = f"a {human.upper()} who is an expert in this domain"
        title = human.title()
        normalized = normalized or "specialist"

    # Honour a per-specialty model default from models.yaml when one exists.
    model_role = normalized if normalized in _MODEL_ROLE_SPECIALTIES else "engineer"

    prompt = (
        f"You are {profile}. You are executing an approved plan to completion. "
        "Use tools to make real changes (write_file, execute_command, "
        "execute_long_command, read_file, browser tools, MCP tools). Follow the "
        "plan step by step, validate your own work, and fix any errors you "
        "introduce. Be thorough and production-minded."
    )
    return AgentRole(
        key=f"specialist:{normalized}",
        title=title,
        model_role=model_role,
        uses_tools=True,
        system_prompt=prompt,
    )


def known_specialties() -> Dict[str, str]:
    return dict(_SPECIALIST_PROFILES)



