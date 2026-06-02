"""tests/telemetry/test_session_span.py
========================================
Unit tests for the Step-5 session-root span API added to
``core/telemetry/tracer.py``:

    start_session_span(session_id)  -> (Span, token)
    end_session_span(span, duration_ms)
    get_session_span(session_id)    -> Span | None
    get_session_context(session_id) -> Context | None
    session_span_context(session_id) [context-manager]

All tests are isolated: they use an InMemorySpanExporter so no network
collector is required, and they reset the tracer singleton + session
registry between every test case.

Run:
    cd /Users/sorravit/sandbox/ClineSandbox
    python -m pytest tests/telemetry/test_session_span.py -v
"""
from __future__ import annotations

import os
import sys
import time
import threading
from typing import List

import pytest

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(autouse=True)
def _reset_tracer_singleton():
    """Tear down the tracer singleton and session registry before every test."""
    import core.telemetry.tracer as _t
    # --- pre-test teardown ---
    if _t._provider is not None:
        try:
            _t._provider.shutdown()
        except Exception:
            pass
        _t._provider = None
    with _t._session_spans_lock:
        _t._session_spans.clear()

    yield

    # --- post-test teardown ---
    if _t._provider is not None:
        try:
            _t._provider.shutdown()
        except Exception:
            pass
        _t._provider = None
    with _t._session_spans_lock:
        _t._session_spans.clear()


@pytest.fixture
def in_memory_tracer():
    """
    Wire up an InMemorySpanExporter and patch get_tracer in tracer.py so
    all session spans land in the exporter.

    Yields (provider, exporter, patched_get_tracer).
    """
    import unittest.mock as mock
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    import core.telemetry.tracer as _tracer_mod

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def _get_tracer(name: str = "beacon"):
        return provider.get_tracer(name)

    with mock.patch.object(_tracer_mod, "get_tracer", side_effect=_get_tracer):
        yield provider, exporter


# ===========================================================================
# 1. Public-API imports
# ===========================================================================

class TestPublicAPIImports:
    """All new symbols must be importable from core.telemetry."""

    def test_start_session_span_importable(self):
        from core.telemetry import start_session_span
        assert callable(start_session_span)

    def test_end_session_span_importable(self):
        from core.telemetry import end_session_span
        assert callable(end_session_span)

    def test_get_session_span_importable(self):
        from core.telemetry import get_session_span
        assert callable(get_session_span)

    def test_get_session_context_importable(self):
        from core.telemetry import get_session_context
        assert callable(get_session_context)

    def test_session_span_context_importable(self):
        from core.telemetry import session_span_context
        assert callable(session_span_context)


# ===========================================================================
# 2. start_session_span — span creation & attributes
# ===========================================================================

