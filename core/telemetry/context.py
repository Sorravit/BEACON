"""core/telemetry/context.py
Thread/async-safe ContextVar carrying active session_id and SessionReporter."""
from __future__ import annotations
import contextvars
from typing import Optional, Tuple, TYPE_CHECKING
if TYPE_CHECKING:
    from core.telemetry.session_reporter import SessionReporter

_cv_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar('beacon_session_id', default=None)
_cv_reporter: contextvars.ContextVar[Optional['SessionReporter']] = contextvars.ContextVar('beacon_session_reporter', default=None)

def set_session_context(session_id: str, reporter: 'SessionReporter') -> Tuple[contextvars.Token, contextvars.Token]:
    token_sid = _cv_session_id.set(session_id)
    token_rep = _cv_reporter.set(reporter)
    return token_sid, token_rep

def clear_session_context(token_sid: contextvars.Token, token_rep: contextvars.Token) -> None:
    try:
        _cv_session_id.reset(token_sid)
    except Exception:
        pass
    try:
        _cv_reporter.reset(token_rep)
    except Exception:
        pass

def get_session_id() -> Optional[str]:
    return _cv_session_id.get()

def get_reporter() -> Optional['SessionReporter']:
    return _cv_reporter.get()
