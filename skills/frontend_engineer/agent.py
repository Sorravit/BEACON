"""
BEACON Skill Agent — Frontend Engineer
ECC Skill-as-Agent Pattern v1.0
"""
from __future__ import annotations
import json, time
from pathlib import Path


class SkillResult:
    def __init__(self, skill_id, status, data, error="", duration_ms=0.0):
        self.skill_id = skill_id
        self.status = status
        self.data = data
        self.error = error
        self.duration_ms = duration_ms

    def to_dict(self):
        return {"skill_id": self.skill_id, "status": self.status,
                "data": self.data, "error": self.error, "duration_ms": self.duration_ms}


class FrontendEngineerAgent:
    """Frontend Engineer — Builds React/TypeScript components with hooks, RTL tests, Storybook stories and accessibility compliance."""
    skill_id = "frontend_engineer"
    version = "1.0.0"
    display_name = "Frontend Engineer"
    description = "Builds React/TypeScript components with hooks, RTL tests, Storybook stories and accessibility compliance."

    def __init__(self):
        self._manifest = None
        self._memory = []

    def load_manifest(self):
        if self._manifest is None:
            with open(Path(__file__).parent / "manifest.json") as f:
                self._manifest = json.load(f)
        return self._manifest

    def _validate_inputs(self, inputs):
        manifest = self.load_manifest()
        required = [k for k, v in manifest.get("inputs", {}).items()
                    if "optional" not in str(v).lower()]
        missing = [k for k in required if not inputs.get(k)]
        if missing:
            raise ValueError(f"Missing required inputs: {missing}")

    def execute(self, inputs: dict) -> SkillResult:
        start = time.time()
        try:
            self._validate_inputs(inputs)
            self._memory.append({"role": "user", "content": str(inputs)})
            manifest = self.load_manifest()
            role_cfg = manifest.get("role", {})
            output_schema = manifest.get("outputs", {})
            steps = [
                f"1. Parse and validate inputs: {list(inputs.keys())}",
                f"2. Apply Frontend Engineer expertise and domain knowledge",
                "3. Structure output according to manifest output schema",
                "4. Apply safety constraints",
                "5. Validate completeness and quality"
            ]
            result_data = {
                "skill_id": self.skill_id,
                "display_name": self.display_name,
                "persona": role_cfg.get("persona", ""),
                "reasoning_steps": steps,
                "inputs_received": inputs,
                "outputs": {k: f"[Frontend Engineer — {k} output]" for k in output_schema},
                "ready_for_llm": True
            }
            self._memory.append({"role": "assistant", "content": str(result_data)})
            return SkillResult(self.skill_id, "success", result_data,
                              duration_ms=(time.time()-start)*1000)
        except Exception as e:
            return SkillResult(self.skill_id, "error", {}, str(e),
                              duration_ms=(time.time()-start)*1000)