class TestStartSessionSpan:
    """start_session_span() must create a valid root span with correct attributes."""

    def test_returns_span_and_token(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span
        _, exporter = in_memory_tracer
        sid = "ses-test-001"

        span, token = start_session_span(sid)
        try:
            assert span is not None, "span must not be None"
            assert token is not None, "context token must not be None"
        finally:
            end_session_span(span, duration_ms=0.0)

    def test_span_name_is_session(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span
        _, exporter = in_memory_tracer
        sid = "ses-test-name-001"

        span, _ = start_session_span(sid)
        end_session_span(span, duration_ms=10.0)

        finished = exporter.get_finished_spans()
        session_spans = [s for s in finished if s.name == "session"]
        assert len(session_spans) == 1, (
            f"Expected exactly one span named 'session', got: "
            f"{[s.name for s in finished]}"
        )

    def test_span_has_session_id_attribute(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span
        _, exporter = in_memory_tracer
        sid = "ses-attr-001"

        span, _ = start_session_span(sid)
        end_session_span(span, duration_ms=5.0)

        finished = exporter.get_finished_spans()
        s = next(sp for sp in finished if sp.name == "session")
        attrs = dict(s.attributes or {})
        assert attrs.get("session.id") == sid, (
            f"session.id should be '{sid}', got: {attrs.get('session.id')!r}"
        )

    def test_span_has_session_start_time_attribute(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span
        _, exporter = in_memory_tracer
        sid = "ses-start-time-001"

        before = time.time()
        span, _ = start_session_span(sid)
        end_session_span(span, duration_ms=0.0)

        finished = exporter.get_finished_spans()
        s = next(sp for sp in finished if sp.name == "session")
        attrs = dict(s.attributes or {})

        assert "session.start_time" in attrs, (
            f"session.start_time missing from attributes: {attrs}"
        )
        # Must be a non-empty ISO-8601 string
        start_time_val = attrs["session.start_time"]
        assert isinstance(start_time_val, str) and len(start_time_val) > 10, (
            f"session.start_time should be ISO-8601 string, got: {start_time_val!r}"
        )
        # Must contain a 'T' separator (ISO-8601 datetime)
        assert "T" in start_time_val, (
            f"session.start_time does not look like ISO-8601: {start_time_val!r}"
        )

    def test_span_kind_is_server(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span
        from opentelemetry.trace import SpanKind
        _, exporter = in_memory_tracer
        sid = "ses-kind-001"

        span, _ = start_session_span(sid)
        end_session_span(span, duration_ms=0.0)

        finished = exporter.get_finished_spans()
        s = next(sp for sp in finished if sp.name == "session")
        assert s.kind == SpanKind.SERVER, (
            f"Expected SpanKind.SERVER, got {s.kind}"
        )

    def test_span_has_valid_trace_id(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span
        _, exporter = in_memory_tracer
        sid = "ses-traceid-001"

        span, _ = start_session_span(sid)
        end_session_span(span, duration_ms=0.0)

        finished = exporter.get_finished_spans()
        s = next(sp for sp in finished if sp.name == "session")
        trace_id = s.get_span_context().trace_id
        assert trace_id != 0, "trace_id must be a non-zero value"

    def test_distinct_sessions_have_distinct_trace_ids(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span
        _, exporter = in_memory_tracer

        span1, _ = start_session_span("ses-multi-001")
        end_session_span(span1, duration_ms=1.0)

        span2, _ = start_session_span("ses-multi-002")
        end_session_span(span2, duration_ms=1.0)

        finished = exporter.get_finished_spans()
        session_spans = [s for s in finished if s.name == "session"]
        assert len(session_spans) == 2

        trace_ids = {s.get_span_context().trace_id for s in session_spans}
        assert len(trace_ids) == 2, (
            "Two distinct sessions must produce distinct trace_ids"
        )


# ===========================================================================
# 3. end_session_span — duration attribute & span status
# ===========================================================================

class TestEndSessionSpan:
    """end_session_span() must set session.duration_ms and status=OK."""

    def test_sets_duration_ms_attribute(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span
        _, exporter = in_memory_tracer
        sid = "ses-dur-001"

        span, _ = start_session_span(sid)
        end_session_span(span, duration_ms=123.456)

        finished = exporter.get_finished_spans()
        s = next(sp for sp in finished if sp.name == "session")
        attrs = dict(s.attributes or {})

        assert "session.duration_ms" in attrs, (
            f"session.duration_ms missing from attributes: {attrs}"
        )
        assert abs(float(attrs["session.duration_ms"]) - 123.456) < 0.01, (
            f"Expected ~123.456, got {attrs['session.duration_ms']}"
        )

    def test_duration_ms_zero_is_valid(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span
        _, exporter = in_memory_tracer

        span, _ = start_session_span("ses-dur-zero")
        end_session_span(span, duration_ms=0.0)

        finished = exporter.get_finished_spans()
        s = next(sp for sp in finished if sp.name == "session")
        attrs = dict(s.attributes or {})
        assert float(attrs["session.duration_ms"]) == 0.0

    def test_sets_status_ok(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span
        from opentelemetry.trace import StatusCode
        _, exporter = in_memory_tracer
        sid = "ses-status-ok-001"

        span, _ = start_session_span(sid)
        end_session_span(span, duration_ms=50.0)

        finished = exporter.get_finished_spans()
        s = next(sp for sp in finished if sp.name == "session")
        assert s.status.status_code == StatusCode.OK, (
            f"Expected StatusCode.OK, got {s.status.status_code}"
        )

    def test_span_is_finished_after_end(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span
        _, exporter = in_memory_tracer
        sid = "ses-finished-001"

        span, _ = start_session_span(sid)
        # Before end — should not yet be in finished spans
        assert not any(s.name == "session" for s in exporter.get_finished_spans())

        end_session_span(span, duration_ms=10.0)

        # After end — must appear in finished spans
        finished = exporter.get_finished_spans()
        assert any(s.name == "session" for s in finished), (
            "span must appear in finished spans after end_session_span()"
        )

    def test_end_session_span_never_raises(self, in_memory_tracer):
        """end_session_span must not propagate exceptions to the caller."""
        from core.telemetry import start_session_span, end_session_span
        from opentelemetry.trace import NonRecordingSpan, INVALID_SPAN_CONTEXT

        # Pass a no-op span — should never raise
        noop_span = NonRecordingSpan(INVALID_SPAN_CONTEXT)
        try:
            end_session_span(noop_span, duration_ms=99.9)
        except Exception as exc:
            pytest.fail(f"end_session_span raised unexpectedly: {exc}")


# ===========================================================================
# 4. Session registry — get_session_span / get_session_context
# ===========================================================================

class TestSessionRegistry:
    """The module-level registry correctly stores and removes session entries."""

    def test_get_session_span_returns_span_while_active(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span, get_session_span
        _, exporter = in_memory_tracer
        sid = "ses-registry-001"

        span, _ = start_session_span(sid)
        try:
            retrieved = get_session_span(sid)
            assert retrieved is span, (
                "get_session_span must return the same span object that was started"
            )
        finally:
            end_session_span(span, duration_ms=0.0)

    def test_get_session_span_returns_none_after_end(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span, get_session_span
        _, exporter = in_memory_tracer
        sid = "ses-registry-end-001"

        span, _ = start_session_span(sid)
        end_session_span(span, duration_ms=0.0)

        assert get_session_span(sid) is None, (
            "get_session_span must return None after end_session_span()"
        )

    def test_get_session_span_returns_none_for_unknown_id(self):
        from core.telemetry import get_session_span
        result = get_session_span("ses-does-not-exist-xyz")
        assert result is None

    def test_get_session_context_returns_context_while_active(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span, get_session_context
        from opentelemetry.context import Context
        _, exporter = in_memory_tracer
        sid = "ses-ctx-001"

        span, _ = start_session_span(sid)
        try:
            ctx = get_session_context(sid)
            assert ctx is not None, "get_session_context must return a Context while active"
            assert isinstance(ctx, Context), (
                f"Expected Context instance, got {type(ctx)}"
            )
        finally:
            end_session_span(span, duration_ms=0.0)

    def test_get_session_context_returns_none_after_end(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span, get_session_context
        _, exporter = in_memory_tracer
        sid = "ses-ctx-end-001"

        span, _ = start_session_span(sid)
        end_session_span(span, duration_ms=0.0)

        assert get_session_context(sid) is None, (
            "get_session_context must return None after end_session_span()"
        )

    def test_registry_is_thread_safe(self, in_memory_tracer):
        """Concurrent start/end calls must not corrupt the registry."""
        from core.telemetry import start_session_span, end_session_span, get_session_span
        _, exporter = in_memory_tracer

        errors: List[Exception] = []

        def run(i: int) -> None:
            sid = f"ses-thread-{i:04d}"
            try:
                span, _ = start_session_span(sid)
                # Briefly verify the span is registered
                assert get_session_span(sid) is span
                time.sleep(0.001)
                end_session_span(span, duration_ms=float(i))
                assert get_session_span(sid) is None
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread-safety errors: {errors}"


# ===========================================================================
# 5. Context propagation — child spans parented to session root
# ===========================================================================

class TestContextPropagation:
    """Child spans created after start_session_span must be parented to it."""

    def test_child_span_has_session_trace_id(self, in_memory_tracer):
        from core.telemetry import start_session_span, end_session_span
        from core.telemetry.tracer import get_tracer
        _, exporter = in_memory_tracer
        sid = "ses-ctx-prop-001"

        span, _ = start_session_span(sid)
        root_trace_id = span.get_span_context().trace_id
        root_span_id = span.get_span_context().span_id

        # Create a child span using start_as_current_span; it should attach
        # to the session context that was attached by start_session_span.
        tracer = get_tracer("beacon.test")
        with tracer.start_as_current_span("child.operation") as child:
            child_trace_id = child.get_span_context().trace_id

        end_session_span(span, duration_ms=5.0)

        assert child_trace_id == root_trace_id, (
            f"Child trace_id {child_trace_id:032x} must match "
            f"session trace_id {root_trace_id:032x}"
        )

        # Verify the parent span id is the session span id
        finished = exporter.get_finished_spans()
        child_finished = next(
            (s for s in finished if s.name == "child.operation"), None
        )
        assert child_finished is not None, "child.operation span not in finished spans"
        parent_id = child_finished.parent.span_id if child_finished.parent else None
        assert parent_id == root_span_id, (
            f"Child parent_span_id {parent_id} must equal session "
            f"span_id {root_span_id}"
        )

    def test_get_session_context_enables_cross_thread_parenting(self, in_memory_tracer):
        """A child span created in another thread using get_session_context()
        must share the session's trace_id."""
        from core.telemetry import (
            start_session_span, end_session_span, get_session_context
        )
        from core.telemetry.tracer import get_tracer
        _, exporter = in_memory_tracer
        sid = "ses-cross-thread-001"

        span, _ = start_session_span(sid)
        root_trace_id = span.get_span_context().trace_id

        child_trace_ids: List[int] = []

        def thread_work():
            ctx = get_session_context(sid)
            if ctx is None:
                return
            tracer = get_tracer("beacon.test.thread")
            with tracer.start_as_current_span("thread.child", context=ctx) as cs:
                child_trace_ids.append(cs.get_span_context().trace_id)

        t = threading.Thread(target=thread_work)
        t.start()
        t.join()

        end_session_span(span, duration_ms=2.0)

        assert len(child_trace_ids) == 1, "Thread did not produce a child span"
        assert child_trace_ids[0] == root_trace_id, (
            f"Cross-thread child trace_id {child_trace_ids[0]:032x} must "
            f"match session trace_id {root_trace_id:032x}"
        )


# ===========================================================================
# 6. session_span_context — context-manager wrapper
# ===========================================================================

class TestSessionSpanContext:
    """session_span_context() must behave identically to manual start/end."""

    def test_context_manager_creates_and_ends_span(self, in_memory_tracer):
        from core.telemetry import session_span_context
        _, exporter = in_memory_tracer
        sid = "ses-cm-001"

        with session_span_context(sid) as span:
            assert span is not None

        finished = exporter.get_finished_spans()
        session_spans = [s for s in finished if s.name == "session"]
        assert len(session_spans) == 1, (
            f"Expected 1 session span, got {[s.name for s in finished]}"
        )

    def test_context_manager_sets_duration_ms(self, in_memory_tracer):
        from core.telemetry import session_span_context
        _, exporter = in_memory_tracer
        sid = "ses-cm-dur-001"

        with session_span_context(sid):
            time.sleep(0.02)

        finished = exporter.get_finished_spans()
        s = next(sp for sp in finished if sp.name == "session")
        attrs = dict(s.attributes or {})
        assert "session.duration_ms" in attrs
        assert float(attrs["session.duration_ms"]) >= 15.0, (
            f"Expected duration >= 15ms after sleep(0.02), "
            f"got {attrs['session.duration_ms']}"
        )

    def test_context_manager_sets_error_status_on_exception(self, in_memory_tracer):
        from core.telemetry import session_span_context
        from opentelemetry.trace import StatusCode
        _, exporter = in_memory_tracer
        sid = "ses-cm-err-001"

        with pytest.raises(ValueError, match="test error"):
            with session_span_context(sid):
                raise ValueError("test error")

        finished = exporter.get_finished_spans()
        s = next(sp for sp in finished if sp.name == "session")
        assert s.status.status_code == StatusCode.ERROR, (
            f"Expected ERROR status on exception, got {s.status.status_code}"
        )

    def test_context_manager_cleans_registry_on_normal_exit(self, in_memory_tracer):
        from core.telemetry import session_span_context, get_session_span
        _, exporter = in_memory_tracer
        sid = "ses-cm-cleanup-001"

        with session_span_context(sid):
            assert get_session_span(sid) is not None

        assert get_session_span(sid) is None, (
            "Registry entry must be removed after context-manager exit"
        )

    def test_context_manager_cleans_registry_on_exception(self, in_memory_tracer):
        from core.telemetry import session_span_context, get_session_span
        _, exporter = in_memory_tracer
        sid = "ses-cm-cleanup-err-001"

        with pytest.raises(RuntimeError):
            with session_span_context(sid):
                assert get_session_span(sid) is not None
                raise RuntimeError("boom")

        assert get_session_span(sid) is None, (
            "Registry entry must be removed after context-manager exception"
        )

    def test_context_manager_sets_session_id_attribute(self, in_memory_tracer):
        from core.telemetry import session_span_context
        _, exporter = in_memory_tracer
        sid = "ses-cm-attrs-001"

        with session_span_context(sid):
            pass

        finished = exporter.get_finished_spans()
        s = next(sp for sp in finished if sp.name == "session")
        attrs = dict(s.attributes or {})
        assert attrs.get("session.id") == sid

    def test_context_manager_span_is_root(self, in_memory_tracer):
        """session_span_context must create a root span (no parent)."""
        from core.telemetry import session_span_context
        _, exporter = in_memory_tracer
        sid = "ses-cm-root-001"

        with session_span_context(sid):
            pass

        finished = exporter.get_finished_spans()
        s = next(sp for sp in finished if sp.name == "session")
        # A root span has no parent
        assert s.parent is None, (
            f"Session span must be a root span (parent=None), got parent={s.parent}"
        )


# ===========================================================================
# 7. Integration — session span wraps tool call spans end-to-end
# ===========================================================================

class TestSessionSpanIntegration:
    """Full integration: session root + tool child spans form a coherent trace."""

    def test_session_and_tool_spans_share_trace_id(self, in_memory_tracer):
        """Tool-call child spans must share the session root's trace_id."""
        import unittest.mock as mock
        from core.telemetry import start_session_span, end_session_span
        from core.telemetry import record_tool_call
        import core.telemetry.metrics as _metrics_mod
        from core.telemetry.tracer import get_tracer as _get_tracer_real

        provider, exporter = in_memory_tracer
        sid = "ses-integration-001"

        # Patch get_tracer inside metrics too so tool spans go to same exporter
        def _get_tracer_patch(name: str = "beacon"):
            return provider.get_tracer(name)

        with mock.patch.object(_metrics_mod, "get_tracer", side_effect=_get_tracer_patch):
            span, _ = start_session_span(sid)
            root_trace_id = span.get_span_context().trace_id

            with record_tool_call("web_search", session_id=sid):
                time.sleep(0.005)

            with record_tool_call("execute_command", session_id=sid):
                time.sleep(0.005)

            end_session_span(span, duration_ms=100.0)

        finished = exporter.get_finished_spans()
        session_span_finished = next(
            (s for s in finished if s.name == "session"), None
        )
        tool_spans = [s for s in finished if s.name.startswith("tool/")]

        assert session_span_finished is not None, "session span not found"
        assert len(tool_spans) == 2, (
            f"Expected 2 tool spans, got {[s.name for s in finished]}"
        )

        for ts in tool_spans:
            assert ts.get_span_context().trace_id == root_trace_id, (
                f"Tool span '{ts.name}' trace_id {ts.get_span_context().trace_id:032x} "
                f"must match session trace_id {root_trace_id:032x}"
            )

    def test_session_span_duration_ms_reflects_real_elapsed_time(
        self, in_memory_tracer
    ):
        """session.duration_ms set via end_session_span must be >= actual sleep."""
        from core.telemetry import start_session_span, end_session_span
        _, exporter = in_memory_tracer
        sid = "ses-timing-001"

        span, _ = start_session_span(sid)
        t0 = time.perf_counter()
        time.sleep(0.03)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        end_session_span(span, duration_ms=elapsed_ms)

        finished = exporter.get_finished_spans()
        s = next(sp for sp in finished if sp.name == "session")
        attrs = dict(s.attributes or {})
        stored_ms = float(attrs["session.duration_ms"])

        assert stored_ms >= 25.0, (
            f"duration_ms {stored_ms:.2f} should be >= 25ms after sleep(0.03)"
        )

    def test_multiple_concurrent_sessions_are_independent(self, in_memory_tracer):
        """Concurrent sessions must not share trace context."""
        from core.telemetry import start_session_span, end_session_span
        _, exporter = in_memory_tracer

        span_a, _ = start_session_span("ses-concurrent-A")
        trace_a = span_a.get_span_context().trace_id

        span_b, _ = start_session_span("ses-concurrent-B")
        trace_b = span_b.get_span_context().trace_id

        end_session_span(span_a, duration_ms=10.0)
        end_session_span(span_b, duration_ms=20.0)

        assert trace_a != trace_b, (
            "Concurrent sessions must have distinct trace_ids"
        )

        finished = exporter.get_finished_spans()
        session_spans = [s for s in finished if s.name == "session"]
        assert len(session_spans) == 2

        durations = {
            dict(s.attributes or {}).get("session.id"): dict(s.attributes or {}).get("session.duration_ms")
            for s in session_spans
        }
        assert abs(float(durations["ses-concurrent-A"]) - 10.0) < 0.1
        assert abs(float(durations["ses-concurrent-B"]) - 20.0) < 0.1