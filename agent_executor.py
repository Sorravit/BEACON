#!/usr/bin/env python3
"""
Agent Executor - Autonomous Task Execution Engine
Transforms the chat-based AI into a true autonomous agent.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

def safe_encode_string(text: str, errors: str = 'replace') -> str:
    """
    Safely encode string to handle UTF-8 surrogate characters.
    Fixes: 'utf-8' codec can't encode characters - surrogates not allowed
    
    Args:
        text: String that may contain invalid UTF-8 characters
        errors: How to handle encoding errors ('replace', 'ignore', 'strict')
                
    Returns:
        str: Safely encoded string without surrogate characters
    """
    if not text:
        return text
    try:
        return text.encode('utf-8', errors=errors).decode('utf-8')
    except Exception as e:
        logger.warning(f"Failed to encode string safely: {e}")
        try:
            return str(text).encode('utf-8', errors='ignore').decode('utf-8')
        except:
            return "[Invalid UTF-8 content]"



class TaskStatus(Enum):
    """Status of a task execution"""
    PENDING = "pending"
    PLANNING = "planning"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskStep:
    """Represents a single step in task execution"""
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
    """Represents a complete task with execution plan"""
    task_id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    steps: List[TaskStep] = field(default_factory=list)
    result: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class AgentExecutor:
    """
    Autonomous agent executor that can plan and execute multi-step tasks.
    
    This transforms the chat-based AI into a true agent that can:
    - Accept task descriptions
    - Create execution plans
    - Execute steps autonomously
    - Track progress and results
    - Handle errors and retries
    """
    
    def __init__(self, ai_agent, max_steps: int = 20, max_retries: int = 3, step_callback=None):
        """
        Initialize the agent executor.
        
        Args:
            ai_agent: The AIAgent instance with tools
            max_steps: Maximum steps per task
            max_retries: Maximum retries per step
        """
        self.ai_agent = ai_agent
        self.max_steps = max_steps
        self.max_retries = max_retries
        self.tasks: Dict[str, Task] = {}
        self.current_task: Optional[Task] = None
        self.step_callback = step_callback
        
    def _emit(self, event, data):
        if self.step_callback:
            try:
                self.step_callback(event, data)
            except Exception:
                pass

    async def execute_task(self, task_description: str, task_id: Optional[str] = None) -> Task:
        """
        Execute a task autonomously from start to finish.
        
        Args:
            task_description: Natural language description of the task
            task_id: Optional task ID (auto-generated if not provided)
            
        Returns:
            Task: Completed task with results
        """
        # Create task
        if not task_id:
            task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        task = Task(
            task_id=task_id,
            description=task_description,
            status=TaskStatus.PLANNING
        )
        self.tasks[task_id] = task
        self.current_task = task
        
        logger.info(f"Starting task {task_id}: {task_description}")
        self._emit('task_started', {'task_id': task_id, 'description': task_description})
        # Immediately signal that planning is underway so the UI exits "Submitting…"
        self._emit('task_planning', {'task_id': task_id})
        
        try:
            # Phase 1: Planning
            task.started_at = datetime.now()
            await self._plan_task(task)
            self._emit('task_planned', {'task_id': task_id, 'steps': [{'step_id': s.step_id, 'description': s.description} for s in task.steps]})
            
            # Phase 2: Execution
            task.status = TaskStatus.EXECUTING
            await self._execute_steps(task)
            
            # Phase 3: Completion
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now()
            
            # Compile results from all steps
            task.result = self._compile_task_result(task)
            
            self._emit('task_completed', {'task_id': task_id, 'result': task.result})
            logger.info(f"Task {task_id} completed successfully")
            return task
            
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            task.completed_at = datetime.now()
            self._emit('task_failed', {'task_id': task_id, 'error': str(e)})
            logger.error(f"Task {task_id} failed: {e}")
            return task
    
    async def _plan_task(self, task: Task):
        """
        Create an execution plan for the task using the AI.
        
        Args:
            task: Task to plan
        """
        # Get available tool names safely — tools may not be initialised yet
        try:
            tool_names = ', '.join([t['function']['name'] for t in self.ai_agent.tools.tools])
        except Exception:
            tool_names = '(tools unavailable at planning time)'

        planning_prompt = f"""You are an autonomous agent planning a task. Break down this task into concrete, executable steps.

