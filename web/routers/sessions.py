"""Session CRUD + pin/rename routes."""

from datetime import datetime

from fastapi import APIRouter, HTTPException

from web import state
from web.schemas import RenameRequest, ReorderRequest
from web.helpers import (
    _create_session,
    _save_session_async,
    _session_file,
    _schedule_save,
)

router = APIRouter()


@router.get("/sessions")
async def list_sessions():
    result = []
    for sid, s in state._sessions.items():
        bg = state._bg.get(sid, {})
        task = bg.get("task")
        running = task is not None and not task.done()
        result.append({
            "id":            sid,
            "title":         s["title"],
            "created_at":    s["created_at"],
            "updated_at":    s["updated_at"],
            "message_count": len(s["messages"]),
            "running":       running,
            "pinned":        s.get("pinned", False),
            "pin_order":     s.get("pin_order", 0),
        })
    pinned   = [x for x in result if x["pinned"]]
    unpinned = [x for x in result if not x["pinned"]]
    pinned.sort(key=lambda x: x.get("pin_order", 0))
    unpinned.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"sessions": pinned + unpinned}


@router.post("/sessions")
async def create_session():
    sid = _create_session()
    return {"id": sid, "title": "New Chat"}


# NOTE: /sessions/reorder-pins must be registered BEFORE /sessions/{session_id}
# so FastAPI does not treat "reorder-pins" as a session_id path parameter.
@router.patch("/sessions/reorder-pins")
async def reorder_pins(req: ReorderRequest):
    for i, sid in enumerate(req.order):
        s = state._sessions.get(sid)
        if s and s.get("pinned"):
            s["pin_order"] = i
            _schedule_save(sid)
    return {"status": "ok"}


@router.patch("/sessions/{session_id}/pin")
async def pin_session(session_id: str):
    s = state._sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    s["pinned"] = not s.get("pinned", False)
    if s["pinned"]:
        max_order = max(
            (v.get("pin_order", 0) for v in state._sessions.values() if v.get("pinned")),
            default=-1,
        )
        s["pin_order"] = max_order + 1
    else:
        s["pin_order"] = 0
    s["updated_at"] = datetime.now().isoformat()
    await _save_session_async(session_id)   # immediate durability for pin
    return {"id": session_id, "pinned": s["pinned"], "pin_order": s["pin_order"]}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    s = state._sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    bg = state._bg.get(session_id, {})
    task = bg.get("task")
    running = task is not None and not task.done()
    return {
        "id":         session_id,
        "title":      s["title"],
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "messages":   s["messages"],
        "running":    running,
    }


@router.patch("/sessions/{session_id}/rename")
async def rename_session(session_id: str, req: RenameRequest):
    s = state._sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    s["title"] = req.title.strip() or "New Chat"
    s["manually_named"] = True
    s["updated_at"] = datetime.now().isoformat()
    await _save_session_async(session_id)   # immediate durability for rename
    return {"id": session_id, "title": s["title"]}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    s = state._sessions.pop(session_id, None)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    fp = _session_file(session_id)
    if fp.exists():
        fp.unlink()
    state._bg.pop(session_id, None)
    # Phase 6 / #2: release browser context for this session
    if state._shared_agent and state._shared_agent.browser_pool:
        try:
            await state._shared_agent.browser_pool.close_session(session_id)
        except Exception:
            pass
    return {"status": "deleted", "id": session_id}


@router.post("/sessions/{session_id}/delete-if-empty")
async def delete_session_if_empty(session_id: str):
    """Delete a session only if it has no messages. Called on page unload."""
    s = state._sessions.get(session_id)
    if not s:
        return {"status": "not_found"}
    if len(s.get("messages", [])) == 0:
        state._sessions.pop(session_id, None)
        fp = _session_file(session_id)
        if fp.exists():
            fp.unlink()
        state._bg.pop(session_id, None)
        return {"status": "deleted"}
    return {"status": "kept", "message_count": len(s["messages"])}
