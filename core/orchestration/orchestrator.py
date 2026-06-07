"""The BEACON multi-agent Orchestrator.

The orchestrator coordinates a team of role-scoped sub-agents to complete a
complex goal, looping until the work passes verification:

    RESEARCH  → PLAN → ACT (engineer / dynamic specialist) → VERIFY
       ▲                                                        │
       └───────────────── feedback on failure ──────────────────┘

Key behaviours
--------------
* **Fixed core roles + dynamic spawning.** The pipeline always runs researcher,
  planner, an ACT specialist and verifier. The planner chooses *which* specialist
  to spawn for ACT (lead software engineer, devops, kubernetes, …), so the team
  composition adapts to the task.
* **Per-agent model selection.** Every role resolves a model from ``models.yaml``
  by default, and any role can be overridden per run (automatic + manual).
* **Spec-aware verification.** When the goal references a Jira issue, research and
  verification are instructed to fetch the issue, its acceptance criteria, and
  related Confluence pages / tickets in that project via MCP, then validate
  against them. Otherwise the planner-derived acceptance criteria are used.
* **SSE-friendly.** Progress is emitted through a callback using event names the
  existing Task-Mode frontend already understands, plus a few additive events
  (``agent_spawned``) that older clients can safely ignore.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from core.orchestration.roles import (
    PLANNER,
    RESEARCHER,
    VERIFIER,
    AgentRole,
    specialist_role,
)
from core.orchestration.sub_agent import SubAgent, extract_json
from utils.encoding import safe_encode_string

logger = logging.getLogger(__name__)

_JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
# Default limit for inter-agent context. Increased from 4000 to preserve more
# information between agents while still preventing token overflow.
_MAX_CONTEXT_CHARS = 8000


@dataclass
class PhaseRecord:
    """A record of one sub-agent's contribution."""

    role: str
    title: str
    model: str
    output: str = ""
    started_at: str = ""
    completed_at: str = ""


@dataclass
class OrchestrationResult:
    task_id: str
    goal: str
    status: str = "pending"            # completed | failed
    verified: bool = False
    rounds: int = 0
    final_output: str = ""
    acceptance_criteria: List[str] = field(default_factory=list)
    specialist: str = ""
    phases: List[PhaseRecord] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status,
            "verified": self.verified,
            "rounds": self.rounds,
            "final_output": self.final_output,
            "acceptance_criteria": self.acceptance_criteria,
            "specialist": self.specialist,
            "phases": [
                {"role": p.role, "title": p.title, "model": p.model, "output": p.output}
                for p in self.phases
            ],
            "error": self.error,
        }


