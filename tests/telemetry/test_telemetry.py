"""
tests/telemetry/test_telemetry.py
==================================
Unit + integration tests for the BEACON telemetry package.

Tests verify ALL acceptance criteria:
  AC1 – all public symbols importable
  AC2 – session_id + total_duration_ms emitted via SessionReporter
  AC3 – per-tool-call duration emitted (record_tool_call + track_tool)
  AC4 – sample trace file present and valid
  AC5 – no port conflicts with BEACON (8181) or Weaviate (8090)
  AC6 – this test file (>= 5 test functions, all pass)
  AC7 – documentation file present

Run:
    cd /Users/sorravit/sandbox/ClineSandbox
    python -m pytest tests/telemetry/ -v
"""
from __future__ import annotations

import os
import sys
import time
import unittest.mock as mock

import pytest

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so imports work from any CWD
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def clean_tracer_singleton():
    """
    Reset BEACON's tracer singleton before and after every test to prevent
    state leakage between tests.
    """
    import core.telemetry.tracer as _tracer_mod
    if _tracer_mod._provider is not None:
        try:
            _tracer_mod._provider.shutdown()
        except Exception:
            pass
        _tracer_mod._provider = None
    yield
    if _tracer_mod._provider is not None:
        try:
            _tracer_mod._provider.shutdown()
        except Exception:
            pass
        _tracer_mod._provider = None


