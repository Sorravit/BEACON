#!/usr/bin/env python3
"""
Agent Memory - State and Context Management
Provides memory and context management for complex multi-step tasks.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """A single memory entry"""
    timestamp: datetime
    key: str
    value: Any
    context: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class AgentMemory:
    """
    Memory system for agents to maintain state across tasks.
    
    Features:
    - Short-term memory (current session)
    - Long-term memory (persistent across sessions)
    - Context tracking
    - Knowledge base
    """
    
    def __init__(self, memory_dir: str = ".agent_memory"):
        """
        Initialize agent memory.
        
        Args:
            memory_dir: Directory for persistent memory storage
        """
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(exist_ok=True)
        
        # Short-term memory (current session)
        self.short_term: Dict[str, MemoryEntry] = {}
        
        # Long-term memory (persistent)
        self.long_term: Dict[str, MemoryEntry] = {}
        
        # Context stack for nested operations
        self.context_stack: List[str] = []
        
        # Knowledge base
        self.knowledge: Dict[str, Any] = {}
        
        # Load persistent memory
        self._load_long_term_memory()
    
    def remember(
        self,
        key: str,
        value: Any,
        persistent: bool = False,
        context: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Store information in memory.
        
        Args:
            key: Memory key
            value: Value to store
            persistent: If True, save to long-term memory
            context: Optional context description
            metadata: Optional metadata
        """
        entry = MemoryEntry(
            timestamp=datetime.now(),
            key=key,
            value=value,
            context=context or self.get_current_context(),
            metadata=metadata or {}
        )
        
        # Store in short-term memory
        self.short_term[key] = entry
        
        # Store in long-term if requested
        if persistent:
            self.long_term[key] = entry
            self._save_long_term_memory()
        
        logger.debug(f"Remembered: {key} = {str(value)[:100]}")
    
    def recall(self, key: str, default: Any = None) -> Any:
        """
        Retrieve information from memory.
        
        Args:
            key: Memory key
            default: Default value if not found
            
        Returns:
            Stored value or default
        """
        # Check short-term first
        if key in self.short_term:
            return self.short_term[key].value
        
        # Check long-term
        if key in self.long_term:
            return self.long_term[key].value
        
        return default
    
    def forget(self, key: str, from_long_term: bool = False):
        """
        Remove information from memory.
        
        Args:
            key: Memory key
            from_long_term: If True, also remove from long-term memory
        """
        if key in self.short_term:
            del self.short_term[key]
        
        if from_long_term and key in self.long_term:
            del self.long_term[key]
            self._save_long_term_memory()
    
    def push_context(self, context: str):
        """
        Push a new context onto the stack.
        
        Args:
            context: Context description
        """
        self.context_stack.append(context)
        logger.debug(f"Context pushed: {context}")
    
    def pop_context(self) -> Optional[str]:
        """
        Pop the current context from the stack.
        
        Returns:
            Popped context or None
        """
        if self.context_stack:
            context = self.context_stack.pop()
            logger.debug(f"Context popped: {context}")
            return context
        return None
    
    def get_current_context(self) -> str:
        """
        Get the current context.
        
        Returns:
            Current context string
        """
        return " > ".join(self.context_stack) if self.context_stack else "root"
    
    def add_knowledge(self, topic: str, information: Any):
        """
        Add information to the knowledge base.
        
        Args:
            topic: Knowledge topic
            information: Information to store
        """
        if topic not in self.knowledge:
            self.knowledge[topic] = []
        
        self.knowledge[topic].append({
            'timestamp': datetime.now().isoformat(),
            'info': information
        })
        
        logger.debug(f"Knowledge added: {topic}")
    
    def get_knowledge(self, topic: str) -> List[Any]:
        """
        Retrieve knowledge on a topic.
        
        Args:
            topic: Knowledge topic
            
        Returns:
            List of knowledge entries
        """
        return self.knowledge.get(topic, [])
    
    def search_memory(self, query: str) -> List[MemoryEntry]:
        """
        Search memory for entries matching query.
        
        Args:
            query: Search query
            
        Returns:
            List of matching memory entries
        """
        results = []
        query_lower = query.lower()
        
        # Search short-term memory
        for entry in self.short_term.values():
            if (query_lower in entry.key.lower() or
                query_lower in str(entry.value).lower() or
                (entry.context and query_lower in entry.context.lower())):
                results.append(entry)
        
        # Search long-term memory
        for entry in self.long_term.values():
            if entry not in results:
                if (query_lower in entry.key.lower() or
                    query_lower in str(entry.value).lower() or
                    (entry.context and query_lower in entry.context.lower())):
                    results.append(entry)
        
        return results
    
    def get_memory_summary(self) -> Dict[str, Any]:
        """
        Get a summary of current memory state.
        
        Returns:
            Memory summary dict
        """
        return {
            'short_term_entries': len(self.short_term),
            'long_term_entries': len(self.long_term),
            'context_depth': len(self.context_stack),
            'current_context': self.get_current_context(),
            'knowledge_topics': len(self.knowledge),
            'recent_memories': [
                {
                    'key': entry.key,
                    'timestamp': entry.timestamp.isoformat(),
                    'context': entry.context
                }
                for entry in sorted(
                    self.short_term.values(),
                    key=lambda e: e.timestamp,
                    reverse=True
                )[:5]
            ]
        }
    
    def clear_short_term(self):
        """Clear short-term memory"""
        self.short_term.clear()
        logger.info("Short-term memory cleared")
    
    def clear_all(self, include_long_term: bool = False):
        """
        Clear all memory.
        
        Args:
            include_long_term: If True, also clear long-term memory
        """
        self.short_term.clear()
        self.context_stack.clear()
        self.knowledge.clear()
        
        if include_long_term:
            self.long_term.clear()
            self._save_long_term_memory()
        
        logger.info("Memory cleared")
    
    def _load_long_term_memory(self):
        """Load long-term memory from disk"""
        memory_file = self.memory_dir / "long_term.json"
        
        if not memory_file.exists():
            return
        
        try:
            with open(memory_file, 'r') as f:
                data = json.load(f)
            
            for key, entry_data in data.items():
                self.long_term[key] = MemoryEntry(
                    timestamp=datetime.fromisoformat(entry_data['timestamp']),
                    key=entry_data['key'],
                    value=entry_data['value'],
                    context=entry_data.get('context'),
                    metadata=entry_data.get('metadata', {})
                )
            
            logger.info(f"Loaded {len(self.long_term)} long-term memories")
            
        except Exception as e:
            logger.error(f"Failed to load long-term memory: {e}")
    
    def _save_long_term_memory(self):
        """Save long-term memory to disk"""
        memory_file = self.memory_dir / "long_term.json"
        
        try:
            data = {}
            for key, entry in self.long_term.items():
                data[key] = {
                    'timestamp': entry.timestamp.isoformat(),
                    'key': entry.key,
                    'value': entry.value,
                    'context': entry.context,
                    'metadata': entry.metadata
                }
            
            with open(memory_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.debug(f"Saved {len(self.long_term)} long-term memories")
            
        except Exception as e:
            logger.error(f"Failed to save long-term memory: {e}")
    
    def export_memory(self, filepath: str):
        """
        Export all memory to a file.
        
        Args:
            filepath: Path to export file
        """
        export_data = {
            'exported_at': datetime.now().isoformat(),
            'short_term': {
                key: {
                    'timestamp': entry.timestamp.isoformat(),
                    'key': entry.key,
                    'value': entry.value,
                    'context': entry.context,
                    'metadata': entry.metadata
                }
                for key, entry in self.short_term.items()
            },
            'long_term': {
                key: {
                    'timestamp': entry.timestamp.isoformat(),
                    'key': entry.key,
                    'value': entry.value,
                    'context': entry.context,
                    'metadata': entry.metadata
                }
                for key, entry in self.long_term.items()
            },
            'knowledge': self.knowledge
        }
        
        with open(filepath, 'w') as f:
            json.dump(export_data, f, indent=2)
        
        logger.info(f"Memory exported to {filepath}")