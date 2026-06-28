"""System routes: upload, index page, health, memory health, models."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse

from web import state
from web.logging_setup import logger

router = APIRouter()


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a file and save it to temp/ folder. Returns the saved path."""
    import shutil  # noqa: F401  (preserved from original)

    dest_dir = Path("temp")
    dest_dir.mkdir(exist_ok=True)

    safe_name = Path(file.filename).name.replace(" ", "_")
    if not safe_name:
        safe_name = "upload"
    dest = dest_dir / safe_name

    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    counter = 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    contents = await file.read()
    dest.write_bytes(contents)

    return {"path": str(dest.resolve()), "name": dest.name, "size": dest.stat().st_size}


@router.get("/", response_class=HTMLResponse)
async def index():
    html_file = Path("static/index.html")
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(), status_code=200)
    return HTMLResponse(content="<h1>Frontend not found</h1>", status_code=404)


@router.get("/health")
async def health():
    return {"status": "ok", "sessions": len(state._sessions)}


@router.get("/memory/health")
async def memory_health():
    """Phase 2: inspect auto-memory worker health + Weaviate availability."""
    a = state._shared_agent
    vm_ok = bool(a and a.memory_available)
    stats = {}
    if a and a.memory_worker:
        stats = dict(a.memory_worker.stats)
    auto_count = 0
    personal_count = 0
    if vm_ok:
        try:
            facts = await a.vector_memory.get_all_auto_facts()
            auto_count = len(facts or [])
        except Exception:
            pass
        try:
            pfacts = await a.vector_memory.get_all_personal_facts()
            personal_count = len(pfacts or [])
        except Exception:
            pass
    return {
        "weaviate": vm_ok,
        "worker": stats,
        "auto_fact_count": auto_count,
        "personal_fact_count": personal_count,
    }


@router.get("/models")
async def list_models():
    """Return the curated list of selectable models + role defaults.

    Drives the chat model picker and the per-agent selectors in Task Mode.
    """
    if state._config is None:
        raise HTTPException(status_code=503, detail="Config not initialised")
    registry = state._config.models
    return {
        "models": registry.to_public_list(),
        "default": registry.default_model,
        "current": state._config.model,
        "roles": registry.role_defaults(),
    }


@router.post("/models/reload")
async def reload_models():
    """Hot-reload models.yaml without restarting the server."""
    if state._config is None:
        raise HTTPException(status_code=503, detail="Config not initialised")
    from core.models import ModelRegistry

    state._config.models = ModelRegistry.load(env_default=state._config.model)
    logger.info("Model registry reloaded: %d models", len(state._config.models.ids()))
    return {"status": "reloaded", "count": len(state._config.models.ids())}
