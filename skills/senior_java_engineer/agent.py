"""
    BEACON Skill Agent — Senior / Lead Java Backend Engineer
ECC Skill-as-Agent Pattern v3.0

Modes:
  new_feature   — domain model → API contract → Controller/Service/Repository/Tests/Migration
  code_review   — SOLID/security/performance/design rated findings with before/after
  refactor      — improve code without changing behaviour
  architecture  — options analysis, ADR, component diagram
  debug_fix     — diagnose and fix a specific failure
  test_writing  — JUnit5 / Mockito / Testcontainers / WireMock suites
  performance   — N+1, HikariCP, caching, virtual threads, index recommendations
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from enum import Enum


# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------

class Mode(str, Enum):
    NEW_FEATURE   = "new_feature"
    CODE_REVIEW   = "code_review"
    REFACTOR      = "refactor"
    ARCHITECTURE  = "architecture"
    DEBUG_FIX     = "debug_fix"
    TEST_WRITING  = "test_writing"
    PERFORMANCE   = "performance"
    UNKNOWN       = "unknown"


class ReviewSeverity(str, Enum):
    CRITICAL   = "🔴 Critical"
    MAJOR      = "🟠 Major"
    MINOR      = "🟡 Minor"
    SUGGESTION = "🔵 Suggestion"


class SkillResult:
    def __init__(
        self,
        skill_id: str,
        status: str,
        data: dict,
        error: str = "",
        duration_ms: float = 0.0,
    ):
        self.skill_id    = skill_id
        self.status      = status
        self.data        = data
        self.error       = error
        self.duration_ms = duration_ms

    def to_dict(self) -> dict:
        return {
            "skill_id":    self.skill_id,
            "status":      self.status,
            "data":        self.data,
            "error":       self.error,
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# Main agent class
# ---------------------------------------------------------------------------

class SeniorJavaEngineerAgent:
    """
    Senior / Lead Java Backend Engineer.

    Designs, implements, reviews, and architects Java 17/21 Spring Boot 3
    microservices, REST/gRPC APIs, event-driven systems, JPA/Hibernate,
    security, observability, and production-grade test suites.
    """

    skill_id     = "senior_java_engineer"
    version      = "3.0.0"
    display_name = "Senior / Lead Java Backend Engineer"
    description  = (
        "Acts as a Senior/Lead Java Backend Engineer. Designs, implements, "
        "reviews, and architects Java 17/21 Spring Boot 3 microservices, "
        "REST/gRPC APIs, event-driven systems, JPA/Hibernate, security, "
        "observability, and production-grade test suites."
    )

    # Required inputs (mode and constraints are optional)
    REQUIRED_INPUTS = ["requirement", "domain"]

    # Auto-detection keywords for mode inference
    _MODE_KEYWORDS: dict[Mode, list[str]] = {
        Mode.CODE_REVIEW:  ["review", "check", "audit", "analyse", "analyze", "smell", "issue"],
        Mode.REFACTOR:     ["refactor", "clean up", "improve", "restructure", "simplify"],
        Mode.ARCHITECTURE: ["architecture", "design", "adr", "pattern", "system design", "diagram", "compare"],
        Mode.DEBUG_FIX:    ["bug", "fix", "error", "exception", "stacktrace", "npe", "debug", "failing"],
        Mode.TEST_WRITING: ["test", "junit", "mockito", "testcontainer", "spec", "coverage"],
        Mode.PERFORMANCE:  ["slow", "performance", "n+1", "optimise", "optimize", "latency", "throughput", "cache"],
        Mode.NEW_FEATURE:  ["implement", "create", "build", "add", "new", "feature", "endpoint", "api", "service"],
    }

    def __init__(self):
        self._manifest: dict | None = None
        self._memory: list[dict]    = []

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def load_manifest(self) -> dict:
        if self._manifest is None:
            manifest_path = Path(__file__).parent / "manifest.json"
            with open(manifest_path, encoding="utf-8") as fh:
                self._manifest = json.load(fh)
        return self._manifest

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_inputs(self, inputs: dict) -> None:
        missing = [k for k in self.REQUIRED_INPUTS if not inputs.get(k)]
        if missing:
            raise ValueError(
                f"Missing required inputs: {missing}. "
                f"Please provide: {', '.join(missing)}"
            )

    # ------------------------------------------------------------------
    # Mode detection
    # ------------------------------------------------------------------

    def _detect_mode(self, inputs: dict) -> Mode:
        """Infer mode from the 'mode' field or from keywords in requirement."""
        explicit = (inputs.get("mode") or "").strip().lower().replace("-", "_")
        if explicit:
            try:
                return Mode(explicit)
            except ValueError:
                pass  # fall through to keyword detection

        text = (inputs.get("requirement", "") + " " + inputs.get("domain", "")).lower()
        for mode, keywords in self._MODE_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                return mode
        return Mode.NEW_FEATURE  # sensible default

    # ------------------------------------------------------------------
    # Per-mode reasoning steps
    # ------------------------------------------------------------------

    def _steps_for_mode(self, mode: Mode, inputs: dict) -> list[str]:
        req    = inputs.get("requirement", "(no requirement provided)")
        domain = inputs.get("domain", "(no domain provided)")

        base = [
            f"[1] Received request — domain: '{domain}', mode: {mode.value}",
            f"[2] Requirement: {req[:200]}",
        ]

        mode_steps: dict[Mode, list[str]] = {
            Mode.NEW_FEATURE: [
                "[3] Define domain model: entities, value objects, aggregates, domain events",
                "[4] Design API contract (OpenAPI 3 annotations / interface first)",
                "[5] Implement Controller — input validation, DTO mapping, HTTP status codes",
                "[6] Implement Service — business logic, @Transactional boundaries",
                "[7] Implement Repository — Spring Data JPA / custom JPQL / native SQL if justified",
                "[8] Add MapStruct mapper: DTO ↔ Entity",
                "[9] Add global @ControllerAdvice — RFC 7807 Problem Details error responses",
                "[10] Add Jakarta Bean Validation on request DTOs",
                "[11] Add @PreAuthorize security annotations",
                "[12] Add structured SLF4J logging + Micrometer timer on critical service methods",
                "[13] Write Flyway migration SQL for any schema changes",
                "[14] Write unit tests (Mockito), @WebMvcTest slice, Testcontainers integration test",
                "[15] Document assumptions and follow-up recommendations",
            ],
            Mode.CODE_REVIEW: [
                "[3] Scan for correctness: logic errors, NPE risks, unchecked exceptions, missing validation",
                "[4] Scan for security: injection, insecure deserialization, missing auth, sensitive data in logs",
                "[5] Scan for performance: N+1 queries, unbounded queries, missing pagination, missing indexes",
                "[6] Scan for design: SOLID violations, layer leakage, anemic domain model, God classes",
                "[7] Scan for testability: static calls, hard-coded deps, missing tests",
                "[8] Rate each finding: 🔴 Critical | 🟠 Major | 🟡 Minor | 🔵 Suggestion",
                "[9] Provide before/after code examples for each finding",
                "[10] Summarise overall quality score and priority fix list",
            ],
            Mode.REFACTOR: [
                "[3] Identify code smells: duplication, long methods, large class, primitive obsession",
                "[4] Apply Extract Method / Extract Class / Replace Conditional with Polymorphism",
                "[5] Ensure all public contracts remain unchanged (no behaviour change)",
                "[6] Improve naming for clarity",
                "[7] Remove dead code and redundant comments",
                "[8] Update or add tests to cover refactored paths",
            ],
            Mode.ARCHITECTURE: [
                "[3] Clarify problem context: scale, team size, latency SLA, data volume",
                "[4] Present 2–3 architectural options with pros/cons",
                "[5] Recommend best fit with rationale",
                "[6] Draw component diagram (ASCII or Mermaid)",
                "[7] Identify integration points, failure modes, and scalability ceiling",
                "[8] Produce Architecture Decision Record (ADR) if requested",
            ],
            Mode.DEBUG_FIX: [
                "[3] Reproduce the failure path from the stack trace / symptom description",
                "[4] Identify root cause: exception type, null check, concurrency, transaction boundary",
                "[5] Propose minimal targeted fix",
                "[6] Add regression test to prevent recurrence",
                "[7] Check for related issues in the same code path",
            ],
            Mode.TEST_WRITING: [
                "[3] Identify test scope: unit (Mockito), slice (@WebMvcTest / @DataJpaTest), integration (Testcontainers)",
                "[4] Write unit tests for service layer — mock all dependencies",
                "[5] Write @WebMvcTest for controller — verify HTTP status, body, validation errors",
                "[6] Write @DataJpaTest / Testcontainers for repository — real DB assertions",
                "[7] Add WireMock stubs for external HTTP dependencies if applicable",
                "[8] Follow should_<outcome>_when_<condition> naming",
                "[9] Ensure AAA (Arrange / Act / Assert) structure",
                "[10] Target branch coverage ≥ 80%, mutation coverage where critical",
            ],
            Mode.PERFORMANCE: [
                "[3] Identify hot path from profiling data / slow query log / thread dump",
                "[4] Detect N+1 queries — recommend JOIN FETCH, @EntityGraph, or batch fetching",
                "[5] Review HikariCP pool sizing and connection timeout settings",
                "[6] Evaluate Spring Cache + Redis for read-heavy paths",
                "[7] Evaluate virtual threads (Project Loom) or @Async for I/O-bound paths",
                "[8] Suggest database indexes with EXPLAIN ANALYZE rationale",
                "[9] Add Micrometer @Timed / Timer around optimised section",
                "[10] Define before/after benchmark strategy",
            ],
        }

        return base + mode_steps.get(mode, [
            "[3] Analyse request and apply Senior/Lead Java engineering expertise",
            "[4] Produce production-ready output with full context",
        ])

    # ------------------------------------------------------------------
    # Output schema builder
    # ------------------------------------------------------------------

    def _output_schema(self, mode: Mode) -> dict:
        """Return the expected output keys for the given mode."""
        common = {
            "objective":                "🎯 Objective — clear statement of what is being built or solved",
            "assumptions":              "⚠️ Numbered list of assumptions due to missing context",
            "follow_up_recommendations": "🚀 Security hardening, performance, observability, scalability notes",
        }

        per_mode: dict[Mode, dict] = {
            Mode.NEW_FEATURE: {
                "design":              "🏗 Domain model, API contract, architectural decisions",
                "project_structure":   "📁 Recommended package layout",
                "implementation":      "💻 Production-ready Java source files with paths",
                "tests":               "🧪 JUnit5 + Mockito / Testcontainers test files with paths",
                "db_migration":        "🗄 Flyway/Liquibase SQL migration script",
                "configuration":       "⚙️ application.yml relevant snippets",
                "maven_dependencies":  "📦 pom.xml additions",
            },
            Mode.CODE_REVIEW: {
                "review_findings":     "🔍 Rated findings with before/after — 🔴🟠🟡🔵",
                "quality_summary":     "📊 Overall quality score and priority fix list",
            },
            Mode.REFACTOR: {
                "refactored_code":     "💻 Refactored Java source files with paths",
                "tests":               "🧪 Updated / new tests",
                "change_summary":      "📋 What changed and why",
            },
            Mode.ARCHITECTURE: {
                "options":             "🔀 2–3 options with pros/cons",
                "recommendation":      "✅ Recommended option with rationale",
                "component_diagram":   "🖼 ASCII or Mermaid component diagram",
                "adr":                 "📄 Architecture Decision Record (if requested)",
            },
            Mode.DEBUG_FIX: {
                "root_cause":          "🐛 Root cause analysis",
                "fix":                 "🔧 Targeted fix with code",
                "regression_test":     "🧪 Regression test to prevent recurrence",
            },
            Mode.TEST_WRITING: {
                "tests":               "🧪 Full JUnit5 / Mockito / Testcontainers / WireMock suite",
                "coverage_notes":      "📊 Coverage targets and strategy notes",
            },
            Mode.PERFORMANCE: {
                "findings":            "⚡ Performance issues found with severity",
                "optimised_code":      "💻 Optimised code with before/after comparison",
                "index_recommendations": "🗄 Index suggestions with EXPLAIN ANALYZE rationale",
                "benchmark_strategy":  "📊 Before/after measurement approach",
            },
        }

        return {**common, **per_mode.get(mode, {})}

    # ------------------------------------------------------------------
    # Memory helpers
    # ------------------------------------------------------------------

    def _remember(self, role: str, content: str) -> None:
        manifest = self.load_manifest()
        max_turns = manifest.get("memory_policy", {}).get("max_history_turns", 20)
        self._memory.append({"role": role, "content": content})
        # Keep memory within bounds (each turn = user + assistant = 2 entries)
        if len(self._memory) > max_turns * 2:
            self._memory = self._memory[-(max_turns * 2):]

    def get_memory(self) -> list[dict]:
        """Return conversation memory for use by the LLM context window."""
        return list(self._memory)

    def clear_memory(self) -> None:
        self._memory.clear()

    # ------------------------------------------------------------------
    # Safety checks
    # ------------------------------------------------------------------

    def _apply_safety_constraints(self, inputs: dict) -> list[str]:
        """Return a list of active safety warnings relevant to this request."""
        warnings: list[str] = []
        req = (inputs.get("requirement", "") + inputs.get("existing_code", "")).lower()

        if any(kw in req for kw in ["password", "secret", "token", "apikey", "api_key"]):
            warnings.append(
                "⚠️ SAFETY: Sensitive data detected in input. "
                "Ensure no credentials appear in output code or log statements."
            )
        if "system.out" in req:
            warnings.append(
                "⚠️ SAFETY: System.out.println detected. "
                "Must be replaced with SLF4J logger."
            )
        if "@autowired" in req and "field" in req:
            warnings.append(
                "⚠️ SAFETY: Field injection (@Autowired) detected. "
                "Must be replaced with constructor injection."
            )
        return warnings

    # ------------------------------------------------------------------
    # Main execute
    # ------------------------------------------------------------------

    def execute(self, inputs: dict) -> SkillResult:
        """
        Execute the Senior/Lead Java Backend Engineer skill.

        Args:
            inputs: dict with keys: requirement, domain, mode (opt),
                    existing_code (opt), constraints (opt)

        Returns:
            SkillResult with structured data for LLM consumption.
        """
        start = time.time()
        try:
            self._validate_inputs(inputs)

            mode     = self._detect_mode(inputs)
            steps    = self._steps_for_mode(mode, inputs)
            schema   = self._output_schema(mode)
            warnings = self._apply_safety_constraints(inputs)
            manifest = self.load_manifest()

            self._remember("user", json.dumps(inputs, ensure_ascii=False))

            result_data = {
                "skill_id":           self.skill_id,
                "version":            self.version,
                "display_name":       self.display_name,
                "mode":               mode.value,
                "persona":            manifest["role"]["persona"],
                "tone":               manifest["role"]["tone"],
                "output_format":      manifest["role"]["output_format"],
                "capabilities":       manifest["capabilities"],
                "reasoning_steps":    steps,
                "expected_outputs":   schema,
                "safety_warnings":    warnings,
                "safety_constraints": manifest["safety_constraints"],
                "inputs_received":    {
                    "requirement":    inputs.get("requirement", ""),
                    "domain":         inputs.get("domain", ""),
                    "mode":           inputs.get("mode", f"auto-detected: {mode.value}"),
                    "has_existing_code": bool(inputs.get("existing_code")),
                    "constraints":    inputs.get("constraints", ""),
                },
                "conversation_turns": len(self._memory) // 2,
                "ready_for_llm":      True,
            }

            self._remember("assistant", json.dumps(result_data, ensure_ascii=False))

            return SkillResult(
                skill_id=self.skill_id,
                status="success",
                data=result_data,
                duration_ms=(time.time() - start) * 1000,
            )

        except ValueError as exc:
            return SkillResult(
                skill_id=self.skill_id,
                status="validation_error",
                data={},
                error=str(exc),
                duration_ms=(time.time() - start) * 1000,
            )
        except Exception as exc:  # noqa: BLE001
            return SkillResult(
                skill_id=self.skill_id,
                status="error",
                data={},
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.time() - start) * 1000,
            )


# ---------------------------------------------------------------------------
# Convenience factory (mirrors pattern used by BEACON skill loader)
# ---------------------------------------------------------------------------

def create_agent() -> SeniorJavaEngineerAgent:
    """Return a fresh SeniorJavaEngineerAgent instance."""
    return SeniorJavaEngineerAgent()