Task: {task.description}

Available tools: {tool_names}

Create a step-by-step plan. For each step:
1. Describe what needs to be done
2. Identify which tool to use (if any)
3. Specify the tool arguments

Respond in JSON format:
{{
    "steps": [
        {{
            "description": "Step description",
            "tool": "tool_name or null",
            "args": {{"arg1": "value1"}} or null
        }}
    ]
}}

Be specific and actionable. Each step should be independently executable."""

        # Get plan from AI with encoding safety
        try:
            raw_response = await self.ai_agent.get_response(planning_prompt)
            response = safe_encode_string(raw_response) if raw_response else None
        except Exception as e:
            logger.error(f"Error getting planning response: {e}")
            raise Exception(f"Failed to get planning response: {e}")
        
        if not response:
            raise Exception("Failed to generate task plan")
        
        # Parse plan
        try:
            # Extract JSON from response
            json_start = response.find('{')
            json_end = response.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                plan_json = response[json_start:json_end]
                plan = json.loads(plan_json)
            else:
                raise ValueError("No JSON found in response")
            
            # Create task steps
            for i, step_data in enumerate(plan.get('steps', [])[:self.max_steps]):
                step = TaskStep(
                    step_id=i + 1,
                    description=step_data['description'],
                    tool_name=step_data.get('tool'),
                    tool_args=step_data.get('args')
                )
                task.steps.append(step)
            
            logger.info(f"Created plan with {len(task.steps)} steps")
            
        except Exception as e:
            logger.error(f"Failed to parse plan: {e}")
            # Fallback: create single step
            task.steps.append(TaskStep(
                step_id=1,
                description=task.description,
                tool_name=None,
                tool_args=None
            ))
    
    async def _execute_steps(self, task: Task):
        """
        Execute all steps in the task plan with intelligent error detection and recovery.
        
        Args:
            task: Task with steps to execute
        """
        for step in task.steps:
            logger.info(f"Executing step {step.step_id}: {step.description}")
            
            step.status = TaskStatus.EXECUTING
            step.started_at = datetime.now()
            self._emit('step_started', {'task_id': task.task_id, 'step': {'step_id': step.step_id, 'description': step.description, 'status': 'executing'}})
            
            try:
                # Execute step with intelligent retries
                for attempt in range(self.max_retries):
                    try:
                        if step.tool_name and step.tool_args:
                            # Execute tool directly when specified in plan
                            result = await self.ai_agent.tools.execute_tool(
                                step.tool_name,
                                step.tool_args
                            )
                            step.result = result
                        else:
                            # Let AI choose and execute the appropriate tool using function calling
                            raw_result = await self.ai_agent.get_response(
                                f"Execute this step: {step.description}\n\n"
                                f"Context: This is step {step.step_id} of task '{task.description}'\n"
                                f"Use the appropriate tool to complete this step."
                            )
                            step.result = safe_encode_string(raw_result) if raw_result else None
                        
                        # CRITICAL NEW FEATURE: Analyze the result for errors
                        has_error, error_analysis = await self._analyze_step_result(step, task)
                        
                        if has_error:
                            logger.warning(f"Step {step.step_id} completed but output contains errors: {error_analysis}")
                            
                            if attempt < self.max_retries - 1:
                                # Try to fix the error automatically
                                logger.info(f"Attempting to fix error automatically (attempt {attempt + 1}/{self.max_retries})")
                                fix_applied = await self._attempt_error_fix(step, error_analysis, task)
                                
                                if fix_applied:
                                    logger.info(f"Fix applied, retrying step {step.step_id}")
                                    await asyncio.sleep(1)
                                    continue  # Retry the step after applying fix
                                else:
                                    logger.warning(f"Could not apply automatic fix, treating as failure")
                                    raise Exception(f"Step produced errors that could not be fixed: {error_analysis}")
                            else:
                                raise Exception(f"Step failed after {self.max_retries} attempts: {error_analysis}")
                        
                        # No errors detected, mark as completed
                        step.status = TaskStatus.COMPLETED
                        step.completed_at = datetime.now()
                        logger.info(f"Step {step.step_id} completed successfully")
                        self._emit('step_completed', {'task_id': task.task_id, 'step': {'step_id': step.step_id, 'description': step.description, 'status': 'completed'}})
                        break
                        
                    except Exception as e:
                        if attempt < self.max_retries - 1:
                            logger.warning(f"Step {step.step_id} attempt {attempt + 1} failed: {e}")
                            await asyncio.sleep(1)
                        else:
                            raise
                
            except Exception as e:
                step.status = TaskStatus.FAILED
                step.error = str(e)
                step.completed_at = datetime.now()
                logger.error(f"Step {step.step_id} failed: {e}")
                
                # Decide whether to continue or abort
                if self._is_critical_failure(step):
                    raise Exception(f"Critical step failed: {step.description}")
    
    async def _analyze_step_result(self, step: TaskStep, task: Task) -> Tuple[bool, str]:
        """
        Analyze the step result to detect if there are errors, even if the tool executed successfully.
        
        Args:
            step: The step that was just executed
            task: The parent task
            
        Returns:
            Tuple of (has_error: bool, error_analysis: str)
        """
        if not step.result:
            return False, ""
        
        # Safely encode result before analyzing
        try:
            safe_result = safe_encode_string(step.result)
            result_lower = safe_result.lower()
        except Exception as e:
            logger.error(f"Error encoding result for analysis: {e}")
            return True, f"Could not analyze result due to encoding error: {e}"
        
        # Quick pattern matching for common errors
        error_patterns = [
            "error:",
            "exception:",
            "traceback",
            "modulenotfounderror",
            "importerror",
            "filenotfound",
            "permission denied",
            "command not found",
            "no such file",
            "failed",
            "http 403",
            "http 404",
            "http 500",
            "http 502",
            "http 503",
            "connection refused",
            "timeout",
            "syntax error",
            "name error",
            "type error",
            "value error",
            "attribute error",
        ]
        
        # Check for error patterns
        has_error_pattern = any(pattern in result_lower for pattern in error_patterns)
        
        if not has_error_pattern:
            # No obvious error pattern, consider it successful
            return False, ""
        
        # Found error pattern, ask AI to analyze it
        logger.info(f"Error pattern detected in step {step.step_id}, asking AI to analyze...")
        
        analysis_prompt = f"""Analyze this command/script output and determine if it indicates an error that needs to be fixed.

