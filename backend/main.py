import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.api import interviews, routes, websocket
from backend.config import get_settings
from backend.db import repository
from backend.db.engine import create_engine, create_sessionmaker
from backend.db.migrations import upgrade_to_head
from backend.logging_config import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # Alembic's env.py starts its own event loop, so it must run outside this one.
    await asyncio.to_thread(upgrade_to_head, settings.database_url)

    engine = create_engine(settings.database_url, echo=settings.database_echo)
    app.state.db_sessionmaker = create_sessionmaker(engine)

    async with app.state.db_sessionmaker() as db:
        abandoned = await repository.abandon_in_progress_interviews(db)
        failed = await repository.fail_pending_evaluations(db)
    if abandoned:
        logger.warning("Marked %d interview(s) left in progress by the last run as abandoned", abandoned)
    if failed:
        logger.warning("Marked %d evaluation(s) interrupted by the last shutdown as failed", failed)

    logger.info("Database ready")
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)

    app = FastAPI(title="AI Interview Bot", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=settings.frontend_dir), name="static")
    app.include_router(routes.router)
    app.include_router(interviews.router)
    app.include_router(websocket.router)

    for key in ("groq_api_key", "deepgram_api_key", "elevenlabs_api_key"):
        if not getattr(settings, key):
            logger.warning("%s is not set in .env", key.upper())

    return app


app = create_app()


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run("backend.main:app", host=settings.host, port=settings.port, reload=True)
