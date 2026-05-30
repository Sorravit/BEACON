"""Agent Skills for BEACON (Anthropic "Agent Skills" style).

A *skill* is a self-contained folder under ``skills/`` that packages domain
expertise the agent can pull in on demand:

    skills/
      web-research/
        SKILL.md          <- YAML frontmatter + markdown instructions
        scripts/...       <- optional bundled scripts/resources

``SKILL.md`` begins with a YAML frontmatter block:

    ---
    name: web-research
    description: Use when the user needs thorough, cited web research.
    version: 1.0.0
    ---
    # Detailed instructions the agent should follow...

**Progressive disclosure** is the core idea: only each skill's ``name`` and
``description`` are injected into the system prompt (cheap, always present). The
full instruction body is loaded *on demand* via the ``load_skill`` tool when the
model decides a skill is relevant. This keeps the context window small while
giving the agent access to arbitrarily detailed playbooks.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_SKILLS_DIR = Path(os.getenv("SKILLS_DIR", "skills"))
_MAX_DESCRIPTION_CHARS = 500


@dataclass
class Skill:
    """A single discovered skill."""

    name: str
    description: str
    body: str
    path: Path
    metadata: Dict[str, object] = field(default_factory=dict)
    resources: List[str] = field(default_factory=list)

    @property
    def version(self) -> str:
        return str(self.metadata.get("version", "")) or "—"

    def summary(self) -> Dict[str, object]:
        """Lightweight view (no body) used for progressive disclosure."""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "resources": self.resources,
        }

    def full_text(self) -> str:
        """Full instruction body + a note about bundled resources."""
        text = self.body.strip()
        if self.resources:
            listing = "\n".join(f"  - {r}" for r in self.resources)
            text += f"\n\nBundled resources (in {self.path.parent}):\n{listing}"
        return text


class SkillManager:
    """Discovers and serves skills from the ``skills/`` directory."""

    def __init__(self, skills: Optional[List[Skill]] = None, source: Optional[Path] = None) -> None:
        self._skills: Dict[str, Skill] = {}
        for skill in skills or []:
            self._skills[skill.name] = skill
        self.source = source

    # ── discovery ────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, skills_dir: Optional[os.PathLike | str] = None) -> "SkillManager":
        directory = Path(skills_dir or DEFAULT_SKILLS_DIR)
        manager = cls(source=directory)
        if not directory.exists():
            logger.info("No skills directory at %s — skills disabled", directory)
            return manager

        for skill_file in sorted(directory.glob("*/SKILL.md")):
            try:
                skill = cls._parse_skill_file(skill_file)
                if skill:
                    manager._skills[skill.name] = skill
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to load skill %s: %s", skill_file, exc)

        # Also support a flat skills/SKILL.md or skills/<name>.md? Keep it to the
        # folder convention for predictability.
        logger.info("Loaded %d skill(s) from %s", len(manager._skills), directory)
        return manager

    @staticmethod
    def _parse_skill_file(skill_file: Path) -> Optional[Skill]:
        raw = skill_file.read_text(encoding="utf-8")
        frontmatter, body = SkillManager._split_frontmatter(raw)

        # Fall back to the folder name if no explicit name is given.
        name = str(frontmatter.get("name") or skill_file.parent.name).strip()
        description = str(frontmatter.get("description") or "").strip()
        if not description:
            # Use the first non-empty body line as a description fallback.
            for line in body.splitlines():
                stripped = line.strip().lstrip("#").strip()
                if stripped:
                    description = stripped
                    break
        description = description[:_MAX_DESCRIPTION_CHARS]

        if not name:
            logger.warning("Skipping skill without a name: %s", skill_file)
            return None

        resources = [
            str(p.relative_to(skill_file.parent))
            for p in sorted(skill_file.parent.rglob("*"))
            if p.is_file() and p.name != "SKILL.md"
        ]

        return Skill(
            name=name,
            description=description,
            body=body.strip(),
            path=skill_file,
            metadata={k: v for k, v in frontmatter.items() if k not in ("name", "description")},
            resources=resources,
        )

    @staticmethod
    def _split_frontmatter(raw: str) -> tuple[Dict[str, object], str]:
        """Split a ``---`` delimited YAML frontmatter block from the body."""
        if raw.lstrip().startswith("---"):
            stripped = raw.lstrip()
            parts = stripped.split("---", 2)
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                    if isinstance(meta, dict):
                        return meta, parts[2]
                except Exception as exc:
                    logger.warning("Invalid YAML frontmatter: %s", exc)
        return {}, raw

    # ── access ───────────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._skills)

    def has_skills(self) -> bool:
        return bool(self._skills)

    def names(self) -> List[str]:
        return list(self._skills.keys())

    def get(self, name: str) -> Optional[Skill]:
        if not name:
            return None
        skill = self._skills.get(name)
        if skill:
            return skill
        # Case-insensitive / fuzzy fallback so the model's phrasing is forgiving.
        lowered = name.strip().lower()
        for skill in self._skills.values():
            if skill.name.lower() == lowered:
                return skill
        return None

    def catalog(self) -> List[Dict[str, object]]:
        return [s.summary() for s in self._skills.values()]

    def system_prompt_block(self) -> str:
        """Compact catalog injected into the system prompt (progressive disclosure)."""
        if not self._skills:
            return ""
        lines = [
            "\n\nAVAILABLE SKILLS — specialised playbooks you can load on demand:",
        ]
        for skill in self._skills.values():
            lines.append(f'  - {skill.name}: {skill.description}')
        lines.append(
            "When a request matches a skill, FIRST call load_skill(name=\"<skill>\") "
            "to retrieve its full instructions, then follow them precisely. "
            "Do not guess a skill's contents — always load it."
        )
        return "\n".join(lines)