Step Description: {step.description}
Tool Used: {step.tool_name or 'AI-selected tool'}
Output:
{safe_encode_string(step.result[:2000]) if step.result else "No output"}  

IMPORTANT: Look for actual errors that prevent the step from completing successfully.
- ModuleNotFoundError/ImportError = YES, error
- HTTP 403/404/500 errors = YES, error  
- "Failed" or "Error" messages = YES, error
- Successful execution messages = NO, not an error
- Informational warnings = NO, not an error

Respond in JSON format:
{{
    "has_error": true/false,
    "error_type": "type of error (e.g., 'missing_dependency', 'http_error', 'file_not_found', 'permission_error', 'syntax_error', etc.)",
    "error_description": "brief description of the error",
    "suggested_fix": "what needs to be done to fix it"
}}
"""
        
        try:
            raw_analysis = await self.ai_agent.get_response(analysis_prompt)
            analysis_response = safe_encode_string(raw_analysis) if raw_analysis else None
            
            if not analysis_response:
                return True, "Could not get analysis response"
            
            # Parse JSON response
            json_start = analysis_response.find('{')
            json_end = analysis_response.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                analysis_json = analysis_response[json_start:json_end]
                analysis = json.loads(analysis_json)
                
                if analysis.get('has_error', False):
                    error_desc = f"{analysis.get('error_type', 'unknown')}: {analysis.get('error_description', 'No description')}"
                    logger.info(f"AI confirmed error in step {step.step_id}: {error_desc}")
                    return True, error_desc
                else:
                    logger.info(f"AI determined no actionable error in step {step.step_id}")
                    return False, ""
            else:
                # Couldn't parse JSON, fall back to pattern matching result
                logger.warning(f"Could not parse AI analysis, using pattern matching result")
                return True, "Error detected in output (pattern matching)"
                
        except Exception as e:
            logger.error(f"Error during result analysis: {e}")
            # If analysis fails, be conservative and report error
            return True, f"Error detected in output: {str(e)}"
    
    async def _attempt_error_fix(self, step: TaskStep, error_analysis: str, task: Task) -> bool:
        """
        Attempt to automatically fix the error detected in the step.
        
        Args:
            step: The step that failed
            error_analysis: Description of the error
            task: The parent task
            
        Returns:
            bool: True if fix was applied, False if couldn't fix
        """
        logger.info(f"Attempting to generate fix for: {error_analysis}")
        
        fix_prompt = f"""You are an autonomous agent that can fix errors. An error was detected during task execution.

