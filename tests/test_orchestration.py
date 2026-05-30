"""Tests for the multi-agent orchestrator using a scripted fake agent.

These exercise the pipeline wiring, per-role model selection, dynamic specialist
spawning and the verification feedback loop without any network calls.
"""

import asyncio
import json
import os

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from core.orchestration import Orchestrator  # noqa: E402
from core.orchestration.roles import specialist_role  # noqa: E402


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, agent):
        self._agent = agent

    def create(self, **kwargs):
        # The planner is the only caller that goes through the raw client.
        return _Response(self._agent.plan_json)


class _Chat:
    def __init__(self, agent):
        self.completions = _Completions(agent)


class _Client:
    def __init__(self, agent):
        self.chat = _Chat(agent)


class FakeAgent:
    """Minimal stand-in for AIAgent with scripted phase responses."""

    def __init__(self, specialist="devops", pass_on_round=2):
        from main import Config

        self.config = Config()
        self.tools = None
        self.client = _Client(self)
        self.verify_calls = 0
        self.pass_on_round = pass_on_round
        self.models_used = []
        self.plan_json = json.dumps({
            "specialist": specialist,
            "acceptance_criteria": ["It works", "Tests pass"],
            "steps": [{"description": "Do step one"}, {"description": "Do step two"}],
        })

    async def get_response(self, user_input, conversation=None, tools=None,
                           model=None, token_callback=None):
        self.models_used.append(model)
        system = conversation[0]["content"] if conversation else ""
        if "RESEARCH AGENT" in system:
            return "Research summary: the approach is to do X and Y."
        if "VERIFICATION AGENT" in system:
            self.verify_calls += 1
            passed = self.verify_calls >= self.pass_on_round
            return "Verified. " + json.dumps({
                "passed": passed,
                "feedback": "all good" if passed else "missing test coverage",
            })
        # Engineer / specialist execution.
        return "Implemented the requested change."


def _run(coro):
    return asyncio.run(coro)


def test_pipeline_runs_all_roles_and_verifies_first_round():
    agent = FakeAgent(specialist="lead-software-engineer", pass_on_round=1)
    events = []
    orch = Orchestrator(agent, max_rounds=2, emit=lambda e, d: events.append((e, d)))

    result = _run(orch.run("Build a thing", task_id="t1"))

    assert result.status == "completed"
    assert result.verified is True
    assert result.rounds == 1
    roles = [p.role for p in result.phases]
    assert "researcher" in roles
    assert "planner" in roles
    assert "verifier" in roles
    assert any(r.startswith("specialist:") for r in roles)
    assert {"It works", "Tests pass"} == set(result.acceptance_criteria)

    event_names = [e for e, _ in events]
    assert "task_started" in event_names
    assert "task_planned" in event_names
    assert "task_verified" in event_names
    assert "task_completed" in event_names


def test_verification_loop_retries_until_pass():
    agent = FakeAgent(pass_on_round=2)
    orch = Orchestrator(agent, max_rounds=3)

    result = _run(orch.run("Fix the bug", task_id="t2"))

    assert result.verified is True
    assert result.rounds == 2  # failed once, passed on the second round
    # Researcher + planner + specialist + verifier each ran twice.
    assert sum(1 for p in result.phases if p.role == "verifier") == 2


def test_unverified_after_max_rounds_still_completes():
    agent = FakeAgent(pass_on_round=99)
    orch = Orchestrator(agent, max_rounds=2)

    result = _run(orch.run("Hard task", task_id="t3"))

    assert result.status == "completed"
    assert result.verified is False
    assert result.rounds == 2


def test_per_role_model_defaults_from_registry():
    agent = FakeAgent(pass_on_round=1)
    spawned = []
    orch = Orchestrator(
        agent, max_rounds=1,
        emit=lambda e, d: spawned.append(d) if e == "agent_spawned" else None,
    )
    _run(orch.run("Task", task_id="t4"))

    by_role = {d["role"]: d["model"] for d in spawned}
    # Researcher default per models.yaml is gpt-5.1-chat; verifier is sonnet-4.5.
    assert by_role["researcher"] == "global/gpt-5.1-chat"
    assert by_role["verifier"] == "global/anthropic.claude-sonnet-4-5-20250929-v1:0"


def test_model_overrides_apply_per_role_and_all():
    agent = FakeAgent(pass_on_round=1)
    spawned = []
    orch = Orchestrator(
        agent, max_rounds=1,
        model_overrides={"researcher": "global/o3-mini", "all": "global/o3-mini"},
        emit=lambda e, d: spawned.append(d) if e == "agent_spawned" else None,
    )
    _run(orch.run("Task", task_id="t5"))

    by_role = {d["role"]: d["model"] for d in spawned}
    # Researcher explicit override wins; everything else falls back to "all".
    assert by_role["researcher"] == "global/o3-mini"
    assert by_role["planner"] == "global/o3-mini"


def test_dynamic_specialist_role_is_built():
    role = specialist_role("kubernetes")
    assert role.uses_tools is True
    assert "KUBERNETES" in role.system_prompt.upper()

    unknown = specialist_role("blockchain-wizard")
    assert unknown.key.startswith("specialist:")
    assert "BLOCKCHAIN-WIZARD" in unknown.system_prompt.upper()