class Orchestrator:
    """Coordinates role-scoped sub-agents with a verification loop."""

    def __init__(
        self,
        ai_agent,
        tools=None,
        *,
        max_rounds: int = 2,
        model_overrides: Optional[Dict[str, str]] = None,
        emit: Optional[Callable[[str, dict], None]] = None,
        session_conversation: Optional[List[Dict]] = None,
    ) -> None:
        """
        Args:
            ai_agent: An initialised ``AIAgent`` (provides client, config, tools).
            tools: ToolManager for tool-enabled roles. Defaults to ``ai_agent.tools``.
            max_rounds: Max full research→verify rounds before accepting the result.
            model_overrides: Optional ``{role_or_model_role: model_id}`` map. The
                special key ``"all"`` overrides every role's model.
            emit: ``callable(event_name, data)`` for SSE progress events.
            session_conversation: Prior chat history for additional context.
        """
        self.ai_agent = ai_agent
        self.tools = tools if tools is not None else getattr(ai_agent, "tools", None)
        self.max_rounds = max(1, max_rounds)
        self.model_overrides = dict(model_overrides or {})
        self._emit_cb = emit
        self.session_conversation = session_conversation
        self._task_id = ""

        # ── Interactive Q&A state ────────────────────────────────────────────
        # The planner may surface a genuine clarifying question. When it does we
        # pause on this event until the web layer calls submit_answer().
        self._answer_event: Optional[asyncio.Event] = None
        self._answer: Optional[str] = None
        self._awaiting_answer = False
        # Accumulated user clarifications, threaded into plan + act context.
        self._clarifications = ""

    # ── interactive Q&A ──────────────────────────────────────────────────────

    def submit_answer(self, task_id: str, answer: str) -> bool:
        """Called by the web layer when the user answers a clarifying question.

        Returns True if the orchestrator was paused and is now resumed.
        """
        if not self._awaiting_answer or self._answer_event is None:
            logger.warning("[%s] submit_answer called but not awaiting an answer", task_id)
            return False
        self._answer = answer
        self._answer_event.set()
        logger.info("[%s] Answer received: %s", task_id, (answer or "")[:80])
        return True

    @property
    def awaiting_answer(self) -> bool:
        return self._awaiting_answer

    async def _ask_question(self, question: str, timeout: float = 1800.0) -> Optional[str]:
        """Emit a task_question event, pause, and wait for the user's answer.

        Returns the answer string, or None on timeout.
        """
        self._answer_event = asyncio.Event()
        self._answer = None
        self._awaiting_answer = True
        self._emit("task_question", {"task_id": self._task_id, "question": question})
        logger.info("[%s] Waiting for user answer to: %s", self._task_id, question)
        try:
            await asyncio.wait_for(self._answer_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[%s] Question timed out after %.0fs — continuing without answer",
                           self._task_id, timeout)
            self._awaiting_answer = False
            self._answer_event = None
            return None
        ans = self._answer
        self._awaiting_answer = False
        self._answer_event = None
        self._answer = None
        return ans


    # ── helpers ──────────────────────────────────────────────────────────────

    def _emit(self, event: str, data: dict) -> None:
        if self._emit_cb:
            try:
                self._emit_cb(event, data)
            except Exception:  # pragma: no cover - defensive
                logger.debug("emit failed for %s", event, exc_info=True)

    def _handoff(self, frm: str, to: str, label: str, content: str) -> None:
        """Log + emit the context being passed from one agent to the next.

        This is the single place that makes the agent-to-agent data flow visible:
        it logs a one-line summary and emits an ``agent_context`` event carrying a
        preview so the UI/log shows exactly what the next agent receives.
        """
        size = len(content or "")
        logger.info("[%s] HANDOFF  %s → %s | %s (%d chars)",
                    self._task_id, frm, to, label, size)
        self._emit("agent_context", {
            "task_id": self._task_id,
            "from": frm,
            "to": to,
            "label": label,
            "chars": size,
            "preview": self._truncate(content, 800),
        })

    def _model_for(self, role: AgentRole) -> Optional[str]:
        """Pick the override model for a role, if any (role key > model role > all)."""
        return (
            self.model_overrides.get(role.key)
            or self.model_overrides.get(role.model_role)
            or self.model_overrides.get("all")
        )

    def _spawn(self, role: AgentRole) -> SubAgent:
        agent = SubAgent(
            self.ai_agent,
            role,
            model=self._model_for(role),
            tools=self.tools,
        )
        self._emit("agent_spawned", {
            "task_id": self._task_id,
            "role": role.key,
            "title": role.title,
            "model": agent.model,
        })
        logger.info("[%s] SPAWN    %-22s model=%s tools=%s",
                    self._task_id, role.title, agent.model, role.uses_tools)
        return agent

    def _session_context(self) -> str:
        if not self.session_conversation:
            return ""
        lines = []
        for msg in self.session_conversation[-10:]:
            role = msg.get("role", "")
            content = (msg.get("content") or "")[:400]
            if role in ("user", "assistant") and content:
                lines.append(f"{role.capitalize()}: {content}")
        return "\n".join(lines)

    @staticmethod
    def _truncate(text: str, limit: int = _MAX_CONTEXT_CHARS) -> str:
        text = text or ""
        return text if len(text) <= limit else text[:limit] + "\n…[truncated]"

    # ── main entry point ─────────────────────────────────────────────────────

    async def run(self, goal: str, task_id: Optional[str] = None) -> OrchestrationResult:
        task_id = task_id or "orch_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        self._task_id = task_id
        result = OrchestrationResult(task_id=task_id, goal=goal)
        start_time = datetime.now()

        jira_keys = sorted(set(_JIRA_KEY_RE.findall(goal)))
        spec_note = ""
        if jira_keys:
            spec_note = (
                "\n\nThis task references Jira issue(s): " + ", ".join(jira_keys) +
                ". Use the Atlassian MCP tools to fetch each issue (summary, "
                "description, acceptance criteria, status), its linked issues, and "
                "any Confluence pages or related tickets in that project. Treat "
                "those as the authoritative specification."
            )

        logger.info("[%s] ORCHESTRATION START | goal=%r | max_rounds=%d | jira=%s",
                    task_id, goal, self.max_rounds, jira_keys or "none")
        self._emit("task_started", {"task_id": task_id, "description": goal})

        try:
            session_ctx = self._session_context()
            verify_feedback = ""

            for round_no in range(self.max_rounds):
                result.rounds = round_no + 1
                is_retry = round_no > 0
                logger.info("[%s] ===== ROUND %d/%d %s=====", task_id, round_no + 1,
                            self.max_rounds, "(retry after failed verification) " if is_retry else "")

                # ── RESEARCH ─────────────────────────────────────────────────
                research = await self._phase_research(
                    task_id, goal, session_ctx, spec_note, verify_feedback, is_retry, result
                )
                # Hand the research findings to the planner.
                self._handoff("Research Agent", "Project Planner", "research findings", research)

                # ── PLAN ─────────────────────────────────────────────────────
                plan = await self._phase_plan(
                    task_id, goal, research, verify_feedback, is_retry, result
                )

                # ── Interactive Q&A: ask any genuine clarifying questions ────
                # Only on the first round, and only when a live UI is attached.
                questions = plan.get("questions") or []
                if questions and not is_retry and self._emit_cb:
                    new_clar: List[str] = []
                    for q in questions:
                        answer = await self._ask_question(q)
                        if answer:
                            new_clar.append(f"Q: {q}\nA: {answer}")
                    if new_clar:
                        block = "\n\n".join(new_clar)
                        self._clarifications = (
                            self._clarifications + "\n\n" + block
                            if self._clarifications else block
                        )
                        self._emit("task_planning", {
                            "task_id": task_id,
                            "message": "Got your answer — refining the plan…",
                        })
                        # Re-plan with the new clarifications so the steps reflect them.
                        plan = await self._phase_plan(
                            task_id, goal, research, verify_feedback, False, result
                        )

                steps: List[str] = plan["steps"]
                result.acceptance_criteria = plan["acceptance_criteria"]
                result.specialist = plan["specialist"]
                # Hand the plan + chosen specialist to the ACT agent.
                self._handoff(
                    "Project Planner", f"{plan['specialist']} (ACT)", "execution plan",
                    "Specialist: " + plan["specialist"] + "\nAcceptance criteria:\n"
                    + "\n".join(f"- {c}" for c in plan["acceptance_criteria"])
                    + "\nSteps:\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps)),
                )

                # ── ACT (dynamic specialist) ─────────────────────────────────
                act_output = await self._phase_act(
                    task_id, goal, research, steps, plan["specialist"], result
                )
                # Hand the work product to the verifier.
                self._handoff(f"{plan['specialist']} (ACT)", "Verification Agent",
                              "work product + acceptance criteria", act_output)

                # ── VERIFY ───────────────────────────────────────────────────
                passed, feedback = await self._phase_verify(
                    task_id, goal, act_output, result.acceptance_criteria,
                    jira_keys, spec_note, result
                )
                result.verified = passed
                result.final_output = act_output

                if passed:
                    logger.info("[%s] ROUND %d VERIFIED ✅", task_id, round_no + 1)
                    break
                verify_feedback = feedback
                if round_no + 1 < self.max_rounds:
                    logger.info("[%s] ROUND %d FAILED ❌ — looping back to research. "
                                "Feedback handed to next round: %s",
                                task_id, round_no + 1, self._truncate(feedback, 300))
                    # The verifier's feedback becomes context for the next research round.
                    self._handoff("Verification Agent", "Research Agent (next round)",
                                  "failure feedback", feedback)

            result.status = "completed"
            summary = self._compile_summary(result)
            result.final_output = summary
            duration_seconds = (datetime.now() - start_time).total_seconds()
            logger.info("[%s] ORCHESTRATION DONE | verified=%s | rounds=%d | duration=%.2fs",
                        task_id, result.verified, result.rounds, duration_seconds)
            self._emit("task_completed", {
                "task_id": task_id,
                "description": goal,
                "result": summary,
                "verified": result.verified,
                "duration_seconds": duration_seconds,
            })
            return result

        except Exception as exc:  # pragma: no cover - network dependent
            logger.error("[%s] Orchestration failed: %s", task_id, exc)
            result.status = "failed"
            result.error = str(exc)
            self._emit("task_failed", {"task_id": task_id, "error": str(exc)})
            return result

    # ── phases ───────────────────────────────────────────────────────────────

    async def _phase_research(
        self, task_id, goal, session_ctx, spec_note, verify_feedback, is_retry, result
    ) -> str:
        agent = self._spawn(RESEARCHER)
        record = PhaseRecord(role=RESEARCHER.key, title=RESEARCHER.title,
                             model=agent.model, started_at=datetime.now().isoformat())
        self._emit("task_researching", {
            "task_id": task_id,
            "message": ("Re-researching after verification failure…" if is_retry
                        else "Researching requirements and gathering context…"),
        })
        retry_ctx = ""
        if is_retry and verify_feedback:
            retry_ctx = ("\n\nThe previous attempt FAILED verification because:\n"
                         f"{verify_feedback}\nResearch with this failure in mind.")
        context = self._truncate(
            (f"Prior conversation:\n{session_ctx}\n\n" if session_ctx else "") +
            f"Goal: {goal}{spec_note}{retry_ctx}"
        )
        instruction = (
            "Research everything needed to complete the goal below. Gather facts, "
            "constraints, and the authoritative specification. Do NOT perform the "
            "task. End with a concise 'Research summary:' followed by the key "
            "findings and the best approach.\n\nGoal: " + goal
        )
        output = await agent.act(instruction, context=context)
        record.output = output
        record.completed_at = datetime.now().isoformat()
        result.phases.append(record)
        logger.info("[%s] RESEARCH done | %d chars | %s", task_id, len(output),
                    self._truncate(output, 160).replace("\n", " "))
        self._emit("task_researching", {
            "task_id": task_id, "summary": self._truncate(output, 1200),
            "message": "Research complete.",
        })
        return output

    async def _phase_plan(
        self, task_id, goal, research, verify_feedback, is_retry, result
    ) -> Dict[str, Any]:
        agent = self._spawn(PLANNER)
        record = PhaseRecord(role=PLANNER.key, title=PLANNER.title,
                             model=agent.model, started_at=datetime.now().isoformat())
        self._emit("task_planning", {
            "task_id": task_id,
            "message": ("Re-planning based on verification feedback…" if is_retry
                        else "Planning execution steps and selecting a specialist…"),
        })
        fb = ("\n\nPrevious verification FAILED:\n" + verify_feedback + "\nFix this."
              if is_retry and verify_feedback else "")
        clar_block = (
            "\n\nClarifications already provided by the user (treat as authoritative):\n"
            + self._clarifications
            if self._clarifications else ""
        )
        # Only invite a clarifying question on the first attempt and when there is
        # a live UI to answer it — never re-ask once clarifications exist.
        may_ask = (not is_retry) and bool(self._emit_cb) and not self._clarifications
        question_rules = (
            "\n\nQUESTION RULES — read carefully:\n"
            "- You MAY include ONE clarifying question in \"questions\" ONLY if the goal is\n"
            "  GENUINELY ambiguous and ONLY the user can resolve it (e.g. 'Deploy to\n"
            "  staging or production?', 'Which branch should I target?').\n"
            "- You MUST NOT ask for permission ('Should I proceed?') or anything you can\n"
            "  discover with tools. If nothing is ambiguous, return an empty array.\n"
            if may_ask else
            "\n\nDo not ask any questions; return an empty \"questions\" array.\n"
        )
        instruction = (
            "Create an execution plan for the goal. Choose the single best "
            "specialist to execute it and define explicit, testable acceptance "
            "criteria.\n\n"
            f"Goal: {goal}\n\nResearch findings:\n{self._truncate(research, 5000)}{fb}{clar_block}\n\n"
            "Specialist must be one of: lead-software-engineer, devops, kubernetes, "
            "data-engineer, sre, security — or another short role name if none fit."
            + question_rules +
            '\nRespond ONLY with JSON:\n'
            '{"specialist": "<role>", '
            '"acceptance_criteria": ["criterion 1", "criterion 2"], '
            '"questions": ["single clarifying question if truly needed, else empty array"], '
            '"steps": [{"description": "..."}]}'
        )
        data = await agent.reason_json(instruction)
        steps, criteria, specialist, questions = self._parse_plan(data, goal)
        record.output = safe_encode_string(str(data) if data else "")
        record.completed_at = datetime.now().isoformat()
        result.phases.append(record)
        logger.info("[%s] PLAN done | specialist=%s | %d step(s) | %d criteria | %d question(s)",
                    task_id, specialist, len(steps), len(criteria), len(questions))
        self._emit("task_planned", {
            "task_id": task_id,
            "specialist": specialist,
            "acceptance_criteria": criteria,
            "steps": [{"step_id": i + 1, "description": s} for i, s in enumerate(steps)],
        })
        return {"steps": steps, "acceptance_criteria": criteria,
                "specialist": specialist, "questions": questions}

    @staticmethod
    def _parse_plan(data, goal):
        steps: List[str] = []
        criteria: List[str] = []
        questions: List[str] = []
        specialist = "lead-software-engineer"
        if isinstance(data, dict):
            specialist = str(data.get("specialist") or specialist).strip() or specialist
            for c in data.get("acceptance_criteria") or []:
                if isinstance(c, str) and c.strip():
                    criteria.append(c.strip())
            for s in data.get("steps") or []:
                if isinstance(s, dict) and s.get("description"):
                    steps.append(str(s["description"]).strip())
                elif isinstance(s, str) and s.strip():
                    steps.append(s.strip())
            # Filter out blank and permission-seeking questions.
            _permission_words = (
                "should i", "do you want", "can i", "would you like",
                "shall i", "is it ok", "may i", "do you need",
            )
            for q in data.get("questions") or []:
                if isinstance(q, str) and q.strip() and not any(
                    p in q.lower() for p in _permission_words
                ):
                    questions.append(q.strip())
        if not steps:
            steps = [f"Complete the goal: {goal}"]
        if not criteria:
            criteria = ["The goal is fully and correctly accomplished."]
        return steps, criteria, specialist, questions

    async def _phase_act(self, task_id, goal, research, steps, specialist, result) -> str:
        role = specialist_role(specialist)
        agent = self._spawn(role)
        record = PhaseRecord(role=role.key, title=role.title,
                             model=agent.model, started_at=datetime.now().isoformat())
        self._emit("task_executing", {
            "task_id": task_id,
            "message": f"{role.title} executing {len(steps)} step(s)…",
        })

        accumulated = f"Goal: {goal}\n\nResearch findings:\n{self._truncate(research, 3000)}"
        if self._clarifications:
            accumulated += f"\n\nUser clarifications (authoritative):\n{self._clarifications}"
        outputs: List[str] = []
        for idx, step in enumerate(steps, start=1):
            logger.info("[%s] ACT step %d/%d | %s | %s", task_id, idx, len(steps),
                        role.title, self._truncate(step, 120).replace("\n", " "))
            self._emit("step_started", {
                "task_id": task_id,
                "step": {"step_id": idx, "description": step},
            })
            instruction = (
                f"Execute step {idx} of {len(steps)} of the plan. Use tools to make "
                f"real changes as needed.\n\nStep: {step}"
            )
            step_out = await agent.act(instruction, context=self._truncate(accumulated))
            outputs.append(f"Step {idx}: {step}\n{step_out}")
            # The running context (goal + research + prior step results) is what
            # the specialist sees on the next step — this is how ACT stays coherent.
            accumulated = self._truncate(accumulated + f"\n\nStep {idx} result:\n{step_out}", 6000)
            logger.info("[%s] ACT step %d done | %d chars", task_id, idx, len(step_out))
            self._emit("step_completed", {
                "task_id": task_id,
                "step": {"step_id": idx, "description": step,
                         "result": self._truncate(step_out, 500)},
            })

        combined = "\n\n".join(outputs)
        record.output = combined
        record.completed_at = datetime.now().isoformat()
        result.phases.append(record)
        return combined

    async def _phase_verify(
        self, task_id, goal, act_output, criteria, jira_keys, spec_note, result
    ):
        agent = self._spawn(VERIFIER)
        record = PhaseRecord(role=VERIFIER.key, title=VERIFIER.title,
                             model=agent.model, started_at=datetime.now().isoformat())
        self._emit("task_verifying", {
            "task_id": task_id,
            "message": "Verifying output against acceptance criteria and the spec…",
        })
        criteria_block = "\n".join(f"- {c}" for c in criteria)
        jira_block = ""
        if jira_keys:
            jira_block = (
                "\n\nAuthoritative spec: re-fetch Jira issue(s) "
                + ", ".join(jira_keys)
                + " and related Confluence pages / project tickets via MCP, and "
                "validate against every acceptance criterion found there."
            )
        instruction = (
            "Independently verify that the work below satisfies EVERY acceptance "
            "criterion and the original goal. Use tools to check for real (run "
            "tests, read files, fetch the spec). Be skeptical.\n\n"
            f"Goal: {goal}{spec_note}{jira_block}\n\n"
            f"Acceptance criteria:\n{criteria_block}\n\n"
            f"What was done:\n{self._truncate(act_output, 3000)}\n\n"
            'After verifying, end your reply with ONLY this JSON object on its own:\n'
            '{"passed": true/false, "feedback": "what you tested, what passed, what failed"}'
        )
        raw = await agent.act(instruction, context="")
        data = extract_json(raw)
        passed = bool(data.get("passed", False)) if isinstance(data, dict) else False
        feedback = (data.get("feedback", "") if isinstance(data, dict) else raw)[:1500]
        record.output = safe_encode_string(raw)
        record.completed_at = datetime.now().isoformat()
        result.phases.append(record)
        logger.info("[%s] VERIFY done | passed=%s | %s", task_id, passed,
                    self._truncate(feedback, 200).replace("\n", " "))
        self._emit("task_verified", {
            "task_id": task_id,
            "passed": passed,
            "message": ("Verification passed." if passed
                        else "Verification failed: " + feedback),
        })
        return passed, feedback

    # ── result compilation ───────────────────────────────────────────────────

    def _compile_summary(self, result: OrchestrationResult) -> str:
        status_icon = "✅" if result.verified else "⚠️"
        lines = [
            f"{status_icon} Orchestration {'verified' if result.verified else 'completed (unverified)'} "
            f"in {result.rounds} round(s).",
            f"Specialist: {result.specialist}",
            "",
            "Acceptance criteria:",
        ]
        lines += [f"  - {c}" for c in result.acceptance_criteria]
        # Show full result to user - no truncation for final output
        lines += ["", "Result:", result.final_output or ""]
        return "\n".join(lines)

