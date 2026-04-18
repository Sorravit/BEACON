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
    
    def __init__(self, ai_agent, max_steps: int = 20, max_retries: int = 3):
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
        
        try:
            # Phase 1: Planning
            task.started_at = datetime.now()
            await self._plan_task(task)
            
            # Phase 2: Execution
            task.status = TaskStatus.EXECUTING
            await self._execute_steps(task)
            
            # Phase 3: Completion
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now()
            
            # Compile results from all steps
            task.result = self._compile_task_result(task)
            
            logger.info(f"Task {task_id} completed successfully")
            return task
            
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            task.completed_at = datetime.now()
            logger.error(f"Task {task_id} failed: {e}")
            return task
    
    async def _plan_task(self, task: Task):
        """
        Create an execution plan for the task using the AI.
        
        Args:
            task: Task to plan
        """
        planning_prompt = f"""You are an autonomous agent planning a task. Break down this task into concrete, executable steps.

Task: {task.description}

Available tools: {', '.join([t['function']['name'] for t in self.ai_agent.tools.tools])}

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

        # Get plan from AI
        response = await self.ai_agent.get_response(planning_prompt)
        
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
        Execute all steps in the task plan.
        
        Args:
            task: Task with steps to execute
        """
        for step in task.steps:
            logger.info(f"Executing step {step.step_id}: {step.description}")
            
            step.status = TaskStatus.EXECUTING
            step.started_at = datetime.now()
            
            try:
                # Execute step with retries
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
                            result = await self.ai_agent.get_response(
                                f"Execute this step: {step.description}\n\n"
                                f"Context: This is step {step.step_id} of task '{task.description}'\n"
                                f"Use the appropriate tool to complete this step."
                            )
                            step.result = result
                        
                        step.status = TaskStatus.COMPLETED
                        step.completed_at = datetime.now()
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