@pytest.fixture
def in_memory_setup():
    """
    Patch core.telemetry.tracer.get_tracer so that record_tool_call() /
    record_llm_call() route spans to an InMemorySpanExporter.

    This avoids the OTel 'cannot override global provider' restriction by
    working at the BEACON module boundary rather than the OTel global.

    Returns (provider, exporter).
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    import core.telemetry.tracer as _tracer_mod
    import core.telemetry.metrics as _metrics_mod
    import core.telemetry.session_reporter as _sess_mod

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def _get_tracer(name: str = "beacon"):
        return provider.get_tracer(name)

    # Patch get_tracer in all submodules that call it
    with mock.patch.object(_tracer_mod, "get_tracer", side_effect=_get_tracer), \
         mock.patch.object(_metrics_mod, "get_tracer", side_effect=_get_tracer), \
         mock.patch.object(_sess_mod, "get_tracer", side_effect=_get_tracer):
        yield provider, exporter


# ═══════════════════════════════════════════════════════════════════════════
# AC1 – Public API surface is importable
# ═══════════════════════════════════════════════════════════════════════════

def test_telemetry_package_imports():
    """AC1: All public symbols must import without error."""
    from core.telemetry import (  # noqa: F401
        init_tracer,
        get_tracer,
        shutdown,
        record_tool_call,
        record_llm_call,
        record_session,
        SessionReporter,
    )
    assert callable(init_tracer)
    assert callable(get_tracer)
    assert callable(shutdown)
    assert callable(record_session)
    assert SessionReporter is not None


# ═══════════════════════════════════════════════════════════════════════════
# AC2 – session_id + total_duration_ms emitted by SessionReporter
# ═══════════════════════════════════════════════════════════════════════════

def test_session_reporter_emits_session_id_and_duration(in_memory_setup):
    """AC2: SessionReporter.to_dict() must return session_id and duration_ms > 0."""
    from core.telemetry import SessionReporter

    session_id = "test-session-ac2-001"
    reporter = SessionReporter(session_id=session_id)

    # session_span sets _started_at and _ended_at, computing duration
    with reporter.session_span(user_message="hello world"):
        time.sleep(0.02)  # ensure measurable wall-clock duration

    result = reporter.to_dict()

    assert result.get("session_id") == session_id, (
        f"Expected session_id={session_id!r}, got {result.get('session_id')!r}"
    )
    duration = result.get("duration_ms", 0)
    assert duration > 0, f"duration_ms must be > 0, got {duration}"
    assert "status" in result, "status key missing from to_dict()"


def test_session_reporter_tracks_tool_calls_per_session(in_memory_setup):
    """AC2+AC3: track_tool() records per-call durations visible in to_dict()."""
    from core.telemetry import SessionReporter

    reporter = SessionReporter(session_id="test-session-ac3-001")

    with reporter.session_span(user_message="do something"):
        with reporter.track_tool("web_search"):
            time.sleep(0.01)
        with reporter.track_tool("execute_command"):
            time.sleep(0.01)
        try:
            with reporter.track_tool("read_file"):
                raise ValueError("file not found")
        except ValueError:
            pass

    result = reporter.to_dict()

    tool_calls = result.get("tool_calls", [])
    assert len(tool_calls) == 3, f"Expected 3 tool calls, got {len(tool_calls)}"

    tool_names = {t["tool_name"] for t in tool_calls}
    assert "web_search" in tool_names
    assert "execute_command" in tool_names
    assert "read_file" in tool_names

    for tc in tool_calls:
        assert tc["duration_ms"] >= 0, f"duration_ms must be >= 0 for {tc['tool_name']}"
        assert "status" in tc

    failed = next(t for t in tool_calls if t["tool_name"] == "read_file")
    assert failed["status"] == "error", "Failed tool must have status=error"

    sm = result.get("summary", {})
    assert sm.get("total_tool_calls") == 3
    assert result.get("session_id") == "test-session-ac3-001"


# ═══════════════════════════════════════════════════════════════════════════
# AC3 – per-tool-call OTel span emitted by record_tool_call()
# ═══════════════════════════════════════════════════════════════════════════

def test_record_tool_call_emits_span_with_duration(in_memory_setup):
    """AC3: record_tool_call() must produce a span with tool.duration_ms attribute."""
    provider, exporter = in_memory_setup
    from core.telemetry import record_tool_call

    with record_tool_call("list_files", session_id="sess-001") as span:
        time.sleep(0.01)
        span.set_attribute("tool.result_count", 5)

    spans = exporter.get_finished_spans()
    assert len(spans) >= 1, (
        f"Expected at least one span. Got 0. Provider: {provider!r}"
    )

    tool_span = next((s for s in spans if "list_files" in s.name), None)
    assert tool_span is not None, \
        f"No span named 'tool/list_files'. Got: {[s.name for s in spans]}"

    attrs = dict(tool_span.attributes or {})
    assert attrs.get("tool.name") == "list_files", f"tool.name not set: {attrs}"
    assert "tool.duration_ms" in attrs, f"tool.duration_ms missing: {attrs}"
    assert float(attrs["tool.duration_ms"]) >= 0
    assert attrs.get("session.id") == "sess-001", f"session.id not set: {attrs}"


def test_record_tool_call_sets_error_attributes_on_exception(in_memory_setup):
    """AC3: record_tool_call() must record error.type + set ERROR status on exception."""
    from opentelemetry.trace import StatusCode
    provider, exporter = in_memory_setup
    from core.telemetry import record_tool_call

    with pytest.raises(RuntimeError, match="boom"):
        with record_tool_call("execute_command", session_id="sess-err"):
            raise RuntimeError("boom")

    spans = exporter.get_finished_spans()
    err_span = next((s for s in spans if "execute_command" in s.name), None)
    assert err_span is not None, \
        f"No span for execute_command. Got: {[s.name for s in spans]}"

    attrs = dict(err_span.attributes or {})
    assert "tool.duration_ms" in attrs, "duration_ms must be set even on error"
    assert attrs.get("error.type") == "RuntimeError", f"error.type missing/wrong: {attrs}"
    assert err_span.status.status_code == StatusCode.ERROR


def test_record_llm_call_emits_span_with_duration(in_memory_setup):
    """AC3: record_llm_call() must produce a span with llm.duration_ms attribute."""
    provider, exporter = in_memory_setup
    from core.telemetry import record_llm_call

    with record_llm_call("gpt-4o", session_id="sess-llm-001") as span:
        time.sleep(0.01)
        span.set_attribute("llm.input_tokens", 150)
        span.set_attribute("llm.output_tokens", 75)

    spans = exporter.get_finished_spans()
    llm_span = next((s for s in spans if s.name == "llm/call"), None)
    assert llm_span is not None, \
        f"No 'llm/call' span. Got: {[s.name for s in spans]}"

    attrs = dict(llm_span.attributes or {})
    assert attrs.get("llm.model") == "gpt-4o"
    assert "llm.duration_ms" in attrs, f"llm.duration_ms missing: {attrs}"
    assert float(attrs["llm.duration_ms"]) >= 0


def test_track_tool_produces_otel_span(in_memory_setup):
    """AC3: SessionReporter.track_tool() produces an OTel span via record_tool_call."""
    provider, exporter = in_memory_setup
    from core.telemetry import SessionReporter

    reporter = SessionReporter(session_id="sess-track-001")
    with reporter.session_span(user_message="test"):
        with reporter.track_tool("web_search") as span:
            time.sleep(0.01)
            span.set_attribute("tool.query", "test query")

    spans = exporter.get_finished_spans()
    tool_span = next((s for s in spans if "web_search" in s.name), None)
    assert tool_span is not None, \
        f"No span for web_search. Spans: {[s.name for s in spans]}"
    attrs = dict(tool_span.attributes or {})
    assert "tool.duration_ms" in attrs
    assert float(attrs["tool.duration_ms"]) >= 0


# ═══════════════════════════════════════════════════════════════════════════
# AC3 – ToolManager.execute_tool() has OTel instrumentation in source code
# ═══════════════════════════════════════════════════════════════════════════

def test_tool_manager_execute_tool_has_otel_instrumentation():
    """AC3: tools/manager.py execute_tool() source contains OTel + duration code."""
    tools_manager_path = os.path.join(PROJECT_ROOT, "tools", "manager.py")
    assert os.path.exists(tools_manager_path)

    with open(tools_manager_path) as f:
        src = f.read()

    assert "opentelemetry" in src, \
        "tools/manager.py must import opentelemetry"
    assert "time.perf_counter" in src or "time.monotonic" in src or "time.time()" in src, \
        "tools/manager.py must measure execution time"
    assert "tool.duration_ms" in src, \
        "tools/manager.py must set 'tool.duration_ms' span attribute"
    assert "start_as_current_span" in src, \
        "tools/manager.py must use start_as_current_span"
    assert "session.id" in src or "session_id" in src, \
        "tools/manager.py must propagate session_id to span"


# ═══════════════════════════════════════════════════════════════════════════
# AC4 – Sample trace document is present and valid
# ═══════════════════════════════════════════════════════════════════════════

def test_sample_trace_file_exists_and_is_valid():
    """AC4: output/sample_trace.json must exist and be valid JSON with session data."""
    import json

    sample_path = os.path.join(PROJECT_ROOT, "output", "sample_trace.json")
    assert os.path.exists(sample_path), \
        f"output/sample_trace.json not found at {sample_path}"
    assert os.path.getsize(sample_path) > 100, "sample_trace.json is too small"

    with open(sample_path) as f:
        data = json.load(f)

    content = json.dumps(data)
    assert "session_id" in content or "session" in content, \
        "sample_trace.json must contain session information"
    assert "duration" in content, \
        "sample_trace.json must contain duration information"


# ═══════════════════════════════════════════════════════════════════════════
# AC5 – Port design does not conflict with BEACON (8181) or Weaviate (8090)
# ═══════════════════════════════════════════════════════════════════════════

def test_docker_compose_telemetry_ports_no_conflict_with_beacon():
    """AC5: docker-compose.telemetry.yml must not use ports 8181 or 8090."""
    compose_path = os.path.join(PROJECT_ROOT, "docker-compose.telemetry.yml")
    assert os.path.exists(compose_path), "docker-compose.telemetry.yml not found"

    with open(compose_path) as f:
        content = f.read()

    assert "8181" not in content, \
        "docker-compose.telemetry.yml must NOT use port 8181 (BEACON)"
    assert "8090" not in content, \
        "docker-compose.telemetry.yml must NOT use port 8090 (Weaviate)"

    for port in ["3200", "4317", "4318"]:
        assert port in content, f"Expected telemetry port {port}"


# ═══════════════════════════════════════════════════════════════════════════
# AC6 – TracerProvider initialises + shutdown correctly
# ═══════════════════════════════════════════════════════════════════════════

def test_init_tracer_is_idempotent():
    """AC6: init_tracer() returns the same singleton on repeated calls."""
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""
    os.environ["BEACON_OTEL_CONSOLE"] = "false"

    from core.telemetry import init_tracer, shutdown

    p1 = init_tracer(service_name="beacon-test-1")
    p2 = init_tracer(service_name="beacon-test-2")   # same singleton
    assert p1 is p2

    shutdown()
    p3 = init_tracer(service_name="beacon-test-3")
    assert p3 is not None
    assert p3 is not p1

    shutdown()


def test_get_tracer_returns_valid_tracer():
    """AC6: get_tracer() returns a usable Tracer that can start spans."""
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

    from core.telemetry import get_tracer, shutdown

    tracer = get_tracer("beacon.test")
    assert tracer is not None

    with tracer.start_as_current_span("test.span") as span:
        span.set_attribute("test.key", "value")
        assert span is not None

    shutdown()


# ═══════════════════════════════════════════════════════════════════════════
# AC7 – Documentation file exists
# ═══════════════════════════════════════════════════════════════════════════

def test_documentation_file_exists():
    """AC7: output/TELEMETRY_IMPLEMENTATION.md must exist and be non-trivial."""
    doc_path = os.path.join(PROJECT_ROOT, "output", "TELEMETRY_IMPLEMENTATION.md")
    assert os.path.exists(doc_path), \
        f"output/TELEMETRY_IMPLEMENTATION.md not found"
    assert os.path.getsize(doc_path) > 500


# ═══════════════════════════════════════════════════════════════════════════
# Bonus – SessionReporter completeness
# ═══════════════════════════════════════════════════════════════════════════

def test_session_reporter_to_dict_has_all_required_keys(in_memory_setup):
    """SessionReporter.to_dict() must contain all required top-level keys."""
    from core.telemetry import SessionReporter

    reporter = SessionReporter(session_id="sess-keys-test")
    with reporter.session_span(user_message="query"):
        with reporter.track_tool("web_search"):
            pass

    result = reporter.to_dict()
    required_keys = {
        "schema_version", "session_id", "started_at",
        "ended_at", "duration_ms", "status", "tool_calls",
        "llm_calls", "summary",
    }
    for key in required_keys:
        assert key in result, f"Key '{key}' missing from to_dict()"


def test_session_reporter_empty_session_valid(in_memory_setup):
    """SessionReporter with no tool calls produces valid to_dict()."""
    from core.telemetry import SessionReporter

    reporter = SessionReporter(session_id="sess-empty-001")
    with reporter.session_span(user_message="just a question"):
        pass

    result = reporter.to_dict()
    assert result.get("session_id") == "sess-empty-001"
    assert result.get("tool_calls") == []
    assert result.get("llm_calls") == []
    sm = result.get("summary", {})
    assert sm.get("total_tool_calls") == 0
    assert sm.get("total_llm_calls") == 0
    assert result.get("duration_ms") >= 0