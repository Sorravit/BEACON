"""Memory tool handlers."""


class MemoryToolsMixin:
    async def _memory_add_fact(self, topic: str, fact: str):
        if not self.vector_memory:
            return "Memory system is not available."
        if not await self.vector_memory.ensure_ready():
            return "Memory system is currently unavailable."
        stored = await self.vector_memory.store_personal_fact(topic, fact)
        if stored is None:
            return "Failed to store fact because the memory database is unreachable. Please try again shortly."
        if stored:
            return f"Remembered: [{topic}] {fact}"
        return "Failed to store fact."

    async def _memory_list_facts(self):
        if not self.vector_memory:
            return "Memory system is not available."
        if not await self.vector_memory.ensure_ready():
            return "Memory system is currently unavailable."
        facts = await self.vector_memory.get_all_personal_facts()
        if facts is None:
            return "Failed to list personal facts because the memory database is unreachable. Please try again shortly."
        if not facts:
            return "No personal facts stored in memory."
        lines = [
            f"[{fact.get('topic')}] {fact.get('fact')} (saved: {fact.get('stored_at', '')[:10]})"
            for fact in facts
        ]
        return f"Personal facts in memory ({len(facts)}):\n" + "\n".join(lines)

    async def _memory_delete_fact(self, keyword: str):
        if not self.vector_memory:
            return "Memory system is not available."
        if not await self.vector_memory.ensure_ready():
            return "Memory system is currently unavailable."
        count = await self.vector_memory.delete_personal_facts(keyword)
        if count is None:
            return "Failed to delete personal facts because the memory database is unreachable. Please try again shortly."
        if count == 0:
            return f"No personal facts found matching '{keyword}'."
        return f"Deleted {count} personal fact(s) matching '{keyword}'."

    async def _memory_delete_research(self, keyword: str):
        if not self.vector_memory:
            return "Memory system is not available."
        if not await self.vector_memory.ensure_ready():
            return "Memory system is currently unavailable."
        count = await self.vector_memory.delete_research(keyword)
        if count is None:
            return "Failed to delete research memory because the memory database is unreachable. Please try again shortly."
        if count == 0:
            return f"No research entries found matching '{keyword}'."
        return f"Deleted {count} research entry/entries matching '{keyword}'."

    async def _memory_clear_research(self):
        if not self.vector_memory:
            return "Memory system is not available."
        if not await self.vector_memory.ensure_ready():
            return "Memory system is currently unavailable."
        count = await self.vector_memory.clear_all_research()
        if count is None:
            return "Failed to clear research memory because the memory database is unreachable. Please try again shortly."
        return f"Cleared all {count} research memory entries."

