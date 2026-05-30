#!/usr/bin/env python3
"""
Agent API - Programmatic Interface for Agent Operations
Provides a clean API for using the agent programmatically.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Callable
from pathlib import Path

from api.agent_executor import AgentExecutor, Task, TaskStatus

logger = logging.getLogger(__name__)


class AgentAPI:
    """
    High-level API for agent operations.
    
    This provides a clean interface for:
    - Running tasks programmatically
    - Monitoring task progress
    - Managing agent lifecycle
    - Handling callbacks and events
    """
    
    def __init__(self, ai_agent):
        """
        Initialize the agent API.
        
        Args:
            ai_agent: Initialized AIAgent instance
        """
        self.ai_agent = ai_agent
        self.executor = AgentExecutor(ai_agent)
        self.callbacks: Dict[str, List[Callable]] = {
            'task_started': [],
            'task_completed': [],
            'task_failed': [],
            'step_started': [],
            'step_completed': [],
            'step_failed': []
        }
    
    async def run_task(
        self,
        task_description: str,
        task_id: Optional[str] = None,
        on_progress: Optional[Callable[[str, Any], None]] = None
    ) -> Dict[str, Any]:
        """
        Run a task and return the result.
        
        Args:
            task_description: What the agent should do
            task_id: Optional task ID
            on_progress: Optional callback for progress updates
            
        Returns:
            Dict with task results
        """
        logger.info(f"Running task: {task_description}")
        
        # Execute task
        task = await self.executor.execute_task(task_description, task_id)
        
        # Format result
        result = {
            'success': task.status == TaskStatus.COMPLETED,
            'task_id': task.task_id,
            'description': task.description,
            'status': task.status.value,
            'steps_completed': sum(1 for s in task.steps if s.status == TaskStatus.COMPLETED),
            'steps_total': len(task.steps),
            'result': task.result,
            'error': task.error,
            'duration_seconds': (
                (task.completed_at - task.started_at).total_seconds()
                if task.completed_at and task.started_at else None
            )
        }
        
        return result
    
    async def run_task_async(
        self,
        task_description: str,
        task_id: Optional[str] = None
    ) -> str:
        """
        Start a task asynchronously and return task ID immediately.
        
        Args:
            task_description: What the agent should do
            task_id: Optional task ID
            
        Returns:
            Task ID for tracking
        """
        # Create task ID if not provided
        from datetime import datetime
        if not task_id:
            task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # Start task in background
        asyncio.create_task(self.executor.execute_task(task_description, task_id))
        
        return task_id
    
    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Get current status of a task.
        
        Args:
            task_id: Task ID
            
        Returns:
            Task status dict or None
        """
        return self.executor.get_task_status(task_id)
    
    def get_task_report(self, task_id: str) -> Optional[str]:
        """
        Get detailed report for a task.
        
        Args:
            task_id: Task ID
            
        Returns:
            Formatted report or None
        """
        return self.executor.get_task_report(task_id)
    
    def list_tasks(self) -> List[Dict[str, Any]]:
        """
        List all tasks.
        
        Returns:
            List of task summaries
        """
        return self.executor.list_tasks()
    
    async def run_workflow(
        self,
        tasks: List[str],
        sequential: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Run multiple tasks as a workflow.
        
        Args:
            tasks: List of task descriptions
            sequential: If True, run tasks one after another. If False, run in parallel.
            
        Returns:
            List of task results
        """
        if sequential:
            results = []
            for task_desc in tasks:
                result = await self.run_task(task_desc)
                results.append(result)
                
                # Stop if a task fails
                if not result['success']:
                    logger.warning(f"Workflow stopped due to task failure: {task_desc}")
                    break
            
            return results
        else:
            # Run all tasks in parallel
            tasks_coros = [self.run_task(task_desc) for task_desc in tasks]
            results = await asyncio.gather(*tasks_coros, return_exceptions=True)
            
            # Convert exceptions to error results
            formatted_results = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    formatted_results.append({
                        'success': False,
                        'task_id': f'task_{i}',
                        'description': tasks[i],
                        'error': str(result)
                    })
                else:
                    formatted_results.append(result)
            
            return formatted_results
    
    def register_callback(self, event: str, callback: Callable):
        """
        Register a callback for agent events.
        
        Args:
            event: Event name (task_started, task_completed, etc.)
            callback: Callback function
        """
        if event in self.callbacks:
            self.callbacks[event].append(callback)
    
    def _trigger_callback(self, event: str, *args, **kwargs):
        """Trigger all callbacks for an event"""
        for callback in self.callbacks.get(event, []):
            try:
                callback(*args, **kwargs)
            except Exception as e:
                logger.error(f"Callback error for {event}: {e}")


class AgentBuilder:
    """
    Builder pattern for creating and configuring agents.
    """
    
    def __init__(self):
        self.config = None
        self.ai_agent = None
        self.api = None
    
    def with_config(self, config):
        """Set configuration"""
        self.config = config
        return self
    
    def with_env_file(self, env_file: str = ".env"):
        """Load configuration from env file"""
        from main import Config
        self.config = Config()
        return self
    
    async def build(self) -> AgentAPI:
        """
        Build and initialize the agent.
        
        Returns:
            Initialized AgentAPI
        """
        if not self.config:
            from main import Config
            self.config = Config()
        
        if not self.config.validate():
            raise ValueError("Invalid configuration: API key required")
        
        # Create and initialize AI agent
        from main import AIAgent
        self.ai_agent = AIAgent(self.config)
        
        if not await self.ai_agent.initialize():
            raise RuntimeError("Failed to initialize AI agent")
        
        # Create API
        self.api = AgentAPI(self.ai_agent)
        
        logger.info("Agent built and initialized successfully")
        return self.api
    
    @staticmethod
    async def quick_start() -> AgentAPI:
        """
        Quick start with default configuration.
        
        Returns:
            Initialized AgentAPI
        """
        builder = AgentBuilder()
        return await builder.with_env_file().build()


# Convenience functions for simple use cases

async def run_agent_task(task_description: str) -> Dict[str, Any]:
    """
    Quick function to run a single task with default configuration.
    
    Args:
        task_description: What the agent should do
        
    Returns:
        Task result dict
    """
    api = await AgentBuilder.quick_start()
    return await api.run_task(task_description)


async def run_agent_workflow(tasks: List[str], sequential: bool = True) -> List[Dict[str, Any]]:
    """
    Quick function to run a workflow with default configuration.
    
    Args:
        tasks: List of task descriptions
        sequential: Run tasks sequentially or in parallel
        
    Returns:
        List of task results
    """
    api = await AgentBuilder.quick_start()
    return await api.run_workflow(tasks, sequential)

