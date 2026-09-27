import logging

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.db.engine import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index():
    index_path = get_settings().frontend_dir / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse("<h1>AI Interview Bot</h1><p>Frontend not found.</p>")


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    try:
        await db.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        logger.exception("Database health check failed")
        database_ok = False
    return {
        "status": "healthy" if database_ok else "degraded",
        "database": database_ok,
        "groq_configured": bool(settings.groq_api_key),
        "deepgram_configured": bool(settings.deepgram_api_key),
        "elevenlabs_configured": bool(settings.elevenlabs_api_key),
    }
