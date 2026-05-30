"""Tests for the Agent Skills subsystem (core.skills.SkillManager)."""

import asyncio
import textwrap

from core.skills import SkillManager
from tools.manager import ToolManager


def _make_skill(tmp_path, name: str, frontmatter: str, body: str):
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(frontmatter) + "\n" + textwrap.dedent(body), encoding="utf-8"
    )
    return skill_dir


def test_discovers_skill_with_frontmatter(tmp_path) -> None:
    _make_skill(
        tmp_path,
        "pdf",
        """
        ---
        name: pdf-tools
        description: Use to extract text from PDFs.
        version: 2.1.0
        ---
        """,
        """
        # PDF Tools
        Step 1. Do the thing.
        """,
    )
    manager = SkillManager.load(skills_dir=tmp_path)

    assert manager.has_skills()
    skill = manager.get("pdf-tools")
    assert skill is not None
    assert skill.description == "Use to extract text from PDFs."
    assert skill.version == "2.1.0"
    assert "Step 1" in skill.full_text()


def test_name_defaults_to_folder(tmp_path) -> None:
    _make_skill(
        tmp_path,
        "my-folder-skill",
        """
        ---
        description: A skill without an explicit name.
        ---
        """,
        "# Body\nInstructions here.",
    )
    manager = SkillManager.load(skills_dir=tmp_path)
    assert manager.get("my-folder-skill") is not None


def test_get_is_case_insensitive(tmp_path) -> None:
    _make_skill(
        tmp_path,
        "research",
        """
        ---
        name: Web-Research
        description: research things
        ---
        """,
        "# Research",
    )
    manager = SkillManager.load(skills_dir=tmp_path)
    assert manager.get("web-research") is not None
    assert manager.get("WEB-RESEARCH") is not None


def test_bundled_resources_listed(tmp_path) -> None:
    skill_dir = _make_skill(
        tmp_path,
        "bundled",
        """
        ---
        name: bundled
        description: has resources
        ---
        """,
        "# Bundled",
    )
    (skill_dir / "helper.py").write_text("print('hi')", encoding="utf-8")
    manager = SkillManager.load(skills_dir=tmp_path)
    skill = manager.get("bundled")
    assert "helper.py" in skill.resources
    assert "helper.py" in skill.full_text()


def test_system_prompt_block_lists_skills(tmp_path) -> None:
    _make_skill(
        tmp_path,
        "alpha",
        """
        ---
        name: alpha
        description: alpha skill
        ---
        """,
        "# Alpha",
    )
    manager = SkillManager.load(skills_dir=tmp_path)
    block = manager.system_prompt_block()
    assert "alpha: alpha skill" in block
    assert "load_skill" in block


def test_empty_dir_has_no_skills(tmp_path) -> None:
    manager = SkillManager.load(skills_dir=tmp_path / "nonexistent")
    assert not manager.has_skills()
    assert manager.system_prompt_block() == ""


def test_skill_tools_registered_and_callable(tmp_path) -> None:
    _make_skill(
        tmp_path,
        "demo",
        """
        ---
        name: demo
        description: a demo skill
        ---
        """,
        "# Demo\nDo X then Y.",
    )
    manager = SkillManager.load(skills_dir=tmp_path)
    tm = ToolManager(skill_manager=manager)
    asyncio.run(tm.initialize())

    assert "list_skills" in tm.tool_handlers
    assert "load_skill" in tm.tool_handlers

    listed = asyncio.run(tm.execute_tool("list_skills", {}))
    assert "demo" in listed

    loaded = asyncio.run(tm.execute_tool("load_skill", {"name": "demo"}))
    assert "Do X then Y." in loaded

    missing = asyncio.run(tm.execute_tool("load_skill", {"name": "ghost"}))
    assert "not found" in missing.lower()

