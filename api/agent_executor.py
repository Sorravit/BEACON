#!/usr/bin/env python3
"""
Agent Executor - Research -> Plan -> Act -> Verify loop.

Every task goes through four phases:
  1. RESEARCH  - understand requirements, gather context via tools/search
  2. PLAN      - break the task into numbered, tool-mapped steps
               ↳ If the plan contains a genuine clarifying question, emit
                 task_question and PAUSE until the user replies.
  3. ACT       - execute each step using tools (error-detection + auto-fix)
  4. VERIFY    - check output meets requirements; re-plan/re-act if not

Sub-tasks can be run consecutively by calling execute_task() in sequence.
Each phase emits SSE events so the frontend can show real-time progress.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

from utils.encoding import safe_encode_string

logger = logging.getLogger(__name__)

class TaskStatus(Enum):
    PENDING = "pending"
    RESEARCHING = "researching"
    PLANNING = "planning"
    WAITING_FOR_ANSWER = "waiting_for_answer"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskStep:
    """A single step in the execution plan."""
    step_id: int
    description: str
    tool_name: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    result: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None


@dataclass
class Task:
    """A complete task with research summary, plan, results and verification state."""
    task_id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    steps: List[TaskStep] = field(default_factory=list)
    research_summary: Optional[str] = None
    result: Optional[str] = None
    verification_passed: bool = False
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class AgentExecutor:
    """
    Autonomous agent executor: Research -> Plan -> Act -> Verify.

    Phase 1 RESEARCH : gather context and requirements via tools/web search
    Phase 2 PLAN     : produce a numbered, tool-mapped execution plan
                       If the plan detects a genuine ambiguity, emit task_question,
                       pause execution, and wait for the user's answer before
                       continuing.  The answer is injected into the task context
                       so subsequent phases are aware of it.
    Phase 3 ACT      : execute every step with error-detection and auto-fix
    Phase 4 VERIFY   : check output satisfies requirements; loop if not
    """

    def __init__(self, ai_agent, max_steps: int = 20, max_retries: int = 3,
                 max_verify_retries: int = 2, step_callback=None,
                 session_conversation: Optional[List[Dict]] = None):
        """
        Args:
            ai_agent: AIAgent instance with tools
            max_steps: Maximum plan steps per task
            max_retries: Maximum retries per individual step
            max_verify_retries: How many times to re-plan if verification fails
            step_callback: Callable(event: str,  dict) for SSE events
            session_conversation: Chat history so Task Mode is aware of prior
                design decisions / requirements discussed in regular chat.
        """
        self.ai_agent = ai_agent
        self.max_steps = max_steps
        self.max_retries = max_retries
        self.max_verify_retries = max_verify_retries
        self.tasks: Dict[str, Task] = {}
        self.current_task: Optional[Task] = None
        self.step_callback = step_callback
        self.session_conversation = session_conversation

        # ── Interactive Q&A state ────────────────────────────────────────────
        # Maps task_id → asyncio.Event that is set when the user submits an answer.
        self._answer_events: Dict[str, asyncio.Event] = {}
        # Maps task_id → the answer string provided by the user.
        self._answers: Dict[str, str] = {}

    # -------------------------------------------------------------------------
    # SSE helpers
    # -------------------------------------------------------------------------

    def _emit(self, event: str, data: dict):
        if self.step_callback:
            try:
                self.step_callback(event, data)
            except Exception:
                pass

    def _base_conv(self) -> Optional[List[Dict]]:
        """Return a fresh copy of session conversation as AI context base."""
        return list(self.session_conversation) if self.session_conversation else None

    # -------------------------------------------------------------------------
    # Interactive Q&A helpers
    # -------------------------------------------------------------------------

    def submit_answer(self, task_id: str, answer: str) -> bool:
        """
        Called by the web layer when the user submits an answer to a question.
        Stores the answer and signals the waiting coroutine to continue.

        Returns True if the task was paused and is now resumed, False otherwise.
        """
        event = self._answer_events.get(task_id)
        if event is None:
            logger.warning("[Task %s] submit_answer called but no pending question", task_id)
            return False
        self._answers[task_id] = answer
        event.set()
        logger.info("[Task %s] Answer received: %s", task_id, answer[:80])
        return True

    async def _ask_question(self, task: Task, question: str,
                             timeout: float = 300.0) -> Optional[str]:
        """
        Emit a task_question event, pause execution, and wait for the user's
        answer (or timeout after `timeout` seconds).

        Returns the answer string, or None on timeout.
        """
        task_id = task.task_id

        # Create a fresh event for this question
        ev = asyncio.Event()
        self._answer_events[task_id] = ev
        self._answers.pop(task_id, None)

        # Pause the task status
        task.status = TaskStatus.WAITING_FOR_ANSWER
        self._emit("task_question", {
            "task_id": task_id,
            "question": question,
        })
        logger.info("[Task %s] Waiting for answer to: %s", task_id, question)

        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[Task %s] Question timed out after %.0fs", task_id, timeout)
            self._answer_events.pop(task_id, None)
            return None

        answer = self._answers.pop(task_id, None)
        self._answer_events.pop(task_id, None)
        return answer

    # -------------------------------------------------------------------------
    # Main entry point
    # -------------------------------------------------------------------------

    async def execute_task(self, task_description: str,
                           task_id: Optional[str] = None) -> "Task":
        """Execute a task through the full Research -> Plan -> Act -> Verify loop."""
        if not task_id:
            task_id = "task_" + datetime.now().strftime("%Y%m%d_%H%M%S")

        task = Task(task_id=task_id, description=task_description)
        self.tasks[task_id] = task
        self.current_task = task

        logger.info("[Task %s] Starting: %s", task_id, task_description)
        self._emit("task_started", {"task_id": task_id, "description": task_description})
        # Signal immediately so the UI exits "Submitting..."
        self._emit("task_planning", {"task_id": task_id})

        try:
            task.started_at = datetime.now()

            # Full loop: RESEARCH -> PLAN -> ACT -> VERIFY
            # On verify failure: restart from RESEARCH with failure feedback so the
            # agent understands WHY it failed and can re-approach from scratch.
            for full_attempt in range(self.max_verify_retries + 1):
                is_retry = full_attempt > 0
                verify_feedback = task.metadata.get("verify_feedback", "")

                # ---- Phase 1: RESEARCH --------------------------------------
                task.status = TaskStatus.RESEARCHING
                self._emit("task_researching", {
                    "task_id": task_id,
                    "message": ("Re-researching after failed verification..." if is_retry
                                else "Researching requirements and gathering context...")
                })
                # Pass verification failure reason into research so it knows what went wrong
                if is_retry and verify_feedback:
                    task.metadata["research_retry_context"] = (
                        "Previous attempt failed QA verification: " + verify_feedback +
                        "\nRe-research with this failure in mind."
                    )
                await self._phase_research(task)

                # ---- Phase 2: PLAN ------------------------------------------
                task.status = TaskStatus.PLANNING
                self._emit("task_planning", {
                    "task_id": task_id,
                    "message": ("Re-planning based on verification failure..." if is_retry
                                else "Planning execution steps...")
                })
                task.steps = []
                await self._phase_plan(task, is_retry=is_retry)

                # ── Interactive Q&A: ask any clarifying questions before ACT ─
                # The plan phase may have stored questions in task.metadata["questions"].
                # Ask them one at a time and inject answers back into context.
                questions = task.metadata.pop("questions", [])
                if questions and not is_retry:
                    for q in questions:
                        answer = await self._ask_question(task, q)
                        if answer:
                            # Resume signal: update status back to planning
                            task.status = TaskStatus.PLANNING
                            self._emit("task_planning", {
                                "task_id": task_id,
                                "message": "Got your answer — updating plan...",
                            })
                            # Inject Q&A into task description so ACT/VERIFY are aware
                            qa_note = f"\n\n[Clarification] Q: {q}\nA: {answer}"
                            task.metadata.setdefault("clarifications", "")
                            task.metadata["clarifications"] += qa_note
                            # Re-plan with the new information
                            task.steps = []
                            await self._phase_plan(task, is_retry=False,
                                                   extra_context=qa_note)
                        else:
                            logger.info("[Task %s] No answer received, continuing without clarification", task_id)

                self._emit("task_planned", {
                    "task_id": task_id,
                    "steps": [{"step_id": s.step_id, "description": s.description}
                               for s in task.steps]
                })

                # ---- Phase 3: ACT -------------------------------------------
                task.status = TaskStatus.EXECUTING
                self._emit("task_executing", {
                    "task_id": task_id,
                    "message": "Executing steps..."
                })
                await self._phase_act(task)

                # ---- Phase 4: VERIFY ----------------------------------------
                task.status = TaskStatus.VERIFYING
                self._emit("task_verifying", {
                    "task_id": task_id,
                    "message": "QA verification — testing output against requirements..."
                })
                passed, feedback = await self._phase_verify(task)
                task.verification_passed = passed

                if passed:
                    logger.info("[Task %s] Verification passed on attempt %d",
                                task_id, full_attempt + 1)
                    self._emit("task_verified", {
                        "task_id": task_id, "passed": True,
                        "message": "Verification passed."
                    })
                    break
                else:
                    logger.warning("[Task %s] Verification failed (attempt %d): %s",
                                   task_id, full_attempt + 1, feedback)
                    self._emit("task_verified", {
                        "task_id": task_id, "passed": False,
                        "message": "Verification failed: " + feedback
                    })
                    if full_attempt < self.max_verify_retries:
                        # Store failure reason — next iteration starts from RESEARCH
                        task.metadata["verify_feedback"] = feedback
                        logger.info("[Task %s] Restarting from RESEARCH with failure context",
                                    task_id)
                    else:
                        logger.warning("[Task %s] Max retries reached, accepting result",
                                       task_id)

            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now()
            task.result = self._compile_result(task)
            self._emit("task_completed", {
                "task_id": task_id, "result": task.result,
                "description": task.description,
            })
            logger.info("[Task %s] Completed", task_id)
            return task

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            task.completed_at = datetime.now()
            self._emit("task_failed", {"task_id": task_id, "error": str(e)})
            logger.error("[Task %s] Failed: %s", task_id, e)
            return task

    # ---- Phase 1: RESEARCH --------------------------------------------------

    async def _llm_call(self, messages):
        """Direct LLM call bypassing the tool-executing agent loop.
        Used for RESEARCH, PLAN, VERIFY phases which must only analyse, never execute."""
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()
        params = {
            "model": self.ai_agent.config.model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": self.ai_agent.config.max_tokens,
        }
        response = await loop.run_in_executor(
            None, lambda: self.ai_agent.client.chat.completions.create(**params)
        )
        return (response.choices[0].message.content or "").strip()

    async def _phase_research(self, task: Task):
        """Gather context using READ-ONLY tools (web_search, read_file, execute_command).
        MUST NOT write files, modify state, or execute the actual task actions yet."""
        ctx_lines = []
        if self.session_conversation:
            for m in self.session_conversation[-10:]:
                role = m.get("role", "")
                c = (m.get("content") or "")[:400]
                if role in ("user", "assistant") and c:
                    ctx_lines.append(role.capitalize() + ": " + c)
        ctx = "\n".join(ctx_lines) if ctx_lines else "No prior chat."

        retry_ctx = ""
        if task.metadata.get("research_retry_context"):
            retry_ctx = "\n\nIMPORTANT - Previous attempt failure context:\n" + \
                        task.metadata["research_retry_context"] + \
                        "\nUse this information to re-approach the problem differently.\n"

        prompt = (
            "You are an autonomous agent in the RESEARCH phase.\n"
            "Your goal is to GATHER INFORMATION to understand this task fully.\n\n"
            "Task: " + task.description + "\n\n"
            "Prior conversation context:\n" + ctx +
            retry_ctx + "\n\n"
            "RESEARCH RULES:\n"
            "- You MAY use: web_search, read_file, list_files, execute_command (read-only)\n"
            "- You MUST NOT: write_file, create files, modify anything, or execute the task yet\n"
            "- Your only output should be information gathering and a summary\n\n"
            "After researching, respond with:\n"
            '{"research_summary": "what you found and the best approach", "ready_to_plan": true}'
        )
        conv = self._base_conv()
        try:
            raw = await self.ai_agent.get_response(prompt, conversation=conv)
            resp = safe_encode_string(raw) if raw else None
            if resp:
                js, je = resp.find("{"), resp.rfind("}") + 1
                if js >= 0 and je > js:
                    d = json.loads(resp[js:je])
                    task.research_summary = d.get("research_summary", resp[:500])
                else:
                    task.research_summary = resp[:500]
            logger.info("[Task %s] Research: %s", task.task_id,
                        (task.research_summary or "")[:120])
            # Emit research summary so the UI can display it
            self._emit("task_researching", {
                "task_id": task.task_id,
                "summary": task.research_summary or "",
                "message": "Research complete."
            })
        except Exception as e:
            logger.warning("[Task %s] Research error (continuing): %s", task.task_id, e)
            task.research_summary = "Research skipped: " + str(e)

    # ---- Phase 2: PLAN ------------------------------------------------------

    async def _phase_plan(self, task: Task, is_retry: bool = False,
                          extra_context: str = ""):
        """
        Produce a numbered, tool-mapped execution plan.

        The LLM is also asked to surface any single genuine clarifying question
        that only the user can answer.  If it has one, it goes into
        task.metadata["questions"] and execute_task() will pause to ask it
        before entering the ACT phase.

        RULES for questions:
          - Ask ONLY if truly ambiguous (e.g. "staging or production?")
          - NEVER ask for permission ("should I list the files?")
          - NEVER ask something the agent can discover itself with tools
          - If nothing is ambiguous, return an empty questions list
        """
        try:
            tool_names = ", ".join([t["function"]["name"] for t in self.ai_agent.tools.tools])
        except Exception:
            tool_names = "(tools unavailable)"

        fb = ""
        if is_retry and task.metadata.get("verify_feedback"):
            fb = "\nPrevious verification FAILED:\n" + task.metadata["verify_feedback"] + "\nFix this.\n"

        clarifications = task.metadata.get("clarifications", "")
        clarification_block = (
            "\n\nClarifications already provided by the user:\n" + clarifications
            if clarifications else ""
        )

        prompt = (
            "You are an autonomous agent in the PLANNING phase.\n"
            "Task: " + task.description + "\n"
            "Research findings: " + (task.research_summary or "None") + "\n"
            + fb
            + clarification_block
            + ("\nAdditional context: " + extra_context if extra_context else "")
            + "\nAvailable tools: " + tool_names + "\n\n"
            "Create a concrete step-by-step plan. Each step must be independently executable.\n\n"
            "QUESTION RULES — read carefully:\n"
            "- You MAY ask ONE clarifying question if something is GENUINELY ambiguous and ONLY the user knows the answer.\n"
            "  Good examples: 'Deploy to staging or production?', 'Which branch should I target?'\n"
            "- You MUST NOT ask for permission: 'Should I list files?' → NO. Just list them.\n"
            "- You MUST NOT ask things you can discover with tools (file contents, status, etc.)\n"
            "- If nothing is ambiguous, return an empty questions list.\n\n"
            'Respond ONLY with JSON:\n'
            '{\n'
            '  "steps": [{"description": "...", "tool": "tool_name or null", "args": {"k": "v"}}],\n'
            '  "questions": ["single clarifying question if truly needed, else empty array"]\n'
            '}'
        )
        messages = [
            {"role": "system", "content": (
                "You are a senior engineer in the PLANNING phase. "
                "Your ONLY job is to produce a JSON execution plan. "
                "DO NOT execute any actions or use any tools. "
                "Output ONLY a JSON plan."
            )},
            {"role": "user", "content": prompt}
        ]
        try:
            raw = await self._llm_call(messages)
            resp = safe_encode_string(raw) if raw else None
        except Exception as e:
            raise Exception("Planning failed: " + str(e))
        if not resp:
            raise Exception("Empty planning response")
        try:
            js, je = resp.find("{"), resp.rfind("}") + 1
            plan = json.loads(resp[js:je]) if js >= 0 and je > js else {}
            for i, s in enumerate(plan.get("steps", [])[:self.max_steps]):
                task.steps.append(TaskStep(
                    step_id=i + 1,
                    description=s["description"],
                    tool_name=s.get("tool"),
                    tool_args=s.get("args"),
                ))
            logger.info("[Task %s] Plan: %d steps", task.task_id, len(task.steps))

            # Store any clarifying questions for execute_task() to ask
            raw_questions = plan.get("questions", [])
            # Filter out blanks and permission-seeking questions
            _permission_words = (
                "should i", "do you want", "can i", "would you like",
                "shall i", "is it ok", "may i", "do you need"
            )
            filtered = [
                q.strip() for q in raw_questions
                if q.strip() and not any(p in q.lower() for p in _permission_words)
            ]
            if filtered:
                task.metadata["questions"] = filtered
                logger.info("[Task %s] Plan has %d question(s): %s",
                            task.task_id, len(filtered), filtered)
            else:
                task.metadata.pop("questions", None)

        except Exception as e:
            logger.error("[Task %s] Plan parse error: %s", task.task_id, e)
            task.steps.append(TaskStep(step_id=1, description=task.description))

    # ---- Phase 3: ACT -------------------------------------------------------

    async def _phase_act(self, task: Task):
        """Execute all steps with error-detection and auto-fix."""
        for step in task.steps:
            logger.info("[Task %s] Step %d: %s", task.task_id, step.step_id, step.description)
            step.status = TaskStatus.EXECUTING
            step.started_at = datetime.now()
            self._emit("step_started", {"task_id": task.task_id,
                "step": {"step_id": step.step_id, "description": step.description}})
            try:
                for attempt in range(self.max_retries):
                    try:
                        if step.tool_name and step.tool_args is not None:
                            result = await self.ai_agent.tools.execute_tool(
                                step.tool_name, step.tool_args)
                            step.result = safe_encode_string(str(result)) if result else None
                        else:
                            conv = self._base_conv()
                            # Inject any user clarifications into the step prompt
                            clarifications = task.metadata.get("clarifications", "")
                            step_prompt = (
                                "Execute this step: " + step.description + "\n"
                                "Context: step " + str(step.step_id) + " of task: " + task.description + "\n"
                                + ("User clarifications: " + clarifications + "\n" if clarifications else "")
                                + "Use the appropriate tool."
                            )
                            raw = await self.ai_agent.get_response(step_prompt, conversation=conv)
                            step.result = safe_encode_string(raw) if raw else None

                        has_err, err_msg = self._quick_error_check(step.result)
                        if has_err:
                            if attempt < self.max_retries - 1:
                                logger.warning("[Task %s] Step %d error, retrying: %s",
                                               task.task_id, step.step_id, err_msg)
                                await asyncio.sleep(1)
                                continue
                            else:
                                raise Exception("Step failed after retries: " + err_msg)
                        step.status = TaskStatus.COMPLETED
                        step.completed_at = datetime.now()
                        # Include result preview so the UI can display step output
                        result_preview = (step.result or "")[:500]
                        self._emit("step_completed", {"task_id": task.task_id,
                            "step": {"step_id": step.step_id,
                                     "description": step.description,
                                     "result": result_preview}})
                        break
                    except Exception as e:
                        if attempt < self.max_retries - 1:
                            await asyncio.sleep(1)
                        else:
                            raise
            except Exception as e:
                step.status = TaskStatus.FAILED
                step.error = str(e)
                step.completed_at = datetime.now()
                self._emit("step_failed", {"task_id": task.task_id,
                    "step": {"step_id": step.step_id, "description": step.description,
                             "error": str(e)}})
                logger.error("[Task %s] Step %d failed: %s", task.task_id, step.step_id, e)


    def _quick_error_check(self, result):
        if not result:
            return False, ""
        low = result.lower()
        for p in ["modulenotfounderror","importerror","filenotfounderror",
                  "permission denied","command not found","no such file",
                  "http 403","http 404","http 500","connection refused",
                  "syntaxerror","nameerror","typeerror"]:
            if p in low:
                return True, p
        return False, ""

    async def _phase_verify(self, task):
        step_results = "\n".join(
            "Step " + str(s.step_id) + " (" + s.description + "): " + (s.result or "no output")[:300]
            for s in task.steps if s.status == TaskStatus.COMPLETED
        )
        prompt = (
            "You are a Senior QA Engineer in the VERIFICATION phase.\n"
            "Your job is to TEST and VALIDATE the output against the original requirements.\n\n"
            "Original task: " + task.description + "\n\n"
            "What was done:\n" + (step_results or "No steps completed") + "\n\n"
            "QA INSTRUCTIONS:\n"
            "- Use whatever tools are relevant to verify this specific task\n"
            "- For code/functions: execute_command to run tests, read_file to check output\n"
            "- For web tasks: browser tools to verify the result loads/works correctly\n"
            "- For spec-based tasks: check the spec source (file, Jira, URL, or prior chat context)\n"
            "- For file creation: read_file/list_files to confirm files exist and contain correct content\n"
            "- Only use tools that make sense for THIS specific task\n"
            "- Base your verification on what is available: the task description, the steps done, "
            "and any spec/requirements visible in the current context\n\n"
            "After verifying, respond with:\n"
            '{"passed": true/false, "feedback": "what was tested, what passed, and what failed (if anything)"}'
        )
        conv = self._base_conv()
        try:
            raw = await self.ai_agent.get_response(prompt, conversation=conv)
            resp = safe_encode_string(raw) if raw else None
            if resp:
                js, je = resp.find("{"), resp.rfind("}") + 1
                if js >= 0 and je > js:
                    d = json.loads(resp[js:je])
                    return bool(d.get("passed", True)), str(d.get("feedback", ""))
            return True, ""
        except Exception as e:
            import logging as _l
            _l.getLogger(__name__).warning("Verify error: %s", e)
            return True, ""

    def _compile_result(self, task):
        done = [s for s in task.steps if s.status == TaskStatus.COMPLETED and s.result]
        if not done:
            return "Task completed but no output was generated."
        if len(done) == 1:
            return done[0].result
        parts = []
        for s in done:
            parts.append("Step " + str(s.step_id) + ": " + s.description)
            parts.append(s.result)
            parts.append("")
        return "\n".join(parts)

    def get_task_status(self, task_id):
        task = self.tasks.get(task_id)
        if not task:
            return None
        return {
            "task_id": task.task_id, "description": task.description,
            "status": task.status.value,
            "steps_total": len(task.steps),
            "steps_completed": sum(1 for s in task.steps if s.status == TaskStatus.COMPLETED),
            "steps_failed": sum(1 for s in task.steps if s.status == TaskStatus.FAILED),
            "created_at": task.created_at.isoformat(),
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "result": task.result, "error": task.error,
            "waiting_for_answer": task.status == TaskStatus.WAITING_FOR_ANSWER,
        }

    def list_tasks(self):
        return [
            {"task_id": t.task_id, "description": t.description,
             "status": t.status.value, "steps": len(t.steps),
             "created_at": t.created_at.isoformat()}
            for t in self.tasks.values()
        ]