Original Task: {task.description}
Failed Step: {step.description}
Tool Used: {step.tool_name or 'AI-selected tool'}
Error Analysis: {error_analysis}
Step Output:
{safe_encode_string(step.result[:1500]) if step.result else "No output"}

Your job is to fix this error automatically. Common fixes:
- ModuleNotFoundError → Install the missing module with pip
- HTTP 403/404 errors → Try different approach (browser automation instead of HTTP)
- File not found → Create the file or fix the path
- Permission denied → Adjust permissions
- Syntax errors → Fix the code

Generate a fix action. Respond in JSON format:
{{
    "can_fix": true/false,
    "fix_action": "tool_name to use (e.g., 'execute_command', 'write_file', etc.)",
    "fix_args": {{"arg1": "value1"}},
    "fix_description": "what this fix does"
}}

If you cannot fix it automatically, set can_fix to false.
"""
        
        try:
            raw_fix_response = await self.ai_agent.get_response(fix_prompt)
            fix_response = safe_encode_string(raw_fix_response) if raw_fix_response else None
            
            if not fix_response:
                logger.warning("Could not get fix response")
                return False
            
            # Parse JSON response
            json_start = fix_response.find('{')
            json_end = fix_response.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                fix_json = fix_response[json_start:json_end]
                fix_plan = json.loads(fix_json)
                
                if not fix_plan.get('can_fix', False):
                    logger.info(f"AI determined error cannot be fixed automatically")
                    return False
                
                # Apply the fix
                fix_action = fix_plan.get('fix_action')
                fix_args = fix_plan.get('fix_args', {})
                fix_desc = fix_plan.get('fix_description', 'No description')
                
                logger.info(f"Applying fix: {fix_desc}")
                logger.info(f"Fix action: {fix_action}({fix_args})")
                
                # Execute the fix
                if fix_action and self.ai_agent.tools:
                    fix_result = await self.ai_agent.tools.execute_tool(fix_action, fix_args)
                    safe_fix_result = safe_encode_string(str(fix_result)[:200]) if fix_result else "No result"
                    logger.info(f"Fix applied. Result: {safe_fix_result}")
                    return True
                else:
                    logger.warning(f"Fix action not available: {fix_action}")
                    return False
            else:
                logger.warning(f"Could not parse fix plan JSON")
                return False
                
        except Exception as e:
            logger.error(f"Error during fix attempt: {e}")
            return False
    
    def _compile_task_result(self, task: Task) -> str:
        """
        Compile results from all completed steps into a final task result.
        
        Args:
            task: Task with completed steps
            
        Returns:
            Compiled result string
        """
        completed_steps = [s for s in task.steps if s.status == TaskStatus.COMPLETED and s.result]
        
        if not completed_steps:
            return "Task completed but no results were generated."
        
        if len(completed_steps) == 1:
            # Single step - return its result directly
            return completed_steps[0].result
        
        # Multiple steps - compile all results
        result_parts = []
        for step in completed_steps:
            result_parts.append(f"Step {step.step_id}: {step.description}")
            result_parts.append(f"{step.result}")
            result_parts.append("")  # Empty line between steps
        
        return "\n".join(result_parts)
    
    def _is_critical_failure(self, step: TaskStep) -> bool:
        """
        Determine if a step failure is critical enough to abort the task.
        
        Args:
            step: Failed step
            
        Returns:
            bool: True if failure is critical
        """
        # For now, treat all failures as non-critical
        # In production, this could be more sophisticated
        return False
    
    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the current status of a task.
        
        Args:
            task_id: Task ID
            
        Returns:
            Dict with task status or None if not found
        """
        task = self.tasks.get(task_id)
        if not task:
            return None
        
        return {
            'task_id': task.task_id,
            'description': task.description,
            'status': task.status.value,
            'steps_total': len(task.steps),
            'steps_completed': sum(1 for s in task.steps if s.status == TaskStatus.COMPLETED),
            'steps_failed': sum(1 for s in task.steps if s.status == TaskStatus.FAILED),
            'created_at': task.created_at.isoformat(),
            'started_at': task.started_at.isoformat() if task.started_at else None,
            'completed_at': task.completed_at.isoformat() if task.completed_at else None,
            'result': task.result,
            'error': task.error
        }
    
    def get_task_report(self, task_id: str) -> Optional[str]:
        """
        Generate a detailed report for a task.
        
        Args:
            task_id: Task ID
            
        Returns:
            Formatted report string or None if not found
        """
        task = self.tasks.get(task_id)
        if not task:
            return None
        
        report = []
        report.append(f"Task Report: {task.task_id}")
        report.append("=" * 60)
        report.append(f"Description: {task.description}")
        report.append(f"Status: {task.status.value}")
        report.append(f"Created: {task.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
        
        if task.started_at:
            report.append(f"Started: {task.started_at.strftime('%Y-%m-%d %H:%M:%S')}")
        
        if task.completed_at:
            report.append(f"Completed: {task.completed_at.strftime('%Y-%m-%d %H:%M:%S')}")
            if task.started_at:
                duration = (task.completed_at - task.started_at).total_seconds()
                report.append(f"Duration: {duration:.2f} seconds")
        
        report.append(f"\nSteps: {len(task.steps)}")
        report.append("-" * 60)
        
        for step in task.steps:
            report.append(f"\nStep {step.step_id}: {step.description}")
            report.append(f"  Status: {step.status.value}")
            if step.tool_name:
                report.append(f"  Tool: {step.tool_name}")
            if step.result:
                result_preview = step.result[:200] + "..." if len(step.result) > 200 else step.result
                report.append(f"  Result: {result_preview}")
            if step.error:
                report.append(f"  Error: {step.error}")
        
        if task.result:
            report.append(f"\nFinal Result:")
            report.append("-" * 60)
            report.append(task.result)
        
        if task.error:
            report.append(f"\nError:")
            report.append("-" * 60)
            report.append(task.error)
        
        return "\n".join(report)
    
    def list_tasks(self) -> List[Dict[str, Any]]:
        """
        List all tasks.
        
        Returns:
            List of task summaries
        """
        return [
            {
                'task_id': task.task_id,
                'description': task.description,
                'status': task.status.value,
                'steps': len(task.steps),
                'created_at': task.created_at.isoformat()
            }
            for task in self.tasks.values()
        ]