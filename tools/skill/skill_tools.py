"""Skill tool handlers — expose Agent Skills (SKILL.md) to the model.

These tools implement progressive disclosure: ``list_skills`` shows the catalog
(name + description) and ``load_skill`` returns a skill's full instruction body
on demand. They operate on ``self.skill_manager`` (a ``core.skills.SkillManager``)
which the ToolManager receives at construction time.
"""


class SkillToolsMixin:
    async def _list_skills(self):
        manager = getattr(self, "skill_manager", None)
        if not manager or not manager.has_skills():
            return "No skills are currently installed."
        lines = [f"Available skills ({len(manager)}):"]
        for item in manager.catalog():
            lines.append(f"- {item['name']} (v{item['version']}): {item['description']}")
        lines.append("\nLoad a skill's full instructions with load_skill(name=\"<skill>\").")
        return "\n".join(lines)

    async def _load_skill(self, name: str):
        manager = getattr(self, "skill_manager", None)
        if not manager or not manager.has_skills():
            return "No skills are currently installed."
        skill = manager.get(name)
        if not skill:
            available = ", ".join(manager.names()) or "none"
            return f"Skill '{name}' not found. Available skills: {available}."
        header = f"# Skill: {skill.name} (v{skill.version})\n\n"
        return header + skill.full_text()

